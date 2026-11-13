"""QQMusicApi 0.9 adapter. Credentials never leave the server."""
import asyncio
import base64
import os
from pathlib import Path


class QQError(ValueError):
    pass


def run(coroutine):
    try:
        return asyncio.run(asyncio.wait_for(coroutine, timeout=90))
    except QQError:
        raise
    except Exception:
        raise QQError('QQ 请求失败，请检查平台连接、登录状态或稍后重试') from None


def client(saved=None):
    from qqmusic_api import Client
    from qqmusic_api.models.request import Credential
    home = Path(os.environ.get('FNMUSIC_HOME', '/data'))
    home.mkdir(parents=True, exist_ok=True)
    return Client(credential=Credential.model_validate(saved or {}),
                  device_path=str(home / 'hub-qq-device.json'), max_concurrency=2)


def track(t):
    return {'platform':'qq', 'source_id':t.mid, 'title':t.name,
            'artists':[s.name for s in t.singer], 'album':t.album.name,
            'duration_ms':int(t.interval)*1000, 'media_mid':t.file.media_mid,
            'numeric_id':str(t.id), 'album_mid':str(t.album.mid),
            'cover_url':f'https://y.gtimg.cn/music/photo_new/T002R500x500M000{t.album.mid}.jpg' if t.album.mid else ''}


async def start_qr(method):
    from qqmusic_api.models.login import QRLoginType
    async with client() as c:
        qr = await c.login.get_qrcode(QRLoginType(method))
        return qr, 'data:'+qr.mimetype+';base64,'+base64.b64encode(qr.data).decode()


async def poll_qr(qr):
    async with client() as c:
        r = await c.login.check_qrcode(qr)
        state = {'SCAN':'pending','CONF':'scanned','DONE':'complete','TIMEOUT':'expired','REFUSE':'cancelled'}[r.event.name]
        return state, r.credential.model_dump() if r.credential else None


async def search(keyword, saved):
    async with client(saved) as c:
        r = await c.search.search_by_type(keyword,num=30,page=1,highlight=False)
        return [track(t) for t in r.song]


class QQSnapshot(list):
    def __init__(self, rows, total):
        super().__init__(rows)
        self.total = total


async def collect(fetch, field, *, allow_partial=False):
    output, seen, expected = [], set(), None
    first_keys = []
    for page in range(1,501):
        r = await fetch(page)
        total = int(r.total)
        if expected is not None and total != expected:
            raise QQError('QQ 列表在分页期间发生变化，请重试')
        expected = total
        rows = getattr(r,field)
        for row in rows:
            key = str(getattr(row,'mid',None) or row.id)
            if key in seen:
                raise QQError('QQ 分页返回重复条目，保留上次结果')
            seen.add(key)
        if page == 1:
            first_keys = [str(getattr(row,"mid",None) or row.id) for row in rows]
        output.extend(rows)
        if len(output) == total:
            return output
        # QQ omits hasmore on some successful responses (SDK defaults it to 0).
        # The declared total and actual unique rows are the completeness contract.
        if not rows and output and allow_partial and len(output)<total:
            # Some QQ lists count unavailable entries but omit their song objects.
            # Recheck the head; expose a non-authoritative snapshot, never an empty replacement.
            check = await fetch(1)
            keys = [str(getattr(row,'mid',None) or row.id) for row in getattr(check,field)]
            if int(check.total) != total or keys != first_keys:
                raise QQError('QQ 列表在分页期间发生变化，请重试')
            return QQSnapshot(output,total)
        if len(output)>total or not rows:
            raise QQError(f'QQ 列表未取全（第 {page} 页，已读取 {len(output)}/{total} 首），保留上次结果')
    raise QQError('QQ 列表超出分页上限')


async def discover(kind, saved):
    async with client(saved) as c:
        cr = c.credential
        if not cr.musicid or not cr.musickey:
            raise QQError('请先扫码登录 QQ 音乐')
        if kind == 'liked':
            return [{'id':'liked:'+str(cr.musicid),'name':'我的喜欢'}]
        if kind == 'created':
            r = await c.user.get_created_songlist(cr.musicid)
            if len(r.playlists) != r.total or not r.finished:
                raise QQError('QQ 自建歌单未完整返回，保留上次结果')
            rows = [p for p in r.playlists if p.dirid != 201]
        else:
            if not cr.encrypt_uin:
                raise QQError('QQ 登录信息缺少账号标识，请重新扫码')
            rows = await collect(lambda page:c.user.get_fav_songlist(cr.encrypt_uin,page=page,num=100),'playlists')
        return [{'id':str(p.id),'name':p.title} for p in rows]


async def playlist(ref, saved):
    async with client(saved) as c:
        if ref.startswith('top:'):
            return await chart_tracks(c,int(ref[4:]))
        if ref.startswith('liked:'):
            if str(c.credential.musicid) != ref.split(':',1)[1] or not c.credential.encrypt_uin:
                raise QQError('QQ 喜欢列表需要对应账号重新扫码登录')
            fetch = lambda page:c.user.get_fav_song(c.credential.encrypt_uin,page=page,num=100)
        else:
            import fnsync
            if 'c6.y.qq.com' in ref:
                ref=fnsync.resolve_qq_short(ref)
            pid = int(fnsync.parse_id(ref))
            fetch = lambda page:c.songlist.get_detail(pid,num=100,page=page)
        rows = await collect(fetch,'songs',allow_partial=True)
        total = getattr(rows,'total',len(rows))
        return {'tracks':[track(t) for t in rows],'declared_count':total,
                'snapshot_complete':len(rows)==total,
                'warning':f'QQ 返回 {len(rows)}/{total} 首；已同步可见歌曲，未返回项保留旧记录并稍后补齐' if len(rows)!=total else ''}


async def resolve(sid, quality, saved, meta, preview=False):
    from qqmusic_api.modules.song import SongFileInfo, SongFileType
    async with client(saved) as c:
        # Let the platform grant public full audio to guest sessions too.
        types = {'standard':SongFileType.MP3_128,'exhigh':SongFileType.MP3_320,
                 'lossless':SongFileType.FLAC,'hires':SongFileType.FLAC}
        requested = 'lossless' if quality=='hires' else quality
        levels = ['lossless','exhigh','standard']
        for actual in levels[levels.index(requested):]:
            ft = types[actual]
            r = await c.song.get_song_urls([SongFileInfo(mid=sid,media_mid=meta.get('media_mid') or sid)],file_type=ft)
            if r.data and r.data[0].result == 0 and r.data[0].purl:
                break
        trial = False
        if preview and (not r.data or r.data[0].result != 0 or not r.data[0].purl):
            from qqmusic_api.modules.song import SpecialSongFileType
            ft = SpecialSongFileType.TRY
            trial = True
            r = await c.song.get_song_urls([SongFileInfo(mid=sid,media_mid=meta.get('media_mid') or sid)],file_type=ft)
        if not r.data or r.data[0].result != 0 or not r.data[0].purl:
            code = r.data[0].result if r.data else 'empty'
            raise QQError(f'QQ 未授权此音质的完整音频（{code}），可降低音质后重试或重新登录')
        cdn = await c.song.get_cdn_dispatch()
        if not cdn.sip:
            raise QQError('QQ 未返回音频 CDN')
        return {'url':cdn.sip[0].rstrip('/')+'/'+r.data[0].purl.lstrip('/'),
                'quality':actual,
                'ext':'flac' if ft==SongFileType.FLAC else 'mp3', 'preview':trial,
                'cover_url':meta.get('cover_url','')}


async def charts(saved):
    async with client(saved) as c:
        r=await c.top.get_category()
        return {'items':[{'id':'top:'+str(t.id),'name':t.name} for g in r.group for t in g.toplist],'note':''}


async def chart_tracks(c, top_id):
    rows=[]; seen=set(); total=None
    for page in range(1,501):
        r=await c.top.get_detail(top_id,num=100,page=page)
        if total is not None and total!=r.info.total_num:
            raise QQError('QQ 榜单更新中，请稍后重试')
        total=r.info.total_num
        for t in r.songs:
            if t.mid in seen:
                raise QQError('QQ 榜单分页重复')
            seen.add(t.mid); rows.append(track(t))
        if len(rows)==total:
            return {'tracks':rows,'declared_count':total}
        if not r.songs or len(rows)>total:
            raise QQError('QQ 榜单未取全')
    raise QQError('QQ 榜单超出分页上限')


async def lyric(sid, saved=None):
    async with client(saved) as c:
        return (await c.lyric.get_lyric(sid)).lyric


async def metadata(sid, saved=None):
    async with client(saved) as c:
        detail = await c.song.get_detail(sid)
        row = track(detail.track)
        row['date'] = next((x.value for x in detail.pub_time if x.value), '')
        return row
