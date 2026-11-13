"""LX source adapter. Only song metadata enters the short-lived JS process."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

MAX_SCRIPT=262144
PLATFORMS={'wy':'netease','tx':'qq','kg':'kugou','kw':'kuwo'}
LX_PLATFORM={v:k for k,v in PLATFORMS.items()}
SLOTS=threading.BoundedSemaphore(2)


def invoke(code,operation='inspect',request=None,check=lambda:None):
    if not isinstance(code,str) or not code.strip() or len(code.encode())>MAX_SCRIPT:
        raise ValueError('JS 音源文件须为 UTF-8 文本，最多 256 KB')
    deadline=time.monotonic()+50
    while not SLOTS.acquire(timeout=0.1):
        check()
        if time.monotonic()>deadline:raise ValueError('音源运行繁忙，请稍后重试')
    process=None
    try:
        check()
        process=subprocess.Popen([sys.executable,'-I',str(Path(__file__).with_name('lx_worker.py'))],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            env={'LANG':'C.UTF-8'},cwd='/tmp',start_new_session=True)
        payload=json.dumps({'code':code,'operation':operation,'request':request},ensure_ascii=False).encode()+b'\n'
        # communicate handles long pipe inputs without blocking cancellation polling.
        first=True
        deadline=time.monotonic()+(5 if operation=='inspect' else 45)
        while True:
            check()
            if time.monotonic()>deadline:raise ValueError('JS 音源运行超时')
            try:
                output,_=process.communicate(payload if first else None,timeout=0.1)
                break
            except subprocess.TimeoutExpired:first=False
        if process.returncode or len(output)>MAX_SCRIPT:raise ValueError('JS 音源运行中断或超出资源限制')
        try:result=json.loads(output)
        except ValueError:raise ValueError('JS 音源未返回有效结果')
        if not result.get('ok'):raise ValueError('JS 音源：'+str(result.get('error','运行失败')))
        return result.get('value')
    finally:
        if process is not None:
            if process.poll() is None:process.kill()
            process.communicate()
        SLOTS.release()


def inspect_script(code,filename):
    info=invoke(code)
    declared=info.get('sources')
    if info.get('status') is False or not isinstance(declared,dict):raise ValueError('音源初始化失败或未声明支持的平台')
    supported={}
    for key,platform in PLATFORMS.items():
        entry=declared.get(key)
        if not isinstance(entry,dict) or 'musicUrl' not in entry.get('actions',[]):continue
        qualities=entry.get('qualitys',entry.get('qualities',[]))
        known=[q for q in qualities if q in ('128k','192k','320k','flac','flac24bit','hires')]
        if known:supported[platform]=list(dict.fromkeys(known))
    if not supported:raise ValueError('音源没有归音支持的平台 musicUrl 接口或音质')
    def tag(name):
        match=re.search(r'@'+name+r'\s+([^\r\n]+)',code[:4096])
        return match.group(1).strip().rstrip('*/').strip()[:100] if match else ''
    return {'kind':'lx','name':tag('name') or Path(filename).stem[:100] or 'JS 音源',
            'filename':Path(filename).name[:160],'version':tag('version'),'author':tag('author'),
            'platforms':list(supported),'qualities':supported,'sha256':hashlib.sha256(code.encode()).hexdigest(),
            'enabled':True,'note':'LX JS · 接口已识别，实际可用性请测试'}


def music_info(platform,sid,meta):
    artists=meta.get('artists') or []
    singer='、'.join(str(a.get('name','') if isinstance(a,dict) else a) for a in artists)
    duration=meta.get('duration_ms',0) or 0
    # Whitelist only public song metadata; never forward platform sessions/cookies.
    value={'id':str(sid),'songmid':str(sid),'name':str(meta.get('title','')),'singer':singer,
           'albumName':str(meta.get('album','')),'albumId':str(meta.get('album_id','')),
           'interval':f'{int(duration/60000):02}:{int(duration/1000)%60:02}',
           'duration':duration/1000,'_types':{}}
    if platform=='kugou':value['hash']=str(sid)
    if platform=='qq':
        value['songId']=str(meta.get('numeric_id',''))
        value['songid']=value['songId']
        value['albumId']=str(meta.get('album_mid',''))
    if platform=='kuwo':value['rid']=str(sid)
    return value


def resolve_js(registry,item,platform,sid,quality,meta,check=lambda:None):
    code=registry.script(item['id'])
    ladder={'hires':['flac24bit','hires','flac','320k','128k'], 'lossless':['flac','320k','128k'],
            'exhigh':['320k','128k'],'standard':['128k']}.get(quality,['128k'])
    supported=item['qualities'].get(platform,[])
    selected=next((q for q in ladder if q in supported),None)
    if not selected:raise ValueError('JS 音源不支持所选平台音质')
    value=invoke(code,'resolve',{'action':'musicUrl','source':LX_PLATFORM[platform],
                 'info':{'type':selected,'musicInfo':music_info(platform,sid,meta)}},check)
    if isinstance(value,str):value={'url':value}
    if not isinstance(value,dict) or value.get('preview') or value.get('freeTrialInfo'):
        raise ValueError('JS 音源未返回完整音频地址')
    from hub_providers import public_audio_url
    url=value.get('url','')
    public_audio_url(url)
    # Most LX scripts return just a URL and may downgrade internally. The requested
    # quality is not evidence of the real codec; transfer validation reads the file.
    return {'url':url,'quality':'unknown','duration_ms':meta.get('duration_ms',0),
            'provider':item['id'],'requires_duration':True}


def resolve_assets(registry,item,platform,sid,meta,want_lyric=True,want_cover=True,check=lambda:None):
    supported=item.get('qualities',{}).get(platform,[])
    return invoke(registry.script(item['id']),'assets',{
        'source':LX_PLATFORM[platform], 'wantLyric':want_lyric,'wantCover':want_cover,
        'info':{'type':'128k' if '128k' in supported else next(iter(supported),'128k'),'musicInfo':music_info(platform,sid,meta)}},check)
