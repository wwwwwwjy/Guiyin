import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock

import pytest
from hub_engine import Engine
from hub_store import Store
from hub_accounts import Accounts
from hub_api import AccountOptions
from hub_fn_worker import reconcile, match
import hub_qq
import hub_kugou


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path/'data',tmp_path/'music',lambda *a:([],[],'',''))


def song(sid='1'):
    return {'source_id':sid,'title':'晴天','artists':['周杰伦'],'duration_ms':2000,'album':'测试'}


def rule():
    return {'name':'通勤','platform':'netease','kind':'playlist','ref':'1','group_name':'喜欢',
            'interval_hours':24,'enabled':True,'auto_download':True,'quality':'standard'}


def test_qr_owner_expiry_cancel_and_single_bind(engine):
    a=engine.accounts; opts=AccountOptions().model_dump()
    with patch('hub_qq.start_qr',new=AsyncMock(return_value=('key','data:image/png;base64,AA'))):
        r=a.start('qq','owner-a',opts,method='qq')
    with pytest.raises(ValueError):a.poll(r['flow_id'],'owner-b')
    saved={'musicid':123,'musickey':'PRIVATE','encrypt_uin':'euin'}
    with patch('hub_qq.poll_qr',new=AsyncMock(return_value=('complete',saved))):
        assert a.poll(r['flow_id'],'owner-a')['state']=='complete'
        assert a.poll(r['flow_id'],'owner-a')['state']=='complete'
    assert len(engine.store.rows('SELECT * FROM rules'))==3
    assert len(engine.store.rows('SELECT * FROM jobs'))==3
    assert all(r['ref']=='account:123' for r in engine.store.rows('SELECT * FROM rules'))
    assert 'PRIVATE' not in json.dumps(a.public())
    assert a.path.stat().st_mode & 0o777 == 0o600
    with patch('hub_qq.start_qr',new=AsyncMock(return_value=('key','data:image/png;base64,AA'))):
        r=a.start('qq','owner-a',opts,method='qq')
    a.flows[r['flow_id']]['expires']=0
    assert a.poll(r['flow_id'],'owner-a')['state']=='expired'
    assert 'key' not in a.flows[r['flow_id']]
    with patch('hub_qq.start_qr',new=AsyncMock(return_value=('key','data:image/png;base64,AA'))):
        r=a.start('qq','owner-a',opts,method='qq')
    assert a.cancel(r['flow_id'],'owner-a')['state']=='cancelled'
    assert a.poll(r['flow_id'],'owner-a')['state']=='cancelled'


def test_switch_identity_pauses_old_and_disconnect_revokes(engine):
    a=engine.accounts;opts=AccountOptions().model_dump()
    a.bind('qq','1',{'musickey':'OLD'},opts)
    a.bind('qq','2',{'musickey':'NEW'},opts)
    rows=engine.store.rows('SELECT * FROM rules WHERE ref=?',('account:1',))
    assert all(not r['enabled'] for r in rows)
    assert len(engine.store.rows("SELECT * FROM jobs WHERE status='cancelled'"))==3
    with pytest.raises(ValueError,match='未登录'):engine.providers.discover('qq','account:1','liked')
    a.disconnect('qq')
    assert not a.private('qq')
    assert not engine.store.rows('SELECT * FROM rules WHERE enabled=1')


def test_account_choices_are_idempotent_and_survive_restart(engine):
    opts=AccountOptions(kinds=['liked'],group_name='三平台喜欢',quality='standard').model_dump()
    engine.accounts.bind('netease','42',{'cookie':'MUSIC_U=private'},opts)
    engine.accounts.apply_options('netease',opts)
    assert len(engine.store.rows("SELECT * FROM jobs WHERE status='queued'"))==1
    assert len(engine.store.rows('SELECT * FROM rules WHERE enabled=1'))==1
    restored=Engine(engine.store.home,engine.root,lambda *a:None)
    assert restored.providers.cookie()=='MUSIC_U=private'
    assert restored.accounts.public()[0]['options']['group_name']=='三平台喜欢'


def test_qq_complete_pages_and_partial_rejected():
    rows=[NS(mid='a'),NS(mid='b')]
    async def valid(page):return NS(total=2,hasmore=page<2,songs=[rows[page-1]])
    assert len(asyncio.run(hub_qq.collect(valid,'songs')))==2
    async def short(page):return NS(total=2,hasmore=False,songs=[rows[0]])
    with pytest.raises(ValueError,match='未取全'):asyncio.run(hub_qq.collect(short,'songs'))
    async def overlap(page):return NS(total=2,hasmore=True,songs=[rows[0]])
    with pytest.raises(ValueError,match='重复'):asyncio.run(hub_qq.collect(overlap,'songs'))


class ContextClient:
    def __init__(self, **kw):self.__dict__.update(kw)
    async def __aenter__(self):return self
    async def __aexit__(self,*a):pass


def test_qq_authorized_url_exact_quality_and_no_permission():
    urls=AsyncMock(return_value=NS(data=[NS(result=0,purl='song.flac?vkey=secret')]))
    c=ContextClient(credential=NS(musickey='key'),song=NS(get_song_urls=urls,get_cdn_dispatch=AsyncMock(return_value=NS(sip=['https://cdn.example/']))))
    with patch('hub_qq.client',return_value=c):
        result=asyncio.run(hub_qq.resolve('mid','hires',{}, {'media_mid':'media'}))
        assert result['quality']=='lossless' and result['ext']=='flac'
        assert urls.call_args.args[0][0].media_mid=='media'
        urls.return_value=NS(data=[NS(result=104003,purl='')])
        with pytest.raises(ValueError,match='未授权'):asyncio.run(hub_qq.resolve('mid','lossless',{},{}))


def test_qq_errors_do_not_leak_transport_credentials():
    async def fail():raise RuntimeError('url?musickey=PRIVATE')
    with pytest.raises(ValueError) as e:hub_qq.run(fail())
    assert 'PRIVATE' not in str(e.value)


def test_kugou_search_mapping_and_preview_block():
    row={'FileHash':'A'*32,'HQFileHash':'B'*32,'SongName':'晴天','SingerName':'周杰伦',
         'AlbumID':'22','MixSongID':'33','Duration':269,'AlbumName':'叶惠美'}
    with patch('kugou.request',return_value={'data':{'lists':[row]}}):
        t=hub_kugou.search('晴天',{})[0]
    assert t['duration_ms']==269000 and t['album_audio_id']=='33'
    with patch('kugou.sessions',return_value={'1':'TOKEN'}),patch('kugou.request',return_value={'url':['https://cdn.example/a'],'is_free_part':1}) as req:
        with pytest.raises(ValueError,match='试听'):hub_kugou.resolve('A'*32,'exhigh',{'uid':'1'},t)
        assert req.call_args.args[1]['hash']=='b'*32
        assert req.call_args.args[1]['IsFreePart']==0
        assert req.call_args.args[1]['token']=='TOKEN'


def test_kugou_private_playlist_pagination():
    listed=[{'id':'collection_1_1_1_1','name':'我喜欢','favorited':False,'raw':{'listid':1}}]
    with patch('hub_kugou.account_lists',return_value=listed),patch('hub_kugou.credentials',return_value={'userid':1,'token':'secret'}),patch('kugou.request',return_value={'data':{'count':2,'info':[{'hash':'a','name':'A - S','timelen':1000}]}}):
        with pytest.raises(ValueError,match='分页重叠'):hub_kugou.playlist('collection_1_1_1_1',{})


class Fn:
    def __init__(self, visible=None, scan_code=0):
        self.visible=visible or [];self.scan_code=scan_code;self.scans=0;self.created=0;self.lists=[]
    def get(self,*a,**kw):return {'code':0,'data':{'list':[{'guid':'library'}]}}
    def scan_library(self,g):self.scans+=1;return {'code':self.scan_code}
    def export_library(self,strict=False):assert strict;return self.visible
    def playlists(self):return self.lists
    def playlist_create(self,name):
        self.created+=1;pl={'guid':'playlist','name':name};self.lists.append(pl);return pl


def prepared(engine):
    tid=engine.store.upsert_track('netease',song())
    r=engine.store.save_rule(rule())
    (engine.root/'song.wav').write_bytes(b'audio-fixture')
    with engine.store.connect() as db:
        db.execute('UPDATE tracks SET path=? WHERE id=?',('song.wav',tid))
        db.execute('INSERT INTO membership VALUES(?,?,0)',(r['id'],tid))
    cfg={'fn_auto':True,'fn_user':'me','fn_music_root':'/nas/music','music_dir':str(engine.root)}
    return tid,cfg


def test_scan_acceptance_is_not_index_completion(engine):
    tid,cfg=prepared(engine);fn=Fn()
    with patch('fnsync._add_only_tracks') as add:
        r=reconcile(engine.store,cfg,fn,1,timeout=0,path_reader=lambda:{})
        assert r['status']=='partial' and r['counts']['missing']==1
        assert not add.called and fn.created==0


def test_scan_delayed_index_then_append_idempotently(engine):
    tid,cfg=prepared(engine);fn=Fn([{'guid':'g'}]);paths=iter([{}, {'g':'/nas/music/song.wav'}])
    with patch('hub_fn_worker.owned_delta',return_value=(1,0)) as add:
        r=reconcile(engine.store,cfg,fn,1,pause=lambda _:None,path_reader=lambda:next(paths))
        assert r['status']=='succeeded'
        assert add.call_args.args[-1]=={tid:'g'}
        reconcile(engine.store,cfg,fn,2,timeout=0,path_reader=lambda:{'g':'/nas/music/song.wav'})
        assert fn.created==1  # stable native GUID on repeated deliveries


def test_scan_failure_no_native_write(engine):
    _,cfg=prepared(engine)
    with patch('fnsync._add_only_tracks') as add:
        with pytest.raises(ValueError,match='扫描'):reconcile(engine.store,cfg,Fn(scan_code=5),1,path_reader=lambda:{})
        assert not add.called


def test_exact_path_and_account_visibility_required(engine):
    _,cfg=prepared(engine);rows=engine.store.rows('SELECT * FROM tracks')
    assert match(rows,[{'guid':'other'}],{'hidden':'/nas/music/song.wav'},'/nas/music')=={}
    assert match(rows,[{'guid':'g'}],{'g':'/other/song.wav'},'/nas/music')=={}
    exact={'guid':'g','title':'晴天','artists':[{'name':'周杰伦'}],'duration':2000}
    assert match(rows,[exact],None,'')
    assert not match(rows,[exact,{**exact,'guid':'duplicate'}],None,'')
    assert not match(rows,[{**exact,'title':'晴天 Live'}],None,'')


def test_child_store_does_not_requeue_live_download(engine):
    jid=engine.store.enqueue('download','x',{'track_id':'x'})
    engine.store.claim()
    Store(engine.store.home,recover_running=False)
    assert engine.store.rows('SELECT status FROM jobs WHERE id=?',(jid,))[0]['status']=='running'


def test_pipeline_preserves_new_revision_busy_and_restart(engine):
    tasks={};counter=[0]
    def submit(kind,args):
        counter[0]+=1
        task={'id':str(counter[0]),'status':'running','args':args,'result':{}}
        tasks[task['id']]=task
        return task
    engine.bridge={'BRIDGE':{'store':NS(get=tasks.get)},'submit_task':submit}
    engine.save_settings({'fn_auto':True,'fn_user':'me'})
    with engine.store.connect() as db:db.execute('UPDATE pipeline SET retry_at=0')
    engine.pump_pipeline();first=engine.store.pipeline();assert first['task_id']=='1'
    engine.store.dirty() # download completes while native worker is running
    tasks['1']['status']='succeeded';engine.pump_pipeline()
    state=engine.store.pipeline();assert state['revision']>state['completed']
    assert state['completed']==first['revision']
    engine.bridge['submit_task']=lambda *a:(_ for _ in ()).throw(RuntimeError('busy'))
    with engine.store.connect() as db:db.execute('UPDATE pipeline SET retry_at=0')
    engine.pump_pipeline();assert engine.store.pipeline()['status']=='waiting'
    restored=Engine(engine.store.home,engine.root,lambda *a:None,engine.bridge)
    assert restored.store.pipeline()['revision']>restored.store.pipeline()['completed']


def test_native_dispatch_uses_shared_playlist_mutex():
    from bridge_store import BridgeStore
    row={'kind':'playlist_sync','args_json':json.dumps({'action':'hub_reconcile'})}
    assert BridgeStore._task_resource(row)=='playlist'


def test_music_root_confined_to_mount(engine,tmp_path):
    with pytest.raises(ValueError,match='挂载'):engine.save_settings({'music_dir':str(tmp_path/'outside')})
    engine.save_settings({'music_dir':str(engine.root/'sub')})
    assert engine.root.name=='sub'
    engine.store.upsert_track('netease',song())
    with pytest.raises(ValueError,match='已有歌曲'):engine.save_settings({'music_dir':str(tmp_path/'music')})


def test_login_to_merged_audio_to_native_playlist_end_to_end(engine,tmp_path):
    """Two platform identities, one shared file, delayed native scan, preserve manual songs."""
    from test_hub import wav_bytes, Response
    opts=AccountOptions(kinds=['liked'],group_name='共同喜欢',quality='standard').model_dump()
    engine.accounts.bind('netease','11',{'cookie':'test'},opts)
    engine.accounts.bind('qq','22',{'musickey':'test'},opts)
    engine.providers.discover=lambda p,r,k:[{'id':p+'liked','name':'喜欢'}]
    engine.providers.playlist=lambda p,r:{'tracks':[song(p+'song')]}
    engine.providers.resolve=lambda *a:{'url':'https://cdn.example/audio','ext':'mp3','quality':'standard'}
    with patch('hub_engine.open_audio',return_value=Response(wav_bytes())):
        while (job:=engine.store.claim()):
            if job['kind']=='sync':engine.sync(job)
            else:engine.download(job)
            engine.store.update_job(job['id'],status='done')
    tracks=engine.store.rows('SELECT * FROM tracks')
    assert len(tracks)==1 and len(engine.store.rows('SELECT * FROM sources'))==2
    assert len(list((engine.root/'Library').glob('*.wav')))==1
    playlist=next((engine.root/'Playlists').glob('*.m3u8')).read_text()
    assert playlist.count('#EXTINF')==1
    class WritableFn(Fn):
        user='me'
        def __init__(self):
            super().__init__([{'guid':'native-song'}]);self.members=['manual-song']
        def playlist_tracks(self,guid):return [{'guid':g} for g in self.members]
        def playlist_add_tracks(self,guid,guids):self.members=guids+self.members;return {'code':0}
        def playlist_remove_tracks(self,guid,guids):self.members=[g for g in self.members if g not in guids];return {'code':0}
    fn=WritableFn();cfg={'fn_auto':True,'fn_user':'me','fn_music_root':'/nas/music','music_dir':str(engine.root)}
    paths={'native-song':'/nas/music/'+tracks[0]['path']}
    with patch('fnsync.BACKUP',str(tmp_path/'snapshots')):
        result=reconcile(engine.store,cfg,fn,1,timeout=0,path_reader=lambda:paths)
        assert result['status']=='succeeded'
        assert fn.members==['native-song','manual-song']
        result=reconcile(engine.store,cfg,fn,2,timeout=0,path_reader=lambda:paths)
        assert result['counts']['added']==0 and fn.created==1


def test_native_creation_intent_recovers_uncertain_reply(engine):
    from hub_fn_worker import target
    fn=Fn()
    first=target(engine.store,fn,'me','group')
    with engine.store.connect() as db:db.execute('UPDATE fn_targets SET guid=NULL')
    second=target(engine.store,fn,'me','group')
    assert first==second and fn.created==1


def test_new_api_endpoints_auth_csrf_and_secret_redaction(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hub_api import install
    class Auth:
        def session_info(self,r):return {'authenticated':r.headers.get('x-test-auth')=='yes'}
        def authorize_write(self,r):return r.headers.get('x-bridge-csrf')=='valid','csrf missing'
    monkeypatch.setenv('MUSIC_DIR',str(tmp_path/'music'))
    app=FastAPI();state=install(app,{'HOME':tmp_path/'data','AUTH':{'auth':Auth()},
        'resolve_platform_playlists':lambda *a:([],[],'',''),'tokens':lambda:{'me':'secret'}})
    with TestClient(app) as c:
        assert c.get('/api/hub/accounts').status_code==401
        headers={'x-test-auth':'yes'}
        assert c.post('/api/hub/accounts/qq/qr',headers=headers,json={}).status_code==403
        headers['x-bridge-csrf']='valid'
        with patch('hub_qq.start_qr',new=AsyncMock(return_value=('qrkey','data:image/png;base64,AA'))):
            r=c.post('/api/hub/accounts/qq/qr',headers=headers,json={})
        assert r.status_code==200
        fid=r.json()['flow_id']
        with patch('hub_qq.poll_qr',new=AsyncMock(return_value=('complete',{'musicid':1,'musickey':'SECRET-VALUE'}))):
            assert c.post('/api/hub/qr/'+fid+'/poll',headers=headers,json={}).json()['state']=='complete'
        response=c.get('/api/hub/accounts',headers=headers)
        assert 'SECRET-VALUE' not in response.text
        assert response.json()[1]['connected']
        assert c.get('/api/hub/pipeline',headers=headers).status_code==200
        assert c.put('/api/hub/settings',headers=headers,json={'fn_music_root':'relative'}).status_code==400
        assert c.put('/api/hub/settings',headers=headers,json={'fn_user':'unknown'}).status_code==400
        assert c.put('/api/hub/settings',headers=headers,json={'fn_auto':True,'fn_user':'me','fn_music_root':'/nas/music'}).status_code==200
        assert c.delete('/api/hub/accounts/qq',headers=headers).status_code==200
        assert not state['engine'].accounts.private('qq')
