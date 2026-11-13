import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from hub_engine import Engine
from hub_providers import PLATFORMS

Platform = Literal['netease','qq','kugou','kuwo']
Quality = Literal['standard','exhigh','lossless','hires']


class JobBatch(BaseModel):
    action: Literal['retry','pause','start','cancel']
    ids: list[str] = Field(min_length=1,max_length=20000)


class Rule(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1,max_length=100)
    platform: Platform
    kind: Literal['playlist','liked','created','favorited','chart'] = 'playlist'
    ref: str = Field(min_length=1,max_length=1500)
    group_name: str = Field(default='我的音乐',min_length=1,max_length=80)
    interval_hours: int = Field(default=24,ge=1,le=720)
    enabled: bool = True
    auto_download: bool = True
    quality: Quality = 'lossless'


class Track(BaseModel):
    platform: Platform
    source_id: str = Field(min_length=1,max_length=200)
    title: str = Field(min_length=1,max_length=500)
    artists: list[str] = Field(default_factory=list,max_length=30)
    album: str = Field(default='',max_length=500)
    duration_ms: int = Field(default=0,ge=0,le=86400000)
    quality: Quality = 'lossless'
    media_mid: str = Field(default='',max_length=200)
    numeric_id: str = Field(default='',max_length=100)
    album_id: str = Field(default='0',pattern=r'^\d{1,30}$')
    album_audio_id: str = Field(default='0',pattern=r'^\d{1,30}$')
    hashes: dict[Quality,str] = Field(default_factory=dict,max_length=4)


class Gateway(BaseModel):
    url: str = Field(default='',max_length=2000)
    search_url: str = Field(default='',max_length=2000)
    account_url: str = Field(default='',max_length=2000)
    token: str = Field(default='',max_length=4000)
    clear_token: bool = False


class AccountOptions(BaseModel):
    kinds: list[Literal['liked','created','favorited']] = Field(default_factory=lambda:['liked','created','favorited'],max_length=3)
    group_name: str = Field(default='我的音乐',min_length=1,max_length=80)
    interval_hours: int = Field(default=24,ge=1,le=720)
    auto_download: bool = True
    quality: Quality = 'lossless'


class LoginStart(BaseModel):
    options: AccountOptions = Field(default_factory=AccountOptions)
    method: Literal['mobile','qq','wx'] = 'mobile'


class Settings(BaseModel):
    download_quality: Quality = 'lossless'
    library_dirs: list[str] = Field(default_factory=list,max_length=30)
    downloads_paused: bool = True
    sync_enabled: bool = False
    music_dir: str = Field(default='',max_length=1000)
    fn_auto: bool = False
    fn_user: str = Field(default='',max_length=100)
    fn_music_root: str = Field(default='',max_length=1000)
    fn_library: str = Field(default='',max_length=100)
    netease_cookie: str = Field(default='',max_length=20000)
    clear_cookie: bool = False
    gateways: dict[Platform,Gateway] = Field(default_factory=dict)


class BridgeTarget(BaseModel):
    user: str = Field(min_length=1,max_length=100)


class CommentDirectory(BaseModel):
    path: str = Field(min_length=1,max_length=2000)


class CommentDirectoryControl(BaseModel):
    action: Literal['pause','resume','scan']


def install(app, bridge):
    state = {'engine':None}

    def auth(request: Request):
        active = bridge['AUTH']['auth']
        if active is None:
            raise HTTPException(503,'认证服务尚未就绪')
        if not active.session_info(request).get('authenticated'):
            raise HTTPException(401,'请先登录')
        if request.method not in ('GET','HEAD'):
            allowed, reason = active.authorize_write(request)
            if not allowed:
                raise HTTPException(403,reason)

    def engine():
        if state['engine'] is None:
            raise HTTPException(503,'下载服务尚未就绪')
        return state['engine']

    @app.on_event('startup')
    def start():
        state['engine'] = Engine(str(bridge['HOME']),os.environ.get('MUSIC_DIR','/music'),bridge['resolve_platform_playlists'],bridge)
        state['engine'].start()

    @app.on_event('shutdown')
    def close():
        if state['engine']:
            state['engine'].close()

    @app.get('/', response_class=HTMLResponse)
    @app.get('/studio', response_class=HTMLResponse)
    def page():
        return (Path(__file__).parent/'static'/'studio.html').read_text(encoding='utf-8')

    router = APIRouter(prefix='/api/hub',dependencies=[Depends(auth)])

    @router.get('/stats')
    def stats():
        return engine().stats()

    @router.get('/settings')
    def settings():
        return engine().public_settings()

    @router.put('/settings')
    def save_settings(body: Settings):
        for gateway in body.gateways.values():
            for url in (gateway.url,gateway.account_url,gateway.search_url):
                if not url:
                    continue
                parsed = urlsplit(url)
                if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query:
                    raise HTTPException(400,'接口请填写无账号、密码及查询串的 HTTP(S) 地址；令牌单独填写')
        if body.fn_music_root and (not body.fn_music_root.startswith('/') or '..' in Path(body.fn_music_root).parts):
            raise HTTPException(400,'NAS 音乐目录请填写不含 .. 的绝对路径')
        if body.fn_user and body.fn_user not in bridge.get('tokens',lambda:{})():
            raise HTTPException(400,'请先在设置中绑定该飞牛账号')
        try:
            engine().save_settings(body.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        return {'ok':True}

    @router.get('/sources')
    def sources_list():
        return {'sources':engine().source_registry.rows()}

    @router.put('/sources/order')
    def source_order(body: dict):
        try:return {'sources':engine().source_registry.set_order(body.get('ids'))}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.put('/sources')
    def source_save(body: dict):
        try:return engine().source_registry.save(body)
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/sources/import')
    def source_import(body: dict):
        if len(json.dumps(body))>262144:raise HTTPException(400,'音源配置文件最多 256 KB')
        try:return engine().source_registry.import_sources(body)
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.get('/sources/export')
    def source_export():
        from fastapi.responses import Response
        return Response(json.dumps(engine().source_registry.export(),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':'attachment; filename="guiyin-sources.json"'})

    @router.post('/sources/import-js')
    async def source_import_js(body: dict):
        code=body.get('code');filename=body.get('filename','source.js')
        if not isinstance(filename,str) or not filename.lower().endswith('.js') or len(filename)>200:
            raise HTTPException(400,'请选择 .js 音源文件')
        try:return await asyncio.to_thread(engine().source_registry.import_js,code,filename)
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.get('/sources/{source_id}/script')
    def source_script(source_id: str):
        from fastapi.responses import Response
        try:code=engine().source_registry.script(source_id)
        except ValueError as exc:raise HTTPException(400,str(exc))
        return Response(code,media_type='text/plain',headers={'Content-Disposition':'attachment; filename="'+source_id+'.js"','X-Content-Type-Options':'nosniff'})

    @router.post('/sources/test')
    async def source_test(body: dict):
        from hub_sources import test_source
        key=body.get('id','');platform=body.get('platform','');sid=str(body.get('song_id',''))
        if not isinstance(key,str) or not isinstance(platform,str) or len(sid)>200:raise HTTPException(400,'音源测试参数无效')
        try:return await asyncio.to_thread(test_source,engine(),key,platform,sid)
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.get('/asset-directories')
    def asset_directories(key: str='', offset: int=Query(0,ge=0)):
        return engine().asset_directories.status(key,offset)

    @router.post('/asset-directories')
    def asset_directory_add(body: CommentDirectory):
        try:return {'id':engine().asset_directories.add(body.path)}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/asset-directories/{key}')
    def asset_directory_control(key: str,body: CommentDirectoryControl):
        try:engine().asset_directories.control(key,body.action)
        except ValueError as exc:raise HTTPException(400,str(exc))
        return {'ok':True}

    @router.get('/metadata-directories')
    def metadata_directories(key: str='', offset: int=Query(0,ge=0)):
        return {**engine().metadata_directories.status(key,offset),'history':engine().metadata_directories.history()}

    @router.post('/metadata-directories')
    def metadata_directory_add(body: CommentDirectory):
        try:return {'id':engine().metadata_directories.add(body.path)}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/metadata-directories/{key}')
    def metadata_directory_control(key: str,body: CommentDirectoryControl):
        try:engine().metadata_directories.control(key,body.action)
        except ValueError as exc:raise HTTPException(400,str(exc))
        return {'ok':True}

    @router.post('/metadata-restore/{key}')
    def metadata_restore(key: str):
        try:engine().metadata_directories.restore(key)
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc))
        return {'ok':True}

    @router.get('/assets')
    def asset_status(offset: int=Query(0,ge=0)):
        return engine().assets.status(offset)

    @router.post('/assets/{action}')
    def asset_action(action: str):
        if action=='stop':
            engine().assets.stop.set()
            return {'ok':True}
        if action not in ('scan','match'):raise HTTPException(400,'未知操作')
        if engine().asset_directories.active is not None:raise HTTPException(409,'按目录匹配正在运行，请先暂停目录任务')
        try:return engine().assets.start(action)
        except ValueError as exc:raise HTTPException(409,str(exc))

    @router.get('/files')
    def files(q: str='', offset: int=0):
        return engine().library.page(q[:200],offset)

    @router.get('/comment-directories')
    def comment_directories():
        return engine().comment_directories.status()

    @router.post('/comment-directories')
    def comment_directory_add(body: CommentDirectory):
        try:return {'id':engine().comment_directories.add(body.path)}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/comment-directories/{key}')
    def comment_directory_control(key: str,body: CommentDirectoryControl):
        try:engine().comment_directories.control(key,body.action)
        except ValueError as exc:raise HTTPException(400,str(exc))
        return {'ok':True}

    @router.get('/comments')
    def comments(title: str=Query('',max_length=500), artist: str=Query('',max_length=500),
                 trackGuid: str=Query('',max_length=200), limit: int=Query(60,ge=1,le=200)):
        import sqlite3
        from hub_comments import query
        path=Path(os.environ.get('COMMENTHUB_DB',str(engine().store.home/'commenthub.db')))
        try:return query(path,title,artist,trackGuid,limit)
        except sqlite3.Error:raise HTTPException(503,'评论库暂时忙，请稍后重试')

    @router.get('/commented-tracks')
    def commented_tracks():
        import sqlite3
        path=Path(os.environ.get('COMMENTHUB_DB',str(engine().store.home/'commenthub.db')))
        if not path.is_file():return []
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            rows=[dict(r) for r in db.execute('''SELECT t.guid,t.title,t.artists_json AS artists,COUNT(*) count,MAX(c.last_seen) updated
                FROM comments c JOIN tracks t ON t.guid=c.track_guid GROUP BY t.guid ORDER BY updated DESC LIMIT 12''')]
            for row in rows:row['artists']=json.loads(row['artists'] or '[]')
            return rows
        finally:db.close()

    @router.get('/directories')
    def directories(path: str=''):
        try:return engine().library.browse(path)
        except (ValueError,OSError) as exc:raise HTTPException(400,str(exc))

    @router.post('/files/scan')
    def scan_files():
        return {'started':engine().library.start()}

    @router.get('/diagnostics')
    def diagnostics():
        from hub_errors import describe
        import fnsync
        e=engine();cfg=e.settings();user=cfg.get('fn_user')
        result={'version':(Path(__file__).parent/'HUB_VERSION').read_text().strip(),'library':e.library.stats(),'settings':e.public_settings(),
                'worker':any(t.is_alive() and t.name=='hub-worker' for t in e.threads)}
        if not user:return {**result,'fn_error':'请先选择飞牛音乐账号'}
        try:
            fn=fnsync.FnMusic(user)
            data=fn.get('/shared-library/list',lan='zh-CN')
            result['fn_code']=data.get('code')
            result['fn_libraries']=[{'name':r.get('name'),'guid':r.get('guid')} for r in (data.get('data') or {}).get('list',[])]
            from hub_fn_worker import native_paths
            paths=native_paths()
            result['index_paths']=len(paths or {})
            result['mapped_files']=sum((e.root/Path(path).relative_to(cfg['fn_music_root'])).is_file() for path in set((paths or {}).values()) if cfg.get('fn_music_root') and Path(path).is_relative_to(cfg['fn_music_root']))
            downloaded=e.store.rows('SELECT path FROM tracks WHERE path IS NOT NULL')
            expected=[str(Path(cfg['fn_music_root'])/r['path']) for r in downloaded] if cfg.get('fn_music_root') else []
            known=set((paths or {}).values())
            result['downloaded_indexed']=sum(p in known for p in expected)
            result['downloaded_total']=len(downloaded)
            result['missing_path_samples']=[p for p in expected if p not in known][:3]
            result['index_path_samples']=list(known)[:3]
        except Exception as exc:result['fn_error']=describe(exc)[1];result['error_type']=type(exc).__name__
        return result

    class Group(BaseModel):
        name: str = Field(min_length=1,max_length=80)
        rules: list[str] = Field(min_length=1,max_length=1000)
        previous_name: str = Field(default='',max_length=80)
        directory: str = Field(min_length=1,max_length=1000)

    @router.put('/group')
    def group(body: Group):
        e=engine();name=body.name.strip()
        if e.settings().get('sync_enabled',False):raise HTTPException(409,'请先停止同步，再调整融合目录')
        import hub_folders
        try:folder=hub_folders.directory(e,body.directory)
        except ValueError as exc:raise HTTPException(400,str(exc))
        if not name:raise HTTPException(400,'请填写目标歌单名称')
        try:hub_folders.save_group(e,name,body.rules,folder,body.previous_name)
        except ValueError as exc:raise HTTPException(400,str(exc))
        # Saving configuration is complete even if playlist export needs a retry.
        warning=''
        try:e.export()
        except Exception:warning='配置已保存，歌单文件暂未更新；下次同步时重试'
        return {'ok':True,'warning':warning}


    @router.get('/groups')
    def groups():
        e=engine()
        rows=e.store.rows("SELECT DISTINCT group_name FROM rules WHERE id NOT IN (SELECT id FROM hub_catalog_parents) AND group_name!='待分类' ORDER BY group_name")
        for row in rows:
            name=row['group_name']
            dirs=e.store.rows('SELECT directory FROM hub_group_dirs WHERE name=?',(name,))
            row['directory']=dirs[0]['directory'] if dirs else ''
            row['rules']=e.store.rows('SELECT id,name,platform FROM rules WHERE group_name=?',(name,))
            import hub_folders
            row['counts']=hub_folders.counts(e,name)
        return rows

    @router.post('/jobs/retry-network')
    def retry_network():
        e=engine();n=0
        for row in e.store.rows("SELECT * FROM jobs WHERE status='failed' AND (error LIKE '[network]%' OR error LIKE '[rate_limit]%' OR error LIKE '[tls]%' OR error NOT LIKE '[%')"):
            _,requeued=e.store.retry(row['id']);n+=int(requeued)
        return {'count':n}

    @router.get('/source-playlists')
    def source_playlists():
        return engine().store.rows('''SELECT p.*,r.name rule_name,r.group_name,r.enabled,
          (SELECT COUNT(*) FROM source_members m WHERE m.rule_id=p.rule_id AND m.playlist_id=p.source_id) tracks
          FROM source_playlists p JOIN rules r ON r.id=p.rule_id ORDER BY p.state,p.name''')

    @router.get('/rules')
    def rules():
        import hub_folders
        e=engine()
        rows=e.store.rows("SELECT * FROM rules WHERE id NOT IN (SELECT id FROM hub_catalog_parents) ORDER BY name")
        for row in rows:
            count=hub_folders.counts(e,row['group_name'],row['id'])
            row.update(tracks=count['total'],downloaded=count['downloaded'])
        return rows

    @router.post('/rules')
    def save_rule(body: Rule):
        data = body.model_dump()
        data['name'] = data['name'].strip()
        data['ref'] = data['ref'].strip()
        data['group_name'] = data['group_name'].strip()
        if not all(data[x] for x in ('name','ref','group_name')):
            raise HTTPException(400,'名称、来源、融合组不能为空')
        saved=engine().store.save_rule(data)
        engine().export()
        return saved

    @router.delete('/rules/{rid}')
    def delete_rule(rid: str):
        e = engine()
        with e.store.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE dedupe=? AND status IN ('queued','running')",('sync:'+rid,))
            db.execute('DELETE FROM rules WHERE id=?',(rid,))
            db.execute('DELETE FROM membership WHERE rule_id=?',(rid,))
            db.execute('DELETE FROM rule_playlists WHERE rule_id=?',(rid,))
            db.execute('DELETE FROM source_members WHERE rule_id=?',(rid,))
            db.execute("UPDATE source_playlists SET state='removed',updated=? WHERE rule_id=?",(time.time(),rid))
            db.execute('UPDATE pipeline SET revision=revision+1 WHERE id=1')
        e.export()
        return {'ok':True,'note':'已取消订阅；本地歌曲文件保留'}

    @router.post('/rules/{rid}/sync')
    def sync(rid: str):
        e = engine()
        try:
            e.store.rule(rid)
        except ValueError as exc:
            raise HTTPException(404,str(exc))
        import hub_folders
        if not hub_folders.selected(e,rid):raise HTTPException(409,'请先在融合歌单中勾选该来源并设置目录')
        if not e.settings().get('sync_enabled',False):raise HTTPException(409,'请先设置保存目录并点击开始同步')
        return {'job_id':e.store.enqueue('sync','sync:'+rid,{'rule_id':rid})}

    @router.post('/catalog/refresh')
    def refresh_catalog():
        import hub_folders
        return hub_folders.refresh(engine())

    @router.post('/sync/stop')
    def stop_sync():
        e=engine();e.save_settings({'sync_enabled':False,'downloads_paused':True})
        return {'ok':True,'message':'已停止派发同步和下载；正在处理的单曲完成后停止'}

    @router.post('/sync')
    def sync_all():
        e=engine()
        active=e.store.rows("SELECT * FROM rules WHERE enabled=1 AND kind NOT IN ('created','favorited') AND id NOT IN (SELECT id FROM hub_catalog_parents)")
        if not active:raise HTTPException(400,'请先设置融合歌单、来源和保存目录')
        for r in active:
            if not e.store.rows('SELECT name FROM hub_group_dirs WHERE name=?',(r['group_name'],)):
                raise HTTPException(400,'请先为“'+r['group_name']+'”设置保存目录')
        e.save_settings({'sync_enabled':True,'downloads_paused':False})
        e.export()
        return {'jobs':[e.store.enqueue('sync','sync:'+r['id'],{'rule_id':r['id']}) for r in active]}

    @router.get('/jobs')
    def jobs(status: str='', offset: int=0, limit: int=100, paged: bool=False):
        try:result=engine().store.job_page(status,offset,limit)
        except ValueError as exc:raise HTTPException(400,str(exc))
        rows=result['jobs']
        for row in rows:
            row['source_attempts']=json.loads(row.get('source_attempts') or '[]')
            payload = json.loads(row.pop('payload'))
            if row['kind']=='download':
                found = engine().store.rows('SELECT meta FROM tracks WHERE id=?',(payload['track_id'],))
                row['label'] = json.loads(found[0]['meta'])['title'] if found else '歌曲下载'
            else:
                found = engine().store.rows('SELECT name FROM rules WHERE id=?',(payload['rule_id'],))
                row['label'] = found[0]['name'] if found else '已删除的订阅'
        return result if paged else rows

    @router.post('/jobs/clear')
    def clear_jobs():
        return engine().store.clear_download_jobs()

    @router.post('/jobs/restore')
    def restore_jobs():
        return engine().store.restore_download_jobs()

    @router.get('/jobs/selection')
    def job_selection(status: str=''):
        try:
            rows=engine().store.select_jobs(status)
            if len(rows)>20000:raise HTTPException(400,'筛选结果超过 20000 项，请缩小筛选范围')
            return {'ids':[r['id'] for r in rows],'total':len(rows)}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/jobs/bulk')
    def job_bulk(value: JobBatch):
        return engine().store.bulk_jobs(value.action,value.ids)

    @router.post('/jobs/{jid}/{action}')
    def job_action(jid: str, action: Literal['retry','cancel']):
        e = engine()
        with e.store.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:
                raise HTTPException(404,'任务不存在')
            if action=='cancel':
                db.execute("UPDATE jobs SET status='cancelled',generation=generation+1 WHERE id=? AND status IN ('queued','running','paused')",(jid,))
                return {'ok':True}
            if row['status'] not in ('failed','cancelled','blocked'):
                raise HTTPException(409,'仅异常或已取消任务可以重试')
            kind,dedupe,payload = row['kind'],row['dedupe'],json.loads(row['payload'])
        jid,requeued=e.store.retry(jid)
        return {'job_id':jid,'requeued':requeued}

    @router.get('/search')
    async def search(q: str, platforms: str='netease,qq,kugou'):
        if not q.strip() or len(q)>200:
            raise HTTPException(400,'请输入 1–200 字的关键词')
        selected = list(dict.fromkeys(platforms.split(',')))
        if any(x not in PLATFORMS for x in selected):
            raise HTTPException(400,'未知平台')
        async def one(platform):
            try:
                rows = await asyncio.to_thread(engine().providers.search,platform,q.strip())
                return {'platform':platform,'tracks':rows,'error':None}
            except Exception:
                return {'platform':platform,'tracks':[],'error':'搜索暂不可用，请检查平台连接或稍后重试'}
        return {'results':await asyncio.gather(*(one(p) for p in selected))}

    @router.post('/download')
    def download(body: Track):
        track = body.model_dump()
        tid = engine().store.upsert_track(body.platform,track)
        return engine().enqueue_download(tid,body.quality,force=True)

    @router.get('/library')
    def library(q: str='', offset: int=0):
        offset = max(0,offset)
        rows = engine().store.rows('SELECT * FROM tracks WHERE meta LIKE ? ORDER BY created DESC LIMIT 100 OFFSET ?',('%'+q+'%',offset))
        for row in rows:
            row['meta'] = json.loads(row['meta'])
            row['platforms'] = [r['platform'] for r in engine().store.rows('SELECT platform FROM sources WHERE track_id=?',(row['id'],))]
        return rows

    @router.get('/charts')
    def charts(platform: Platform='netease'):
        try:
            return engine().providers.charts(platform)
        except Exception:
            raise HTTPException(502,'榜单目录暂不可用，可直接粘贴榜单分享链接')

    @router.get('/pipeline')
    def pipeline():
        return {**engine().store.pipeline(),'configured':bool(engine().settings().get('fn_user'))}

    @router.post('/scan')
    def scan():
        raise HTTPException(410,'请在飞牛音乐中扫描音乐库；归音仅负责保存音频、歌词和封面')

    @router.get('/accounts')
    def accounts():
        return engine().accounts.public()

    @router.post('/accounts/{platform}/qr')
    def qr_start(platform: Literal['netease','qq','kugou'], body: LoginStart, request: Request):
        owner=hashlib.sha256(request.cookies.get('bridge_session','').encode()).hexdigest()
        try:
            return engine().accounts.start(platform,owner,body.options.model_dump(),body.method)
        except Exception:
            raise HTTPException(502,'二维码签发失败，请检查平台连接或稍后重试')

    @router.post('/qr/{fid}/{action}')
    def qr_poll(fid: str, action: Literal['poll','cancel'], request: Request):
        owner=hashlib.sha256(request.cookies.get('bridge_session','').encode()).hexdigest()
        try:
            return getattr(engine().accounts,action)(fid,owner)
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        except Exception:
            raise HTTPException(502,'登录状态暂时无法读取，请稍后重试')

    @router.put('/accounts/{platform}')
    def account_options(platform: Literal['netease','qq','kugou'], body: AccountOptions):
        try:
            engine().accounts.apply_options(platform,body.model_dump())
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        return {'ok':True}

    @router.delete('/accounts/{platform}')
    def disconnect(platform: Literal['netease','qq','kugou']):
        engine().accounts.disconnect(platform)
        return {'ok':True}

    @router.get('/bridge-targets')
    def bridge_targets():
        return {'users':list(bridge['tokens']())}

    @router.post('/bridge-config')
    def bridge_config(body: BridgeTarget):
        if body.user not in bridge['tokens']():
            raise HTTPException(400,'请先在设置中绑定该飞牛账号')
        rows = engine().store.rows('''SELECT r.group_name,p.platform,p.source_id
            FROM rules r JOIN rule_playlists p ON p.rule_id=r.id ORDER BY r.name''')
        groups = {}
        for row in rows:
            sources = groups.setdefault(row['group_name'],[])
            item = {'platform':row['platform'],'id':row['source_id']}
            if item not in sources:
                sources.append(item)
        if not groups:
            raise HTTPException(400,'请先成功同步至少一条订阅，再生成融合配置')
        # Prepare additive configuration only. Native writes remain in Bridge's task manager.
        current = bridge['readj']('merges.json',[])
        additions = 0
        for name,sources in groups.items():
            target = next((g for g in current if g.get('fn_user')==body.user and g.get('name')=='归音 · '+name),None)
            if target is None:
                target = {'fn_user':body.user,'name':'归音 · '+name,'sources':[]}
                current.append(target)
                additions += 1
            for source in sources:
                if source not in target['sources']:
                    target['sources'].append(source)
        bridge['writej']('merges.json',current)
        return {'ok':True,'groups':len(groups),'added':additions,
                'note':'已生成飞牛融合配置。请进入飞牛桥接页检查融合组，并设置原有自动同步计划。文件须先经飞牛扫描入库。'}

    from hub_playback import install_playback
    install_playback(router, engine)
    app.include_router(router)
    return state
