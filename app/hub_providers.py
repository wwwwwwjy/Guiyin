"""Platform adapters: use upstream metadata clients and account-authorized resolvers."""
import http.cookiejar
import ipaddress
import json
import socket
import ssl
import certifi
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from hub_errors import UpstreamFailure

import fnsync
from commenthub.adapters.http import StdlibTransport
from commenthub.adapters.netease import NeteaseAdapter
from commenthub.adapters.qq import QQAdapter
from commenthub.adapters.kugou import KugouAdapter

PLATFORMS = {'netease':'网易云音乐','qq':'QQ 音乐','kugou':'酷狗音乐','kuwo':'酷我音乐'}
ADAPTERS = {'netease':NeteaseAdapter,'qq':QQAdapter,'kugou':KugouAdapter}


def json_request(url, payload=None, token=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('接口须为 HTTP(S) 地址，凭据请填写到令牌栏')
    headers = {'User-Agent':fnsync.UA_PC, 'Content-Type':'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with urllib.request.urlopen(request, timeout=25,context=ssl.create_default_context(cafile=certifi.where())) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('接口响应超过 4 MB')
        return json.loads(raw)


def cookie_dict(text):
    cookie = SimpleCookie()
    cookie.load(text)
    return {k:v.value for k,v in cookie.items()}


class Providers:
    def __init__(self, settings, discover, accounts=lambda platform: {}):
        self.settings = settings
        self.accounts = accounts
        self.discover_fn = discover

    def cookie(self):
        return self.accounts('netease').get('credential',{}).get('cookie') or self.settings().get('netease_cookie','')

    def search(self, platform, keyword):
        gateway = self.settings().get('gateways',{}).get(platform,{})
        if gateway.get('search_url'):
            result = json_request(gateway['search_url'],{'platform':platform,'keyword':keyword,'limit':30},gateway.get('token'))
            rows = result.get('tracks')
            if not isinstance(rows,list):
                raise ValueError('搜索接口须返回 tracks 列表')
            output = []
            for row in rows[:100]:
                if not isinstance(row,dict) or not row.get('source_id') or not row.get('title') or not isinstance(row.get('artists'),list):
                    raise ValueError('搜索结果缺少 source_id、title 或 artists')
                output.append({**row,'platform':platform})
            return output
        if platform == 'netease':
            from netease_provider import NeteaseAPI
            items = NeteaseAPI().search_music(keyword, cookie_dict(self.cookie()), 30)
            return [{'platform':platform, 'source_id':str(t['id']), 'title':t['name'],
                     'artists':str(t.get('artists') or '').split('/'), 'album':t.get('album') or '',
                     'duration_ms':t.get('duration_ms',0),'cover_url':t.get('picUrl','')} for t in items]
        if platform == 'qq':
            import hub_qq
            return hub_qq.run(hub_qq.search(keyword,self.accounts('qq').get('credential')))
        if platform == 'kugou':
            import hub_kugou
            return hub_kugou.search(keyword,self.accounts('kugou'))
        if platform not in ADAPTERS:
            raise ValueError('该平台暂不提供搜索，可使用歌单链接订阅')
        songs = ADAPTERS[platform](StdlibTransport()).search(keyword, [], limit=30)
        return [{'platform':platform,'source_id':s.song_mid or s.song_id,'title':s.name,
                 'artists':list(s.artists),'album':s.album or '', 'duration_ms':s.duration_ms or 0}
                for s in songs]

    def playlist(self, platform, ref):
        if platform=='qq':
            import hub_qq
            return hub_qq.run(hub_qq.playlist(ref,self.accounts('qq').get('credential')))
        if platform=='kugou':
            import hub_kugou
            return hub_kugou.playlist(ref,self.accounts('kugou'))
        # Authenticated NetEase fetch, including duration for conservative cross-platform dedup.
        if platform == 'netease' and self.cookie():
            from netease_provider import NeteaseAPI, APIConstants
            import requests
            pid = fnsync.parse_id(ref)
            cookies = cookie_dict(self.cookie())
            headers = {'User-Agent':APIConstants.USER_AGENT,'Referer':APIConstants.REFERER}
            r = requests.post(APIConstants.PLAYLIST_DETAIL_API, data={'id':pid}, cookies=cookies, headers=headers, timeout=25)
            r.raise_for_status()
            data = r.json()
            if data.get('code') != 200 or not data.get('playlist'):
                raise ValueError('网易歌单读取失败，请检查 Cookie 与歌单可见性')
            pl = data['playlist']
            if not isinstance(pl.get('trackIds'),list) or not isinstance(pl.get('trackCount'),int):
                raise ValueError('网易歌单缺少完整歌曲总数，保留上次结果')
            ids = [str(t['id']) for t in pl['trackIds']]
            if len(ids)!=pl['trackCount'] or len(set(ids))!=len(ids):
                raise ValueError('网易歌单 ID 列表不完整，保留上次结果')
            tracks = []
            for i in range(0,len(ids),100):
                r = requests.post(APIConstants.SONG_DETAIL_V3, data={'c':json.dumps([{'id':int(s)} for s in ids[i:i+100]])},
                                  cookies=cookies,headers=headers,timeout=25)
                r.raise_for_status()
                details = r.json()
                if details.get('code') != 200:
                    raise ValueError('网易歌曲详情未完整返回')
                tracks.extend(fnsync._ne_track_obj(t) for t in details.get('songs',[]))
            result = {'tracks':tracks,'declared_count':pl.get('trackCount'),'name':pl.get('name')}
        else:
            try:
                result = fnsync.RAW_FETCHERS[platform](ref)
            except SystemExit:
                raise ValueError('歌单读取失败，请检查链接、登录状态或平台限制') from None
        expected = result.get('declared_count')
        if expected is not None and len(result['tracks']) != int(expected):
            raise ValueError(f'歌单未取全（{len(result["tracks"])}/{expected}），保留上次结果')
        return result

    def discover(self, platform, ref, kind):
        if ref.startswith('account:'):
            account = self.accounts(platform)
            if account.get('uid')!=ref.split(':',1)[1]:
                raise ValueError('订阅账号未登录，请重新扫码')
            if platform=='qq':
                import hub_qq
                return hub_qq.run(hub_qq.discover(kind,account.get('credential')))
            if platform=='kugou':
                import hub_kugou
                return hub_kugou.discover(kind,account)
            ref=account['uid']
            account_liked = kind=='liked'
        else:
            account_liked = False
        if kind in ('playlist','chart') or (kind=='liked' and not account_liked):
            # "liked" accepts the actual liked-playlist ID rather than guessing by title.
            return [{'id':ref,'name':'指定歌单'}]
        config = self.settings().get('gateways',{}).get(platform,{})
        endpoint = config.get('account_url','').strip()
        if endpoint:
            result = json_request(endpoint, {'platform':platform,'account':ref,'kind':kind}, config.get('token'))
            if result.get('complete') is not True or not isinstance(result.get('playlists'),list):
                raise ValueError('账号接口须返回 complete=true 和 playlists 列表')
            return result['playlists']
        if platform == 'netease':
            import requests
            uid = fnsync.parse_id(ref)
            cookies = cookie_dict(self.cookie())
            output, offset, seen = [], 0, set()
            while True:
                r = requests.get('https://music.163.com/api/user/playlist',params={'uid':uid,'limit':100,'offset':offset},
                                 headers={'User-Agent':fnsync.UA_PC,'Referer':'https://music.163.com/'},cookies=cookies,timeout=25)
                r.raise_for_status()
                data = r.json()
                if data.get('code') != 200 or not isinstance(data.get('playlist'),list):
                    raise ValueError('网易账号歌单读取失败，请更新登录凭据')
                if not isinstance(data.get('more'),bool):
                    raise ValueError('网易账号分页状态不完整，保留上次结果')
                rows = data['playlist']
                for row in rows:
                    sid=str(row.get('id',''))
                    if not sid or sid in seen:
                        raise ValueError('网易账号歌单分页重复，保留上次结果')
                    seen.add(sid)
                    owned = str((row.get('creator') or {}).get('userId')) == str(uid)
                    liked = owned and row.get('specialType')==5
                    if (kind=='liked' and liked) or (kind == 'created' and owned and not liked) or (kind == 'favorited' and not owned):
                        output.append({'id':str(row['id']),'name':row.get('name','')})
                if not data.get('more'):
                    return output
                if not rows or offset >= 10000:
                    raise ValueError('账号分页未完成，保留上次结果')
                offset += len(rows)
        if platform == 'qq' and kind == 'favorited':
            raise ValueError('QQ 收藏列表需要配置已登录账号接口，或逐个粘贴收藏歌单链接订阅')
        if platform == 'kuwo':
            raise ValueError('酷我暂支持单张歌单链接，不支持账号分类发现')
        created, favorited, _, _ = self.discover_fn(platform, ref)
        return created if kind == 'created' else favorited

    def charts(self, platform):
        if platform=='qq':
            import hub_qq
            return hub_qq.run(hub_qq.charts(self.accounts('qq').get('credential')))
        if platform=='kugou':
            import hub_kugou
            return hub_kugou.charts()
        if platform != 'netease':
            return {'items':[], 'note':'请从对应音乐平台复制飙升榜、热歌榜或新歌榜的分享链接，按榜单订阅。'}
        data = json_request('https://music.163.com/api/toplist')
        if data.get('code') != 200:
            raise ValueError('榜单目录暂不可用，请粘贴榜单歌单链接')
        return {'items':[{'id':str(x['id']),'name':x['name']} for x in data.get('list',[])], 'note':''}

    def resolve(self, platform, sid, quality, meta=None, preview=False):
        # Only use audio the platform grants to this session. Prefer requested quality,
        # then fall back to ordinary full audio; never save a preview as a full track.
        from hub_errors import describe
        levels=['standard'] if preview else ['hires','lossless','exhigh','standard'][['hires','lossless','exhigh','standard'].index(quality):]
        last=None
        for level in levels:
            try:
                result=self._resolve(platform,sid,level,meta,preview)
                result.setdefault('quality',level)
                return result
            except Exception as exc:
                last=exc
                if describe(exc)[2]:
                    if platform=='netease':break
                    raise
        if platform=='netease' and self.source_enabled('builtin-netease-public'):
            try:return self.public_netease_audio(sid,meta or {},preview)
            except Exception as fallback:
                raise UpstreamFailure('网易云公开备用解析',fallback) from fallback
        raise last

    def resolve_native(self,platform,sid,quality,meta):
        levels=['hires','lossless','exhigh','standard']
        last=None
        for level in levels[levels.index(quality):]:
            try:
                result=self._resolve(platform,sid,level,meta,skip_gateway=True)
                result.setdefault('quality',level)
                return result
            except Exception as exc:last=exc
        raise last

    def source_enabled(self,key):
        registry=getattr(self,'source_registry',None)
        return registry.enabled(key) if registry else True

    def resolve_gateway(self,source,platform,sid,quality):
        # One explicit candidate; the engine owns fallthrough and its priority.
        raw=json_request(source['url'],{'platform':platform,'id':sid,'quality':quality},source.get('token'))
        result=raw.get('data',raw)
        if not isinstance(result,dict) or not result.get('url') or result.get('preview') or result.get('freeTrialInfo'):
            raise ValueError('原有接口未返回完整音频')
        public_audio_url(result['url'])
        return result

    def public_netease_audio(self,sid,meta,preview=False):
        # User-requested public provider: send only a public song ID, no account cookie.
        raw=json_request('https://musicjx.com/api/song?provider=netease&id='+urllib.parse.quote(str(sid),safe=''))
        song=raw.get('song') or {}
        if not raw.get('ok') or str(song.get('id'))!=str(sid) or song.get('provider')!='netease':
            raise ValueError('备用音源歌曲不一致')
        url=song.get('audioUrl')
        if not url:raise ValueError('备用音源暂不可用')
        public_audio_url(url)
        duration=float(song.get('duration') or 0)*1000
        if not preview and duration<=0:raise ValueError('备用音源缺少完整时长，不能确认完整歌曲')
        expected=meta.get('duration_ms') or 0
        if expected and duration and abs(expected-duration)>5000:raise ValueError('备用音源版本时长不一致')
        bitrate=int(song.get('bitrate') or 0)
        return {'url':url,'ext':song.get('format') or 'mp3','quality':'standard' if bitrate<=128000 else 'exhigh',
                'lyric':song.get('lyrics') or '', 'cover_url':song.get('cover') or '',
                'duration_ms':duration,'provider':'musicjx','preview':False}

    def _resolve(self, platform, sid, quality, meta=None, preview=False, skip_gateway=False):
        cfg = self.settings()
        custom = cfg.get('gateways',{}).get(platform,{})
        if custom.get('url') and self.source_enabled('legacy-'+platform) and not skip_gateway:
            try:
                raw = json_request(custom['url'], {'platform':platform,'id':sid,'quality':quality}, custom.get('token'))
                result = raw.get('data',raw)
                if not isinstance(result,dict) or not result.get('url') or (not preview and (result.get('preview') or result.get('freeTrialInfo'))):
                    raise ValueError('原有接口未返回完整音频')
            except Exception:
                return self._resolve(platform,sid,quality,meta,preview,skip_gateway=True)
        elif not self.source_enabled('builtin-'+platform):
            raise ValueError('内置平台音源已停用')
        elif platform=='qq':
            import hub_qq
            result=hub_qq.run(hub_qq.resolve(sid,quality,self.accounts('qq').get('credential'),meta or {}, preview=preview))
        elif platform=='kugou':
            import hub_kugou
            result=hub_kugou.resolve(sid,quality,self.accounts('kugou'),meta or {}, preview=preview)
        elif platform == 'netease':
            from netease_provider import NeteaseAPI
            api = NeteaseAPI()
            result = api.resolve_audio(int(sid),quality,cookie_dict(self.cookie()),preview=preview)
            if result.get('freeTrialInfo') and not preview:
                raise ValueError('平台仅返回试听片段，未写入完整音乐库')
            result = {'url':result.get('url'), 'quality':result.get('level'), 'ext':result.get('type'), 'preview':bool(result.get('freeTrialInfo'))}
            try:
                result['lyric'] = '' if preview else api.get_lyric(int(sid),cookie_dict(self.cookie())).get('lrc',{}).get('lyric','')
            except Exception:
                result['lyric'] = ''
            if not preview:
                try:
                    songs = api.get_song_detail(int(sid)).get('songs') or []
                    result['cover_url'] = (songs[0].get('al') or {}).get('picUrl', '') if songs else ''
                except Exception:
                    result['cover_url'] = (meta or {}).get('cover_url', '')
        else:
            raise ValueError(f'{PLATFORMS[platform]}尚未配置下载解析接口，请在设置中添加')
        if not isinstance(result,dict) or not result.get('url'):
            raise ValueError('解析未返回可下载地址，请检查账号权限、音质或接口配置')
        if not preview and (result.get('preview') or result.get('freeTrialInfo')):
            raise ValueError('解析返回试听片段，已跳过')
        if platform=='netease' and isinstance(result.get('url'),str):
            parsed=urllib.parse.urlsplit(result['url'])
            if parsed.scheme=='http' and (parsed.hostname or '').endswith('.music.126.net'):
                result['url']=urllib.parse.urlunsplit(parsed._replace(scheme='https'))
        return result


def public_audio_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('音频链接格式无效')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme=='https' else 80))
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('音频链接必须指向公网音乐 CDN')
    return url


class AudioRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_audio_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def open_audio(url, byte_range=None):
    public_audio_url(url)
    return urllib.request.build_opener(AudioRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))).open(
        urllib.request.Request(url,headers={'User-Agent':fnsync.UA_PC, **({'Range':byte_range} if byte_range else {})}), timeout=30)
