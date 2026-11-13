"""0.4 regression tests; no external requests or NAS data touched."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
if 'certifi' not in sys.modules and importlib.util.find_spec('certifi') is None:sys.modules['certifi']=NS(where=lambda:None)
from hub_engine import Engine
from hub_bootstrap import ensure_comments
from hub_errors import describe

class Repairs(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.e=Engine(self.base/'data',self.base/'music',lambda *a:None)
    def setup_physical_group(self):
        import hub_folders
        r=self.e.store.save_rule(dict(name='喜欢',platform='netease',kind='playlist',ref='1',group_name='喜欢',interval_hours=24,enabled=True,auto_download=True,quality='standard'))
        tid=self.e.store.upsert_track('netease',dict(source_id='1',title='测试',artists=['歌手']))
        with self.e.store.connect() as db:
            db.execute("INSERT INTO hub_group_dirs VALUES('喜欢','喜欢')")
            db.execute('INSERT INTO membership VALUES(?,?,0)',(r['id'],tid))
            db.execute("UPDATE tracks SET path='missing.mp3',bytes=100 WHERE id=?",(tid,))
        return tid

    def test_missing_file_count_and_requeue(self):
        import hub_folders
        tid=self.setup_physical_group()
        self.assertEqual(hub_folders.counts(self.e,'喜欢'),{'total':1,'downloaded':0})
        self.assertIn('job_id',self.e.enqueue_download(tid))
        self.assertIsNone(self.e.store.rows('SELECT path FROM tracks WHERE id=?',(tid,))[0]['path'])

    def test_wrong_folder_not_counted_then_publish_recovers(self):
        import hub_folders
        tid=self.setup_physical_group()
        (self.e.root/'missing.mp3').write_bytes(b'audio')
        self.assertEqual(hub_folders.counts(self.e,'喜欢')['downloaded'],0)
        self.e.save_settings({'sync_enabled':True})
        self.assertTrue(self.e.enqueue_download(tid)['skipped'])
        self.assertEqual(hub_folders.counts(self.e,'喜欢')['downloaded'],1)
        (self.e.root/'喜欢'/'missing.mp3').unlink()
        self.assertEqual(hub_folders.counts(self.e,'喜欢')['downloaded'],0)

    def test_changed_directory_does_not_count_old_manifest(self):
        import hub_folders
        tid=self.setup_physical_group()
        old=self.e.root/'旧目录';old.mkdir();(old/'song.mp3').write_bytes(b'audio')
        with self.e.store.connect() as db:db.execute("INSERT INTO hub_group_files VALUES('喜欢',?,?)",(tid,'旧目录/song.mp3'))
        self.assertEqual(hub_folders.counts(self.e,'喜欢')['downloaded'],0)
        self.e.save_settings({'sync_enabled':True})
        self.assertTrue(self.e.enqueue_download(tid)['skipped'])
        self.assertTrue((self.e.root/'喜欢'/'song.mp3').is_file())
        self.assertEqual(hub_folders.counts(self.e,'喜欢')['downloaded'],1)

    def test_pagination_totals_filters_and_out_of_range_clamp(self):
        store=self.e.store
        self.assertEqual((store.job_page()['page'],store.job_page()['pages']),(0,0))
        for i in range(25):
            jid=store.enqueue('download',str(i),{})
            store.update_job(jid,status='blocked')
        one=store.job_page('attention',limit=25)
        self.assertEqual((one['total'],one['pages'],len(one['jobs'])),(25,1,25))
        store.enqueue('download','extra',{})
        two=store.job_page(offset=9999,limit=25)
        self.assertEqual((two['total'],two['page'],two['pages'],two['offset'],len(two['jobs'])),(26,2,2,25,1))
        self.assertEqual(store.job_page('attention')['total'],25)
        self.assertEqual(store.job_page('done')['total'],0)
        store.clear_download_jobs()
        empty=store.job_page(offset=25)
        self.assertEqual((empty['page'],empty['pages'],empty['offset'],empty['cleared_total']),(0,0,0,26))

    def test_clear_restore_all_states_preserves_files_sync_and_blocks_stale_worker(self):
        from hub_store import Store
        from hub_engine import JobControlled
        store=self.e.store
        for ext in ('mp3','lrc','jpg'):(self.e.root/('song.'+ext)).write_bytes(b'keep')
        running=store.enqueue('download','running',{'manual':True})
        job=store.claim();job['_worker']=True
        ids={'running':running}
        for status in ('queued','paused','done','failed','blocked','cancelled','superseded'):
            ids[status]=store.enqueue('download',status,{})
            store.update_job(ids[status],status=status)
        sync=store.enqueue('sync','sync',{})
        self.assertEqual(store.clear_download_jobs()['changed'],8)
        self.assertEqual(store.clear_download_jobs()['changed'],0)
        self.assertFalse(store.update_claimed(job,status='done'))
        with self.assertRaises(JobControlled):self.e.check(job)
        self.assertEqual(store.select_jobs(),[])
        self.assertEqual(store.job_page()['jobs'][0]['id'],sync)
        self.assertEqual(store.bulk_jobs('retry',[ids['blocked']])['changed'],0)
        reopened=Store(store.home)
        self.assertEqual(reopened.job_page()['cleared_total'],8)
        result=reopened.restore_download_jobs()
        self.assertEqual((result['changed'],result['paused']),(8,3))
        for status,jid in ids.items():
            self.assertEqual(reopened.rows('SELECT status FROM jobs WHERE id=?',(jid,))[0]['status'],
                             'paused' if status in ('queued','running','paused') else status)
        self.assertEqual(reopened.restore_download_jobs()['changed'],0)
        for ext in ('mp3','lrc','jpg'):self.assertEqual((self.e.root/('song.'+ext)).read_bytes(),b'keep')

    def test_restore_deduplicates_new_active_download(self):
        store=self.e.store
        old=store.enqueue('download','same',{})
        store.clear_download_jobs()
        new=store.enqueue('download','same',{})
        result=store.restore_download_jobs()
        self.assertEqual(result['superseded'],1)
        self.assertEqual(store.rows('SELECT status FROM jobs WHERE id=?',(old,))[0]['status'],'superseded')
        self.assertEqual(store.rows('SELECT status FROM jobs WHERE id=?',(new,))[0]['status'],'queued')

    def test_bulk_pause_resume_cannot_be_overwritten_by_old_worker(self):
        from hub_engine import JobControlled
        jid=self.e.store.enqueue('download','one',{'manual':True})
        old=self.e.store.claim();old['_worker']=True
        self.assertEqual(self.e.store.bulk_jobs('pause',[jid,jid])['changed'],1)
        with self.assertRaises(JobControlled):self.e.check(old)
        self.assertFalse(self.e.store.update_claimed(old,status='done',progress=100))
        self.assertEqual(self.e.store.bulk_jobs('start',[jid])['changed'],1)
        current=self.e.store.claim(manual_only=True,downloads_paused=True);current['_worker']=True
        self.assertFalse(self.e.store.update_claimed(old,status='failed',error='stale'))
        self.assertTrue(self.e.store.update_claimed(current,status='done',progress=100))
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs')[0]['status'],'done')

    def test_bulk_cancel_is_terminal_for_inflight_worker_and_preserves_file(self):
        file=self.e.root/'existing.mp3';file.write_bytes(b'keep')
        jid=self.e.store.enqueue('download','one',{'manual':True})
        job=self.e.store.claim();job['_worker']=True
        self.e.store.bulk_jobs('cancel',[jid])
        self.assertFalse(self.e.store.update_claimed(job,status='queued',error='network'))
        self.assertEqual(file.read_bytes(),b'keep')
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs')[0]['status'],'cancelled')

    def test_paused_job_survives_restart_and_resync_deduplicates(self):
        from hub_store import Store
        jid=self.e.store.enqueue('download','one',{'manual':True})
        self.e.store.bulk_jobs('pause',[jid])
        reopened=Store(self.e.store.home)
        self.assertEqual(reopened.enqueue('download','one',{}),jid)
        self.assertIsNone(reopened.claim())
        self.assertEqual(len(reopened.rows('SELECT * FROM jobs')),1)

    def test_selected_start_does_not_unpause_other_queue(self):
        self.e.store.enqueue('download','unselected',{'manual':True})
        jid=self.e.store.enqueue('download','selected',{})
        self.e.store.bulk_jobs('start',[jid])
        self.assertEqual(self.e.store.claim(manual_only=True,downloads_paused=True)['id'],jid)
        self.assertIsNone(self.e.store.claim(manual_only=True,downloads_paused=True))

    def test_bulk_mixed_states_skip_done_sync_missing_and_duplicate_ids(self):
        ids=[]
        for status in ('blocked','failed','cancelled','done','queued'):
            jid=self.e.store.enqueue('download',status,{'track_id':'x'})
            self.e.store.update_job(jid,status=status);ids.append(jid)
        sid=self.e.store.enqueue('sync','sync',{'rule_id':'x'});self.e.store.update_job(sid,status='failed')
        result=self.e.store.bulk_jobs('retry',ids+[sid,'missing',ids[0]])
        self.assertEqual((result['changed'],result['skipped']),(3,4))
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(sid,))[0]['status'],'failed')
        self.assertEqual(self.e.store.bulk_jobs('retry',ids)['changed'],0)

    def test_bulk_retry_never_duplicates_newer_paused_job(self):
        old=self.e.store.enqueue('download','same',{});self.e.store.update_job(old,status='cancelled')
        new=self.e.store.enqueue('download','same',{});self.e.store.bulk_jobs('pause',[new])
        self.assertEqual(self.e.store.bulk_jobs('retry',[old])['changed'],0)
        self.assertEqual(self.e.store.retry(old),(new,False))

    def test_start_failed_tasks_uses_current_config_and_clears_previous_result(self):
        ids=[]
        for status in ('blocked','failed','paused','cancelled'):
            jid=self.e.store.enqueue('download',status,{'track_id':status,'quality':'standard'})
            self.e.store.update_job(jid,status=status,error='old access error',source_attempts='[{"name":"old source"}]');ids.append(jid)
        self.e.save_settings({'sync_enabled':False,'downloads_paused':True})
        self.assertEqual(self.e.store.bulk_jobs('start',ids)['changed'],4)
        for jid in ids:
            row=self.e.store.rows('SELECT * FROM jobs WHERE id=?',(jid,))[0]
            self.assertEqual((row['status'],row['error'],row['source_attempts']),('queued','','[]'))
            self.assertTrue(json.loads(row['payload'])['user_started'])
        for _ in ids:self.assertIsNotNone(self.e.store.claim(manual_only=True,downloads_paused=True))
        self.assertEqual(self.e.store.bulk_jobs('start',ids)['changed'],0)

    def test_source_log_cannot_be_overwritten_after_cancel_or_restart(self):
        jid=self.e.store.enqueue('download','log',{'manual':True})
        old=self.e.store.claim();old['_worker']=True
        self.assertTrue(self.e.store.update_claimed(old,source_attempts='[{"name":"source"}]'))
        self.e.store.bulk_jobs('cancel',[jid])
        self.assertFalse(self.e.store.update_claimed(old,source_attempts='[{"name":"stale"}]'))
        self.e.store.retry(jid)
        self.assertFalse(self.e.store.update_claimed(old,source_attempts='[{"name":"stale"}]'))
        self.assertEqual(self.e.store.rows('SELECT source_attempts FROM jobs')[0]['source_attempts'],'[]')

    def test_selection_snapshot_across_pages_excludes_sync_and_new_tasks(self):
        for i in range(60):
            jid=self.e.store.enqueue('download',str(i),{});self.e.store.update_job(jid,status='blocked')
        sid=self.e.store.enqueue('sync','sync',{});self.e.store.update_job(sid,status='blocked')
        ids=[j['id'] for j in self.e.store.select_jobs('attention')]
        self.assertEqual(len(ids),60)
        later=self.e.store.enqueue('download','later',{});self.e.store.update_job(later,status='blocked')
        self.assertEqual(self.e.store.bulk_jobs('retry',ids)['changed'],60)
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(later,))[0]['status'],'blocked')
        with self.assertRaises(ValueError):self.e.store.select_jobs('invalid')

    def test_controlled_transfer_never_uses_fallback(self):
        from hub_engine import JobControlled
        with patch.object(self.e,'transfer_audio',side_effect=JobControlled),patch.object(self.e.providers,'public_netease_audio') as public:
            with self.assertRaises(JobControlled):self.e.transfer_with_fallback({},self.base/'part',{}, {'platform':'netease'},0)
            public.assert_not_called()

    def test_index_existing_files_and_confine_symlinks(self):
        root=self.e.root;(root/'a.mp3').write_bytes(b'a');(root/'sub').mkdir();(root/'sub/b.flac').write_bytes(b'b')
        (self.base/'secret.mp3').write_bytes(b'secret');(root/'escape.mp3').symlink_to(self.base/'secret.mp3')
        fake=NS(File=lambda *a,**k:None)
        with patch.dict(sys.modules,{'mutagen':fake}):self.e.library.scan()
        self.assertEqual(self.e.library.stats()['count'],2)
        with self.assertRaises(ValueError):self.e.library.browse(str(self.base))
        before=self.e.library.page()['tracks']
        with patch.object(self.e.library,'directories',return_value=[str(root/'missing')]):self.e.library.scan()
        self.assertEqual(self.e.library.page()['tracks'],before)
    def test_existing_file_reused_instead_of_duplicate_download(self):
        audio=self.e.root/'existing.mp3';audio.write_bytes(b'audio')
        with patch.dict(sys.modules,{'mutagen':NS(File=lambda *a,**k:NS(tags={'title':['Hello'],'artist':['Singer']},info=NS(length=200)))}):self.e.library.scan()
        tid=self.e.store.upsert_track('qq',{'source_id':'1','title':'Hello','artists':['Singer'],'duration_ms':200000})
        self.assertTrue(self.e.enqueue_download(tid)['skipped'])
        self.assertFalse(self.e.store.rows('SELECT * FROM jobs'))
    def test_sync_not_starved_by_download_backlog(self):
        self.e.store.enqueue('download','d',{})
        sid=self.e.store.enqueue('sync','s',{})
        self.assertEqual(self.e.store.claim()['id'],sid)
        self.assertIsNone(self.e.store.claim(sync_only=True))
        self.assertEqual(self.e.store.claim()['kind'],'download')
    def test_blocked_not_automatically_requeued(self):
        tid=self.e.store.upsert_track('qq',{'source_id':'1','title':'Hello','artists':[]})
        jid=self.e.enqueue_download(tid)['job_id'];self.e.store.update_job(jid,status='blocked')
        self.assertTrue(self.e.enqueue_download(tid)['skipped'])
        self.assertIn('job_id',self.e.enqueue_download(tid,force=True))
    def test_manual_download_precedes_automatic_backlog_but_honors_pause(self):
        self.e.store.enqueue('download','old',{})
        tid=self.e.store.upsert_track('qq',{'source_id':'1','title':'Manual','artists':[]})
        job=self.e.enqueue_download(tid,force=True)['job_id']
        self.assertIsNone(self.e.store.claim(sync_only=True))
        self.assertEqual(self.e.store.claim()['id'],job)
    def test_quality_falls_back_without_using_preview_as_full(self):
        seen=[]
        def resolve(platform,sid,quality,meta,preview):
            seen.append(quality)
            if quality!='standard':raise ValueError('当前音质未授权')
            return {'url':'https://example.com/full.mp3','quality':quality}
        with patch.object(self.e.providers,'_resolve',side_effect=resolve):
            self.assertEqual(self.e.providers.resolve('netease','1','lossless')['quality'],'standard')
        self.assertEqual(seen,['lossless','exhigh','standard'])
    def test_audio_cdn_failure_retries_public_source_and_preserves_metadata(self):
        from urllib.error import HTTPError
        part=self.base/'song.part';part.write_bytes(b'partial')
        source={'platform':'netease','source_id':'1','meta':'{"duration_ms":200000}'}
        alternate={'url':'https://example.com/full','duration_ms':200000,'provider':'musicjx','lyric':'lyrics'}
        with patch.object(self.e,'check'),patch.object(self.e.store,'update_job'),patch.object(self.e.providers,'public_netease_audio',return_value=alternate) as public,patch.object(self.e,'transfer_audio',side_effect=[HTTPError('http://example.com?token=secret',403,'denied',{},None),'validated']) as transfer:
            resolved,audio=self.e.transfer_with_fallback({'id':'job'},part,{'url':'http://example.com'},source,200000)
            self.assertEqual(resolved,alternate);self.assertEqual(audio,'validated')
            self.assertFalse(part.exists());self.assertEqual(transfer.call_count,2)
            public.assert_called_once_with('1',{'duration_ms':200000})
    def test_audio_transfer_cancel_never_requests_fallback(self):
        from hub_engine import Cancelled
        with patch.object(self.e,'transfer_audio',side_effect=Cancelled),patch.object(self.e.providers,'public_netease_audio') as public:
            with self.assertRaises(Cancelled):self.e.transfer_with_fallback({},self.base/'part',{}, {'platform':'netease'},0)
            public.assert_not_called()
    def test_transfer_error_reports_stage_without_signed_url(self):
        from urllib.error import HTTPError
        with patch.object(self.e,'transfer_audio',side_effect=HTTPError('https://example.com?token=secret',403,'denied',{},None)):
            with self.assertRaises(RuntimeError) as ctx:self.e.transfer_with_fallback({},self.base/'part',{'provider':'musicjx'},{'platform':'netease'},0)
            self.assertIn('HTTP 403',str(ctx.exception));self.assertNotIn('secret',str(ctx.exception))
    def test_comment_database_bootstraps_once(self):
        p=self.base/'comments.db';self.assertTrue(ensure_comments(p));before=p.read_bytes()
        self.assertFalse(ensure_comments(p));self.assertEqual(before,p.read_bytes())
    def test_export_failure_does_not_invalidate_saved_audio(self):
        with patch.object(self.e,'export',side_effect=ValueError('分类目录存在同名不同文件，请更换目录')),patch.object(self.e.library,'start') as scan:
            self.e.export_after_download()
        self.assertIn('音频已保存',self.e.store.pipeline()['message']);scan.assert_called_once()
    def test_old_export_failure_requires_valid_complete_file(self):
        tid=self.e.store.upsert_track('netease',dict(source_id='1',title='Saved',artists=['Singer'],duration_ms=200000))
        file=self.e.root/'saved.mp3';file.write_bytes(b'unchanged')
        with self.e.store.connect() as db:db.execute("UPDATE tracks SET path='saved.mp3' WHERE id=?",(tid,))
        jid=self.e.store.enqueue('download','download:'+tid,{'track_id':tid,'manual':True})
        self.e.store.update_job(jid,status='blocked',error='[request] 分类目录存在同名不同文件，请更换目录')
        with patch.dict(sys.modules,{'mutagen':NS(File=lambda *a,**k:NS(info=NS(length=30)))}):self.e.reconcile_download_records()
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(jid,))[0]['status'],'blocked')
        with patch.dict(sys.modules,{'mutagen':NS(File=lambda *a,**k:NS(info=NS(length=200)))}):self.e.reconcile_download_records()
        self.assertEqual(self.e.store.rows('SELECT status,progress FROM jobs WHERE id=?',(jid,))[0],{'status':'done','progress':100})
        self.assertEqual(file.read_bytes(),b'unchanged')
    def test_old_unselected_export_failure_cancelled_without_touching_music(self):
        file=self.e.root/'keep.mp3';file.write_bytes(b'original')
        jid=self.e.store.enqueue('download','download:unselected',{'track_id':'unselected'})
        self.e.store.update_job(jid,status='blocked',error='分类目录存在同名不同文件')
        with patch.dict(sys.modules,{'mutagen':NS(File=lambda *a:None)}):self.e.reconcile_download_records()
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(jid,))[0]['status'],'cancelled')
        self.assertEqual(file.read_bytes(),b'original')
    def test_record_repair_never_reads_outside_music_root(self):
        tid=self.e.store.upsert_track('netease',dict(source_id='1',title='Saved',artists=['Singer']))
        outside=self.base/'secret.mp3';outside.write_bytes(b'keep');(self.e.root/'escape.mp3').symlink_to(outside)
        with self.e.store.connect() as db:db.execute("UPDATE tracks SET path='escape.mp3' WHERE id=?",(tid,))
        jid=self.e.store.enqueue('download','download:'+tid,{'track_id':tid,'manual':True})
        self.e.store.update_job(jid,status='blocked',error='分类目录存在同名不同文件')
        from unittest.mock import Mock
        read=Mock()
        with patch.dict(sys.modules,{'mutagen':NS(File=read)}):self.e.reconcile_download_records()
        read.assert_not_called();self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(jid,))[0]['status'],'blocked')
    def test_errors_actionable_without_signed_urls(self):
        self.assertFalse(describe(ValueError('平台仅返回试听片段'))[2])
        self.assertTrue(describe(TimeoutError('connection timed out'))[2])
        self.assertNotIn('secret',describe(RuntimeError('https://example.com/?token=secret'))[1])
    def test_upstream_network_error_keeps_stage_and_safe_reason(self):
        from hub_errors import UpstreamFailure
        from urllib.error import URLError
        code,text,retry=describe(UpstreamFailure('网易云公开备用解析',URLError(TimeoutError('https://example.com/?token=secret'))))
        self.assertEqual(code,'network');self.assertTrue(retry)
        self.assertIn('公开备用解析',text);self.assertIn('TimeoutError',text);self.assertNotIn('secret',text)
    def test_upstream_http_and_certificate_classification_survives_wrapping(self):
        from hub_errors import UpstreamFailure
        from urllib.error import HTTPError,URLError
        import ssl
        code,text,retry=describe(UpstreamFailure('备用音频传输',HTTPError('https://example.com?token=secret',429,'slow',{},None)))
        self.assertEqual(code,'rate_limit');self.assertTrue(retry);self.assertIn('HTTP 429',text);self.assertNotIn('secret',text)
        code,text,retry=describe(UpstreamFailure('备用音频传输',URLError(ssl.SSLCertVerificationError('issuer'))))
        self.assertEqual(code,'tls');self.assertTrue(retry);self.assertIn('备用音频传输',text)
    def test_tracker_response_does_not_require_playlist_status(self):
        import kugou
        with patch('kugou.urllib.request.build_opener') as make:
            response=make.return_value.open.return_value.__enter__.return_value
            response.read.return_value=b'{"url":["https://example.com/audio"],"error_code":0}'
            self.assertIn('url',kugou.request('/v5/url'))
            with self.assertRaises(ValueError):kugou.request('/v2/search/song')
            response.read.return_value=b'{"status":2,"priv_status":1,"auth_through":0}'
            self.assertEqual(kugou.request('/v5/url')['status'],2)
            response.read.return_value=b'{"url":[],"status":0,"error_code":100}'
            with self.assertRaises(ValueError):kugou.request('/v5/url')
    def test_old_mp3_tags_repaired_without_changing_unicode(self):
        from hub_library import display_tag
        self.assertEqual(display_tag('天空'.encode('gbk').decode('latin1')),'天空')
        self.assertEqual(display_tag('   ','歌曲.mp3'),'歌曲.mp3')
        self.assertEqual(display_tag('Beyoncé'),'Beyoncé')

    def test_comments_query_reads_bodies_matches_title_and_deduplicates(self):
        import sqlite3
        from hub_comments import query
        path=self.base/'comments.db';ensure_comments(path)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO tracks(guid,title,artists_json,first_seen,updated_at) VALUES('t','摇篮曲','[\"回音哥\"]',1,1)")
            for platform,likes in [('qq',2),('netease',8)]:
                db.execute("INSERT INTO comments(source,track_guid,song_id,comment_id,content,author,likes,content_hash_v2,first_seen,last_seen) VALUES(?,'t','1','c','喜欢这首歌','听众',?,'same',1,10)",(platform,likes))
        result=query(path,guid='t')
        self.assertEqual(result['count'],1)
        self.assertEqual(result['comments'][0]['content'],'喜欢这首歌')
        self.assertEqual(result['comments'][0]['sources'],['netease','qq'])
        self.assertEqual(result['comments'][0]['likes'],8)
        self.assertEqual(query(path,title='摇篮曲',artist='回音哥')['trackGuid'],'t')
        self.assertEqual(query(path,title='其他歌曲')['count'],0)

    def test_retry_resets_same_job_and_does_not_duplicate(self):
        jid=self.e.store.enqueue('download','d',{})
        self.e.store.update_job(jid,status='failed',error='[network] timeout')
        with self.e.store.connect() as db:db.execute('UPDATE jobs SET attempts=3 WHERE id=?',(jid,))
        self.assertEqual(self.e.store.retry(jid),(jid,True))
        self.assertEqual(self.e.store.retry(jid),(jid,False))
        rows=self.e.store.rows('SELECT * FROM jobs')
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['status'],rows[0]['attempts'],rows[0]['due']),('queued',0,0))

    def test_prior_failures_retained_as_history_after_new_attempt(self):
        old=self.e.store.enqueue('download','d',{})
        self.e.store.update_job(old,status='failed')
        new=self.e.store.enqueue('download','d',{})
        self.assertNotEqual(old,new)
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(old,))[0]['status'],'superseded')
        # Simulate a pre-fix database and verify startup reconciliation.
        self.e.store.update_job(old,status='failed')
        from hub_store import Store
        Store(self.e.store.home)
        self.assertEqual(self.e.store.rows('SELECT status FROM jobs WHERE id=?',(old,))[0]['status'],'superseded')

try:
    from fastapi import FastAPI
    import asyncio
    from urllib.parse import urlsplit
    class TestClient:
        """Exercise real ASGI routing without an optional HTTP test dependency."""
        def __init__(self, app):
            self.app=app;self.loop=asyncio.new_event_loop()
            class Cookies(dict):
                def set(self,k,v):self[k]=v
            self.cookies=Cookies();self.headers={}
        def __enter__(self):
            self.lifespan=self.app.router.lifespan_context(self.app)
            self.loop.run_until_complete(self.lifespan.__aenter__());return self
        def __exit__(self,*args):
            self.loop.run_until_complete(self.lifespan.__aexit__(*args));self.loop.close()
        def request(self,method,url,**kwargs):
            target=urlsplit(url);payload=json.dumps(kwargs['json']).encode() if 'json' in kwargs else b''
            headers={'host':'testserver',**self.headers,**kwargs.get('headers',{})}
            if payload:headers.update({'content-type':'application/json','content-length':str(len(payload))})
            if self.cookies:headers['cookie']='; '.join(k+'='+v for k,v in self.cookies.items())
            scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
              'method':method,'scheme':'http','path':target.path,'raw_path':target.path.encode(),
              'query_string':target.query.encode(),'root_path':'','headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()],
              'client':('127.0.0.1',123),'server':('testserver',80)}
            messages=[]
            async def receive():return {'type':'http.request','body':payload,'more_body':False}
            async def send(message):messages.append(message)
            self.loop.run_until_complete(self.app(scope,receive,send))
            status=next(m['status'] for m in messages if m['type']=='http.response.start')
            body=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
            return NS(status_code=status,content=body,json=lambda:json.loads(body))
        def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
        def post(self,url,**kwargs):return self.request('POST',url,**kwargs)
        def put(self,url,**kwargs):return self.request('PUT',url,**kwargs)
    HAS_HTTP=True
except ImportError:HAS_HTTP=False

@unittest.skipUnless(HAS_HTTP,'FastAPI checks run inside the Docker build')
class ApiRepairs(unittest.TestCase):
    def setUp(self):
        from hub_api import install
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        home=Path(self.tmp.name)
        class Auth:
            def session_info(self,r):return {'authenticated':bool(r.cookies.get('bridge_session'))}
            def authorize_write(self,r):return r.headers.get('x-bridge-csrf')=='ok','csrf'
        self.env=patch.dict(os.environ,{'MUSIC_DIR':str(home/'music')});self.env.start();self.addCleanup(self.env.stop)
        self.app=FastAPI();self.state=install(self.app,{'HOME':home/'data','AUTH':{'auth':Auth()},'resolve_platform_playlists':lambda *a:None,'tokens':lambda:{'wmh':'private'}})
        with patch('hub_engine.Engine.start'):
            self.c=TestClient(self.app);self.c.__enter__()
        self.addCleanup(lambda:self.c.__exit__(None,None,None))
        self.e=self.state['engine'];self.c.cookies.set('bridge_session','owner');self.c.headers['x-bridge-csrf']='ok'
    def test_payment_codes_cannot_be_replaced_via_api(self):
        for method in ('get','put','post'):
            response=getattr(self.c,method)('/api/hub/support')
            self.assertEqual(response.status_code,404)
        self.assertEqual(self.c.get('/api/hub/support/image/alipay').status_code,404)

    def test_comment_directory_routes_and_write_protection(self):
        folder=self.e.root/'评论测试';folder.mkdir()
        with patch.object(self.e.comment_directories,'scan_start',return_value=True):
            result=self.c.post('/api/hub/comment-directories',json={'path':str(folder)})
            self.assertEqual(result.status_code,200);key=result.json()['id']
            self.assertEqual(self.c.get('/api/hub/comment-directories').json()['directories'][0]['enabled'],0)
            self.assertEqual(self.c.post('/api/hub/comment-directories/'+key,json={'action':'resume'}).status_code,200)
            self.assertEqual(self.c.get('/api/hub/comment-directories').json()['directories'][0]['enabled'],1)
            self.assertEqual(self.c.post('/api/hub/comment-directories/'+key,json={'action':'pause'}).status_code,200)
            self.assertEqual(self.c.post('/api/hub/comment-directories',json={'path':'/etc'}).status_code,400)
            self.assertEqual(self.c.post('/api/hub/comment-directories/'+key,json={'action':'delete_files'}).status_code,422)
            self.c.headers.pop('x-bridge-csrf')
            self.assertEqual(self.c.post('/api/hub/comment-directories/'+key,json={'action':'resume'}).status_code,403)
            self.c.cookies.clear();self.assertEqual(self.c.get('/api/hub/comment-directories').status_code,401)

    def test_paged_jobs_clear_restore_and_write_protection(self):
        for i in range(26):self.e.store.enqueue('download',str(i),{'track_id':'missing'})
        page=self.c.get('/api/hub/jobs?paged=true&limit=25&offset=25').json()
        self.assertEqual((page['page'],page['pages'],page['total'],len(page['jobs'])),(2,2,26,1))
        self.assertNotIn('payload',page['jobs'][0])
        self.assertIsInstance(self.c.get('/api/hub/jobs').json(),list)
        self.assertEqual(self.c.post('/api/hub/jobs/clear').json()['changed'],26)
        self.assertEqual(self.c.get('/api/hub/jobs').json(),[])
        self.assertEqual(self.c.get('/api/hub/jobs/selection').json()['total'],0)
        self.assertEqual(self.c.get('/api/hub/stats').json()['jobs'],[])
        self.assertEqual(self.c.get('/api/hub/jobs?paged=true').json()['cleared_total'],26)
        self.assertEqual(self.c.post('/api/hub/jobs/restore').json()['paused'],26)
        self.c.headers.pop('x-bridge-csrf')
        for endpoint in ('clear','restore'):
            self.assertEqual(self.c.post('/api/hub/jobs/'+endpoint).status_code,403)
        self.c.cookies.clear()
        for endpoint in ('clear','restore'):
            self.assertEqual(self.c.post('/api/hub/jobs/'+endpoint).status_code,401)

    def test_bulk_routes_selection_pause_start_cancel_and_auth(self):
        jid=self.e.store.enqueue('download','one',{'track_id':'missing'})
        self.assertEqual(self.c.get('/api/hub/jobs/selection?status=queued').json()['ids'],[jid])
        for action,status in [('pause','paused'),('start','queued'),('cancel','cancelled'),('retry','queued')]:
            result=self.c.post('/api/hub/jobs/bulk',json={'action':action,'ids':[jid]})
            self.assertEqual(result.status_code,200);self.assertEqual(result.json()['changed'],1)
            self.assertEqual(self.c.get('/api/hub/jobs?status='+status).json()[0]['id'],jid)
        self.assertEqual(self.c.post('/api/hub/jobs/bulk',json={'action':'delete','ids':[jid]}).status_code,422)
        self.assertEqual(self.c.post('/api/hub/jobs/bulk',json={'action':'pause','ids':[]}).status_code,422)
        self.c.headers.pop('x-bridge-csrf')
        self.assertEqual(self.c.post('/api/hub/jobs/bulk',json={'action':'pause','ids':[jid]}).status_code,403)
        self.c.cookies.clear();self.assertEqual(self.c.get('/api/hub/jobs/selection').status_code,401)

    def test_source_order_api_validation_persistence_and_auth(self):
        rows=self.c.get('/api/hub/sources').json()['sources'];ids=[x['id'] for x in rows][::-1]
        response=self.c.put('/api/hub/sources/order',json={'ids':ids})
        self.assertEqual(response.status_code,200)
        self.assertEqual([r['id'] for r in self.c.get('/api/hub/sources').json()['sources']],ids)
        self.assertEqual(self.c.put('/api/hub/sources/order',json={'ids':ids[:-1]}).status_code,400)
        self.c.headers.pop('x-bridge-csrf')
        self.assertEqual(self.c.put('/api/hub/sources/order',json={'ids':ids}).status_code,403)
        self.c.cookies.clear()
        self.assertEqual(self.c.put('/api/hub/sources/order',json={'ids':ids}).status_code,401)

    def test_shared_download_quality(self):
        self.assertEqual(self.c.put('/api/hub/settings',json={'download_quality':'standard'}).status_code,200)
        self.assertEqual(self.c.get('/api/hub/settings').json()['download_quality'],'standard')
    def test_settings_partial_update_preserves_paths_and_rejects_escape(self):
        self.assertEqual(self.c.put('/api/hub/settings',json={'fn_user':'wmh','fn_music_root':'/vol/Music','fn_auto':True}).status_code,200)
        self.assertEqual(self.c.put('/api/hub/settings',json={'downloads_paused':True}).status_code,200)
        data=self.c.get('/api/hub/settings').json();self.assertEqual(data['fn_music_root'],'/vol/Music');self.assertTrue(data['fn_auto'])
        self.assertEqual(self.c.get('/api/hub/directories?path=/etc').status_code,400)
        self.assertEqual(self.c.put('/api/hub/settings',json={'library_dirs':['/etc']}).status_code,400)
    def test_group_updates_same_rules_used_by_pipeline(self):
        r={'name':'test','platform':'netease','ref':'123','group_name':'old','auto_download':False}
        rid=self.c.post('/api/hub/rules',json=r).json()['id']
        self.assertEqual(self.c.put('/api/hub/group',json={'name':'new','rules':[rid],'directory':'new'}).status_code,200)
        self.assertEqual(self.e.store.rule(rid)['group_name'],'new')
        self.assertEqual(self.c.get('/api/hub/groups').json()[0]['group_name'],'new')
    def test_unchecked_subscription_leaves_previous_fusion_group(self):
        ids=[]
        for name in ('A','B'):
            ids.append(self.c.post('/api/hub/rules',json={'name':name,'platform':'netease','ref':name,'group_name':'shared','auto_download':False}).json()['id'])
        response=self.c.put('/api/hub/group',json={'name':'renamed','previous_name':'shared','rules':[ids[0]],'directory':'renamed'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.e.store.rule(ids[0])['group_name'],'renamed')
        self.assertNotIn(self.e.store.rule(ids[1])['group_name'],('shared','renamed'))
    def test_file_range_and_auth(self):
        file=self.e.root/'song.mp3';file.write_bytes(b'abcdefghij')
        with patch.dict(sys.modules,{'mutagen':NS(File=lambda *a,**k:None)}):self.e.library.scan()
        fid=self.c.get('/api/hub/files').json()['tracks'][0]['id']
        r=self.c.get('/api/hub/file-audio/'+fid,headers={'Range':'bytes=2-5'})
        self.assertEqual(r.status_code,206);self.assertEqual(r.content,b'cdef')
        self.c.cookies.clear();self.assertEqual(self.c.get('/api/hub/file-audio/'+fid).status_code,401)
    def test_comment_browser_uses_only_tracks_with_saved_comments(self):
        import sqlite3
        path=self.e.store.home/'commenthub.db';ensure_comments(path)
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO tracks(guid,title,artists_json,first_seen,updated_at) VALUES('yes','Song','[]',1,1),('empty','Empty','[]',1,1)")
            db.execute("INSERT INTO comments(source,track_guid,song_id,comment_id,content,content_hash_v2,first_seen,last_seen) VALUES('qq','yes','1','c','hello','h',1,10)")
        with patch.dict(os.environ,{'COMMENTHUB_DB':str(path)}):
            rows=self.c.get('/api/hub/commented-tracks').json()
            result=self.c.get('/api/hub/comments?trackGuid=yes').json()
            self.assertEqual(result['comments'][0]['content'],'hello')
            self.assertEqual(self.c.get('/api/hub/comments?limit=10000').status_code,422)
            self.c.cookies.clear()
            self.assertEqual(self.c.get('/api/hub/comments?trackGuid=yes').status_code,401)
        self.assertEqual([(r['guid'],r['count']) for r in rows],[('yes',1)])
    def test_jobs_filter_and_preview_ticket_owner(self):
        jid=self.e.store.enqueue('download','d',{'track_id':'missing'});self.e.store.update_job(jid,status='blocked')
        self.assertEqual(len(self.c.get('/api/hub/jobs?status=attention').json()),1)
        self.assertEqual(self.c.get('/api/hub/jobs?status=done').json(),[])
        track={'platform':'netease','source_id':'1','title':'Test','artists':[]}
        with patch.object(self.e.providers,'resolve',return_value={'url':'https://example.com/audio','preview':True}),patch('hub_playback.public_audio_url'):
            r=self.c.post('/api/hub/preview',json=track);self.assertEqual(r.status_code,200)
        self.c.cookies.set('bridge_session','other');self.assertEqual(self.c.get(r.json()['url']).status_code,404)

    def test_bulk_retry_removes_failure_count_and_keeps_access_blocks(self):
        for status,error,dedupe in [('failed','[network] timeout','net'),('blocked','[access] denied','vip')]:
            jid=self.e.store.enqueue('download',dedupe,{'track_id':'missing'})
            self.e.store.update_job(jid,status=status,error=error)
        self.assertEqual(self.c.post('/api/hub/jobs/retry-network',json={}).json()['count'],1)
        self.assertEqual(self.c.post('/api/hub/jobs/retry-network',json={}).json()['count'],0)
        self.assertEqual(self.c.get('/api/hub/jobs?status=failed').json(),[])
        self.assertEqual(len(self.c.get('/api/hub/jobs?status=attention').json()),1)

if __name__=='__main__':unittest.main()

class EnhancementAssets(unittest.TestCase):
    def test_artwork_rejects_html_and_oversized_response(self):
        import io
        import hub_artwork
        with patch('hub_artwork.open_audio',return_value=io.BytesIO(b'<html>error</html>')):
            with self.assertRaises(ValueError):hub_artwork.fetch('https://cdn.example/cover')
        with patch('hub_artwork.open_audio',return_value=io.BytesIO(b'x'*(8*1024*1024+1))):
            with self.assertRaises(ValueError):hub_artwork.fetch('https://cdn.example/cover')
        self.assertIsNone(hub_artwork.fetch(''))

    @unittest.skipUnless(importlib.util.find_spec('mutagen'), 'mutagen is installed in the Docker image')
    def test_cover_is_embedded_as_front_cover(self):
        import base64
        import wave
        import mutagen
        import hub_artwork
        data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a30cAAAAASUVORK5CYII=')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'song.wav'
            with wave.open(str(path),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(b'\0'*16000)
            hub_artwork.embed(path,(data,'image/png','.png'))
            picture=mutagen.File(path).tags.getall('APIC')[0]
            self.assertEqual(picture.type,3);self.assertEqual(picture.mime,'image/png');self.assertEqual(picture.data,data)

    def test_comment_gateway_uses_loopback_without_browser_cookies(self):
        import ast
        import http.client
        source=Path(__file__).resolve().parents[1]/'app/webapp.py'
        # The build copies tests outside /app. Resolve the application source there.
        if not source.is_file():source=Path('/app/webapp.py')
        tree=ast.parse(source.read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_ch_request')
        namespace={'COMMENTHUB_HOST':'127.0.0.1','COMMENTHUB_PORT':8770,'CH':{'token':'internal'}}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
        from unittest.mock import MagicMock
        connection=MagicMock();connection.getresponse.return_value.status=200
        connection.getresponse.return_value.getheaders.return_value=[]
        connection.getresponse.return_value.read.return_value=b'ok'
        with patch.object(http.client,'HTTPConnection',return_value=connection):
            result=namespace['_ch_request']('GET','/panel.js','',{'cookie':'private','host':'external','accept':'*/*'},b'')
        self.assertEqual(result,(200,{},b'ok'))
        self.assertEqual(connection.request.call_args.kwargs['headers'],{'accept':'*/*','X-Fnhub-Token':'internal'})
        connection.close.assert_called_once()

@unittest.skipUnless(importlib.util.find_spec('requests') and importlib.util.find_spec('cryptography'),'network client dependencies required')
class NeteaseResolverTests(unittest.TestCase):
    def test_transport_failure_and_trial_do_not_hide_full_fallback(self):
        from netease_provider import NeteaseAPI
        api=NeteaseAPI()
        with patch.object(api,'get_client_audio',side_effect=TimeoutError),patch.object(api,'get_song_url',return_value={'data':[{'url':'trial','freeTrialInfo':{'end':30}}]}),patch.object(api,'get_web_song_url',return_value={'data':[{'url':'full','type':'mp3'}]}):
            self.assertEqual(api.resolve_audio(1,'standard',{})['url'],'full')
    def test_trial_not_saved_as_full(self):
        from netease_provider import NeteaseAPI
        api=NeteaseAPI();trial={'data':[{'url':'trial','freeTrialInfo':{'end':30}}]}
        with patch.object(api,'get_client_audio',return_value=trial),patch.object(api,'get_song_url',return_value=trial),patch.object(api,'get_web_song_url',return_value=trial),patch.object(api,'get_legacy_song_url',return_value=trial):
            with self.assertRaisesRegex(ValueError,'试听'):api.resolve_audio(1,'standard',{})
            self.assertEqual(api.resolve_audio(1,'standard',{},preview=True)['url'],'trial')
    def test_qr_key_does_not_reference_undefined_cookie(self):
        from netease_provider import QRLoginManager
        api=QRLoginManager()
        with patch.object(api.crypto_utils,'encrypt_params',return_value='test'),patch.object(api.http_client,'post_request_full',return_value=NS(text='{"code":200,"unikey":"key"}')):
            self.assertEqual(api.generate_qr_key(),'key')


class PublicNeteaseTests(unittest.TestCase):
    def test_same_id_public_source_and_duration_required(self):
        from hub_providers import Providers
        p=Providers(lambda:{},lambda *a:None)
        raw={'ok':True,'song':{'provider':'netease','id':'1','duration':200,'audioUrl':'https://example.com/a','format':'mp3','bitrate':128000}}
        with patch('hub_providers.json_request',return_value=raw),patch('hub_providers.public_audio_url'):
            self.assertEqual(p.public_netease_audio('1',{'duration_ms':200000})['quality'],'standard')
            with self.assertRaises(ValueError):p.public_netease_audio('2',{})
            with self.assertRaises(ValueError):p.public_netease_audio('1',{'duration_ms':300000})
            raw['song']['duration']=0
            with self.assertRaises(ValueError):p.public_netease_audio('1',{})
