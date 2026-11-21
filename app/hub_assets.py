"""Bounded background discovery and conservative missing sidecar matching."""
import json
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from pathlib import Path
from hub_library import EXTENSIONS, display_tag


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(s)).casefold() if c.isalnum())


def compatible(local, candidate):
    if norm(local['title'])!=norm(candidate.get('title','')):return False
    a={norm(x) for x in local.get('artists',[]) if norm(x)}
    b={norm(x) for x in candidate.get('artists',[]) if norm(x)}
    if not a or not a.intersection(b):return False
    x,y=local.get('duration_ms',0),candidate.get('duration_ms',0)
    return not (x and y and abs(x-y)>5000)


def inspect(path):
    import mutagen
    raw=mutagen.File(path)
    easy=mutagen.File(path,easy=True)
    if raw is None:raise ValueError('无法读取音频信息')
    tags=(easy.tags if easy is not None else None) or {}
    title=display_tag((tags.get('title') or [''])[0])
    artists=[display_tag(x) for x in tags.get('artist',[]) if x]
    stem=re.sub(r'--[a-f0-9]{10}$','',path.stem)
    if not title:
        if ' - ' in stem:
            artist,title=stem.split(' - ',1)
            if not artists:artists=re.split('[、/]',artist)
        else:title=stem
    rt=raw.tags or {}
    cover=bool(getattr(raw,'pictures',None)) or any(str(k).lower().startswith(('apic','covr','metadata_block_picture','coverart')) for k in rt)
    lyric=any(str(k).lower().startswith(('uslt','sylt','lyrics','©lyr')) for k in rt)
    cover=cover or any(p.is_file() and not p.is_symlink() and p.stat().st_size>0 for p in [path.with_suffix(x) for x in ('.jpg','.jpeg','.png')]+[path.parent/'cover.jpg',path.parent/'folder.jpg'])
    lyric=lyric or (path.with_suffix('.lrc').is_file() and not path.with_suffix('.lrc').is_symlink() and path.with_suffix('.lrc').stat().st_size>0)
    return dict(path=str(path),title=title,artists=artists,album=display_tag((tags.get('album') or [''])[0]),duration_ms=int(getattr(raw.info,'length',0)*1000),cover_missing=not cover,lyric_missing=not lyric)


def write_new(path,data):
    """Publish only absent sidecars; never overwrite the user's existing content."""
    fd,tmp=tempfile.mkstemp(prefix='.guiyin-asset-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data)
        try:os.link(tmp,path)
        except FileExistsError:return False
        return True
    finally:Path(tmp).unlink(missing_ok=True)


class Assets:
    def __init__(self,engine,directories=None):
        self.checkpoint=lambda:None
        self.directories=directories
        self.e=engine;self.lock=threading.RLock();self.stop=threading.Event()
        self.running=False;self.phase='尚未扫描';self.rows=[];self.scanned=0;self.error='';self.thread=None

    def status(self,offset=0):
        with self.lock:
            return dict(running=self.running,phase=self.phase,scanned=self.scanned,total=len(self.rows),
                        completed=sum(r.get('status')=='已补全' for r in self.rows),
                        error=self.error,rows=[dict(r) for r in self.rows[offset:offset+100]],offset=offset,
                        directories=(self.directories if self.directories is not None else self.e.library.directories()))

    def start(self,mode):
        with self.lock:
            if self.running:raise ValueError('已有扫描或匹配任务正在执行')
            if mode=='match' and not self.rows:raise ValueError('请先扫描缺失项目')
            self.running=True;self.stop.clear();self.error=''
            self.phase='正在扫描' if mode=='scan' else '正在匹配'
            self.thread=threading.Thread(target=self.run,args=(mode,),daemon=True,name='hub-assets')
            self.thread.start()
        return {'started':True}

    def cancelled(self):return self.stop.is_set() or self.e.stop.is_set()

    def allowed(self,path):
        if path.is_symlink():raise ValueError('不处理符号链接')
        actual=path.resolve()
        if not any(actual.is_relative_to(self.e.library.confined(p)) for p in (self.directories if self.directories is not None else self.e.library.directories())):raise ValueError('文件已不在配置的音乐目录内')
        return actual

    def run(self,mode):
        try:
            if mode=='scan':self.scan()
            else:self.match()
        except Exception as exc:
            from hub_errors import describe
            with self.lock:self.error=describe(exc)[1]
        finally:
            with self.lock:
                self.running=False;self.phase='已停止' if self.cancelled() else '扫描完成' if mode=='scan' else '匹配完成'

    def scan(self):
        with self.lock:self.rows=[];self.scanned=0
        seen=set()
        def fail(exc):raise exc
        for directory in (self.directories if self.directories is not None else self.e.library.directories()):
            root=self.e.library.confined(directory)
            for parent,dirs,files in os.walk(root,followlinks=False,onerror=fail):
                dirs[:]=[d for d in dirs if not d.startswith('.') and not Path(parent,d).is_symlink()]
                for name in files:
                    if self.cancelled():return
                    p=Path(parent,name)
                    if p.suffix.lower() not in EXTENSIONS or p.is_symlink():continue
                    p=self.allowed(p)
                    if p in seen:continue
                    seen.add(p)
                    try:r=inspect(p);r['status']='待匹配'
                    except Exception:r=dict(path=str(p),title=p.stem,artists=[],status='无法读取音频',cover_missing=False,lyric_missing=False)
                    with self.lock:
                        self.scanned+=1
                        if r['cover_missing'] or r['lyric_missing'] or r['status']=='无法读取音频':self.rows.append(r)
                    self.checkpoint()

    def resources(self,local):
        from netease_provider import NeteaseAPI
        from hub_providers import cookie_dict
        from hub_sources import resolve_http
        from hub_js_sources import resolve_assets
        import hub_artwork
        result={};failures=[];candidates={}
        if not local['artists']:return result,'缺少歌手信息，未自动匹配以免串歌'
        registry=getattr(self.e,'source_registry',None)
        rows=registry.rows(True) if registry else [dict(id='builtin-'+p,kind='builtin',name=p,platforms=[p],enabled=True) for p in ('netease','qq','kugou')]
        def complete():
            return (not local['cover_missing'] or result.get('cover')) and (not local['lyric_missing'] or result.get('lyric'))
        def check():
            if self.cancelled():raise InterruptedError('已暂停')
        def accept(value,label):
            if not isinstance(value,dict):return
            lyric=value.get('lyric')
            if local['lyric_missing'] and not result.get('lyric') and isinstance(lyric,str) and len(lyric)<=500000 and re.search(r'\[\d+:\d+',lyric):
                result.update(lyric=lyric,lyric_source=label)
            if local['cover_missing'] and not result.get('cover') and value.get('cover_url'):
                try:result.update(cover=hub_artwork.fetch(value['cover_url']),cover_source=label)
                except Exception:failures.append(label+' 封面请求失败')
        for source in rows:
            if self.cancelled() or complete():break
            if not source.get('enabled'):continue
            label=source.get('name',source['id'])
            for platform in source.get('platforms',[]):
                if self.cancelled() or complete():break
                if platform not in ('netease','qq','kugou'):continue
                try:
                    if platform not in candidates:
                        found=self.e.providers.search(platform,local['title']+' '+local['artists'][0])
                        candidates[platform]=sorted([c for c in found if compatible(local,c)],key=lambda c:norm(c.get('album',''))!=norm(local.get('album','')))[:2]
                    if not candidates[platform]:failures.append(platform+' 未找到歌名、歌手和时长相符的版本')
                    for c in candidates[platform]:
                        check();sid=c['source_id'];kind=source['kind']
                        try:
                            if kind=='lx':
                                value=resolve_assets(registry,source,platform,sid,c,local['lyric_missing'] and not result.get('lyric'),local['cover_missing'] and not result.get('cover'),check)
                                accept(value,label)
                            elif kind=='http':accept(resolve_http(source,platform,sid,'standard',c,assets=True),label)
                            elif kind=='builtin':
                                # Artwork lookup remains usable even if lyric lookup fails.
                                accept({'cover_url':c.get('cover_url')},label)
                                if platform=='qq' and local['lyric_missing'] and not result.get('lyric'):
                                    import hub_qq
                                    accept({'lyric':hub_qq.run(hub_qq.lyric(sid,self.e.providers.accounts('qq').get('credential')))},label)
                                if platform=='netease':
                                    api=NeteaseAPI()
                                    if local['lyric_missing'] and not result.get('lyric'):
                                        try:accept({'lyric':api.get_lyric(int(sid),cookie_dict(self.e.providers.cookie())).get('lrc',{}).get('lyric','')},label)
                                        except Exception:failures.append(label+' 歌词请求失败')
                                    if local['cover_missing'] and not result.get('cover'):
                                        songs=api.get_song_detail(int(sid)).get('songs') or []
                                        if songs:accept({'cover_url':(songs[0].get('al') or {}).get('picUrl','')},label)
                            elif kind=='fallback' and platform=='netease':
                                accept(self.e.providers.public_netease_audio(sid,c),label)
                            elif kind=='legacy':
                                accept(self.e.providers.resolve_gateway(source,platform,sid,'standard'),label)
                        except InterruptedError:raise
                        except Exception:failures.append(label+' 资源请求失败')
                        if complete():return result,''
                except InterruptedError:break
                except Exception:failures.append(label+' 搜索失败')
        return result,'；'.join(dict.fromkeys(failures)) or '已按启用音源优先级尝试，未找到可信的同版本歌词或封面（部分源仅支持音频）'

    def match(self, limit=None):
        import hub_artwork
        pending = (row for row in self.rows if row['status'] != '无法读取音频'
                   and (limit is None or row['status'] == '待匹配'))
        processed = 0
        for row in pending:
            if limit is not None and processed >= limit:
                return True
            processed += 1
            if self.cancelled():return
            try:
                path=self.allowed(Path(row['path']))
                local=inspect(path)
                if not local['cover_missing'] and not local['lyric_missing']:
                    with self.lock:row.update(status='已补全',cover_missing=False,lyric_missing=False)
                    continue
                with self.lock:row['status']='正在匹配'
                result,reason=self.resources(local)
                if self.cancelled():return
                self.allowed(path)
                written=[]
                if result.get('lyric') and inspect(path)['lyric_missing']:
                    if write_new(path.with_suffix('.lrc'),result['lyric'].encode('utf-8')):written.append('歌词：'+result['lyric_source'])
                if result.get('cover') and inspect(path)['cover_missing']:
                    art=result['cover'];stat=path.stat()
                    fd,tmp=tempfile.mkstemp(prefix='.guiyin-cover-',suffix=path.suffix,dir=path.parent);os.close(fd)
                    try:
                        shutil.copy2(path,tmp);hub_artwork.embed(Path(tmp),art)
                        self.allowed(path)
                        now=path.stat()
                        if (now.st_ino,now.st_size,now.st_mtime_ns)!=(stat.st_ino,stat.st_size,stat.st_mtime_ns):raise ValueError('音乐文件已改变，请重新扫描')
                        if self.cancelled():return
                        # Sidecar is mandatory; embedding improves native player compatibility.
                        if write_new(path.with_suffix(art[2]),art[0]):
                            os.replace(tmp,path);written.append('封面：'+result['cover_source'])
                    finally:Path(tmp).unlink(missing_ok=True)
                fresh=inspect(path)
                with self.lock:row.update(cover_missing=fresh['cover_missing'],lyric_missing=fresh['lyric_missing'],status='已补全' if not fresh['cover_missing'] and not fresh['lyric_missing'] else '部分补全' if written else '未匹配',detail='；'.join(written) or reason)
            except Exception as exc:
                from hub_errors import describe
                with self.lock:row.update(status='处理失败',detail=describe(exc)[1])
            self.checkpoint()
            if self.stop.wait(1):return
        self.e.library.start()
