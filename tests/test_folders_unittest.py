import sys, tempfile, unittest, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from test_repair_unittest import Engine
import hub_folders
class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=Engine(Path(self.tmp.name)/'data',Path(self.tmp.name)/'music',lambda *a:None)
    def rule(self,kind='created'):
        return self.e.store.save_rule(dict(name='创建',platform='netease',kind=kind,ref='account:1',group_name='old',interval_hours=24,enabled=1,auto_download=1,quality='standard'))
    def test_native_groups_exclude_unconfigured_and_failed_legacy_targets(self):
        from hub_fn_worker import managed_groups
        self.rule('playlist')
        with self.e.store.connect() as db:
            db.execute("INSERT INTO hub_group_dirs VALUES('已分类','我的喜欢')")
            db.execute("INSERT INTO fn_targets(user,group_name,name) VALUES('u','旧分类','too long')")
            db.execute("INSERT INTO fn_targets(user,group_name,name,guid) VALUES('u','以前创建','existing','g')")
        self.assertEqual(managed_groups(self.e.store,'u'),{'已分类','以前创建'})
    def test_explicit_retry_runs_while_subscriptions_stopped(self):
        jid=self.e.store.enqueue('download','download:sample',{'track_id':'sample','quality':'standard'})
        self.e.store.update_job(jid,status='blocked')
        self.e.store.retry(jid)
        job=self.e.store.claim(manual_only=True)
        self.assertEqual(job['id'],jid)
        self.assertTrue(job['payload']['manual'])
        self.assertFalse(self.e.settings().get('sync_enabled',False))
    def test_default_stopped(self):
        self.assertFalse(self.e.public_settings()['sync_enabled'])
    def test_child_catalog_keeps_separate_selection(self):
        p=self.rule();hub_folders.materialize(self.e,p,[{'id':'1','name':'下雨天'},{'id':'2','name':'开车听'}])
        children=self.e.store.rows('SELECT * FROM hub_catalog_children');self.assertEqual(len(children),2)
        self.assertTrue(all(not self.e.store.rule(c['id'])['enabled'] for c in children))
        with self.e.store.connect() as db:db.execute("UPDATE rules SET group_name='雨天',enabled=1 WHERE id=?",(children[0]['id'],))
        hub_folders.materialize(self.e,p,[{'id':'1','name':'下雨天'},{'id':'2','name':'开车听'}])
        self.assertEqual(self.e.store.rule(children[0]['id'])['group_name'],'雨天')
    def test_confined_folder(self):
        for bad in ('','..','../escape','/tmp/outside','Playlists/test'):
            with self.assertRaises(ValueError):hub_folders.directory(self.e,bad)
        self.assertEqual(hub_folders.directory(self.e,'分类/我的喜欢'),'分类/我的喜欢')
    def test_existing_music_materializes_without_removal(self):
        r=self.rule('playlist');tid=self.e.store.upsert_track('netease',dict(source_id='1',title='Song',artists=['Singer']))
        src=self.e.root/'Library';src.mkdir();(src/'Song.mp3').write_bytes(b'existing audio')
        with self.e.store.connect() as db:
            db.execute("UPDATE tracks SET path='Library/Song.mp3' WHERE id=?",(tid,))
            db.execute('INSERT INTO membership VALUES(?,?,0)',(r['id'],tid))
            db.execute("INSERT INTO hub_group_dirs VALUES('old','我的喜欢')")
        hub_folders.publish(self.e);hub_folders.publish(self.e)
        self.assertEqual((self.e.root/'我的喜欢/Song.mp3').read_bytes(),b'existing audio')
        self.assertTrue((src/'Song.mp3').exists())
        self.e.store.export_playlists(self.e.root)
        self.assertIn('../我的喜欢/Song.mp3',next((self.e.root/'Playlists').glob('*.m3u8')).read_text())
    def test_removed_child_clears_membership_only(self):
        p=self.rule();hub_folders.materialize(self.e,p,[{'id':'1','name':'rain'}]);c=self.e.store.rows('SELECT id FROM hub_catalog_children')[0]['id']
        tid=self.e.store.upsert_track('netease',dict(source_id='x',title='Song',artists=[]))
        with self.e.store.connect() as db:db.execute('INSERT INTO membership VALUES(?,?,0)',(c,tid))
        hub_folders.materialize(self.e,p,[])
        self.assertFalse(self.e.store.rows('SELECT * FROM membership WHERE rule_id=?',(c,)))
        self.assertTrue(self.e.store.rows('SELECT * FROM tracks WHERE id=?',(tid,)))
if __name__=='__main__':unittest.main()

class SelectionRegressionTests(FolderTests):
    def test_disabled_sources_never_publish_or_download(self):
        r=self.rule('playlist')
        tid=self.e.store.upsert_track('netease',dict(source_id='selected',title='Song',artists=['Singer']))
        (self.e.root/'source.mp3').write_bytes(b'music')
        with self.e.store.connect() as db:
            db.execute("UPDATE tracks SET path='source.mp3' WHERE id=?",(tid,))
            db.execute('INSERT INTO membership VALUES(?,?,0)',(r['id'],tid))
            db.execute("INSERT INTO hub_group_dirs VALUES('old','我的音乐')")
            db.execute('UPDATE rules SET enabled=0 WHERE id=?',(r['id'],))
        self.assertFalse(hub_folders.destinations(self.e,tid))
        self.assertFalse(hub_folders.selected(self.e,r['id']))
        hub_folders.publish(self.e)
        self.assertFalse((self.e.root/'我的音乐/source.mp3').exists())
        from hub_engine import Cancelled
        jid=self.e.store.enqueue('download','download:'+tid,{'track_id':tid,'quality':'standard'})
        self.e.save_settings({'sync_enabled':True})
        job=self.e.store.claim();job['_worker']=True
        with self.assertRaises(Cancelled):self.e.check(job)

class AssetRegressionTests(unittest.TestCase):
    def test_match_preserves_version_artist_and_duration(self):
        from hub_assets import compatible
        song=dict(title='Song (Live)',artists=['Singer'],duration_ms=200000)
        self.assertTrue(compatible(song,dict(song)))
        for wrong in (dict(song,title='Song'),dict(song,artists=['Other']),dict(song,duration_ms=220000),dict(song,artists=[])):
            self.assertFalse(compatible(song,wrong))
    def test_sidecars_never_overwrite_existing_or_symlink(self):
        from hub_assets import write_new
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'song.lrc';p.write_text('original')
            self.assertFalse(write_new(p,b'replacement'));self.assertEqual(p.read_text(),'original')
            q=Path(d)/'link.lrc';q.symlink_to(p)
            self.assertFalse(write_new(q,b'replacement'));self.assertEqual(p.read_text(),'original')
            n=Path(d)/'new.lrc';self.assertTrue(write_new(n,b'new'));self.assertEqual(n.read_bytes(),b'new')
    def test_overlapping_directories_scan_once_and_skip_symlink(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            e=Engine(Path(d)/'data',Path(d)/'music',lambda *a:None)
            sub=e.root/'sub';sub.mkdir();(sub/'a.mp3').write_bytes(b'a')
            (sub/'link.mp3').symlink_to(sub/'a.mp3')
            # This test exercises the synchronous asset scan, not the separate
            # library indexer; do not leave a background writer in the temp dir.
            with patch.object(e.library, 'start'):
                e.save_settings({'library_dirs':[str(e.root),str(sub)]})
            def read(p):return dict(path=str(p),title=p.stem,artists=['A'],cover_missing=True,lyric_missing=True)
            with patch('hub_assets.inspect',side_effect=read):e.assets.scan()
            self.assertEqual(e.assets.scanned,1);self.assertEqual(len(e.assets.rows),1)

class GroupSaveTests(FolderTests):
    def test_rename_cleans_orphan_directory_and_persists(self):
        r=self.rule('playlist')
        with self.e.store.connect() as db:
            db.execute("INSERT INTO hub_group_dirs VALUES('old','我的喜欢')")
            db.execute("INSERT INTO hub_group_dirs VALUES('stale','我的喜欢')")
        hub_folders.save_group(self.e,'new',[r['id']],'我的喜欢','old')
        self.assertEqual(self.e.store.rule(r['id'])['group_name'],'new')
        self.assertEqual(self.e.store.rows('SELECT * FROM hub_group_dirs'),[{'name':'new','directory':'我的喜欢'}])
        hub_folders.save_group(self.e,'new',[r['id']],'我的喜欢','new')
    def test_active_collision_rolls_back_selection(self):
        a=self.rule('playlist');b=self.rule('playlist')
        with self.e.store.connect() as db:
            db.execute("UPDATE rules SET group_name='other' WHERE id=?",(b['id'],))
            db.execute("INSERT INTO hub_group_dirs VALUES('other','taken')")
        with self.assertRaises(ValueError):hub_folders.save_group(self.e,'new',[a['id']],'taken','old')
        self.assertEqual(self.e.store.rule(a['id'])['group_name'],'old')
