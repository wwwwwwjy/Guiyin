"""Local filesystem catalog; mounted files are independent of download history."""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

EXTENSIONS={'.mp3','.flac','.m4a','.aac','.wav','.ogg','.opus','.wma','.aiff','.ape','.dsf','.dff'}

def display_tag(value, fallback=''):
    value=str(value or '').strip().strip('\x00')
    if not value:return fallback
    # Older MP3 ID3 tags often contain GBK bytes decoded as Latin-1.
    # Repair display metadata only; never rewrite the user's audio file.
    if not re.search('[\u3400-\u9fff]',value) and sum(128<=ord(c)<=255 for c in value)>=3:
        try:
            fixed=value.encode('latin1').decode('gb18030')
            if len(re.findall('[\u3400-\u9fff]',fixed))>=2:value=fixed
        except (UnicodeError,LookupError):pass
    return value

class Library:
    def __init__(self, engine):
        self.engine=engine
        self.lock=threading.Lock()
        self.running=False
        self.rescan=False
        self.error=''
        self.last_scan=0
        self.scanned=0
        with engine.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS local_files(id TEXT PRIMARY KEY,path TEXT UNIQUE,
              title TEXT,artists TEXT,album TEXT,duration INTEGER,bytes INTEGER,mtime REAL,seen REAL)''')

    def directories(self):
        return self.engine.settings().get('library_dirs', [str(self.engine.mount_root)])

    def confined(self, value):
        path=Path(value).resolve()
        if not path.is_relative_to(self.engine.mount_root):
            raise ValueError('只能选择 Docker 已挂载的音乐目录')
        if not path.is_dir():raise ValueError('目录不存在，请先检查 Docker 卷映射')
        return path

    def browse(self, value=''):
        path=self.confined(value or self.engine.mount_root)
        dirs=[]
        for p in sorted(path.iterdir()):
            if not p.name.startswith('.') and p.is_dir() and p.resolve().is_relative_to(self.engine.mount_root):
                dirs.append({'name':p.name,'path':str(p)})
        return {'path':str(path),'parent':str(path.parent) if path!=self.engine.mount_root else None,'directories':dirs}

    def start(self, *, rescan=False):
        with self.lock:
            if self.running:
                self.rescan = self.rescan or rescan
                return False
            self.running=True
        threading.Thread(target=self.scan,name='hub-file-index',daemon=True).start()
        return True

    def scan(self):
        started=time.time()
        self.scanned=0
        try:
            import mutagen
            old={r['path']:r for r in self.engine.store.rows('SELECT * FROM local_files')}
            rows={}
            def fail(error):raise error
            for directory in self.directories():
                root=self.confined(directory)
                for parent, dirs, files in os.walk(root,followlinks=False,onerror=fail):
                    dirs[:]=[d for d in dirs if not d.startswith('.') and not Path(parent,d).is_symlink()]
                    if self.engine.stop.is_set():raise RuntimeError('目录扫描已停止，将在启动后继续')
                    for name in files:
                        file=Path(parent,name)
                        if file.suffix.lower() not in EXTENSIONS or file.is_symlink():continue
                        file=file.resolve()
                        if not file.is_relative_to(self.engine.mount_root):continue
                        stat=file.stat();rel=str(file.relative_to(self.engine.mount_root))
                        self.scanned+=1
                        prior=old.get(rel)
                        if prior and prior['mtime']==stat.st_mtime and prior['bytes']==stat.st_size:
                            row=dict(prior);row['seen']=started
                        else:
                            title=file.stem;artists=[];album='';duration=0
                            try:
                                audio=mutagen.File(file,easy=True)
                                if audio:
                                    tags=audio.tags or {};title=(tags.get('title') or [title])[0]
                                    artists=list(tags.get('artist') or []);album=(tags.get('album') or [''])[0]
                                    duration=int(audio.info.length*1000)
                            except Exception:pass
                            row={'id':hashlib.sha256(rel.encode()).hexdigest()[:32],'path':rel,'title':title,
                                 'artists':json.dumps(artists,ensure_ascii=False),'album':album,'duration':duration,
                                 'bytes':stat.st_size,'mtime':stat.st_mtime,'seen':started}
                        rows[rel]=row
                        row['title']=display_tag(row['title'],file.stem)
                        row['album']=display_tag(row['album'])
                        row['artists']=json.dumps([display_tag(x) for x in json.loads(row['artists']) if display_tag(x)],ensure_ascii=False)
            # Publish only a complete traversal. An inaccessible directory keeps the old catalog.
            with self.engine.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.executemany('INSERT OR REPLACE INTO local_files VALUES(:id,:path,:title,:artists,:album,:duration,:bytes,:mtime,:seen)',rows.values())
                db.execute('DELETE FROM local_files WHERE seen<>?',(started,))
            self.error='';self.last_scan=started
        except Exception as exc:
            from hub_errors import describe
            self.error=describe(exc)[1]
        finally:
            with self.lock:
                self.running = False
                rescan, self.rescan = self.rescan, False
            if rescan and not self.engine.stop.is_set():
                self.start()

    def stats(self):
        row=self.engine.store.rows('SELECT COUNT(*) count,COALESCE(SUM(bytes),0) bytes FROM local_files')[0]
        return {**row,'running':self.running,'scanned':self.scanned,'error':self.error,'last_scan':self.last_scan,'directories':self.directories()}

    def page(self,q='',offset=0):
        where='WHERE title LIKE ? OR artists LIKE ? OR album LIKE ?';args=('%'+q+'%',)*3
        count=self.engine.store.rows('SELECT COUNT(*) n FROM local_files '+where,args)[0]['n']
        rows=self.engine.store.rows('SELECT * FROM local_files '+where+' ORDER BY title,path LIMIT 100 OFFSET ?',(*args,max(0,offset)))
        for r in rows:r['artists']=json.loads(r['artists'])
        return {'tracks':rows,'total':count,**{'scan':self.stats()}}
