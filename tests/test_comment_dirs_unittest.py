"""Directory checkpoint, cancellation and comment pacing regression coverage."""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from hub_engine import Engine
from hub_comment_dirs import CommentDirectories

class CommentDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=Engine(Path(self.tmp.name)/'data',Path(self.tmp.name)/'music',lambda *a:None)
        self.d=self.e.comment_directories;self.addCleanup(lambda:self.d.pool.shutdown(wait=True))
        self.tasks={};self.submitted=[];self.cancelled=[]
        def submit(kind,args):
            key=str(len(self.submitted));self.submitted.append(args)
            task={'id':key,'status':'running','result':{}};self.tasks[key]=task;return task
        def cancel(key):self.cancelled.append(key);self.tasks[key]['status']='cancelled'
        self.e.bridge={'BRIDGE':{'store':NS(get=lambda key:self.tasks.get(key)),'manager':NS(cancel=cancel)},'submit_task':submit}
    def add(self,path):
        with patch.object(self.d,'scan_start',return_value=True):key=self.d.add(str(path))
        with patch('mutagen.File',return_value=NS(tags={'title':['song'],'artist':['artist']},info=NS(length=200))):self.d.scan(key)
        return key
    def file(self,path):
        p=self.e.root/path;p.parent.mkdir(exist_ok=True,parents=True);p.write_bytes(b'audio');return p
    def enable(self,key):
        with patch.object(self.d,'scan_start',return_value=True):self.d.control(key,'resume')
    def checkpoint(self,done=True):
        row=self.e.store.rows('SELECT * FROM comment_files WHERE task_id IS NOT NULL')[0]
        self.d.catalog([row['id']]);row=self.e.store.rows('SELECT * FROM comment_files WHERE id=?',(row['id'],))[0]
        self.tasks[row['task_id']]['result']={'track_results':{row['comment_guid']:{'status':'done' if done else 'failed','error':'source unavailable'}}}
        return row
    def test_recursive_scan_paused_default_idempotent_and_symlink_confined(self):
        self.file('A/nested/song.mp3');self.file('A/other.lrc')
        outside=Path(self.tmp.name)/'outside';outside.mkdir();(outside/'other.mp3').write_bytes(b'no')
        (self.e.root/'A/link').symlink_to(outside,target_is_directory=True)
        key=self.add(self.e.root/'A');self.assertEqual(key,self.add(self.e.root/'A'))
        data=self.d.status()['directories'];self.assertEqual(len(data),1)
        self.assertEqual((data[0]['total'],data[0]['enabled']),(1,0));self.assertFalse(self.d.pump())
        with self.assertRaises(ValueError):self.d.add(str(outside))
    def test_overlapping_directories_share_work_and_restart_checkpoints(self):
        self.file('A/B/song.mp3');a=self.add(self.e.root/'A');b=self.add(self.e.root/'A/B')
        self.enable(a);self.enable(b);self.assertTrue(self.d.pump());self.assertTrue(self.d.pump())
        self.assertEqual(len(self.submitted),1);row=self.checkpoint()
        self.tasks[row['task_id']]['status']='succeeded';self.assertFalse(self.d.pump())
        self.assertEqual([x['done'] for x in self.d.status()['directories']],[1,1])
        again=CommentDirectories(self.e);self.addCleanup(lambda:again.pool.shutdown(wait=True))
        self.assertFalse(again.pump());self.assertEqual([x['done'] for x in again.status()['directories']],[1,1])
    def test_pause_one_directory_leaves_other_running_and_resume_keeps_checkpoint(self):
        self.file('A/song.mp3');self.file('B/song.mp3');a=self.add(self.e.root/'A');b=self.add(self.e.root/'B')
        self.enable(a);self.d.pump();row=self.checkpoint();self.d.control(a,'pause')
        self.assertEqual(self.cancelled,[row['task_id']]);self.enable(b);self.d.pump()
        status={x['id']:x for x in self.d.status()['directories']}
        self.assertEqual((status[a]['enabled'],status[a]['done'],status[b]['running']),(0,1,1))
        self.enable(a);self.assertEqual(len(self.submitted),2)
    def test_partial_does_not_mark_failure_completed_and_obeys_backoff(self):
        self.file('A/song.mp3');a=self.add(self.e.root/'A');self.enable(a);self.d.pump();row=self.checkpoint(False)
        self.tasks[row['task_id']]['status']='partial';self.assertFalse(self.d.pump())
        data=self.d.status()['directories'][0];self.assertEqual((data['done'],data['failed']),(0,1))
        self.assertIn('source unavailable',data['detail']);self.enable(a);self.d.pump();self.assertEqual(len(self.submitted),2)
    def test_cancelled_unfinished_is_pending_and_not_restarted_while_paused(self):
        self.file('A/song.mp3');a=self.add(self.e.root/'A');self.enable(a);self.d.pump();self.d.control(a,'pause')
        self.assertFalse(self.d.pump());self.assertEqual(self.d.status()['directories'][0]['pending'],1)
        self.enable(a);self.assertTrue(self.d.pump());self.assertEqual(len(self.submitted),2)
    def test_active_progress_and_completed_checkpoint_are_exposed(self):
        self.file('A/song.mp3');a=self.add(self.e.root/'A');self.enable(a);self.d.pump()
        self.tasks['0']['result']={'progress':[{'done':0,'total':1,'song_name':'song','rows_new':18,'elapsed_s':8,'eta_s':12}]}
        data=self.d.status()['directories'][0];self.assertEqual(data['progress']['rows_new'],18)
        self.assertEqual(data['running'],1)
    def test_scan_failure_preserves_previous_members_and_cached_tags(self):
        p=self.file('A/song.mp3');a=self.add(self.e.root/'A')
        with patch('mutagen.File',side_effect=AssertionError('unchanged file must reuse metadata')):self.d.scan(a)
        self.assertEqual(self.d.status()['directories'][0]['total'],1)
        with patch('os.walk',side_effect=PermissionError('denied')):self.d.scan(a)
        data=self.d.status()['directories'][0];self.assertEqual(data['total'],1);self.assertTrue(data['error'])
    def test_missing_task_after_restart_is_requeued_without_losing_members(self):
        self.file('A/song.mp3');a=self.add(self.e.root/'A');self.enable(a);self.d.pump();self.tasks.clear()
        self.d.pump();self.assertEqual(len(self.submitted),2);self.assertEqual(self.d.status()['directories'][0]['total'],1)
    def test_directory_pause_takes_precedence_over_download_backfill(self):
        import hub_auto_comments
        self.file('A/song.mp3');key=self.add(self.e.root/'A')
        tid=self.e.store.upsert_track('netease',{'source_id':'1','title':'song','artists':['artist']})
        with self.e.store.connect() as db:db.execute("UPDATE tracks SET path='A/song.mp3' WHERE id=?",(tid,))
        hub_auto_comments.enqueue(self.e,tid);hub_auto_comments.pump(self.e)
        self.assertEqual(self.submitted,[])
        self.enable(key);hub_auto_comments.pump(self.e)
        self.assertIn('hub_comment_files',self.submitted[0])
    def test_overlap_live_checkpoint_counts_only_member_files(self):
        self.file('A/one.mp3');self.file('A/B/two.mp3');a=self.add(self.e.root/'A');b=self.add(self.e.root/'A/B')
        self.enable(a);self.d.pump();ids=self.submitted[0]['hub_comment_files'];self.d.catalog(ids)
        rows=self.e.store.rows('SELECT * FROM comment_files');outside=next(r for r in rows if r['path'].endswith('one.mp3'))
        self.tasks['0']['result']={'progress':[{'track_results':{outside['comment_guid']:{'status':'done'}}}]}
        states={r['id']:r for r in self.d.status()['directories']}
        self.assertEqual(states[a]['completed_inflight'],1);self.assertEqual(states[b]['completed_inflight'],0)

    def test_task_manager_preserves_directory_checkpoints(self):
        from bridge_tasks import TaskManager
        result={'track_results':{'one':{'status':'done'}}}
        self.assertEqual(TaskManager._public_result(result),result)

class CollectorPacingTests(unittest.TestCase):
    def test_real_ingest_single_pacing_progress_and_skipped_checkpoint(self):
        from hub_bootstrap import ensure_comments
        import hub_engine
        module_path=Path(hub_engine.__file__).resolve().parent/'commenthub/tools/refresh_v2.py'
        spec=importlib.util.spec_from_file_location('directory_refresh_test',module_path)
        refresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(refresh)
        from adapters.base import Comment,CommentPage
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'comments.db';ensure_comments(db)
            con=sqlite3.connect(db)
            for i in range(6):
                con.execute('INSERT INTO tracks(guid,title,artists_json,duration_ms,first_seen,updated_at) VALUES(?,?,?,?,?,?)',(str(i),'song'+str(i),'["artist"]',200000,1,1))
                con.execute("INSERT INTO platform_mappings(track_guid,source,song_id,song_name,song_artist,duration_ms,grade) VALUES(?,'netease',?,?,?,200000,'high')",(str(i),str(i),'song'+str(i),'artist'))
            con.commit();con.close()
            catalog=Path(tmp)/'catalog.json';catalog.write_text(json.dumps([{'guid':str(i)} for i in range(6)]))
            acquired=[];sleeps=[];logs=[]
            class Pacer:
                def __init__(self,interval,**kw):self.interval=interval
                def acquire(self):acquired.append(self.interval)
                def penalize(self,*args):pass
            class Adapter:
                def __init__(self,source,**kw):self.pacer=kw['pacer']
                def fetch_comments(self,song,**kw):
                    self.pacer.acquire()
                    c=Comment('netease','c'+song.song_id,'This is a long enough comment for song '+song.song_id,'user','u',2,0,1,None,True)
                    yield CommentPage((c,),1,1,False,1)
            with patch.object(refresh.pacing,'Pacer',Pacer),contextlib.redirect_stdout(io.StringIO()):
                stats,code=refresh.run_full_library(str(db),sources=('netease',),comment_count=1,batch_size=1,catalog_file=str(catalog),adapter_factory=Adapter,sleep_fn=sleeps.append,log=logs.append)
                self.assertEqual((code,stats['rows_new'],stats['songs_done']),(0,6,6))
                self.assertEqual(acquired,[2.0]*6);self.assertEqual(sleeps,[])
                self.assertTrue(any('"rows_new": 1' in l and l.startswith('@@P ') for l in logs))
                self.assertTrue(all(r['status']=='done' for r in stats['track_results'].values()))
                stats2,code=refresh.run_full_library(str(db),sources=('netease',),comment_count=1,catalog_file=str(catalog),adapter_factory=Adapter,sleep_fn=sleeps.append,log=lambda *a:None)
                self.assertEqual(stats2['songs_total'],0);self.assertEqual(len(stats2['track_results']),6)
                self.assertEqual(len(acquired),6)
