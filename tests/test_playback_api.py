"""Run inside the existing runtime/test container with FastAPI and httpx available."""
import io
from unittest.mock import patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hub_api import install

@pytest.fixture
def playback(tmp_path,monkeypatch):
    class Auth:
        def session_info(self,r):return {'authenticated':bool(r.cookies.get('bridge_session'))}
        def authorize_write(self,r):return r.headers.get('x-bridge-csrf')=='test','csrf missing'
    monkeypatch.setenv('MUSIC_DIR',str(tmp_path/'music'))
    app=FastAPI();state=install(app,{'HOME':tmp_path/'data','AUTH':{'auth':Auth()},'resolve_platform_playlists':lambda *a:None,'tokens':lambda:{}})
    with patch('hub_engine.Engine.start'),TestClient(app) as client:
        client.cookies.set('bridge_session','owner')
        client.headers['x-bridge-csrf']='test'
        yield client,state['engine']

TRACK={'platform':'netease','source_id':'1','title':'测试','artists':['作者']}

def test_preview_ticket_session_bound_and_url_private(playback):
    client,engine=playback
    with patch.object(engine.providers,'resolve',return_value={'url':'https://cdn.example/audio?secret=private','preview':True}),patch('hub_playback.public_audio_url'):
        response=client.post('/api/hub/preview',json=TRACK)
    assert response.status_code==200
    data=response.json();assert data['preview'] and 'secret' not in response.text
    assert not engine.store.rows('SELECT * FROM jobs')
    client.cookies.set('bridge_session','other')
    assert client.get(data['url']).status_code==404
    client.cookies.clear();assert client.get(data['url']).status_code==401

def test_preview_csrf_and_stream_range(playback):
    client,engine=playback
    assert client.post('/api/hub/preview',json=TRACK,headers={'x-bridge-csrf':'wrong'}).status_code==403
    with patch.object(engine.providers,'resolve',return_value={'url':'https://cdn.example/audio'}),patch('hub_playback.public_audio_url'):
        url=client.post('/api/hub/preview',json=TRACK).json()['url']
    response=io.BytesIO(b'audio');response.headers={'Content-Type':'audio/mpeg','Content-Length':'5','Content-Range':'bytes 0-4/20'};response.status=206
    with patch('hub_playback.open_audio',return_value=response) as open_audio:
        r=client.get(url,headers={'Range':'bytes=0-4'})
        assert r.status_code==206 and r.content==b'audio' and r.headers['content-range']=='bytes 0-4/20'
        assert open_audio.call_args.args[1]=='bytes=0-4'
    assert response.closed
    assert client.get(url,headers={'Range':'bytes=0-4,8-9'}).status_code==416

def test_local_audio_path_confined(playback,tmp_path):
    client,engine=playback
    tid=engine.store.upsert_track('netease',TRACK)
    outside=tmp_path/'private.txt';outside.write_text('private')
    with engine.store.connect() as db:db.execute('UPDATE tracks SET path=? WHERE id=?',(str(outside),tid))
    assert client.get('/api/hub/local-audio/'+tid).status_code==404
