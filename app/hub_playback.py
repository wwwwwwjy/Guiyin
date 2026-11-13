"""Authenticated, short-lived playback tickets; remote URLs never enter the browser."""
import hashlib
import re
import secrets
import threading
import time
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from hub_providers import open_audio, public_audio_url


def install_playback(router, engine):
    from hub_api import Track
    tickets, lock = {}, threading.Lock()

    def owner(request):
        return hashlib.sha256(request.cookies.get('bridge_session','').encode()).hexdigest()

    @router.post('/preview')
    def preview(body: Track, request: Request):
        existing=engine().store.rows('SELECT t.id,t.path FROM sources s JOIN tracks t ON s.track_id=t.id WHERE s.platform=? AND s.source_id=?',(body.platform,body.source_id))
        if existing and existing[0]['path']:
            path=(engine().root/existing[0]['path']).resolve()
            if path.is_relative_to(engine().root) and path.is_file():
                return {'url':'/api/hub/local-audio/'+existing[0]['id'],'local':True,'preview':False}
        try:
            result = engine().providers.resolve(body.platform,body.source_id,'standard',body.model_dump(),preview=True)
            public_audio_url(result['url'])
        except Exception as exc:
            from hub_errors import describe
            code,message,retry=describe(exc)
            raise HTTPException(502 if retry else 422,message) from None
        token=secrets.token_urlsafe(32)
        with lock:
            now=time.monotonic()
            for key in list(tickets):
                if tickets[key]['expires']<now:
                    del tickets[key]
            if len(tickets)>=100:
                raise HTTPException(429,'试听请求较多，请稍后重试')
            tickets[token]={'owner':owner(request),'url':result['url'],'expires':now+900}
        return {'url':'/api/hub/stream/'+token,'preview':bool(result.get('preview')), 'expires_in':900}

    @router.get('/stream/{token}')
    def stream(token: str, request: Request):
        with lock:
            item=tickets.get(token)
            if not item or item['owner']!=owner(request) or item['expires']<time.monotonic():
                raise HTTPException(404,'试听已过期，请重新点击试听')
        byte_range=request.headers.get('range')
        if byte_range and not re.fullmatch(r'bytes=(\d+-\d*|-\d+)',byte_range):
            raise HTTPException(416,'不支持该音频范围')
        try:
            upstream=open_audio(item['url'],byte_range)
            content_type=upstream.headers.get('Content-Type','audio/mpeg').split(';')[0]
            if content_type not in ('application/octet-stream','binary/octet-stream') and not content_type.startswith('audio/'):
                upstream.close()
                raise ValueError('not audio')
            size=int(upstream.headers.get('Content-Length','0'))
            if size>128*1024*1024:
                upstream.close()
                raise ValueError('oversized')
        except Exception:
            raise HTTPException(502,'音频暂不可用，请重新试听') from None
        def chunks():
            read=0
            try:
                while read<128*1024*1024:
                    chunk=upstream.read(min(65536,128*1024*1024-read))
                    if not chunk: break
                    read+=len(chunk)
                    yield chunk
            finally:
                upstream.close()
        headers={k:upstream.headers[k] for k in ('Content-Length','Content-Range','Accept-Ranges') if upstream.headers.get(k)}
        headers.update({'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
        return StreamingResponse(chunks(),status_code=upstream.status,media_type=content_type,headers=headers)

    @router.get('/file-audio/{fid}')
    def file_audio(fid: str):
        e=engine(); rows=e.store.rows('SELECT path FROM local_files WHERE id=?',(fid,))
        if not rows:raise HTTPException(404,'歌曲不在当前目录索引中，请刷新曲库')
        path=(e.mount_root/rows[0]['path']).resolve()
        if not path.is_relative_to(e.mount_root) or not path.is_file():raise HTTPException(404,'文件不存在或未挂载')
        return FileResponse(path,headers={'Cache-Control':'private, no-store'})

    @router.get('/local-audio/{tid}')
    def local_audio(tid: str):
        e=engine()
        rows=e.store.rows('SELECT path FROM tracks WHERE id=?',(tid,))
        if not rows or not rows[0]['path']:
            raise HTTPException(404,'该歌曲尚未下载')
        path=(e.root/rows[0]['path']).resolve()
        if not path.is_relative_to(e.root.resolve()) or not path.is_file():
            raise HTTPException(404,'音频文件不存在')
        return FileResponse(path,headers={'Cache-Control':'private, no-store'})

    native_cache={}
    def native_rows():
        import fnsync
        e=engine(); user=e.settings().get('fn_user') or next(iter((e.bridge or {}).get('tokens',lambda:{})()),None)
        if not user: raise HTTPException(400,'请在设置中选择飞牛音乐账号')
        with lock:
            cached=native_cache.get(user)
            if cached and cached['expires']>time.monotonic():return cached['rows']
        try: rows=fnsync.FnMusic(user).export_library(strict=True)
        except Exception: raise HTTPException(502,'飞牛曲库暂不可用，请检查账号与连接') from None
        with lock:native_cache[user]={'rows':rows,'expires':time.monotonic()+60}
        return rows

    @router.get('/native-library')
    def native_library(q: str='', offset: int=0):
        rows=native_rows();needle=q.strip().casefold()
        selected=[t for t in rows if not needle or needle in (str(t.get('title',''))+' '+ ' '.join(a.get('name','') for a in t.get('artists',[]))).casefold()]
        offset=max(0,offset)
        return {'total':len(selected),'tracks':[{'id':t['guid'],'title':t.get('title',''),
          'artists':[a.get('name','') for a in t.get('artists',[])],'album':(t.get('album') or {}).get('name',''),
          'duration_ms':t.get('duration',0)} for t in selected[offset:offset+100]]}

    @router.get('/native-audio/{guid}')
    def native_audio(guid: str):
        from hub_fn_worker import native_paths
        if not any(t['guid']==guid for t in native_rows()):raise HTTPException(404,'当前账号没有这首歌曲')
        e=engine();nas_root=e.settings().get('fn_music_root');paths=native_paths() or {}
        if not nas_root or guid not in paths:raise HTTPException(400,'请在设置中填写 NAS 音乐目录并挂载飞牛曲库索引')
        try:relative=Path(paths[guid]).relative_to(Path(nas_root))
        except ValueError:raise HTTPException(404,'该文件不在归音挂载的音乐目录内') from None
        path=(e.root/relative).resolve()
        if not path.is_relative_to(e.root.resolve()) or not path.is_file():raise HTTPException(404,'音乐文件未挂载到归音')
        if path.suffix.lower() not in ('.mp3','.flac','.m4a','.aac','.wav','.ogg','.opus','.wma','.aiff','.ape','.dsf','.dff'):
            raise HTTPException(415,'不支持该音频格式')
        return FileResponse(path,headers={'Cache-Control':'private, no-store'})
