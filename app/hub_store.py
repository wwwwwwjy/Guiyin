"""Music Hub durable subscriptions, shared library and download queue."""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path


def norm(value):
    # Keep version words: Live, remix, instrumental and their Chinese equivalents.
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', value).casefold())


def safe_name(value):
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', str(value)).strip(' .')[:80]
    return value or '未命名'


class Store:
    def __init__(self, home, recover_running=True):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'hub.sqlite3'
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS pipeline(
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0, task_id TEXT, retry_at REAL DEFAULT 0,
                status TEXT DEFAULT 'idle', message TEXT DEFAULT '等待配置飞牛自动融合');
              INSERT OR IGNORE INTO pipeline(id) VALUES(1);
              CREATE TABLE IF NOT EXISTS fn_targets(
                user TEXT, group_name TEXT, guid TEXT, name TEXT NOT NULL,
                PRIMARY KEY(user,group_name));

              CREATE TABLE IF NOT EXISTS fn_owned(
                user TEXT,target_guid TEXT,track_guid TEXT,PRIMARY KEY(user,target_guid,track_guid));
              CREATE TABLE IF NOT EXISTS fn_bindings(
                user TEXT,track_id TEXT,track_guid TEXT,PRIMARY KEY(user,track_id,track_guid));
              CREATE TABLE IF NOT EXISTS source_playlists(
                rule_id TEXT,platform TEXT,source_id TEXT,name TEXT,state TEXT,updated REAL,
                PRIMARY KEY(rule_id,platform,source_id));
              CREATE TABLE IF NOT EXISTS source_members(
                rule_id TEXT,playlist_id TEXT,track_id TEXT,PRIMARY KEY(rule_id,playlist_id,track_id));
              CREATE TABLE IF NOT EXISTS rules(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
                kind TEXT NOT NULL, ref TEXT NOT NULL, group_name TEXT NOT NULL,
                interval_hours INTEGER NOT NULL, enabled INTEGER NOT NULL,
                auto_download INTEGER NOT NULL, quality TEXT NOT NULL,
                next_run REAL NOT NULL, last_run REAL, error TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS tracks(
                id TEXT PRIMARY KEY, match_key TEXT NOT NULL, meta TEXT NOT NULL,
                duration INTEGER NOT NULL, path TEXT, bytes INTEGER DEFAULT 0,
                quality TEXT, created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS tracks_match ON tracks(match_key);
              CREATE TABLE IF NOT EXISTS sources(
                platform TEXT, source_id TEXT, track_id TEXT NOT NULL, meta TEXT NOT NULL,
                PRIMARY KEY(platform,source_id));
              CREATE TABLE IF NOT EXISTS membership(
                rule_id TEXT, track_id TEXT, position INTEGER,
                PRIMARY KEY(rule_id,track_id));
              CREATE TABLE IF NOT EXISTS rule_playlists(
                rule_id TEXT, platform TEXT, source_id TEXT, name TEXT,
                PRIMARY KEY(rule_id,platform,source_id));
              CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, dedupe TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                due REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                progress INTEGER DEFAULT 0, error TEXT NOT NULL DEFAULT '');
              CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(dedupe)
                WHERE status IN ('queued','running');
              CREATE INDEX IF NOT EXISTS jobs_history ON jobs(dedupe,created);
            ''')
            if 'generation' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')
            if 'cleared_status' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN cleared_status TEXT NOT NULL DEFAULT ''")
            if 'source_attempts' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute("ALTER TABLE jobs ADD COLUMN source_attempts TEXT NOT NULL DEFAULT '[]'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_pending ON jobs(dedupe) WHERE status IN ('queued','running','paused')")
            if recover_running:
                db.execute("UPDATE jobs SET status='queued', generation=generation+1, error='进程重启，已恢复排队' WHERE status='running'")
            # Older releases created another row on retry. Preserve that history,
            # but do not keep already superseded failures in the attention count.
            db.execute("""UPDATE jobs AS old SET status='superseded' WHERE status IN ('failed','blocked')
                AND EXISTS (SELECT 1 FROM jobs AS newer WHERE newer.dedupe=old.dedupe
                    AND newer.created>old.created)""")
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]


    def dirty(self):
        with self.connect() as db:
            db.execute('UPDATE pipeline SET revision=revision+1,retry_at=CASE WHEN revision=completed THEN ? ELSE retry_at END WHERE id=1',(time.time()+2,))

    def pipeline(self):
        return self.rows('SELECT * FROM pipeline WHERE id=1')[0]

    def rule(self, rid):
        rows = self.rows('SELECT * FROM rules WHERE id=?', (rid,))
        if not rows:
            raise ValueError('订阅不存在')
        return rows[0]

    def save_rule(self, data):
        rid = data.get('id') or uuid.uuid4().hex
        values = [rid] + [data[k] for k in ('name','platform','kind','ref','group_name',
                                          'interval_hours','enabled','auto_download','quality')]
        with self.connect() as db:
            db.execute('''INSERT INTO rules(id,name,platform,kind,ref,group_name,interval_hours,
                enabled,auto_download,quality,next_run) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,platform=excluded.platform,
                kind=excluded.kind,ref=excluded.ref,group_name=excluded.group_name,
                interval_hours=excluded.interval_hours,enabled=excluded.enabled,
                auto_download=excluded.auto_download,quality=excluded.quality,next_run=excluded.next_run''',
                       (*values, time.time()))
        return self.rule(rid)

    def upsert_track(self, platform, track):
        sid = str(track['source_id'])
        title = str(track.get('title') or '').strip()
        artists = track.get('artists') or []
        if not sid or not title or not isinstance(artists, list):
            raise ValueError('歌曲缺少有效 ID、标题或歌手列表')
        duration = max(0, int(track.get('duration_ms') or 0))
        key = norm(title) + '|' + '|'.join(sorted(norm(a) for a in artists))
        encoded = json.dumps(track, ensure_ascii=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT track_id FROM sources WHERE platform=? AND source_id=?', (platform,sid)).fetchone()
            if old:
                db.execute('UPDATE sources SET meta=? WHERE platform=? AND source_id=?',(encoded,platform,sid))
                return old[0]
            # Cross-platform merge only with a known duration and nonempty artist.
            candidates = db.execute('SELECT id,duration FROM tracks WHERE match_key=?', (key,)).fetchall()
            match = next((r for r in candidates if artists and duration and r['duration']
                          and abs(r['duration']-duration) <= 2000), None)
            tid = match['id'] if match else hashlib.sha256(f'{platform}:{sid}'.encode()).hexdigest()[:24]
            if not match:
                db.execute('INSERT INTO tracks(id,match_key,meta,duration,created) VALUES(?,?,?,?,?)',
                           (tid,key,encoded,duration,time.time()))
            db.execute('INSERT INTO sources VALUES(?,?,?,?)', (platform,sid,tid,encoded))
        return tid

    def enqueue(self, kind, dedupe, payload):
        now = time.time()
        jid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO jobs(id,kind,dedupe,payload,status,due,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (jid,kind,dedupe,json.dumps(payload,ensure_ascii=False),'queued',now,now,now))
            row = db.execute("SELECT id FROM jobs WHERE dedupe=? AND status IN ('queued','running','paused')", (dedupe,)).fetchone()
            db.execute("UPDATE jobs SET status='superseded' WHERE dedupe=? AND status IN ('failed','blocked')",(dedupe,))
            return row[0]

    def retry(self, jid):
        """Retry the same record; concurrent retry clicks never duplicate work."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if row is None:raise ValueError('任务不存在')
            active=db.execute("SELECT id FROM jobs WHERE dedupe=? AND status IN ('queued','running','paused')",(row['dedupe'],)).fetchone()
            if active:
                if row['id']!=active['id'] and row['status'] in ('failed','blocked','cancelled'):
                    db.execute("UPDATE jobs SET status='superseded' WHERE id=?",(jid,))
                return active['id'],False
            if row['status'] not in ('failed','blocked','cancelled'):
                return jid,False
            if row['kind']=='download':
                payload=json.loads(row['payload']);payload['manual']=True;payload['user_started']=True
                db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),jid))
            db.execute("UPDATE jobs SET status='queued',generation=generation+1,attempts=0,progress=0,error='',source_attempts='[]',due=0,updated=? WHERE id=?",(time.time(),jid))
            return jid,True

    def select_jobs(self, status=''):
        allowed = ('queued','running','paused','done','failed','blocked','cancelled','attention','superseded')
        if status and status not in allowed:
            raise ValueError('未知任务状态')
        clause = " AND status IN ('failed','blocked')" if status=='attention' else ' AND status=?' if status else ''
        return self.rows("SELECT id,status FROM jobs WHERE kind='download' AND status!='cleared'"+clause+' ORDER BY created DESC,id DESC',
                         () if status in ('','attention') else (status,))

    def job_page(self, status='', offset=0, limit=25):
        if status and status not in ('queued','running','paused','done','failed','blocked','cancelled','attention'):
            raise ValueError('未知任务状态')
        where = "status!='cleared'"
        args = ()
        if status=='attention':where += " AND status IN ('failed','blocked')"
        elif status:where += ' AND status=?';args=(status,)
        limit=max(1,min(100,limit))
        with self.connect() as db:
            # Count and rows must describe the same snapshot while workers run.
            db.execute('BEGIN')
            total=db.execute('SELECT COUNT(*) FROM jobs WHERE '+where,args).fetchone()[0]
            pages=(total+limit-1)//limit
            offset=min(max(0,offset)//limit*limit,max(0,pages-1)*limit)
            rows=[dict(r) for r in db.execute('SELECT * FROM jobs WHERE '+where+' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',(*args,limit,offset))]
            counts=db.execute("SELECT COUNT(CASE WHEN status!='cleared' THEN 1 END),COUNT(CASE WHEN status='cleared' THEN 1 END) FROM jobs WHERE kind='download'").fetchone()
        return dict(jobs=rows,total=total,pages=pages,page=offset//limit+1 if total else 0,offset=offset,limit=limit,download_total=counts[0],cleared_total=counts[1])

    def clear_download_jobs(self):
        """Soft-clear the entire download list, including in-flight work."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute("UPDATE jobs SET cleared_status=status,status='cleared',generation=generation+1,updated=? WHERE kind='download' AND status!='cleared'",(time.time(),)).rowcount
        return {'changed':count}

    def restore_download_jobs(self):
        """Restore records; unfinished downloads stay paused until started."""
        changed=0;paused=0;superseded=0
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM jobs WHERE kind='download' AND status='cleared' ORDER BY created DESC,id DESC").fetchall()
            for row in rows:
                status=row['cleared_status']
                if status in ('queued','running','paused'):status='paused'
                if status=='paused' and db.execute("SELECT 1 FROM jobs WHERE dedupe=? AND status IN ('queued','running','paused')",(row['dedupe'],)).fetchone():
                    status='superseded';superseded+=1
                if status=='paused':paused+=1
                db.execute("UPDATE jobs SET status=?,cleared_status='',generation=generation+1,updated=? WHERE id=?",(status,time.time(),row['id']))
                changed+=1
        return dict(changed=changed,paused=paused,superseded=superseded)

    def bulk_jobs(self, action, ids):
        """Apply an explicit ID snapshot atomically; never modify music files."""
        eligible = {'pause':('queued','running'), 'start':('paused','queued','failed','blocked','cancelled'),
                    'retry':('failed','blocked','cancelled'),
                    'cancel':('queued','running','paused','failed','blocked')}
        if action not in eligible or not ids or len(ids)>20000:
            raise ValueError('请选择 1–20000 个下载任务及有效操作')
        ids = list(dict.fromkeys(ids)); changed=[]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for jid in ids:
                row = db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
                if not row or row['kind']!='download' or row['status'] not in eligible[action]:
                    continue
                if action in ('start','retry'):
                    active = db.execute("SELECT id FROM jobs WHERE dedupe=? AND id!=? AND status IN ('queued','running','paused')",(row['dedupe'],jid)).fetchone()
                    if active:continue
                    payload=json.loads(row['payload']);payload.update(manual=True,user_started=True)
                    db.execute("UPDATE jobs SET status='queued',generation=generation+1,payload=?,attempts=0,progress=0,error='',source_attempts='[]',due=0,updated=? WHERE id=?",
                               (json.dumps(payload),time.time(),jid))
                else:
                    db.execute("UPDATE jobs SET status=?,generation=generation+1,error='',updated=? WHERE id=?",
                               ('paused' if action=='pause' else 'cancelled',time.time(),jid))
                changed.append(jid)
        return {'changed':len(changed),'skipped':len(ids)-len(changed),'ids':changed}

    def claim(self, sync_only=False, manual_only=False, downloads_paused=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            clause = "AND kind='download' AND json_extract(payload,'$.manual')=1 " if manual_only else "AND kind='sync' " if sync_only else ''
            if downloads_paused:
                clause += "AND (kind!='download' OR json_extract(payload,'$.user_started')=1) "
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND due<=? "+clause+"ORDER BY CASE kind WHEN 'sync' THEN 0 ELSE 1 END,due,created LIMIT 1",(time.time(),)).fetchone()
            if not row:return None
            db.execute("UPDATE jobs SET status='running',generation=generation+1,attempts=attempts+1,updated=? WHERE id=?", (time.time(),row['id']))
            item = dict(row)
            item['attempts'] += 1
            item['generation'] += 1
            item['payload'] = json.loads(item['payload'])
            return item

    def update_claimed(self, job, **fields):
        if not job.get('_worker'):
            return self.update_job(job['id'],**fields)
        if not fields or not set(fields)<={'status','due','progress','error','source_attempts'}:
            raise ValueError('Invalid job field')
        with self.connect() as db:
            return db.execute('UPDATE jobs SET '+','.join(f'{k}=?' for k in fields)+
                ",updated=? WHERE id=? AND generation=? AND status='running'",
                (*fields.values(),time.time(),job['id'],job['generation'])).rowcount==1

    def update_job(self, jid, **fields):
        allowed = {'status','due','progress','error','source_attempts'}
        if not set(fields) <= allowed:
            raise ValueError('Invalid job field')
        with self.connect() as db:
            db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) + ',updated=? WHERE id=?',
                       (*fields.values(),time.time(),jid))

    def export_playlists(self, music_root):
        root = Path(music_root).resolve()
        folder = root / 'Playlists'
        folder.mkdir(exist_ok=True, parents=True)
        groups = self.rows('SELECT DISTINCT group_name FROM rules ORDER BY group_name')
        manifest = self.home/'hub-m3u-manifest.json'
        previous = json.loads(manifest.read_text()) if manifest.exists() else []
        current = []
        for group in groups:
            name = group['group_name']
            rows = self.rows('''SELECT t.id,t.meta,t.path,MIN(r.id || ':' || printf('%09d',m.position)) ord
                FROM tracks t JOIN membership m ON m.track_id=t.id JOIN rules r ON r.id=m.rule_id
                WHERE r.enabled=1 AND r.kind NOT IN ('created','favorited') AND r.group_name=? AND t.path IS NOT NULL GROUP BY t.id ORDER BY ord''', (name,))
            lines = ['#EXTM3U']
            for row in rows:
                override=self.rows('SELECT path FROM hub_group_files WHERE name=? AND track_id=?',(name,row['id'])) if self.rows("SELECT name FROM sqlite_master WHERE name='hub_group_files'") else []
                if override:row['path']=override[0]['path']
                path = (root / row['path']).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    continue
                meta = json.loads(row['meta'])
                label = (' / '.join(meta.get('artists',[])) + ' - ' + meta['title']).replace('\n',' ').replace('\r',' ')
                lines.extend([f'#EXTINF:-1,{label}', '../' + row['path']])
            suffix = hashlib.sha256(name.encode()).hexdigest()[:6]
            target = folder / f'{safe_name(name)}--{suffix}.m3u8'
            temp = target.with_suffix('.tmp')
            temp.write_text('\n'.join(lines)+'\n', encoding='utf-8')
            temp.replace(target)
            current.append(target.name)
        # Only remove playlist files previously generated by this module, never audio.
        for name in set(previous)-set(current):
            if Path(name).name == name and name.endswith('.m3u8'):
                (folder/name).unlink(missing_ok=True)
        temp = manifest.with_suffix('.tmp')
        temp.write_text(json.dumps(current,ensure_ascii=False))
        temp.replace(manifest)
