"""Authenticated KuGou adapter; protocol reference MakcRe/KuGouMusicApi (MIT)."""
import hashlib
import re
import kugou


def credentials(account):
    uid = str((account or {}).get('uid') or '')
    token = kugou.sessions().get(uid)
    return {'userid':int(uid),'token':token} if uid.isdigit() and token else {}


def track(row):
    if isinstance(row.get('audio_info'),dict):
        audio=row['audio_info']
        row={**row,**audio,'hash':audio.get('hash_128'),'timelen':audio.get('duration_128'),
             'name':row.get('songname'),'singerinfo':[{'name':a['author_name']} for a in row.get('authors',[]) if a.get('author_name')],
             'albuminfo':{'name':(row.get('album_info') or {}).get('album_name','')}}
    name = row.get('SongName') or row.get('name') or row.get('filename') or ''
    artists = [a['name'] for a in (row.get('Singers') or row.get('singerinfo') or []) if a.get('name')]
    if not artists and row.get('SingerName'):
        artists = row['SingerName'].split('、')
    if ' - ' in name:
        artist, name = name.split(' - ',1)
        artists = artists or artist.split('、')
    hashes = {'standard':row.get('FileHash') or row.get('hash'),
              'exhigh':row.get('HQFileHash') or row.get('hash_320'),
              'lossless':row.get('SQFileHash') or row.get('hash_flac'),
              'hires':row.get('ResFileHash') or row.get('hash_high')}
    return {'platform':'kugou','source_id':str(hashes['standard'] or ''),'title':name.strip(),
            'artists':artists,'album':row.get('AlbumName') or (row.get('albuminfo') or {}).get('name',''),
            'duration_ms':int(row.get('timelen') or int(row.get('Duration') or 0)*1000),
            'album_id':str(row.get('AlbumID') or row.get('album_id') or (row.get('albuminfo') or {}).get('id') or '0'),
            'album_audio_id':str(row.get('MixSongID') or row.get('album_audio_id') or row.get('mixsongid') or '0'),
            'hashes':{k:v for k,v in hashes.items() if v},
            'cover_url':str(row.get('Image') or row.get('img') or row.get('sizable_cover') or '').replace('{size}','500')}


def search(keyword, account):
    r = kugou.request('/v2/search/song',{'keyword':keyword,'page':1,'pagesize':30,
        'platform':'AndroidFilter','iscorrection':1,'privilegefilter':0,'area_code':1,'dopicfull':1,
        **credentials(account)},router='complexsearch.kugou.com')
    rows = (r.get('data') or {}).get('lists')
    if not isinstance(rows,list):
        raise ValueError('酷狗搜索响应不完整')
    return [track(x) for x in rows]


def account_lists(account):
    cr = credentials(account)
    if not cr:
        raise ValueError('请先扫码登录酷狗音乐')
    return kugou.user_playlists(str(cr['userid']),cr['token'])


def discover(kind, account):
    rows = account_lists(account)
    # Only an account-owned reserved favorite list may serve as My Likes.
    def liked(p):
        return not p['favorited'] and (str(p['raw'].get('listid'))=='1' or p['name']=='我喜欢')
    selected = [p for p in rows if liked(p)] if kind=='liked' else [p for p in rows if
        (p['favorited'] if kind=='favorited' else not p['favorited'] and not liked(p))]
    if kind=='liked' and len(selected)!=1:
        raise ValueError('未能唯一确认酷狗「我喜欢」，请用指定歌单订阅')
    return [{'id':p['id'],'name':p['name']} for p in selected]


def playlist(ref, account):
    if ref.startswith('rank:'):
        return rank_tracks(ref[5:])
    kind, gid = kugou.parse_ref(ref)
    if kind!='collection':
        import fnsync
        return fnsync.RAW_FETCHERS['kugou'](ref)
    cr = credentials(account)
    owned = next((p for p in account_lists(account) if p['id']==gid),None) if cr else None
    if not owned:
        # Public collection, still include account authorization when available.
        info = kugou.detail(gid)
        return kugou.tracks(gid,info)
    raw, out, seen, total = owned['raw'], [], set(), None
    if not raw.get('listid'):
        raise ValueError('酷狗账号歌单缺少 listid')
    for page in range(1,501):
        r = kugou.request('/v4/get_list_all_file_v3',cr,data={**cr,'listid':raw['listid'],
            'type':1 if owned['favorited'] else 0,'page':page,'pagesize':100,'area_code':1,
            'allplatform':1,'show_cover':1},router='cloudlist.service.kugou.com')
        d = r.get('data') or {}
        rows = d.get('info',d.get('songs'))
        declared = d.get('count',d.get('total'))
        if not isinstance(rows,list) or declared is None:
            raise ValueError('酷狗登录歌单响应缺少完整分页字段')
        if total is not None and total!=int(declared):
            raise ValueError('酷狗歌单分页期间发生变化')
        total = int(declared)
        for row in rows:
            t = track(row)
            if not t['source_id'] or t['source_id'] in seen:
                raise ValueError('酷狗歌曲标识缺失或分页重叠')
            seen.add(t['source_id']); out.append(t)
        if len(out)==total:
            return {'tracks':out,'declared_count':total,'name':owned['name']}
        if not rows or len(out)>total:
            raise ValueError('酷狗歌单未取全，保留上次结果')
    raise ValueError('酷狗歌单超出分页上限')


def resolve(sid, quality, account, meta, preview=False, _trial=False):
    cr = credentials(account)
    # Public full tracks can be authorized without an account. The official
    # response, including trial flags, decides availability for this session.
    cr = cr or {'userid':0,'token':''}
    song_hash = str(meta.get('hashes',{}).get(quality) or sid).lower()
    if not re.fullmatch('[0-9a-f]{32}',song_hash):
        raise ValueError('酷狗歌曲缺少有效文件哈希，请重新搜索或同步')
    q = {'standard':'128','exhigh':'320','lossless':'flac','hires':'high'}[quality]
    key = hashlib.md5(f'{song_hash}57ae12eb6890223e355ccfcb74edf70d1005{kugou.MID}{cr["userid"]}'.encode()).hexdigest()
    r = kugou.request('/v5/url',{**cr,'hash':song_hash,'key':key,'album_id':int(meta.get('album_id') or 0),
        'album_audio_id':int(meta.get('album_audio_id') or 0),'area_code':1,'ssa_flag':'is_fromtrack',
        'version':11430,'clientver':11430,'page_id':151369488,'quality':q,'behavior':'play',
        'pid':2,'cmd':26,'pidversion':3001,'IsFreePart':int(_trial),'ppage_id':'463467626,350369493,788954147',
        'cdnBackup':1,'module':''},router='trackercdn.kugou.com')
    d = r.get('data') if isinstance(r.get('data'),dict) else r
    urls = d.get('url') or d.get('backup_url') or d.get('backupUrl') or []
    url = urls[0] if isinstance(urls,list) and urls else urls if isinstance(urls,str) else ''
    trial = _trial or any(d.get(k) not in (None,False,0,'0','false','') for k in ('is_free_part','free_part','isFreePart'))
    if trial and not preview:
        raise ValueError('酷狗仅返回试听片段，未写入曲库')
    if not url and preview and not _trial:
        return resolve(sid,quality,account,meta,preview=True,_trial=True)
    if not url:
        raise ValueError('酷狗未授权此音质的完整音频，请检查账号权限或重新登录')
    return {'url':url,'quality':str(d.get('quality') or q),'ext':d.get('extName') or d.get('extname') or 'mp3','preview':trial,
            'cover_url':str(d.get('img') or d.get('album_img') or meta.get('cover_url') or '').replace('{size}','500')}


def rank_catalog():
    d=kugou.request('/ocean/v6/rank/list',{'plat':2,'withsong':1,'parentid':0}).get('data') or {}
    if not isinstance(d.get('info'),list):
        raise ValueError('酷狗榜单目录响应不完整')
    out=[]
    def visit(rows):
        for r in rows:
            if r.get('rankid'):
                out.append(r)
            visit(r.get('children') or [])
    visit(d['info'])
    return out


def charts():
    return {'items':[{'id':'rank:'+str(r['rankid']),'name':r.get('rankname','')} for r in rank_catalog()],'note':''}


def rank_tracks(rankid):
    item=next((r for r in rank_catalog() if str(r['rankid'])==rankid),None)
    if not item:
        raise ValueError('酷狗榜单不存在，请重新选择榜单')
    rows=[]; seen=set(); total=None
    for page in range(1,501):
        r=kugou.request('/openapi/kmr/v2/rank/audio',data={'show_portrait_mv':1,'show_type_total':1,
            'filter_original_remarks':1,'area_code':1,'pagesize':100,'rank_cid':item.get('rank_cid',0),
            'type':1,'page':page,'rank_id':int(rankid)},extra_headers={'kg-tid':'369'})
        d=r.get('data') or {}
        if not isinstance(d.get('songlist'),list) or 'total' not in d:
            raise ValueError('酷狗榜单响应不完整')
        if total is not None and total!=int(d['total']):
            raise ValueError('酷狗榜单更新中，请稍后重试')
        total=int(d['total'])
        for raw in d['songlist']:
            t=track(raw)
            if not t['source_id'] or t['source_id'] in seen:
                raise ValueError('酷狗榜单分页重复或歌曲标识缺失')
            seen.add(t['source_id']); rows.append(t)
        if len(rows)==total:
            return {'tracks':rows,'declared_count':total,'name':item.get('rankname','')}
        if not d['songlist'] or len(rows)>total:
            raise ValueError('酷狗榜单未取全')
    raise ValueError('酷狗榜单超出分页上限')
