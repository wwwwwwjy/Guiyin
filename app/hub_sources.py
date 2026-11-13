"""Persistent HTTP and isolated LX JS sources; no platform credential forwarding."""
import json
import os
import threading
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parent / 'sources'
PLATFORMS = {'netease','qq','kugou','kuwo'}

def validate(item):
    if not isinstance(item,dict):raise ValueError('音源须为 JSON 对象')
    name=str(item.get('name','')).strip()
    url=str(item.get('url','')).strip()
    p=urlsplit(url)
    if not name or len(name)>100:raise ValueError('请填写音源名称（最多 100 字）')
    if len(url)>2000 or p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('接口须为无凭据、查询串及片段的 HTTP(S) 地址；令牌单独填写')
    platforms=item.get('platforms')
    if not isinstance(platforms,list) or not platforms or any(not isinstance(p,str) or p not in PLATFORMS for p in platforms):raise ValueError('请选择支持的平台')
    if type(item.get('enabled',True)) is not bool:raise ValueError('enabled 须为布尔值')
    token=item.get('token','')
    if not isinstance(token,str) or len(token)>4000 or '\n' in token or '\r' in token:raise ValueError('令牌格式无效')
    return {'name':name,'url':url,'platforms':list(dict.fromkeys(platforms)),'enabled':item.get('enabled',True),'token':token,'kind':'http'}

class Sources:
    def __init__(self,home,settings):
        self.path=Path(home)/'sources'/'custom.json'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.settings=settings
        self.lock=threading.RLock()
        self.builtins=json.loads((ROOT/'builtin.json').read_text())['sources']
        if not self.path.exists():
            bundled=json.loads((ROOT/'custom.json').read_text())
            rows=[]
            for item in bundled.get('sources',[]):
                v=validate(item);v['id']='custom-'+uuid.uuid4().hex;rows.append(v)
            self._write({'version':1,'sources':rows,'disabled':[]})
    def _read(self):return json.loads(self.path.read_text())
    def _write(self,data):
        temp=self.path.with_suffix('.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2)
        temp.replace(self.path)
    def rows(self,private=False):
        with self.lock:d=self._read()
        rows=[dict(x) for x in self.builtins]
        for platform,g in self.settings().get('gateways',{}).items():
            if g.get('url'):rows.append({'id':'legacy-'+platform,'name':platform+' · 原有自定义接口','kind':'legacy','platforms':[platform],'url':g['url'],'token':g.get('token','')})
        rows+=d['sources']
        order={key:i for i,key in enumerate(d.get('order',[]))}
        rows.sort(key=lambda row:order.get(row['id'],len(order)))
        for i,row in enumerate(rows):
            row['priority']=i+1
            row['enabled']=row.get('enabled',True) and row['id'] not in d.get('disabled',[])
            row['token_configured']=bool(row.get('token'))
            if not private:row.pop('token',None)
        return rows
    def set_order(self,ids):
        with self.lock:
            current=[row['id'] for row in self.rows()]
            if not isinstance(ids,list) or any(not isinstance(key,str) for key in ids) or len(ids)!=len(current) or len(set(ids))!=len(ids) or set(ids)!=set(current):
                raise ValueError('音源列表已变化或优先级无效，请刷新后重试')
            data=self._read();data['order']=ids;self._write(data)
        return self.rows()
    def enabled(self,key):return any(x['id']==key and x['enabled'] for x in self.rows())
    def save(self,item):
        key=item.get('id','')
        with self.lock:
            data=self._read()
            old=next((x for x in self.rows(True) if x['id']==key),None)
            if key and not old:raise ValueError('音源不存在，请刷新后重试')
            if old and old['kind']!='http':
                if type(item.get('enabled')) is not bool:raise ValueError('请选择启用或停用')
                disabled=set(data.get('disabled',[]))
                disabled.discard(key) if item['enabled'] else disabled.add(key)
                data['disabled']=sorted(disabled)
            else:
                v=validate(item)
                v['id']=key or 'custom-'+uuid.uuid4().hex
                v['token']='' if item.get('clear_token') else v['token'] or (old or {}).get('token','')
                if old:data['sources']=[v if x['id']==key else x for x in data['sources']]
                else:
                    if len(data['sources'])>=32:raise ValueError('最多添加 32 个自定义音源')
                    data['sources'].append(v)
                key=v['id']
            self._write(data)
        return next(x for x in self.rows() if x['id']==key)
    def import_sources(self,body):
        if not isinstance(body,dict) or body.get('version')!=1 or not isinstance(body.get('sources'),list):raise ValueError('请导入 version=1、sources 数组的 JSON 文件')
        # Export contains builtins for reference. Only HTTP definitions are portable.
        items=[validate(x) for x in body['sources'] if isinstance(x,dict) and x.get('kind','http')=='http']
        if not items:raise ValueError('文件中没有可导入的自定义 HTTP 音源')
        with self.lock:
            data=self._read();added=0
            known={(x['url'],tuple(sorted(x['platforms']))) for x in data['sources'] if x['kind']=='http'}
            for v in items:
                signature=(v['url'],tuple(sorted(v['platforms'])))
                if signature in known:continue
                known.add(signature);v['id']='custom-'+uuid.uuid4().hex;data['sources'].append(v);added+=1
            if len(data['sources'])>32:raise ValueError('最多添加 32 个自定义音源；此次未写入')
            self._write(data)
        return {'added':added}
    def import_js(self,code,filename):
        from hub_js_sources import inspect_script
        entry=inspect_script(code,filename)
        entry['id']='js-'+entry['sha256'][:24]
        with self.lock:
            data=self._read()
            existing=next((x for x in data['sources'] if x.get('sha256')==entry['sha256']),None)
            if not existing and len(data['sources'])>=32:raise ValueError('最多添加 32 个自定义音源')
            scripts=self.path.parent/'scripts';scripts.mkdir(exist_ok=True,mode=0o700)
            path=scripts/(entry['sha256']+'.js')
            # Hash, persist and read the same UTF-8 bytes. Text-mode reads convert
            # Windows CRLF to LF, incorrectly rejecting an untouched LX script.
            raw=code.encode('utf-8')
            needs_write=not path.exists() or path.read_bytes()!=raw
            if needs_write:
                temp=None
                try:
                    with tempfile.NamedTemporaryFile(dir=scripts,prefix='.import-',delete=False) as f:
                        temp=Path(f.name);f.write(raw);f.flush();os.fsync(f.fileno())
                    temp.replace(path)
                finally:
                    if temp is not None:temp.unlink(missing_ok=True)
            if existing:
                return {'added':0,'repaired':needs_write,'source':next(x for x in self.rows() if x['id']==existing['id'])}
            data['sources'].append(entry);self._write(data)
        return {'added':1,'source':entry}
    def script(self,key):
        import hashlib,re
        row=next((x for x in self.rows() if x['id']==key and x['kind']=='lx'),None)
        if not row or not re.fullmatch('[0-9a-f]{64}',row.get('sha256','')):raise ValueError('JS 音源不存在')
        try:raw=(self.path.parent/'scripts'/(row['sha256']+'.js')).read_bytes()
        except OSError:raise ValueError('JS 音源文件缺失，请重新导入')
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('JS 音源文件校验失败，请重新导入')
        return raw.decode('utf-8')
    def export(self):return {'version':1,'sources':self.rows(),'note':'JSON 不包含账号凭据、接口令牌或 JS 脚本；JS 文件请使用各音源的“下载 JS”按钮。'}
    def options(self,platform):
        # Each visible row is one candidate; hidden chains would bypass the saved
        # priority (especially when a JS source is placed before platform APIs).
        return [row for row in self.rows(True) if row['enabled'] and platform in row['platforms']]


def resolve_http(item,platform,sid,quality,meta,assets=False):
    import ssl,certifi,urllib.request
    from hub_providers import public_audio_url
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    payload={'platform':platform,'id':sid,'quality':quality,'title':meta.get('title',''),'artists':meta.get('artists',[]),'duration_ms':meta.get('duration_ms',0)}
    if assets:payload['action']='assets'
    headers={'Content-Type':'application/json','User-Agent':'Guiyin/0.9.13'}
    if item.get('token'):headers['Authorization']='Bearer '+item['token']
    opener=urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    with opener.open(urllib.request.Request(item['url'],data=json.dumps(payload).encode(),headers=headers),timeout=15) as r:
        raw=r.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('音源响应超过 1 MB')
    raw=json.loads(raw);result=raw.get('data',raw)
    if assets:
        if not isinstance(result,dict) or result.get('platform')!=platform or str(result.get('id'))!=str(sid):
            raise ValueError('音源返回的歌曲标识不匹配')
        duration=result.get('duration_ms')
        if duration is not None and (type(duration) not in (int,float) or not 1000<=duration<=86400000 or (meta.get('duration_ms') and abs(duration-meta['duration_ms'])>5000)):
            raise ValueError('音源版本时长与歌曲不匹配')
        return {k:result[k] for k in ('lyric','cover_url') if k in result}
    return validate_result(result,platform,sid,meta)

def validate_result(result,platform,sid,meta):
    from hub_providers import public_audio_url
    if not isinstance(result,dict) or result.get('platform')!=platform or str(result.get('id'))!=str(sid):raise ValueError('音源返回的歌曲标识不匹配')
    if result.get('preview') is not False or result.get('freeTrialInfo'):raise ValueError('音源未确认完整音频，或只返回试听片段')
    duration=result.get('duration_ms')
    if type(duration) not in (int,float) or not 1000<=duration<=86400000:raise ValueError('音源缺少有效歌曲时长')
    if meta.get('duration_ms') and abs(meta['duration_ms']-duration)>5000:raise ValueError('音源版本时长与歌曲不匹配')
    if result.get('quality') not in ('standard','exhigh','lossless','hires'):raise ValueError('音源缺少有效音质标识')
    public_audio_url(result.get('url',''))
    return {k:v for k,v in result.items() if k in ('url','quality','duration_ms','preview','lyric','cover_url','ext')}


def test_source(engine,key,platform='',sid=''):
    """Bounded connection probe; does not claim full-file validation or save audio."""
    from hub_providers import open_audio,json_request
    from hub_errors import describe
    row=next((x for x in engine.source_registry.rows(True) if x['id']==key),None)
    if row is None:raise ValueError('音源不存在')
    platform=platform or row['platforms'][0]
    if platform not in row['platforms']:raise ValueError('音源不支持所选平台')
    meta={}
    if sid:
        saved=engine.store.rows('SELECT meta FROM sources WHERE platform=? AND source_id=? LIMIT 1',(platform,sid))
        if saved:meta=json.loads(saved[0]['meta'])
    else:
        saved=engine.store.rows('SELECT source_id,meta FROM sources WHERE platform=? LIMIT 1',(platform,))
        if not saved:raise ValueError('该平台暂无歌曲，请填写一首歌曲 ID')
        sid=saved[0]['source_id'];meta=json.loads(saved[0]['meta'])
    try:
        quality=engine.settings().get('download_quality','lossless')
        if row['kind']=='lx':
            from hub_js_sources import resolve_js
            result=resolve_js(engine.source_registry,row,platform,sid,quality,meta)
        elif row['kind']=='http':result=resolve_http(row,platform,sid,quality,meta)
        elif row['kind']=='fallback':result=engine.providers.public_netease_audio(sid,meta)
        elif row['kind']=='legacy':
            raw=json_request(row['url'],{'platform':platform,'id':sid,'quality':quality},row.get('token'))
            result=raw.get('data',raw)
        else:
            if not row['enabled']:raise ValueError('请先启用内置源再测试')
            result=engine.providers._resolve(platform,sid,quality,meta,skip_gateway=True)
        if not isinstance(result,dict) or not result.get('url'):raise ValueError('音源未返回音频地址')
        if result.get('preview') or result.get('freeTrialInfo'):raise ValueError('返回试听片段')
        with open_audio(result['url'],'bytes=0-4095') as response:
            data=response.read(4096)
        audio=data.startswith((b'ID3',b'fLaC',b'OggS',b'RIFF')) or (len(data)>1 and data[0]==255 and data[1]&224==224) or (len(data)>8 and data[4:8]==b'ftyp')
        if not audio:raise ValueError('连接返回的内容不是可识别的音频头')
        return {'ok':True,'message':'该歌曲解析与音频连接正常；完整文件将在实际下载时校验','platform':platform,'sample_id':sid}
    except Exception as exc:
        return {'ok':False,'message':describe(exc)[1],'platform':platform,'sample_id':sid}
