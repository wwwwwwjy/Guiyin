import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from hub_metadata import consensus,identity,read_tags
from hub_engine import Engine
from hub_assets import Assets

class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.local=dict(title='天亮以前说再见',artists=['何野'],duration_ms=240000,album='广告',date='')
        self.a=dict(self.local,platform='qq',album='天亮以前说再见',date='2018-06-20')
        self.b=dict(self.a,platform='netease',date='2018')
    def test_two_platforms_agree_album_year(self):
        changes,proof=consensus(self.local,[self.a,self.b])
        self.assertEqual(changes,{'album':['天亮以前说再见'],'date':['2018']})
        self.assertEqual(set(proof['album']),{'qq','netease'})
    def test_no_guess_on_artist_version_duration_or_single_provider(self):
        for bad in [dict(self.b,artists=['其他歌手']),dict(self.b,title='天亮以前说再见 (Live)'),dict(self.b,duration_ms=250000),dict(self.b,platform='qq')]:
            self.assertFalse(consensus(self.local,[self.a,bad])[0])
    def test_conflicting_editions_skip_field(self):
        changes,_=consensus(self.local,[self.a,dict(self.b,album='精选集',date='2020')])
        self.assertEqual(changes,{})
    def test_precise_date_and_artist_spelling_preserved(self):
        changes,_=consensus(dict(self.local,date='2018-06-20'),[self.a,self.b])
        self.assertNotIn('date',changes);self.assertNotIn('artist',changes);self.assertNotIn('title',changes)
    def test_directory_persistence_backup_restore_and_conflict(self):
        import wave
        with tempfile.TemporaryDirectory() as tmp:
            e=Engine(Path(tmp)/'data',Path(tmp)/'music',lambda *a:None)
            try:
                p=e.root/'sample.mp3'
                p.write_bytes((bytes.fromhex("fffb9000")+bytes(413))*10)
                from mutagen.id3 import ID3,TIT2,TALB,TPE1
                tags=ID3();tags.add(TIT2(encoding=3,text=['Song']));tags.add(TALB(encoding=3,text=['Bad']));tags.add(TPE1(encoding=3,text=['Artist']));tags.save(p)
                key=e.metadata_directories.add(str(e.root));self.assertFalse(e.metadata_directories.status()['directories'][0]['enabled'])
                self.assertEqual(e.asset_directories.status()['directories'],[])
                original=p.read_bytes()
                before=read_tags(p)
                change=e.metadata_directories.publish(p,{'album':['Correct']},before,{'album':['qq','netease']})
                self.assertEqual(read_tags(p)['album'],['Correct'])
                with patch.object(e.library,'start'):e.metadata_directories.restore(change)
                self.assertEqual(p.read_bytes(),original)
                with self.assertRaisesRegex(ValueError,'此前已撤销'):
                    e.metadata_directories.publish(p,{'album':['Correct']},before,{})
                # Distinct file for changed-after-repair conflict.
                q=e.root/'other.mp3';q.write_bytes(original)
                change=e.metadata_directories.publish(q,{'album':['Correct']},before,{})
                q.write_bytes(q.read_bytes()+b'changed')
                with self.assertRaisesRegex(ValueError,'又发生变化'):e.metadata_directories.restore(change)
            finally:e.close()
    def test_qq_builtin_lyric_can_complete_without_netease(self):
        song=dict(self.local,source_id='abc')
        e=SimpleNamespace(stop=__import__('threading').Event(),source_registry=Mock(),providers=Mock())
        e.source_registry.rows.return_value=[dict(id='builtin-qq',kind='builtin',platforms=['qq'],enabled=True)]
        e.providers.search.return_value=[song];e.providers.accounts.return_value={}
        with patch('hub_qq.lyric',new=AsyncMock(return_value='[00:01]line')):
            result,_=Assets(e).resources(dict(song,lyric_missing=True,cover_missing=False))
        self.assertEqual(result['lyric'],'[00:01]line')

if __name__=='__main__':unittest.main()
