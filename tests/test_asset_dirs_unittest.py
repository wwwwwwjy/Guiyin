import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from hub_engine import Engine
from hub_asset_dirs import AssetDirectories
from hub_assets import Assets

class AssetDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=Engine(Path(self.tmp.name)/'data',Path(self.tmp.name)/'music',lambda *a:None)
        self.addCleanup(self.e.close)
        self.d=self.e.asset_directories
        self.a=self.e.root/'A';self.a.mkdir();self.b=self.e.root/'B';self.b.mkdir()
    def test_scopes_persist_default_paused_and_idempotent(self):
        key=self.d.add(str(self.a));self.assertEqual(key,self.d.add(str(self.a)))
        self.d.add(str(self.b));status=AssetDirectories(self.e).status()
        self.assertEqual(len(status['directories']),2)
        self.assertFalse(any(x['enabled'] for x in status['directories']))
        with self.assertRaises(ValueError):self.d.add(self.tmp.name)
    def test_pause_does_not_change_other_directory(self):
        a=self.d.add(str(self.a));b=self.d.add(str(self.b))
        with patch.object(self.d,'pump'):
            self.d.control(a,'resume');self.d.control(b,'resume');self.d.control(a,'pause')
        state={x['id']:x['enabled'] for x in self.d.status()['directories']}
        self.assertEqual(state,{a:False,b:True})
    def test_scoped_scan_and_restart_progress(self):
        (self.a/'one.mp3').write_bytes(b'x');(self.b/'two.mp3').write_bytes(b'x')
        key=self.d.add(str(self.a));w=Assets(self.e,[str(self.a)])
        def inspect(p):return dict(path=str(p),title=p.stem,artists=['a'],cover_missing=True,lyric_missing=True)
        with patch('hub_assets.inspect',side_effect=inspect):w.scan()
        self.assertEqual(w.scanned,1);self.assertEqual(w.rows[0]['title'],'one')
        w.rows[0]['status']='已补全';self.d.checkpoint(key,w)
        status=AssetDirectories(self.e).status(key)
        self.assertEqual(status['directories'][0]['completed'],1)
        self.assertEqual(status['rows'][0]['title'],'one')
        with self.assertRaises(ValueError):w.allowed(self.b/'two.mp3')
    def test_pause_resume_waits_for_active_worker(self):
        key=self.d.add(str(self.a));w=Assets(self.e,[str(self.a)])
        self.d.active=key;self.d.workers[key]=w
        self.d.control(key,'pause');self.assertTrue(w.stop.is_set())
        self.d.control(key,'resume');self.assertIsNone(self.d.thread)
        self.assertTrue(w.stop.is_set())
        self.d.active=None
    def test_paused_snapshot_does_not_claim_active_matching(self):
        key=self.d.add(str(self.a));w=Assets(self.e,[str(self.a)])
        w.rows=[dict(path=str(self.a/'one.mp3'),status='正在匹配',title='one')]
        self.d.checkpoint(key,w)
        status=AssetDirectories(self.e).status(key)
        self.assertEqual(status['rows'][0]['status'],'待匹配')
        self.assertEqual(status['directories'][0]['current'],'')
        self.assertEqual(status['directories'][0]['processed'],0)
    def test_large_directory_yields_to_due_directory_without_rescanning(self):
        large = self.d.add(str(self.a))
        small = self.d.add(str(self.b))
        self.d.match_batch = 1
        self.a.joinpath('one.mp3').write_bytes(b'x')
        self.a.joinpath('two.mp3').write_bytes(b'x')
        self.b.joinpath('new.mp3').write_bytes(b'x')

        def inspect(path):
            return dict(path=str(path), title=path.stem, artists=['a'],
                        cover_missing=True, lyric_missing=True)

        with self.e.store.connect() as db:
            db.execute('UPDATE asset_directories SET enabled=1,due=1 WHERE id=?', (large,))
            db.execute('UPDATE asset_directories SET enabled=1,due=2 WHERE id=?', (small,))
        with patch('hub_assets.inspect', side_effect=inspect), patch.object(
            Assets, 'resources', return_value=({}, '未匹配')
        ), patch.object(self.e.library, 'start'):
            self.d.pump()
            self.d.thread.join(5)
            worker = self.d.workers[large]
            self.assertEqual(worker.phase, '等待继续匹配')
            self.d.pump()
            self.d.thread.join(5)
            self.assertEqual(self.d.workers[small].scanned, 1)
            self.d.pump()
            self.d.thread.join(5)
            self.assertIs(self.d.workers[large], worker)
            self.assertEqual([row['status'] for row in worker.rows], ['未匹配', '未匹配'])

    def test_remove_missing_directory_while_worker_active(self):
        for directories in (self.d, self.e.metadata_directories):
            path = self.a / directories.table
            path.mkdir()
            key = directories.add(str(path))
            path.rmdir()
            worker = Assets(self.e, [str(path)])
            directories.workers[key] = worker
            directories.active = key
            directories.remove(key)
            self.assertTrue(worker.stop.is_set())
            self.assertEqual(directories.status()['directories'], [])
            directories.run(key, worker, continuing=True)
            self.assertNotIn(key, directories.workers)
            self.assertEqual(directories.status()['directories'], [])

    def test_empty_library_selection_does_not_scan_mount_root(self):
        (self.a / 'song.mp3').write_bytes(b'audio')
        self.e.library.scan()
        self.assertEqual(self.e.library.stats()['count'], 1)
        with patch.object(self.e.library, 'start'):
            self.e.save_settings({'library_dirs': []})
        self.e.library.scan()
        self.assertEqual(self.e.library.stats()['directories'], [])
        self.assertEqual(self.e.library.stats()['count'], 0)
        self.assertTrue((self.a / 'song.mp3').is_file())

    def test_missing_file_does_not_abort_remaining_rows(self):
        w=Assets(self.e,[str(self.a)])
        w.rows=[dict(path=str(self.a/'gone.mp3'),status='待匹配')]
        with patch.object(self.e.library,'start'):
            w.match()
        self.assertEqual(w.rows[0]['status'],'处理失败')


class AssetSourceTests(unittest.TestCase):
    def test_priority_disabled_and_independent_fallback(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        song=dict(title='Song',artists=['Artist'],source_id='1',duration_ms=200000)
        rows=[dict(id='disabled',kind='http',platforms=['qq'],enabled=False),
              dict(id='one',name='First',kind='lx',platforms=['qq'],enabled=True),
              dict(id='two',name='Second',kind='http',platforms=['qq'],enabled=True)]
        e=SimpleNamespace(stop=__import__('threading').Event(),source_registry=Mock(),providers=Mock())
        e.source_registry.rows.return_value=rows;e.providers.search.return_value=[song]
        worker=Assets(e)
        local=dict(song,lyric_missing=True,cover_missing=True)
        with patch('hub_js_sources.resolve_assets',return_value={'lyric':'[00:01]hello','cover_url':'bad'}), patch('hub_sources.resolve_http',return_value={'cover_url':'good'}) as http, patch('hub_artwork.fetch',side_effect=[ValueError('bad image'),b'picture']):
            value,_=worker.resources(local)
        self.assertEqual(value['lyric_source'],'First');self.assertEqual(value['cover_source'],'Second')
        self.assertEqual(http.call_count,1);self.assertEqual(e.providers.search.call_count,1)
    def test_wrong_version_never_sent_to_source(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        e=SimpleNamespace(stop=__import__('threading').Event(),source_registry=Mock(),providers=Mock())
        e.source_registry.rows.return_value=[dict(id='x',kind='http',platforms=['qq'],enabled=True)]
        e.providers.search.return_value=[dict(title='Other',artists=['Artist'])]
        with patch('hub_sources.resolve_http') as resolve:
            Assets(e).resources(dict(title='Song',artists=['Artist'],lyric_missing=True,cover_missing=True))
        resolve.assert_not_called()
    def test_lx_cache_actions_share_process(self):
        from hub_js_sources import invoke
        code="""let cached=false;lx.on(lx.EVENT_NAMES.request,({action})=>{
          if(action==='musicUrl'){cached=true;return 'https://example.com/audio';}
          if(!cached)throw Error('no cache');
          if(action==='lyric')return {lyric:'[00:01]hello'};
          if(action==='pic')return 'https://example.com/cover.jpg';
        });lx.send(lx.EVENT_NAMES.inited,{sources:{tx:{actions:['musicUrl','lyric','pic']}}});"""
        result=invoke(code,'assets',dict(source='tx',wantLyric=True,wantCover=True,info={}))
        self.assertEqual(result,{'lyric':'[00:01]hello','cover_url':'https://example.com/cover.jpg'})
    def test_lx_audio_only_not_called(self):
        from hub_js_sources import invoke
        code="""lx.on(lx.EVENT_NAMES.request,()=>{throw Error('should not call');});lx.send(lx.EVENT_NAMES.inited,{sources:{tx:{actions:['musicUrl']}}});"""
        self.assertEqual(invoke(code,'assets',dict(source='tx',wantLyric=True,wantCover=True,info={})),{})

    def test_http_metadata_does_not_require_audio_and_checks_identity(self):
        import json
        from unittest.mock import MagicMock
        from hub_sources import resolve_http
        opener=MagicMock()
        response=opener.open.return_value.__enter__.return_value
        song=dict(title='Song',artists=['Artist'],duration_ms=200000)
        for payload,valid in [({'platform':'qq','id':'1','lyric':'[00:01]line'},True),
                              ({'platform':'qq','id':'2','lyric':'[00:01]wrong'},False),
                              ({'platform':'qq','id':'1','duration_ms':float('nan')},False),
                              ({'platform':'qq','id':'1','duration_ms':210000},False)]:
            response.read.return_value=json.dumps(payload).encode()
            with patch('urllib.request.build_opener',return_value=opener):
                if valid:
                    value=resolve_http({'url':'https://example.com/resolve'},'qq','1','standard',song,assets=True)
                    self.assertEqual(value,{'lyric':'[00:01]line'})
                    request=opener.open.call_args.args[0]
                    self.assertEqual(json.loads(request.data)['action'],'assets')
                else:
                    with self.assertRaises(ValueError):resolve_http({'url':'https://example.com/resolve'},'qq','1','standard',song,assets=True)

    def test_broken_builtin_cover_does_not_block_lyric(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        song=dict(title='Song',artists=['Artist'],source_id='1',duration_ms=200000,cover_url='bad')
        e=SimpleNamespace(stop=__import__('threading').Event(),source_registry=Mock(),providers=Mock())
        e.source_registry.rows.return_value=[dict(id='builtin-netease',kind='builtin',platforms=['netease'],enabled=True)]
        e.providers.search.return_value=[song];e.providers.cookie.return_value=''
        with patch('netease_provider.NeteaseAPI') as api,patch('hub_artwork.fetch',side_effect=ValueError('bad')):
            api.return_value.get_lyric.return_value={'lrc':{'lyric':'[00:01]hello'}}
            api.return_value.get_song_detail.return_value={}
            value,_=Assets(e).resources(dict(song,lyric_missing=True,cover_missing=True))
        self.assertEqual(value['lyric'],'[00:01]hello');self.assertNotIn('cover',value)

if __name__=='__main__':unittest.main()

