"""Offline regression checks. No NAS writes, network access or dependency installs.

When certifi is absent, only its unused TLS-path accessor is stubbed; every
platform transport is mocked. Store/engine/native diff/snapshots are real code.
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS, ModuleType
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
TEST_HOME=tempfile.TemporaryDirectory(prefix='guiyin-regression-')
os.environ['FNMUSIC_HOME']=TEST_HOME.name
if 'certifi' not in sys.modules and importlib.util.find_spec('certifi') is None:
    sys.modules['certifi']=NS(where=lambda:None)
from hub_store import Store
from hub_engine import Engine, Cancelled
from hub_fn_worker import owned_delta, reconcile
import hub_kugou
import hub_qq
import fnsync


def rule():
    return dict(name='通勤',platform='netease',kind='created',ref='account:1',group_name='共同喜欢',interval_hours=1,enabled=True,auto_download=False,quality='standard')
def song(sid='1'):
    return dict(source_id=sid,title='歌'+sid,artists=['歌手'],duration_ms=200000)

class Fn:
    user='me'
    def __init__(self,members=None):self.members=list(members or []);self.writes=[]
    def playlist_edit(self,guid,name):return True
    def get(self,*a,**kw):return {'code':0,'data':{}}
    def playlist_tracks(self,guid):return [{'guid':g} for g in self.members]
    def playlist_add_tracks(self,guid,guids):self.writes.append(('add',guids));self.members+=list(guids);return {'code':0}
    def playlist_remove_tracks(self,guid,guids):self.writes.append(('remove',guids));self.members=[g for g in self.members if g not in guids];return {'code':0}

class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.engine=Engine(Path(self.temp.name)/'data',Path(self.temp.name)/'music',lambda *a:None)
        self.store=self.engine.store
        with self.store.connect() as db:db.execute("INSERT INTO hub_group_dirs VALUES('共同喜欢','共同喜欢')")
        self.rule=self.store.save_rule(rule());self.tid=self.store.upsert_track('netease',song())
        with self.store.connect() as db:db.execute('INSERT INTO membership VALUES(?,?,0)',(self.rule['id'],self.tid))
        self.pl={'guid':'pl','name':'融合'}
        self.snapshot=patch('fnsync.BACKUP',str(Path(self.temp.name)/'snapshots'));self.snapshot.start();self.addCleanup(self.snapshot.stop)
    def delta(self,fn,mapping=None):return owned_delta(self.store,fn,self.pl,'me','共同喜欢',{self.tid:'song'} if mapping is None else mapping)
    def clear(self):
        with self.store.connect() as db:db.execute('DELETE FROM membership')
    def test_remove_owned_preserve_manual_and_audio(self):
        fn=Fn(['manual']);self.assertEqual(self.delta(fn),(1,0));self.clear()
        audio=self.engine.root/'keep.mp3';audio.write_bytes(b'audio')
        self.assertEqual(self.delta(fn,{}),(0,1));self.assertEqual(fn.members,['manual']);self.assertTrue(audio.exists())
    def test_existing_relations_are_not_claimed_on_upgrade(self):
        fn=Fn(['song','manual']);self.assertEqual(self.delta(fn),(0,0));self.clear()
        self.assertEqual(self.delta(fn,{}),(0,0));self.assertEqual(fn.members,['song','manual'])
    def test_scan_delay_keeps_logically_desired_song(self):
        fn=Fn();self.delta(fn);self.assertEqual(self.delta(fn,{}),(0,0));self.assertEqual(fn.members,['song'])
    def test_paused_rule_preserves_snapshot(self):
        fn=Fn();self.delta(fn)
        with self.store.connect() as db:db.execute('UPDATE rules SET enabled=0')
        self.assertEqual(self.delta(fn,{}),(0,0));self.assertEqual(fn.members,['song'])
    def test_other_source_reference_prevents_delete(self):
        fn=Fn();self.delta(fn);second=self.store.save_rule({**rule(),'name':'QQ','platform':'qq'})
        with self.store.connect() as db:
            db.execute('INSERT INTO membership VALUES(?,?,0)',(second['id'],self.tid));db.execute('DELETE FROM membership WHERE rule_id=?',(self.rule['id'],))
        self.assertEqual(self.delta(fn,{}),(0,0));self.assertEqual(fn.members,['song'])
    def test_last_group_is_still_reconciled(self):
        fn=Fn(['manual']);self.delta(fn)
        with self.store.connect() as db:
            db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me','共同喜欢','pl','融合'));db.execute('DELETE FROM rules');db.execute('DELETE FROM membership')
        cfg={'fn_auto':True,'fn_user':'me','music_dir':str(self.engine.root)}
        r=reconcile(self.store,cfg,fn,1);self.assertEqual(r['counts']['removed'],1);self.assertEqual(fn.members,['manual'])
    def test_failed_delete_keeps_ownership_for_idempotent_retry(self):
        fn=Fn();self.delta(fn);self.clear();original=fn.playlist_remove_tracks
        fn.playlist_remove_tracks=lambda *a: {'code':1}
        with self.assertRaises(RuntimeError):self.delta(fn,{})
        self.assertEqual(len(self.store.rows('SELECT * FROM fn_owned')),1)
        fn.playlist_remove_tracks=original;self.assertEqual(self.delta(fn,{}),(0,1))
        self.assertEqual(self.delta(fn,{}),(0,0))
    def test_uncertain_add_is_not_claimed(self):
        fn=Fn();original=fn.playlist_add_tracks
        def timeout(*args):original(*args);raise TimeoutError()
        fn.playlist_add_tracks=timeout
        with self.assertRaises(TimeoutError):self.delta(fn)
        self.assertFalse(self.store.rows('SELECT * FROM fn_owned'));self.clear()
        self.assertEqual(self.delta(fn,{}),(0,0));self.assertEqual(fn.members,['song'])
    def test_snapshot_restore_cannot_full_replace_owned_delta(self):
        fn=Fn();self.delta(fn)
        paths=list((Path(self.temp.name)/'snapshots').glob('*.json'));self.assertEqual(json.loads(paths[0].read_text())['mode'],'owned_delta')
    def test_complete_source_snapshot_and_tombstones(self):
        jid=self.store.enqueue('sync','test',{'rule_id':self.rule['id']});job=self.store.claim()
        self.engine.providers.discover=lambda *a:[{'id':'p','name':'手机歌单'}]
        self.engine.providers.playlist=lambda *a:{'tracks':[song()]}
        self.engine.sync(job);self.assertEqual(len(self.store.rows('SELECT * FROM source_members')),1)
        self.engine.providers.discover=lambda *a:[];self.engine.sync(job)
        self.assertFalse(self.store.rows('SELECT * FROM membership'));self.assertEqual(self.store.rows('SELECT state FROM source_playlists')[0]['state'],'removed')
        self.assertTrue(self.store.rows('SELECT * FROM tracks'))
    def test_partial_upstream_preserves_all_previous_members(self):
        self.engine.providers.discover=lambda *a:[{'id':'p'},{'id':'bad'}]
        def fetch(p,r):
            if r=='bad':raise ValueError('incomplete')
            return {'tracks':[song('2')]}
        self.engine.providers.playlist=fetch;self.store.enqueue('sync','test',{'rule_id':self.rule['id']});job=self.store.claim()
        with self.assertRaises(ValueError):self.engine.sync(job)
        self.assertEqual(self.store.rows('SELECT track_id FROM membership')[0]['track_id'],self.tid)
    def test_configuration_changed_during_fetch_does_not_commit(self):
        self.engine.providers.discover=lambda *a:[{'id':'p'}]
        def fetch(*args):
            with self.store.connect() as db:db.execute('UPDATE rules SET enabled=0')
            return {'tracks':[]}
        self.engine.providers.playlist=fetch;self.store.enqueue('sync','test',{'rule_id':self.rule['id']});job=self.store.claim()
        with self.assertRaises(Cancelled):self.engine.sync(job)
        self.assertTrue(self.store.rows('SELECT * FROM membership'))
    def test_source_commit_serialized_with_native_write(self):
        fn=Fn();attempted=threading.Event();completed=threading.Event();original=fn.playlist_add_tracks
        def update():
            attempted.set()
            with self.store.connect() as db:db.execute('DELETE FROM membership')
            completed.set()
        threads=[]
        def add(*args):
            t=threading.Thread(target=update);threads.append(t);t.start();attempted.wait(1)
            self.assertFalse(completed.wait(.05));return original(*args)
        fn.playlist_add_tracks=add;self.delta(fn)
        for t in threads:t.join(2)
        self.assertTrue(completed.is_set());self.assertEqual(self.delta(fn,{}),(0,1))

    def test_native_engine_reads_same_completed_source_snapshot(self):
        from hub_snapshot import playlist
        self.engine.providers.discover=lambda *a:[{'id':'private','name':'私人歌单'}]
        self.engine.providers.playlist=lambda *a:{'tracks':[song()]}
        self.store.enqueue('sync','test',{'rule_id':self.rule['id']});job=self.store.claim();self.engine.sync(job)
        with patch.dict(os.environ,{'FNMUSIC_HOME':str(self.store.home)}):
            data=playlist('netease','private');self.assertEqual(data['name'],'私人歌单');self.assertEqual(len(data['tracks']),1)
            with patch.dict(fnsync.RAW_FETCHERS,{'netease':lambda ref:self.fail('must share completed snapshot')}):
                self.assertEqual(fnsync.FETCHERS['netease']('private')['tracks'],data['tracks'])
            self.engine.providers.discover=lambda *a:[];self.engine.sync(job)
            self.assertEqual(playlist('netease','private')['tracks'],[])

    def test_mobile_cancel_prevents_late_account_binding(self):
        callbacks=[]
        class Watcher:
            def __init__(self,callback):callbacks.append(callback)
            def image(self):return 'data:image/png;base64,AA'
            def cancel(self):pass
        opts=dict(kinds=['liked','created','favorited'],group_name='共同喜欢',interval_hours=1,auto_download=True,quality='standard')
        with patch('hub_mobile_qr.MobileQR',Watcher):
            flow=self.engine.accounts.start('qq','owner',opts,'mobile')
            with self.assertRaises(ValueError):self.engine.accounts.poll(flow['flow_id'],'other')
            self.engine.accounts.cancel(flow['flow_id'],'owner')
            callbacks[0]('complete',{'musicid':42,'musickey':'private'})
            self.assertFalse(self.engine.accounts.private('qq'))
            flow=self.engine.accounts.start('qq','owner',opts,'mobile')
            callbacks[1]('complete',{'musicid':42,'musickey':'private'})
            callbacks[1]('complete',{'musicid':42,'musickey':'private'})
            self.assertEqual(len(self.store.rows("SELECT * FROM rules WHERE platform='qq'")),3)
            self.assertEqual(len(self.store.rows("SELECT * FROM jobs WHERE kind='sync'")),0)
            self.assertFalse(self.store.rows("SELECT id FROM rules WHERE enabled=1"))
            self.assertNotIn('private',json.dumps(self.engine.accounts.public()))

class NativeRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=Engine(Path(self.tmp.name)/'data',Path(self.tmp.name)/'music',lambda *a:None)
        self.store=self.e.store
        with self.store.connect() as db:db.execute("INSERT INTO hub_group_dirs VALUES('喜欢','喜欢')")
        self.r=self.store.save_rule({**rule(),'kind':'playlist','group_name':'喜欢'})
        self.cfg={'fn_auto':True,'fn_user':'me','fn_music_root':'/nas','music_dir':str(self.e.root)}
    def add(self,sid,path,exists=True,member=True):
        tid=self.store.upsert_track('netease',song(sid))
        with self.store.connect() as db:
            db.execute('UPDATE tracks SET path=? WHERE id=?',(path,tid))
            if member:db.execute('INSERT INTO membership VALUES(?,?,0)',(self.r['id'],tid))
        if exists:
            file=self.e.root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'audio')
        return tid
    def test_configured_empty_group_creates_native_playlist_before_download(self):
        with patch('hub_fn_worker.target',return_value={'guid':'new','name':'归音 · 喜欢'}) as create,patch('hub_fn_worker.owned_delta',return_value=(0,0)),patch('hub_fn_worker.update_cover',return_value=0):
            result=reconcile(self.store,{**self.cfg,'fn_auto':False},Fn(),1,timeout=0,path_reader=lambda:{})
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(create.call_args.args[-1],'喜欢')
        self.assertEqual(result['counts']['groups'],1)

    def test_pipeline_keeps_new_download_event_while_previous_task_finishes(self):
        tasks=[]
        state={'status':'succeeded','args':{'revision':2},'result':{'summary':'done'}}
        self.e.bridge={'BRIDGE':{'store':NS(get=lambda _:state)},'submit_task':lambda kind,args:tasks.append(args) or {'id':'next'}}
        self.e.save_settings({'fn_user':'me'})
        with self.store.connect() as db:db.execute("UPDATE pipeline SET revision=3,completed=1,task_id='old',retry_at=0")
        self.e.pump_pipeline()
        self.assertEqual(self.store.pipeline()['completed'],2)
        with self.store.connect() as db:db.execute('UPDATE pipeline SET retry_at=0')
        self.e.pump_pipeline()
        self.assertEqual(tasks,[{'action':'hub_reconcile','revision':3}])
        self.e.bridge['BRIDGE']['store']=NS(get=lambda _:{'status':'running'})
        self.e.pump_pipeline()
        self.assertEqual(len(tasks),1)

    def test_missing_old_file_does_not_block_present_and_unselected_ignored(self):
        good=self.add('good','喜欢/good.mp3');self.add('missing','喜欢/missing.mp3',False)
        self.add('unselected','Library/old.mp3',False,False)
        class Native:
            def get(self,*a,**kw):return {'code':0,'data':{'list':[{'guid':'library'}]}}
            def export_library(self,strict=False):return [{'guid':'g'}]
            def scan_library(self,g):return {'code':0}
        with patch('hub_fn_worker.target',return_value={'guid':'pl','name':'归音 · 喜欢'}),patch('hub_fn_worker.owned_delta',return_value=(1,0)) as delta,patch('hub_fn_worker.update_cover',return_value=0):
            result=reconcile(self.store,self.cfg,Native(),1,timeout=0,path_reader=lambda:{'g':'/nas/喜欢/good.mp3'})
        self.assertEqual(result['counts']['indexed'],1);self.assertEqual(result['counts']['missing_files'],1)
        self.assertEqual(delta.call_args.args[-1],{good:'g'})
    def test_classified_copy_recovers_missing_primary(self):
        tid=self.add('good','Library/good.mp3',False)
        file=self.e.root/'喜欢/good.mp3';file.parent.mkdir();file.write_bytes(b'audio')
        class Native:
            def get(self,*a,**kw):return {'code':0,'data':{'list':[{'guid':'library'}]}}
            def export_library(self,strict=False):return [{'guid':'g'}]
        with patch('hub_fn_worker.target',return_value={'guid':'pl'}),patch('hub_fn_worker.owned_delta',return_value=(1,0)),patch('hub_fn_worker.update_cover',return_value=0):
            r=reconcile(self.store,self.cfg,Native(),1,timeout=0,path_reader=lambda:{'g':'/nas/喜欢/good.mp3'})
        self.assertEqual(r['status'],'succeeded');self.assertEqual(r['counts']['indexed'],1)
    def test_rename_preserves_guid_and_does_not_take_over_same_name(self):
        from hub_fn_worker import target
        with self.store.connect() as db:db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me','喜欢','owned','归音 · 喜欢 [12345678]'))
        fn=NS(playlist_edit=lambda guid,name:self.assertEqual((guid,name),('owned','归音 · 喜欢')))
        self.assertEqual(target(self.store,fn,'me','喜欢'),{'guid':'owned','name':'归音 · 喜欢'})
        fn.playlist_edit=lambda *a:self.fail('should not rename twice')
        target(self.store,fn,'me','喜欢')
    def test_cover_uses_source_order_not_download_time_and_is_idempotent(self):
        from hub_fn_worker import update_cover
        a=self.add('a','喜欢/a.mp3');b=self.add('b','喜欢/b.mp3');c=self.add('c','喜欢/c.mp3')
        os.utime(self.e.root/'喜欢/a.mp3',(1,1));os.utime(self.e.root/'喜欢/b.mp3',(2,2))
        (self.e.root/'喜欢/a.jpg').write_bytes(b'\xff\xd8\xffa\xff\xd9')
        (self.e.root/'喜欢/b.jpg').write_bytes(b'\xff\xd8\xffb\xff\xd9')
        with self.store.connect() as db:
            db.execute('UPDATE membership SET position=1 WHERE track_id=?',(b,))
            db.execute('UPDATE membership SET position=2 WHERE track_id=?',(c,))
        class Native:
            cover='';writes=0;uploads=0
            def playlist_tracks(self,g):return [{'guid':'a','coverId':'art-a'},{'guid':'b','coverId':'art-b'},{'guid':'c'}]
            def get(self,*a,**kw):return {'code':0,'data':{'coverId':self.cover}}
            def post(self,path,body):self.cover=body['coverId'];self.writes+=1;return {'code':0}
            def upload_playlist_cover(self,data,mime):
                self.uploads+=1
                assert data==b'\xff\xd8\xffa\xff\xd9'
                return 'playlist-art-a'
        fn=Native();rows=self.store.rows('SELECT * FROM tracks');mapping={a:'a',b:'b',c:'c'}
        self.assertEqual(update_cover(self.store,fn,{'guid':'owned'},'喜欢',rows,mapping,self.e.root),1)
        self.assertEqual(fn.cover,'playlist-art-a')
        self.assertEqual(update_cover(self.store,fn,{'guid':'owned'},'喜欢',rows,mapping,self.e.root),0)
        self.assertEqual(fn.writes,1)
        self.assertEqual(fn.uploads,1)

    def test_playlist_cover_upload_uses_native_multipart(self):
        fn=fnsync.FnMusic(token='test');image=b'\xff\xd8\xffimage\xff\xd9'
        with patch.object(fn,'_req',return_value={'code':0,'data':{'coverId':'playlist-new'}}) as send:
            self.assertEqual(fn.upload_playlist_cover(image,'image/jpeg'),'playlist-new')
        self.assertEqual(send.call_args.args,('POST','/static/cover/playlist'))
        body=send.call_args.kwargs['body'];self.assertIn(b'name="file"',body);self.assertIn(image,body)
        self.assertTrue(send.call_args.kwargs['content_type'].startswith('multipart/form-data; boundary='))
    def test_native_playlist_sync_runs_without_downloads_or_legacy_toggle(self):
        tasks=[]
        self.e.bridge={'BRIDGE':{'store':NS(get=lambda _:None)},'submit_task':lambda kind,args:tasks.append(args) or {'id':'native'}}
        self.e.save_settings({'fn_auto':False,'fn_user':'me','sync_enabled':False})
        with self.store.connect() as db:db.execute('UPDATE pipeline SET retry_at=0')
        self.e.pump_pipeline()
        self.assertEqual(len(tasks),1);self.assertFalse(self.e.settings()['sync_enabled'])

    def test_deleted_retired_target_does_not_block_other_groups(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me','旧歌单','deleted','old'))
            db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me','喜欢','live','归音 · 喜欢'))
            db.execute('INSERT INTO fn_owned VALUES(?,?,?)',('me','deleted','old-track'))
        fn=Fn();fn.get=lambda path,**kw:{'code':100005 if kw['guid']=='deleted' else 0,'data':{}}
        with patch('hub_fn_worker.owned_delta',return_value=(1,0)) as delta,patch('hub_fn_worker.update_cover',return_value=0):
            r=reconcile(self.store,self.cfg,fn,1)
        self.assertEqual(r['status'],'succeeded');self.assertEqual(delta.call_count,1)
        self.assertEqual(delta.call_args.args[2]['guid'],'live')
        self.assertFalse(self.store.rows("SELECT * FROM fn_targets WHERE guid='deleted'"))
        self.assertFalse(self.store.rows('SELECT * FROM fn_owned'))

    def test_transient_target_error_keeps_records_and_other_groups_continue(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me','失效','old','old'))
            db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me','喜欢','live','归音 · 喜欢'))
        fn=Fn();fn.get=lambda path,**kw:{'code':500 if kw['guid']=='old' else 0,'data':{}}
        with patch('hub_fn_worker.owned_delta',return_value=(1,0)) as delta,patch('hub_fn_worker.update_cover',return_value=0):
            r=reconcile(self.store,self.cfg,fn,1)
        self.assertEqual(r['status'],'partial');self.assertEqual(delta.call_count,1)
        self.assertTrue(self.store.rows("SELECT * FROM fn_targets WHERE guid='old'"))

class NativeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=Engine(Path(self.tmp.name)/'data',Path(self.tmp.name)/'music',lambda *a:None)
        self.store=self.e.store;self.paths={}
        self.tid=self.store.upsert_track('netease',song('shared'))
        for group,guid in [('喜欢','likes'),('雨天','rain')]:
            r=self.store.save_rule({**rule(),'kind':'playlist','group_name':group})
            file=self.e.root/group/'shared.mp3';file.parent.mkdir();file.write_bytes(b'audio')
            self.paths[guid]='/nas/'+group+'/shared.mp3'
            with self.store.connect() as db:
                db.execute('INSERT INTO hub_group_dirs VALUES(?,?)',(group,group))
                db.execute('INSERT INTO membership VALUES(?,?,0)',(r['id'],self.tid))
                db.execute('INSERT INTO hub_group_files VALUES(?,?,?)',(group,self.tid,group+'/shared.mp3'))
                db.execute('INSERT INTO fn_targets VALUES(?,?,?,?)',('me',group,guid+'-pl','归音 · '+group))
        with self.store.connect() as db:db.execute("UPDATE tracks SET path='喜欢/shared.mp3' WHERE id=?",(self.tid,))
        case=self
        class Native(Fn):
            def __init__(self):super().__init__();self.lists={'likes-pl':['likes'],'rain-pl':['likes','rain','manual-live']};self.scans=[]
            def get(self,path,**kw):return {'code':0,'data':{'list':[{'guid':'lib'}]}}
            def export_library(self,strict=False):return [{'guid':g} for g in reversed(list(case.paths))]
            def playlist_tracks(self,guid):return [{'guid':g} for g in self.lists[guid]]
            def playlist_add_tracks(self,guid,guids):self.writes.append(('add',guid,list(guids)));self.lists[guid]+=list(guids);return {'code':0}
            def playlist_remove_tracks(self,guid,guids):self.writes.append(('remove',guid,list(guids)));self.lists[guid]=[g for g in self.lists[guid] if g not in guids];return {'code':0}
            def scan_library(self,guid):self.scans.append(guid);return {'code':0}
        self.fn=Native();self.cfg={'fn_user':'me','fn_music_root':'/nas','music_dir':str(self.e.root)}
        p=patch('fnsync.BACKUP',str(Path(self.tmp.name)/'snapshots'));p.start();self.addCleanup(p.stop)
        p=patch('hub_fn_worker.update_cover',return_value=0);p.start();self.addCleanup(p.stop)
    def run_sync(self):return reconcile(self.store,self.cfg,self.fn,1,timeout=0,path_reader=lambda:dict(self.paths))
    def own(self,playlist,guid):
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO fn_owned VALUES(?,?,?)',('me',playlist,guid))
            db.execute('INSERT OR IGNORE INTO fn_bindings VALUES(?,?,?)',('me',self.tid,guid))
    def test_cross_folder_duplicates_removed_without_touching_manual_or_files(self):
        result=self.run_sync()
        self.assertEqual(self.fn.lists,{'likes-pl':['likes'],'rain-pl':['rain','manual-live']})
        self.assertEqual(result['counts']['removed'],1);self.assertEqual(result['status'],'succeeded')
        self.assertEqual(len(list(self.e.root.rglob('*.mp3'))),2)
        writes=list(self.fn.writes)
        self.run_sync();self.assertEqual(self.fn.writes,writes)
        # A new worker/store instance preserves exactly the same result.
        self.store=Store(self.store.home,recover_running=False)
        self.run_sync();self.assertEqual(self.fn.writes,writes)
        backup=json.loads(next((Path(self.tmp.name)/'snapshots').glob('*.json')).read_text())
        self.assertEqual(backup['trackGUIDs'],['likes','rain','manual-live'])
        self.assertEqual(backup['removed'],['likes'])
    def test_each_group_adds_only_its_own_file_and_never_changes_the_other(self):
        self.fn.lists={'likes-pl':[],'rain-pl':[]};self.run_sync()
        self.assertEqual(self.fn.lists,{'likes-pl':['likes'],'rain-pl':['rain']})
        self.run_sync();self.assertEqual(len(self.fn.writes),2)
    def test_index_delay_retains_old_then_replaces_when_destination_ready(self):
        self.fn.lists['rain-pl']=['likes','manual-live'];self.own('rain-pl','likes')
        path=self.paths.pop('rain');result=self.run_sync()
        self.assertEqual(self.fn.lists['rain-pl'],['likes','manual-live'])
        self.assertEqual(result['counts']['group_missing'],1);self.assertEqual(result['status'],'partial')
        self.assertTrue(self.fn.scans)
        self.paths['rain']=path;self.run_sync()
        self.assertEqual(set(self.fn.lists['rain-pl']),{'rain','manual-live'})
        changes=[x[0] for x in self.fn.writes if x[1]=='rain-pl'];self.assertEqual(changes,['add','remove'])
    def test_reindexed_file_retires_historic_owned_guid(self):
        self.fn.lists['rain-pl']=['old-index','manual-live'];self.own('rain-pl','old-index')
        self.run_sync();self.assertEqual(set(self.fn.lists['rain-pl']),{'rain','manual-live'})
        self.run_sync();self.assertEqual(self.fn.lists['rain-pl'].count('rain'),1)
    def test_multiple_native_guids_for_same_path_choose_stable_identity(self):
        self.paths['rain-z']=self.paths['rain'];self.fn.lists['rain-pl']=['rain-z','rain','manual-live']
        self.run_sync();self.assertEqual(self.fn.lists['rain-pl'],['rain','manual-live'])
        self.paths=dict(reversed(list(self.paths.items())));writes=list(self.fn.writes)
        self.run_sync();self.assertEqual(self.fn.writes,writes)
    def test_failed_add_preserves_old_relation_and_retries_without_duplicates(self):
        self.fn.lists['rain-pl']=['likes','manual-live'];self.own('rain-pl','likes')
        original=self.fn.playlist_add_tracks
        def fail_after_write(guid,ids):original(guid,ids);raise TimeoutError('lost acknowledgement')
        self.fn.playlist_add_tracks=fail_after_write
        self.assertEqual(self.run_sync()['status'],'partial')
        self.assertIn('likes',self.fn.lists['rain-pl']);self.assertIn('rain',self.fn.lists['rain-pl'])
        self.fn.playlist_add_tracks=original;self.run_sync()
        self.assertEqual(self.fn.lists['rain-pl'].count('rain'),1);self.assertNotIn('likes',self.fn.lists['rain-pl'])
    def test_failed_remove_is_retryable_and_preserves_canonical(self):
        original=self.fn.playlist_remove_tracks;self.fn.playlist_remove_tracks=lambda *a:{'code':1}
        self.assertEqual(self.run_sync()['status'],'partial');self.assertIn('rain',self.fn.lists['rain-pl'])
        self.fn.playlist_remove_tracks=original;self.run_sync()
        self.assertEqual(self.fn.lists['rain-pl'],['rain','manual-live'])
    def test_missing_path_evidence_never_guesses_another_folders_copy(self):
        self.own('rain-pl','likes')
        result=reconcile(self.store,self.cfg,self.fn,1,timeout=0,path_reader=lambda:None)
        self.assertEqual(result['status'],'partial');self.assertEqual(self.fn.writes,[])
        self.assertEqual(self.fn.lists['rain-pl'],['likes','rain','manual-live'])
    def test_directory_change_replaces_old_copy_only_after_new_file_indexed(self):
        file=self.e.root/'新雨天/shared.mp3';file.parent.mkdir();file.write_bytes(b'audio')
        with self.store.connect() as db:db.execute("UPDATE hub_group_dirs SET directory='新雨天' WHERE name='雨天'")
        self.paths['new-rain']='/nas/新雨天/shared.mp3';self.run_sync()
        self.assertEqual(set(self.fn.lists['rain-pl']),{'new-rain','manual-live'})
        self.assertEqual(self.fn.lists['likes-pl'],['likes'])
    def test_directory_edit_during_matching_defers_remote_writes(self):
        from hub_fn_worker import target as original_target
        def edit_during_sync(store,fn,user,group):
            result=original_target(store,fn,user,group)
            if group=='雨天':
                with store.connect() as db:db.execute("UPDATE hub_group_dirs SET directory='新雨天' WHERE name='雨天'")
            return result
        with patch('hub_fn_worker.target',side_effect=edit_during_sync):
            result=self.run_sync()
        self.assertEqual(result['status'],'partial');self.assertIn('保存目录已变更',result['summary'])
        self.assertEqual(self.fn.writes,[])
        file=self.e.root/'新雨天/shared.mp3';file.parent.mkdir();file.write_bytes(b'audio')
        self.paths['new-rain']='/nas/新雨天/shared.mp3';self.run_sync()
        self.assertEqual(set(self.fn.lists['rain-pl']),{'new-rain','manual-live'})


class PreviewTests(unittest.TestCase):
    def test_guest_full_audio_requires_platform_grant(self):
        with patch('hub_kugou.credentials',return_value={}),patch('kugou.request',return_value={'url':['https://cdn.example/a'],'is_free_part':0}):
            self.assertFalse(hub_kugou.resolve('a'*32,'standard',{}, {})['preview'])
        with patch('hub_kugou.credentials',return_value={}),patch('kugou.request',return_value={'url':[]}):
            with self.assertRaises(ValueError):hub_kugou.resolve('a'*32,'standard',{}, {})
        from unittest.mock import AsyncMock
        urls=AsyncMock(return_value=NS(data=[NS(result=0,purl='audio.mp3')]))
        class Client:
            async def __aenter__(self):return NS(credential=NS(musickey=''),song=NS(get_song_urls=urls,get_cdn_dispatch=AsyncMock(return_value=NS(sip=['https://cdn.example/']))))
            async def __aexit__(self,*args):pass
        module=NS(SongFileInfo=lambda **kw:NS(**kw),SongFileType=NS(MP3_128=1,MP3_320=2,FLAC=3))
        with patch.dict(sys.modules,{'qqmusic_api.modules.song':module}),patch('hub_qq.client',return_value=Client()):
            self.assertFalse(asyncio.run(hub_qq.resolve('mid','standard',None,{}))['preview'])
            urls.return_value=NS(data=[NS(result=1,purl='')])
            with self.assertRaises(hub_qq.QQError):asyncio.run(hub_qq.resolve('mid','standard',None,{}))
    def test_kugou_trial_never_enters_download(self):
        with patch('hub_kugou.credentials',return_value={'userid':1,'token':'test'}),patch('kugou.request',return_value={'url':['https://cdn.example/a'],'is_free_part':1}):
            with self.assertRaises(ValueError):hub_kugou.resolve('a'*32,'standard',{}, {})
            self.assertTrue(hub_kugou.resolve('a'*32,'standard',{}, {},preview=True)['preview'])
    def test_kugou_zero_flag_and_fallback(self):
        with patch('hub_kugou.credentials',return_value={'userid':1,'token':'test'}),patch('kugou.request',return_value={'url':['https://cdn.example/a'],'is_free_part':'0'}):
            self.assertFalse(hub_kugou.resolve('a'*32,'standard',{}, {})['preview'])
        with patch('hub_kugou.credentials',return_value={'userid':1,'token':'test'}),patch('kugou.request',side_effect=[{}, {'url':['https://cdn.example/a']}]) as req:
            self.assertTrue(hub_kugou.resolve('a'*32,'standard',{}, {},preview=True)['preview'])
            self.assertEqual([x.args[1]['IsFreePart'] for x in req.call_args_list],[0,1])
    def test_netease_trial_is_explicit_and_download_rejected(self):
        fake=NS(NeteaseAPI=lambda:NS(resolve_audio=lambda *a,**k:{'url':'https://cdn.example/a','freeTrialInfo':{'start':0,'end':30},'type':'mp3'},get_lyric=lambda *a:{}))
        from hub_providers import Providers
        with patch.dict(sys.modules,{'netease_provider':fake}):
            p=Providers(lambda:{},lambda *a:None)
            self.assertTrue(p.resolve('netease','1','standard',preview=True)['preview'])
            with patch.object(p,'public_netease_audio',side_effect=ValueError('no full audio')):
                from hub_errors import UpstreamFailure
                with self.assertRaises(UpstreamFailure) as ctx:p.resolve('netease','1','standard')
                self.assertIsInstance(ctx.exception.cause,ValueError)

class MobileTests(unittest.TestCase):
    def run_watcher(self,forever=False):
        from hub_mobile_qr import MobileQR
        closed=threading.Event();ready=threading.Event()
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            @property
            def login(self):return self
            async def get_qrcode(self,*a):return NS(data=b'QR',mimetype='image/png')
            async def checking_mobile_qrcode(self,*a,**kw):
                try:
                    yield NS(event=NS(name='SCAN'),credential=None)
                    if forever:
                        await asyncio.sleep(100)
                    else:yield NS(event=NS(name='DONE'),credential=NS(model_dump=lambda:{'musicid':1,'musickey':'secret'}))
                finally:closed.set()
        events=[]
        with patch.dict(sys.modules,{'anyio':NS(current_time=time.monotonic),'qqmusic_api.models.login':NS(QRLoginType=NS(MOBILE='mobile'))}),patch('hub_qq.client',return_value=Client()):
            watcher=MobileQR(lambda state,cred:events.append((state,cred)))
            self.assertTrue(watcher.image().startswith('data:image/png;'))
            if forever:watcher.cancel();watcher.cancel()
            watcher.thread.join(2)
        self.assertTrue(closed.is_set());self.assertFalse(watcher.thread.is_alive())
        return events
    def test_mobile_generator_completes_and_closes(self):
        events=self.run_watcher();self.assertEqual([e[0] for e in events],['pending','complete'])
    def test_cancel_closes_mqtt_generator(self):
        self.assertEqual([e[0] for e in self.run_watcher(True)],['pending'])

if __name__=='__main__':
    unittest.main()


class AutoCommentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.e=Engine(Path(self.temp.name)/'data',Path(self.temp.name)/'music',lambda *a:None)
    def test_queue_is_durable_deduplicated_and_retries_busy(self):
        import hub_auto_comments as ac
        ac.enqueue(self.e,'one');ac.enqueue(self.e,'one')
        self.assertEqual(len(self.e.store.rows('SELECT * FROM auto_comments')),1)
        tasks={};submitted=[]
        def submit(kind,args):
            submitted.append((kind,args));tasks['task']={'status':'running'};return {'id':'task'}
        self.e.bridge={'BRIDGE':{'store':type('Tasks',(),{'get':lambda _,key:tasks.get(key)})()},'submit_task':submit}
        ac.pump(self.e);ac.pump(self.e)
        self.assertEqual(len(submitted),1)
        self.assertEqual(submitted[0][1]['hub_track_ids'],['one'])
        tasks['task']['status']='failed';ac.pump(self.e)
        row=self.e.store.rows('SELECT * FROM auto_comments')[0]
        self.assertEqual(row['status'],'pending');self.assertIsNone(row['task_id'])
        self.assertGreater(row['due'],time.time())
        with self.e.store.connect() as db:db.execute('UPDATE auto_comments SET due=0')
        ac.pump(self.e);self.assertEqual(len(submitted),2)
        tasks['task']['status']='succeeded';ac.pump(self.e);ac.pump(self.e)
        self.assertEqual(len(submitted),2)
        self.assertEqual(self.e.store.rows('SELECT status FROM auto_comments')[0]['status'],'done')
    def test_catalog_uses_metadata_and_existing_comment_identity(self):
        import hub_auto_comments as ac
        tid=self.e.store.upsert_track('netease',song())
        with patch('hub_comments.query',return_value={'trackGuid':'native-guid'}):
            rows=ac.catalog(self.e,[tid,tid,'missing'])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['guid'],'native-guid')
        self.assertEqual(rows[0]['duration_ms'],song()['duration_ms'])
        self.assertEqual(rows[0]['title'],song()['title'])


class QQPaginationTests(unittest.TestCase):
    def test_missing_or_false_hasmore_does_not_truncate(self):
        from hub_qq import collect
        from types import SimpleNamespace as NS
        pages=[]
        async def fetch(page):
            pages.append(page)
            return NS(total=3,songs=[NS(mid=str(x)) for x in ([1,2] if page==1 else [3])],hasmore=0)
        self.assertEqual(len(asyncio.run(collect(fetch,'songs'))),3)
        self.assertEqual(pages,[1,2])
    def test_true_hasmore_at_exact_total_does_not_overfetch(self):
        from hub_qq import collect
        from types import SimpleNamespace as NS
        async def fetch(page):
            self.assertEqual(page,1);return NS(total=1,songs=[NS(mid='a')],hasmore=1)
        self.assertEqual(len(asyncio.run(collect(fetch,'songs'))),1)
    def test_incomplete_duplicate_and_changed_snapshots_fail_closed(self):
        from hub_qq import collect,QQError
        from types import SimpleNamespace as NS
        for second in (NS(total=2,songs=[]),NS(total=2,songs=[NS(mid='a')]),NS(total=3,songs=[NS(mid='b')])):
            async def fetch(page):return NS(total=2,songs=[NS(mid='a')],hasmore=0) if page==1 else second
            with self.assertRaises(QQError):asyncio.run(collect(fetch,'songs'))
    def test_empty_likes_is_authoritative(self):
        from hub_qq import collect
        from types import SimpleNamespace as NS
        async def fetch(page):return NS(total=0,songs=[])
        self.assertEqual(asyncio.run(collect(fetch,'songs')),[])


class QQPartialSnapshotTests(unittest.TestCase):
    def test_stable_omitted_song_returns_explicit_non_authoritative_snapshot(self):
        async def fetch(page):return NS(total=2,songs=[NS(mid='a')] if page==1 else [])
        result=asyncio.run(hub_qq.collect(fetch,'songs',allow_partial=True))
        self.assertEqual(len(result),1);self.assertEqual(result.total,2)
    def test_partial_sync_adds_visible_tracks_without_deleting_old_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=Engine(Path(tmp)/'data',Path(tmp)/'music',lambda *a:None)
            r=e.store.save_rule(rule());old=e.store.upsert_track('netease',song('old'))
            with e.store.connect() as db:
                db.execute('INSERT INTO membership VALUES(?,?,0)',(r['id'],old))
                db.execute('INSERT INTO source_members VALUES(?,?,?)',(r['id'],'pl',old))
            jid=e.store.enqueue('sync','sync:'+r['id'],{'rule_id':r['id']})
            job=e.store.claim();e.providers=NS(discover=lambda *a:[{'id':'pl','name':'喜欢'}],playlist=lambda *a:{'tracks':[song('new')],'snapshot_complete':False,'warning':'1/2'})
            with patch.object(e,'export'):
                self.assertEqual(e.sync(job),'1/2')
            members=e.store.rows('SELECT track_id FROM membership WHERE rule_id=?',(r['id'],))
            self.assertEqual(len(members),2);self.assertIn(old,[m['track_id'] for m in members])
            self.assertEqual(len(e.store.rows('SELECT * FROM source_members')),2)
            self.assertEqual(e.store.rule(r['id'])['error'],'1/2')


class QQQualityFallbackTests(unittest.TestCase):
    def test_lossless_denied_uses_authorized_full_mp3_and_reports_actual_quality(self):
        from unittest.mock import AsyncMock
        urls=AsyncMock(side_effect=[NS(data=[NS(result=1,purl='')]),NS(data=[NS(result=1,purl='')]),NS(data=[NS(result=0,purl='full.mp3')])])
        class Client:
            async def __aenter__(self):return NS(song=NS(get_song_urls=urls,get_cdn_dispatch=AsyncMock(return_value=NS(sip=['https://cdn.example/']))))
            async def __aexit__(self,*args):pass
        module=NS(SongFileInfo=lambda **kw:NS(**kw),SongFileType=NS(MP3_128=1,MP3_320=2,FLAC=3))
        with patch.dict(sys.modules,{'qqmusic_api.modules.song':module}),patch('hub_qq.client',return_value=Client()):
            result=asyncio.run(hub_qq.resolve('mid','lossless',None,{}))
        self.assertEqual(result['quality'],'standard');self.assertEqual(result['ext'],'mp3');self.assertFalse(result['preview'])
        self.assertEqual([c.kwargs['file_type'] for c in urls.call_args_list],[3,2,1])

class BorrowedStrategyTests(unittest.TestCase):
    def test_lossless_checks_codec_not_filename(self):
        from hub_audio_validation import validate_quality
        flac=type('FLAC',(),{'__module__':'mutagen.flac'})()
        mp3=type('MP3',(),{'__module__':'mutagen.mp3'})()
        mp4=type('MP4',(),{'__module__':'mutagen.mp4','info':NS(codec='alac')})()
        validate_quality(flac,'lossless');validate_quality(mp4,'lossless')
        validate_quality(mp3,'standard')
        with self.assertRaisesRegex(ValueError,'声明无损'):validate_quality(mp3,'lossless')
        mp4.info=NS(codec='aac')
        with self.assertRaises(ValueError):validate_quality(mp4,'hires')

    def test_transfer_failure_tries_next_known_source(self):
        from hub_engine import Cancelled
        with tempfile.TemporaryDirectory() as td:
            e=Engine(Path(td)/'data',Path(td)/'music',lambda *a:None)
            sources=[{'platform':p,'source_id':'one','meta':'{}'} for p in ('qq','netease')]
            part=Path(td)/'audio.part';part.write_bytes(b'partial')
            with patch.object(e,'check'),patch.object(e.store,'update_claimed'),patch.object(e.providers,'resolve_native',return_value={'url':'https://example.com/full'}),patch.object(e,'transfer_audio',side_effect=[ValueError('bad file'),'valid']) as transfer:
                result=e.download_from_sources({'payload':{'quality':'lossless'}},part,sources,200000)
                self.assertEqual(result[1],'valid');self.assertEqual(transfer.call_count,2);self.assertFalse(part.exists())
            with patch.object(e,'check'),patch.object(e.store,'update_claimed'),patch.object(e.providers,'resolve_native',side_effect=Cancelled),patch.object(e,'transfer_audio') as transfer:
                with self.assertRaises(Cancelled):e.download_from_sources({'payload':{'quality':'lossless'}},part,sources,200000)
                transfer.assert_not_called()

    def test_reverse_batches_keep_new_native_source_order(self):
        class PrependingFn(Fn):
            def playlist_add_tracks(self,guid,guids):
                self.writes.append(('add',guids));self.members=list(guids)+self.members;return {'code':0}
        with tempfile.TemporaryDirectory() as td,patch('fnsync._playlist_snapshot',return_value='snapshot'),patch('fnsync._snapshot_status'):
            e=Engine(Path(td)/'data',Path(td)/'music',lambda *a:None);r=e.store.save_rule(rule());mapping={}
            with e.store.connect() as db:
                db.execute("INSERT INTO hub_group_dirs VALUES('共同喜欢','共同喜欢')")
                for i in range(205):
                    tid=f'track-{i:04d}';mapping[tid]=f'song-{i:04d}'
                    db.execute('INSERT INTO membership VALUES(?,?,?)',(r['id'],tid,i))
            fn=PrependingFn(['manual'])
            self.assertEqual(owned_delta(e.store,fn,{'guid':'pl','name':'融合'},'me','共同喜欢',mapping),(205,0))
            self.assertEqual(fn.members,list(mapping.values())+['manual'])
            self.assertEqual(owned_delta(e.store,fn,{'guid':'pl','name':'融合'},'me','共同喜欢',mapping),(0,0))

class SourceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.e=Engine(Path(self.temp.name)/'data',Path(self.temp.name)/'music',lambda *a:None)
        self.r=self.e.source_registry
    def entry(self,name='one'):
        return {'name':name,'url':'https://example.com/'+name,'platforms':['qq'],'enabled':True,'token':'private-key'}
    def test_credentials_persist_but_never_export(self):
        x=self.r.save(self.entry());self.assertTrue(x['token_configured']);self.assertNotIn('token',x)
        self.r.save({**x,'token':'','enabled':False})
        self.assertEqual(next(z for z in self.r.rows(True) if z['id']==x['id'])['token'],'private-key')
        self.assertNotIn('private-key',json.dumps(self.r.export()))
        from hub_sources import Sources
        r=Sources(self.r.path.parents[1],self.e.settings)
        self.assertFalse(next(z for z in r.rows() if z['id']==x['id'])['enabled'])
        self.r.save({**x,'clear_token':True});self.assertFalse(next(z for z in self.r.rows() if z['id']==x['id'])['token_configured'])
    def test_import_atomic_and_duplicate_preserves_token(self):
        x=self.r.save(self.entry())
        with self.assertRaises(ValueError):self.r.import_sources({'version':1,'sources':[self.entry('new'),{'name':'bad'}]})
        self.assertEqual(len(self.r.rows()),5)
        self.assertEqual(self.r.import_sources(self.r.export()),{'added':0})
        self.assertTrue(next(z for z in self.r.rows() if z['id']==x['id'])['token_configured'])
        with self.assertRaises(ValueError):self.r.save({**self.entry(),'url':'https://example.com/resolve?token=private'})
    def test_disabled_sources_skipped_builtins_retained(self):
        x=self.r.save(self.entry());self.r.save({'id':'builtin-qq','enabled':False});self.r.save({**x,'enabled':False})
        self.assertEqual(self.r.options('qq'),[])
        self.assertEqual(len(self.r.rows()),5)
        self.r.save({**x,'enabled':True});self.assertEqual([z['id'] for z in self.r.options('qq')],[x['id']])
    def test_transfer_error_tries_custom_then_next_and_cancel_stops(self):
        a=self.r.save(self.entry());b=self.r.save(self.entry('two'))
        src=[{'platform':'qq','source_id':'one','meta':'{}'}];job={'payload':{'quality':'standard'}};part=Path(self.temp.name)/'audio.part'
        with patch.object(self.e,'check'),patch.object(self.e.store,'update_claimed'),patch.object(self.e.providers,'resolve_native',side_effect=ValueError('offline')),patch('hub_sources.resolve_http',return_value={'url':'https://example.com/a'}) as resolve,patch.object(self.e,'transfer_audio',side_effect=[ValueError('short'), 'audio']) as transfer:
            self.assertEqual(self.e.download_from_sources(job,part,src,200000)[1],'audio');self.assertEqual(resolve.call_count,2)
        with patch.object(self.e,'check'),patch.object(self.e.store,'update_claimed'),patch.object(self.e.providers,'resolve_native',side_effect=ValueError('offline')),patch('hub_sources.resolve_http',side_effect=Cancelled) as resolve:
            with self.assertRaises(Cancelled):self.e.download_from_sources(job,part,src,200000)
            self.assertEqual(resolve.call_count,1)
    def test_wrong_song_preview_and_wrong_duration_rejected(self):
        from hub_sources import validate_result
        data=dict(platform='qq',id='one',url='https://example.com/a',duration_ms=200000,preview=False,quality='standard')
        with patch('hub_providers.public_audio_url'):
            self.assertEqual(validate_result(data,'qq','one',{})['duration_ms'],200000)
            for changes in ({'id':'other'},{'preview':True},{'duration_ms':30000},{'quality':'unknown'}):
                with self.assertRaises(ValueError):validate_result({**data,**changes},'qq','one',{'duration_ms':200000})
    def test_existing_gateway_preserved_and_builtin_fallback_available(self):
        self.e.save_settings({'gateways':{'qq':{'url':'https://example.com/old','token':'secret'}}})
        rows=self.r.rows();self.assertTrue(any(x['id']=='legacy-qq' for x in rows))
        self.assertEqual([x['kind'] for x in self.r.options('qq')],['builtin','legacy'])
        self.r.save({'id':'legacy-qq','enabled':False})
        self.assertEqual(self.e.settings()['gateways']['qq']['token'],'secret')
        self.assertEqual([x['kind'] for x in self.r.options('qq')],['builtin'])
