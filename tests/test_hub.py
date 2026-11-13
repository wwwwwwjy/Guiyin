import io
import json
import os
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hub_store import Store, safe_name
from hub_engine import Engine
from hub_api import Rule, install
from hub_providers import Providers, public_audio_url


def track(sid='1', title='晴天', duration=269000):
    return {'source_id':sid,'title':title,'artists':['周杰伦'],'album':'叶惠美','duration_ms':duration}


def rule(**kw):
    return Rule(name='测试歌单',platform='netease',ref='100',enabled=False,**kw).model_dump()


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path/'data',tmp_path/'music',lambda *a:([],[],'',''))


def test_cross_platform_dedup_preserves_versions(engine):
    s=engine.store
    first=s.upsert_track('netease',track())
    assert s.upsert_track('qq',track('2',duration=270000))==first
    assert s.upsert_track('kugou',track('3',title='晴天 (Live)'))!=first
    assert s.upsert_track('qq',track('4',duration=300000))!=first
    assert s.upsert_track('qq',track('5',duration=0))!=first


def test_concurrent_enqueue_is_idempotent(engine):
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids=list(pool.map(lambda _:engine.store.enqueue('download','same',{}),range(12)))
    assert len(set(ids))==1
    assert engine.store.claim()['status']=='queued'
    assert engine.store.claim() is None


def test_restart_recovers_active_job(engine):
    engine.store.enqueue('sync','s',{})
    assert engine.store.claim()
    recovered=Store(engine.store.home)
    assert recovered.claim()['attempts']==2


def test_failed_partial_sync_preserves_membership(engine):
    r=engine.store.save_rule(rule())
    engine.providers.discover=lambda *a:[{'id':'a'},{'id':'b'}]
    engine.providers.playlist=lambda *a:{'tracks':[track()]}
    jid=engine.store.enqueue('sync','sync:'+r['id'],{'rule_id':r['id']})
    job=engine.store.claim()
    engine.sync(job)
    before=engine.store.rows('SELECT * FROM membership')
    def failed(p,ref):
        if ref=='b':raise ValueError('incomplete')
        return {'tracks':[track('new',title='另一首歌')]}
    engine.providers.playlist=failed
    with pytest.raises(ValueError):engine.sync(job)
    assert engine.store.rows('SELECT * FROM membership')==before


def test_secret_redaction_and_clear(engine):
    engine.save_settings({'netease_cookie':'MUSIC_U=test','gateways':{'qq':{'url':'http://resolver/resolve','token':'secret'}}})
    encoded=json.dumps(engine.public_settings())
    assert 'MUSIC_U=test' not in encoded and '"secret"' not in encoded
    engine.save_settings({'netease_cookie':'','gateways':{'qq':{'url':'http://resolver/resolve','token':''}}})
    assert engine.settings()['gateways']['qq']['token']=='secret'
    assert engine.config_path.stat().st_mode & 0o777 == 0o600
    engine.save_settings({'clear_cookie':True,'gateways':{'qq':{'clear_token':True}}})
    assert not engine.settings()['netease_cookie']
    assert not engine.settings()['gateways']['qq']['token']


def test_same_group_single_m3u_reference(engine):
    s=engine.store
    tid=s.upsert_track('netease',track())
    r1=s.save_rule(rule(group_name='通勤'))
    r2=s.save_rule(rule(group_name='通勤'))
    f=engine.root/'Library'/'same.flac';f.parent.mkdir();f.write_bytes(b'audio')
    with s.connect() as db:
        db.execute('UPDATE tracks SET path=? WHERE id=?',('Library/same.flac',tid))
        db.executemany('INSERT INTO membership VALUES(?,?,?)',[(r1['id'],tid,0),(r2['id'],tid,0)])
    engine.export()
    files=list((engine.root/'Playlists').glob('*.m3u8'))
    assert len(files)==1
    assert files[0].read_text().count('../Library/same.flac')==1
    assert '../' not in safe_name('../../escape')


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers={'Content-Length':str(len(data) if length is None else length)}


def wav_bytes(seconds=2):
    out=io.BytesIO()
    with wave.open(out,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000)
        f.writeframes(b'\x00\x00'*8000*seconds)
    return out.getvalue()


def download_job(engine,duration=2000):
    tid=engine.store.upsert_track('netease',track(duration=duration))
    engine.enqueue_download(tid)
    return tid,engine.store.claim()


def test_real_audio_download_and_atomic_publish(engine):
    tid,job=download_job(engine)
    engine.providers.resolve=lambda *a:{'url':'https://cdn/audio','ext':'mp3','quality':'standard','lyric':'[00:00]test'}
    with patch('hub_engine.open_audio',return_value=Response(wav_bytes())):
        engine.download(job)
    row=engine.store.rows('SELECT * FROM tracks')[0]
    assert row['path'].endswith('.wav')  # actual container wins over claimed mp3
    assert (engine.root/row['path']).is_file()
    assert (engine.root/row['path']).with_suffix('.lrc').is_file()
    assert engine.enqueue_download(tid)['skipped']
    assert not list(engine.root.rglob('*.part'))


@pytest.mark.parametrize('data,length,duration',[(b'<html>captcha</html>',None,2000),(wav_bytes(),999999,2000),(wav_bytes(),None,269000)])
def test_invalid_truncated_and_preview_audio_rejected(engine,data,length,duration):
    _,job=download_job(engine,duration)
    engine.providers.resolve=lambda *a:{'url':'https://cdn/audio','ext':'mp3'}
    with patch('hub_engine.open_audio',return_value=Response(data,length)):
        with pytest.raises(Exception):engine.download(job)
    assert engine.store.rows('SELECT path FROM tracks')[0]['path'] is None
    assert not list(engine.root.rglob('*.part'))
    assert not list(engine.root.rglob('*.mp3'))


def test_cancel_during_download(engine):
    _,job=download_job(engine)
    engine.store.update_job(job['id'],status='cancelled')
    with pytest.raises(Exception):engine.download(job)
    assert not list(engine.root.rglob('*.part'))


def test_private_audio_url_rejected():
    with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',80))]):
        with pytest.raises(ValueError):public_audio_url('http://localhost/file')
    with pytest.raises(ValueError):public_audio_url('file:///etc/passwd')


def test_qq_favorites_require_adapter():
    providers=Providers(lambda:{},lambda *a:([],[],'',''))
    with pytest.raises(ValueError,match='QQ 收藏'):providers.discover('qq','123','favorited')


def test_upstream_truncation_is_failure():
    providers=Providers(lambda:{},None)
    with patch('fnsync.RAW_FETCHERS',{'kuwo':lambda _: {'tracks':[track()], 'declared_count':200}}):
        with pytest.raises(ValueError,match='未取全'):providers.playlist('kuwo','1')


def test_api_validation_auth_and_csrf(tmp_path,monkeypatch):
    class Auth:
        def session_info(self,r):return {'authenticated':r.headers.get('x-test-auth')=='yes'}
        def authorize_write(self,r):return (r.headers.get('x-bridge-csrf')=='valid','missing csrf')
    monkeypatch.setenv('MUSIC_DIR',str(tmp_path/'music'))
    app=FastAPI()
    install(app,{'HOME':tmp_path/'data','AUTH':{'auth':Auth()},'resolve_platform_playlists':lambda *a:([],[],'','')})
    with TestClient(app) as client:
        assert client.get('/api/hub/settings').status_code==401
        assert client.get('/').status_code==200
        headers={'x-test-auth':'yes'}
        assert client.post('/api/hub/rules',headers=headers,json=rule()).status_code==403
        headers['x-bridge-csrf']='valid'
        bad=rule();bad['interval_hours']=0
        assert client.post('/api/hub/rules',headers=headers,json=bad).status_code==422
        assert client.post('/api/hub/rules',headers=headers,json=rule()).status_code==200
        assert len(client.get('/api/hub/rules',headers=headers).json())==1
        assert client.put('/api/hub/settings',headers=headers,json={'gateways':{'qq':{'url':'http://host/resolve?token=secret'}}}).status_code==400
        assert client.get('/api/hub/stats',headers=headers).json()['tracks']==0


def test_renamed_group_cleans_only_owned_playlist(engine):
    r=engine.store.save_rule(rule(group_name='旧名称'))
    engine.export()
    custom=engine.root/'Playlists'/'user-owned.m3u8'
    custom.write_text('#EXTM3U')
    old=next(p for p in custom.parent.glob('*.m3u8') if p!=custom)
    engine.store.save_rule({**r,'group_name':'新名称'})
    engine.export()
    assert not old.exists()
    assert custom.exists()
    assert len(list(custom.parent.glob('*.m3u8')))==2


def test_custom_search_adapter_preserves_source():
    providers=Providers(lambda:{'gateways':{'qq':{'search_url':'http://resolver/search','token':'x'}}},None)
    with patch('hub_providers.json_request',return_value={'tracks':[track('QQMID')]}):
        result=providers.search('qq','晴天')
    assert result[0]['platform']=='qq'
    assert result[0]['source_id']=='QQMID'


def test_real_app_authentication_and_static_routes(tmp_path,monkeypatch):
    monkeypatch.setenv('FNMUSIC_HOME',str(tmp_path/'data'))
    monkeypatch.setenv('MUSIC_DIR',str(tmp_path/'music'))
    monkeypatch.setenv('BRIDGE_ADMIN_PASSWORD','integration-password-123')
    monkeypatch.setenv('BRIDGE_TASKS_ENABLE','0')
    monkeypatch.setenv('COMMENTHUB_ENABLE','0')
    monkeypatch.setenv('MUSIC_PROXY_ENABLE','0')
    import webapp
    with TestClient(webapp.app) as client:
        assert client.get('/').status_code==200
        assert '归音' in client.get('/').text
        assert client.get('/bridge').status_code==200
        assert client.get('/static/studio.js').status_code==200
        assert client.get('/api/hub/settings').status_code==401
        hint=client.get('/api/admin/login-hint').json()
        assert 'password' not in hint
        assert client.post('/api/admin/password/reset').status_code==403
        login=client.post('/api/login',json={'username':'admin','password':'integration-password-123'})
        assert login.status_code==200
        csrf=login.json()['csrf']
        assert client.post('/api/hub/rules',json=rule()).status_code==401
        response=client.post('/api/hub/rules',json=rule(),headers={'X-Bridge-CSRF':csrf})
        assert response.status_code==200, response.text
        assert client.get('/api/hub/stats').json()['rules']==1
        assert client.get('/api/hub/bridge-targets').json()=={'users':[]}
        assert client.get('/api/version').json()['update_available'] is False
