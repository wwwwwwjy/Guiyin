"""Conservative, reversible tag repair; no audio transcoding or file renaming."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import mutagen
from hub_assets import Assets, inspect, norm
from hub_asset_dirs import AssetDirectories
from hub_library import EXTENSIONS

LABELS = {'title':'歌名','artist':'歌手','album':'专辑','date':'发行年份'}

def artist_set(values):
    return {norm(part) for value in values for part in re.split(r'[/、;；]', value) if norm(part)}

def identity(local, candidate):
    a,b=local.get('duration_ms',0),candidate.get('duration_ms',0)
    return (bool(norm(local.get('title',''))) and norm(local['title'])==norm(candidate.get('title',''))
        and bool(artist_set(local.get('artists',[])))
        and artist_set(local['artists'])==artist_set(candidate.get('artists',[]))
        and a>0 and b>0 and abs(a-b)<=2000)

def year(value):
    match=re.fullmatch(r'(19\d\d|20\d\d)(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?',str(value).strip())
    return match.group(1) if match else ''

def consensus(local, candidates):
    """All matching releases must agree; at least two independent platforms."""
    matching=[c for c in candidates if identity(local,c)]
    output={};proof={}
    fields={'title':lambda c:c.get('title',''), 'artist':lambda c:c.get('artists',[]),
            'album':lambda c:c.get('album',''), 'date':lambda c:year(c.get('date',''))}
    for field,extract in fields.items():
        values=[]
        for c in matching:
            value=extract(c)
            if not value:continue
            key=tuple(sorted(artist_set(value))) if field=='artist' else norm(value)
            values.append((key,value,c['platform']))
        if values and len({v[0] for v in values})==1 and len({v[2] for v in values})>=2:
            output[field]=values[0][1] if field=='artist' else [values[0][1]]
            proof[field]=sorted({v[2] for v in values})
    # Identity equivalence is not proof that a spelling/style difference is wrong.
    output.pop('title',None);output.pop('artist',None)
    # Never replace an existing precise date with a less precise year.
    if local.get('date') and year(local['date'])==next(iter(output.get('date',[])),''):
        output.pop('date',None)
    return output,proof

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def read_tags(path):
    audio=mutagen.File(path,easy=True)
    if audio is None:raise ValueError('不支持的音频标签')
    tags=audio.tags or {}
    return {key:list(tags.get(key,[])) for key in LABELS}

class MetadataWorker(Assets):
    def scan(self):
        self.rows=[];self.scanned=0
        seen=set()
        for directory in self.directories:
            root=self.e.library.confined(directory)
            def fail(exc):raise exc
            for parent,dirs,files in os.walk(root,followlinks=False,onerror=fail):
                dirs[:]=[d for d in dirs if not d.startswith('.') and not Path(parent,d).is_symlink()]
                for name in files:
                    if self.cancelled():return
                    p=Path(parent,name)
                    if p.suffix.lower() not in EXTENSIONS or p.is_symlink():continue
                    p=self.allowed(p)
                    if p in seen:continue
                    seen.add(p)
                    try:
                        r=inspect(p);r['before']=read_tags(p);r['status']='待匹配'
                        r['date']=next(iter(r['before']['date']),'')
                    except Exception:r=dict(path=str(p),title=p.stem,artists=[],status='无法读取音频')
                    with self.lock:self.rows.append(r);self.scanned+=1
                    self.checkpoint()

    def candidates(self,local):
        found=[];errors=[]
        enabled={p for s in self.e.source_registry.rows() if s['enabled'] and s['kind']=='builtin' for p in s['platforms']}
        for platform in ('netease','qq','kugou'):
            if self.cancelled():break
            if platform not in enabled:continue
            try:
                rows=self.e.providers.search(platform,local['title']+' '+' '.join(local['artists']))
                matched=[c for c in rows if identity(local,c)]
                # Bound detail requests; too many matching editions is ambiguous.
                for index,c in enumerate(matched):
                    if self.cancelled():break
                    c=dict(c)
                    try:
                        if platform=='qq' and index<3:
                            import hub_qq
                            d=hub_qq.run(hub_qq.metadata(c['source_id'],self.e.providers.accounts('qq').get('credential')))
                            if not identity(local,d):continue
                            c.update(d)
                        elif platform=='netease' and index<3:
                            from netease_provider import NeteaseAPI
                            songs=NeteaseAPI().get_song_detail(int(c['source_id'])).get('songs') or []
                            song=next((s for s in songs if str(s.get('id'))==str(c['source_id'])),{})
                            if song.get('publishTime',0)>0:c['date']=datetime.fromtimestamp(song['publishTime']/1000,timezone.utc).strftime('%Y')
                    except Exception:errors.append(platform+' 发行信息暂不可用')
                    found.append(c)
            except Exception:errors.append(platform+' 检索失败')
        return found,errors

    def match(self):
        for row in self.rows:
            if self.cancelled():return
            if row['status']=='无法读取音频':continue
            try:
                path=self.allowed(Path(row['path']))
                local=inspect(path);before=read_tags(path);local['date']=next(iter(before['date']),'')
                if not local['title'] or not local['artists']:
                    row.update(status='未修改',detail='缺少可靠的歌曲身份，不能仅凭文件名修改歌手');continue
                with self.lock:row['status']='正在匹配'
                candidates,errors=self.candidates(local)
                proposed,proof=consensus(local,candidates)
                changes={k:v for k,v in proposed.items() if before[k]!=v}
                if self.cancelled():return
                if not changes:
                    row.update(status='未修改',detail='未发现可安全自动修正的信息；歌手不一致、版本/专辑有歧义或证据不足时保留原值'+('；'+'；'.join(errors) if errors else ''))
                else:
                    key=self.e.metadata_directories.publish(path,changes,before,proof,self.cancelled)
                    row.update(status='已修复',detail='；'.join(LABELS[k]+'：'+(' / '.join(before[k]) or '空')+' → '+' / '.join(v) for k,v in changes.items()),backup_id=key)
            except Exception as exc:
                row.update(status='未修改',detail=str(exc)[:300])
            self.checkpoint()
        self.e.library.start()

class MetadataDirectories(AssetDirectories):
    table='metadata_directories'
    worker_type=MetadataWorker
    success_status='已修复'
    interval=86400
    def __init__(self,e):
        super().__init__(e)
        self.write_lock=__import__('threading').RLock()
        self.backups=e.store.home/'metadata-backups';self.backups.mkdir(exist_ok=True)
        with e.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata_changes (id TEXT PRIMARY KEY,path TEXT NOT NULL,before_tags TEXT,after_tags TEXT,proof TEXT,before_hash TEXT,after_hash TEXT,created REAL,restored INTEGER DEFAULT 0)')
    def history(self):
        return self.e.store.rows('SELECT id,path,before_tags,after_tags,proof,created,restored FROM metadata_changes ORDER BY created DESC LIMIT 100')
    def publish(self,path,changes,before,proof,cancelled=lambda:False):
        with self.write_lock:
            if path.is_symlink():raise ValueError('不处理符号链接')
            path=self.e.library.confined(str(path.parent))/path.name;original=digest(path)
            restored=self.e.store.rows('SELECT 1 FROM metadata_changes WHERE path=? AND before_hash=? AND restored=1 LIMIT 1',(str(path),original))
            if restored:raise ValueError('此前已撤销此文件的修复，保留原信息')
            key=uuid.uuid4().hex;backup=self.backups/(key+path.suffix)
            fd,tmp=tempfile.mkstemp(prefix='.guiyin-tags-',suffix=path.suffix,dir=path.parent);os.close(fd)
            try:
                shutil.copy2(path,tmp)
                audio=mutagen.File(tmp,easy=True)
                if audio is None:raise ValueError('不支持写入此音频格式')
                if audio.tags is None:audio.add_tags()
                for field,value in changes.items():audio[field]=value
                audio.save()
                if any(read_tags(Path(tmp))[k]!=v for k,v in changes.items()):raise ValueError('写入校验未通过，源文件未修改')
                if digest(path)!=original or cancelled():raise ValueError('文件已改变或任务已暂停，未写入')
                shutil.copy2(path,backup)
                os.chmod(backup,0o600)
                if digest(backup)!=original:raise ValueError('备份校验失败，源文件未修改')
                updated=digest(Path(tmp))
                with self.e.store.connect() as db:
                    db.execute('INSERT INTO metadata_changes(id,path,before_tags,after_tags,proof,before_hash,after_hash,created) VALUES (?,?,?,?,?,?,?,?)',(key,str(path),json.dumps(before,ensure_ascii=False),json.dumps(changes,ensure_ascii=False),json.dumps(proof),original,updated,time.time()))
                # Check again after backup; writes publish atomically.
                if digest(path)!=original or cancelled():raise ValueError('文件已改变或暂停，未写入')
                os.replace(tmp,path)
                return key
            finally:Path(tmp).unlink(missing_ok=True)
    def restore(self,key):
        with self.write_lock:
            rows=self.e.store.rows('SELECT * FROM metadata_changes WHERE id=?',(key,))
            if not rows:raise ValueError('修改记录不存在')
            r=rows[0];path=Path(r['path']);path=self.e.library.confined(str(path.parent))/path.name
            if path.is_symlink():raise ValueError('不能恢复符号链接')
            if r['restored']:raise ValueError('已经撤销')
            if digest(path)!=r['after_hash']:raise ValueError('修复后文件又发生变化，为保护后续修改已停止撤销')
            backup=self.backups/(key+path.suffix)
            if digest(backup)!=r['before_hash']:raise ValueError('备份校验失败')
            fd,tmp=tempfile.mkstemp(prefix='.guiyin-restore-',suffix=path.suffix,dir=path.parent);os.close(fd)
            try:
                shutil.copy2(backup,tmp);os.chmod(tmp,path.stat().st_mode & 0o777)
                if digest(path)!=r['after_hash']:raise ValueError('文件已改变，未恢复')
                os.replace(tmp,path)
                with self.e.store.connect() as db:db.execute('UPDATE metadata_changes SET restored=1 WHERE id=?',(key,))
            finally:Path(tmp).unlink(missing_ok=True)
        self.e.library.start()
