"""Persistent directory-scoped comment collection; files never move or change."""
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hub_library import EXTENSIONS, display_tag


class CommentDirectories:
    def __init__(self, engine):
        self.e=engine
        self.lock=threading.RLock()
        self.scanning=set()
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='comment-directory')
        with engine.store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS comment_directories(
                id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
                scanned REAL NOT NULL DEFAULT 0,last_started REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS comment_files(
                id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,signature TEXT NOT NULL,meta TEXT NOT NULL,
                comment_guid TEXT,status TEXT NOT NULL DEFAULT 'pending',due REAL NOT NULL DEFAULT 0,
                task_id TEXT,owner TEXT,error TEXT NOT NULL DEFAULT '',updated REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS comment_directory_files(
                directory_id TEXT NOT NULL,file_id TEXT NOT NULL,PRIMARY KEY(directory_id,file_id));
                CREATE INDEX IF NOT EXISTS comment_files_task ON comment_files(task_id);
                CREATE INDEX IF NOT EXISTS comment_members_file ON comment_directory_files(file_id);''')

    def add(self,path):
        path=str(self.e.library.confined(path))
        key=hashlib.sha256(path.encode()).hexdigest()[:24]
        with self.e.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO comment_directories(id,path) VALUES(?,?)',(key,path))
        self.scan_start(key)
        return key

    def scan_start(self,key):
        with self.lock:
            if key in self.scanning:return False
            if not self.e.store.rows('SELECT id FROM comment_directories WHERE id=?',(key,)):raise ValueError('目录不存在')
            self.scanning.add(key)
            self.pool.submit(self.scan,key)
        return True

    def scan(self,key):
        try:
            import mutagen
            directory=self.e.store.rows('SELECT path FROM comment_directories WHERE id=?',(key,))[0]['path']
            root=self.e.library.confined(directory)
            known={r['path']:r for r in self.e.store.rows('SELECT * FROM comment_files')}
            catalog={str(self.e.mount_root/r['path']):r for r in self.e.store.rows('SELECT * FROM local_files')}
            files=[]
            def fail(exc):raise exc
            for parent,dirs,names in os.walk(root,followlinks=False,onerror=fail):
                dirs[:]=[d for d in dirs if not d.startswith('.') and not Path(parent,d).is_symlink()]
                for name in names:
                    if self.e.stop.is_set():raise RuntimeError('扫描已停止，稍后继续')
                    p=Path(parent,name)
                    if p.is_symlink() or p.suffix.lower() not in EXTENSIONS:continue
                    p=p.resolve()
                    if not p.is_relative_to(self.e.mount_root):continue
                    stat=p.stat();path=str(p);signature=f'{stat.st_size}:{stat.st_mtime_ns}'
                    old=known.get(path)
                    if old and (old['signature']==signature or old['task_id']):
                        files.append((old['id'],path,old['signature'],old['meta']));continue
                    cached=catalog.get(path)
                    if cached and cached['mtime']==stat.st_mtime and cached['bytes']==stat.st_size:
                        meta={'title':cached['title'],'artists':json.loads(cached['artists']),
                              'album':cached['album'],'duration_ms':cached['duration']}
                    else:
                        try:audio=mutagen.File(p,easy=True)
                        except Exception:audio=None
                        tags=audio.tags if audio else {}
                        tags=tags or {}
                        meta={'title':display_tag((tags.get('title') or [p.stem])[0]),
                              'artists':[display_tag(a) for a in tags.get('artist',[])],
                              'album':display_tag((tags.get('album') or [''])[0]),
                              'duration_ms':round(audio.info.length*1000) if audio else 0}
                    files.append((hashlib.sha256(path.encode()).hexdigest()[:32],path,signature,json.dumps(meta,ensure_ascii=False)))
            # Only a complete traversal can replace membership. Overlapping directories
            # share file work and checkpoints, so the same file is never fetched twice.
            with self.e.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for fid,path,signature,meta in files:
                    db.execute('''INSERT INTO comment_files(id,path,signature,meta) VALUES(?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET signature=excluded.signature,meta=excluded.meta,
                        status=CASE WHEN signature<>excluded.signature THEN 'pending' ELSE status END,
                        due=CASE WHEN signature<>excluded.signature THEN 0 ELSE due END,
                        comment_guid=CASE WHEN meta<>excluded.meta THEN NULL ELSE comment_guid END''',(fid,path,signature,meta))
                db.execute('DELETE FROM comment_directory_files WHERE directory_id=?',(key,))
                db.executemany('INSERT INTO comment_directory_files VALUES(?,?)',[(key,f[0]) for f in files])
                db.execute("UPDATE comment_directories SET scanned=?,error='' WHERE id=?",(time.time(),key))
        except Exception as exc:
            from hub_errors import describe
            with self.e.store.connect() as db:db.execute('UPDATE comment_directories SET error=? WHERE id=?',(describe(exc)[1],key))
        finally:
            with self.lock:self.scanning.discard(key)

    def control(self,key,action):
        with self.lock:
            rows=self.e.store.rows('SELECT * FROM comment_directories WHERE id=?',(key,))
            if not rows:raise ValueError('目录不存在')
            if action=='scan':self.scan_start(key);return
            enabled=action=='resume'
            with self.e.store.connect() as db:
                db.execute('UPDATE comment_directories SET enabled=? WHERE id=?',(int(enabled),key))
                if enabled:
                    db.execute("UPDATE comment_files SET due=0 WHERE status='failed' AND id IN (SELECT file_id FROM comment_directory_files WHERE directory_id=?)",(key,))
            if enabled:self.scan_start(key)
            else:
                manager=(self.e.bridge or {}).get('BRIDGE',{}).get('manager')
                if manager:
                    for row in self.e.store.rows('SELECT DISTINCT task_id FROM comment_files WHERE owner=? AND task_id IS NOT NULL',(key,)):
                        manager.cancel(row['task_id'])

    def progress(self,task):
        result=task.get('result') or {}
        history=result.get('progress') or []
        if history:return history[-1]
        store=(self.e.bridge or {}).get('BRIDGE',{}).get('store')
        if store and hasattr(store,'logs_since'):
            for row in reversed(store.logs_since(task['id'],after_seq=max(0,store.max_log_seq(task['id'])-30),limit=40)):
                if row['text'].strip().startswith('@@P '):
                    try:return json.loads(row['text'].strip()[4:])
                    except ValueError:pass
        return {}

    def pump(self):
        bridge=self.e.bridge or {};tasks=bridge.get('BRIDGE',{}).get('store')
        if not tasks:return False
        with self.lock:
            active=False
            for r in self.e.store.rows('SELECT DISTINCT task_id FROM comment_files WHERE task_id IS NOT NULL'):
                task=tasks.get(r['task_id'])
                if task and task['status'] in ('queued','running'):active=True;continue
                outcomes=((task or {}).get('result') or {}).get('track_results') or (self.progress(task) if task else {}).get('track_results',{})
                for row in self.e.store.rows('SELECT * FROM comment_files WHERE task_id=?',(r['task_id'],)):
                    outcome=outcomes.get(row['comment_guid'],{})
                    done=outcome.get('status')=='done'
                    cancelled=not task or task['status']=='cancelled'
                    error='' if done or cancelled else outcome.get('error') or (task or {}).get('summary') or '采集未完成，稍后重试'
                    with self.e.store.connect() as db:
                        db.execute('''UPDATE comment_files SET task_id=NULL,owner=NULL,status=?,due=?,error=?,updated=?
                            WHERE id=? AND task_id=?''',('done' if done else 'pending' if cancelled else 'failed',
                            time.time()+(7*86400 if done else 0 if cancelled else 3600),error[:500],time.time(),row['id'],r['task_id']))
            if active:return True
            for d in self.e.store.rows('SELECT * FROM comment_directories WHERE enabled=1 ORDER BY last_started,id'):
                if d['id'] in self.scanning:continue
                if time.time()-d['scanned']>300:self.scan_start(d['id']);continue
                if d['error']:continue
                rows=self.e.store.rows('''SELECT f.id FROM comment_files f JOIN comment_directory_files m ON m.file_id=f.id
                    WHERE m.directory_id=? AND f.task_id IS NULL AND f.due<=? ORDER BY f.updated,f.path LIMIT 5''',(d['id'],time.time()))
                if not rows:continue
                ids=[r['id'] for r in rows]
                try:task=bridge['submit_task']('comment_refresh',{'hub_comment_files':ids,'comment_directory':d['id'],'comment_count':100,'batch_size':5})
                except Exception:return False
                with self.e.store.connect() as db:
                    db.executemany("UPDATE comment_files SET task_id=?,owner=?,status='running',error='' WHERE id=?",[(task['id'],d['id'],fid) for fid in ids])
                    db.execute('UPDATE comment_directories SET last_started=? WHERE id=?',(time.time(),d['id']))
                return True
        return False

    def catalog(self,ids):
        from hub_comments import query
        path=Path(os.environ.get('COMMENTHUB_DB',str(self.e.store.home/'commenthub.db')))
        catalog={}
        for fid in list(dict.fromkeys(ids))[:5]:
            rows=self.e.store.rows('SELECT * FROM comment_files WHERE id=?',(fid,))
            if not rows:continue
            row=rows[0];meta=json.loads(row['meta'])
            guid=row['comment_guid']
            if not guid:
                found=query(path,title=meta['title'],artist='/'.join(meta.get('artists') or []),limit=1)
                guid=found.get('trackGuid') or 'local-'+fid
                with self.e.store.connect() as db:db.execute('UPDATE comment_files SET comment_guid=? WHERE id=?',(guid,fid))
            catalog[guid]={'guid':guid,**meta,'eligible':True}
        return list(catalog.values())

    def automatic_allowed(self,tid,directories=None):
        """Explicit directory controls take ownership from the download backfill."""
        directories=self.e.store.rows('SELECT path FROM comment_directories') if directories is None else directories
        if not directories:return True
        import hub_folders
        destinations=hub_folders.destinations(self.e,tid)
        paths=[self.e.root/r['directory'] for r in destinations]
        if not paths:
            rows=self.e.store.rows('SELECT path FROM tracks WHERE id=?',(tid,))
            paths=[self.e.root/rows[0]['path']] if rows and rows[0]['path'] else []
        return not any(p.is_relative_to(Path(d['path'])) for p in paths
                       for d in directories)

    def status(self):
        result=[];tasks=(self.e.bridge or {}).get('BRIDGE',{}).get('store')
        for d in self.e.store.rows('SELECT * FROM comment_directories ORDER BY path'):
            rows=self.e.store.rows('''SELECT f.status,COUNT(*) n FROM comment_files f JOIN comment_directory_files m ON f.id=m.file_id
                WHERE m.directory_id=? GROUP BY f.status''',(d['id'],))
            counts={r['status']:r['n'] for r in rows};total=sum(counts.values());done=counts.get('done',0)
            progress={}
            active=self.e.store.rows('SELECT DISTINCT f.task_id FROM comment_files f JOIN comment_directory_files m ON f.id=m.file_id WHERE m.directory_id=? AND f.task_id IS NOT NULL',(d['id'],))
            if active and tasks:
                task=tasks.get(active[0]['task_id'])
                if task:progress={**self.progress(task),'status':task['status']}
            active_guids={r['comment_guid'] for r in self.e.store.rows('''SELECT f.comment_guid FROM comment_files f
                JOIN comment_directory_files m ON m.file_id=f.id WHERE m.directory_id=? AND f.task_id IS NOT NULL''',(d['id'],))}
            completed_inflight=sum(v.get('status')=='done' for g,v in progress.get('track_results',{}).items() if g in active_guids)
            sample=self.e.store.rows("SELECT f.error FROM comment_files f JOIN comment_directory_files m ON f.id=m.file_id WHERE m.directory_id=? AND f.error!='' LIMIT 1",(d['id'],))
            result.append({**d,'scanning':d['id'] in self.scanning,'total':total,'done':done,
                'failed':counts.get('failed',0),'running':counts.get('running',0),'pending':counts.get('pending',0),
                'percent':round(done*100/total,1) if total else 0,'progress':progress,'completed_inflight':completed_inflight,
                'detail':sample[0]['error'] if sample else ''})
        return {'directories':result,'mount_root':str(self.e.mount_root)}
