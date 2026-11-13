"""Real QuickJS/subprocess tests, fake network only; never requests user sources."""
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from hub_js_sources import inspect_script,invoke,music_info,resolve_js
from hub_sources import Sources
from lx_worker import run,destination,request

INIT="lx.send(lx.EVENT_NAMES.inited,{sources:{tx:{actions:['musicUrl'],qualitys:['128k','320k','flac']},wy:{actions:['musicUrl'],qualitys:['128k']}}});"
SIMPLE="/** @name 测试 JS */\nlx.on(lx.EVENT_NAMES.request,p=>'https://audio.example/song.mp3');"+INIT
REMOTE="lx.on('request',p=>new Promise((resolve,reject)=>{lx.request('https://source.example/resolve',{method:'POST',body:p.info.musicInfo},(err,r)=>err?reject(err):resolve(r.body.url));}));"+INIT

@unittest.skipUnless(importlib.util.find_spec('quickjs'),'requires Docker QuickJS dependency')
class JsSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.registry=Sources(self.temp.name,lambda:{})
    def test_import_preserve_and_restart(self):
        original=self.registry.rows()
        result=self.registry.import_js(SIMPLE,'example.js');row=result['source']
        self.assertEqual(row['platforms'],['netease','qq'])
        self.assertEqual(self.registry.import_js(SIMPLE,'renamed.js')['added'],0)
        self.registry.save({'id':row['id'],'enabled':False})
        restarted=Sources(self.temp.name,lambda:{})
        self.assertEqual(restarted.script(row['id']),SIMPLE)
        self.assertFalse(restarted.enabled(row['id']))
        self.assertEqual(restarted.rows()[:4],original)
        self.assertNotIn('code',restarted.export()['sources'][-1])
    def test_json_import_with_existing_js(self):
        self.registry.import_js(SIMPLE,'example.js')
        self.assertEqual(self.registry.import_sources({'version':1,'sources':[{'name':'HTTP','url':'https://example.com','platforms':['qq']} ]})['added'],1)
        self.assertEqual([x['kind'] for x in self.registry.options('qq')],['builtin','lx','http'])
    def test_order_persists_and_new_import_appends_without_losing_settings(self):
        row=self.registry.import_js(SIMPLE,'one.js')['source']
        ids=[row['id']]+[r['id'] for r in self.registry.rows() if r['id']!=row['id']]
        self.registry.set_order(ids)
        self.registry.save({'id':'builtin-qq','enabled':False})
        restarted=Sources(self.temp.name,lambda:{})
        self.assertEqual([r['id'] for r in restarted.rows()],ids)
        self.assertEqual([r['priority'] for r in restarted.rows()],list(range(1,6)))
        self.assertEqual(restarted.options('qq')[0]['id'],row['id'])
        self.assertEqual(len(restarted.options('qq')),1)
        extra=restarted.import_js(SIMPLE+'\n// another','two.js')['source']
        self.assertEqual([r['id'] for r in restarted.rows()],ids+[extra['id']])
        for invalid in (None,ids,ids+[ids[0]],[{}]*6):
            with self.assertRaises(ValueError):restarted.set_order(invalid)
        self.assertFalse(restarted.enabled('builtin-qq'))
    def test_retry_download_executes_ordered_js_and_records_success_after_failure(self):
        from hub_engine import Engine
        engine=Engine(self.temp.name,Path(self.temp.name)/'music',lambda *a:None)
        bad=engine.source_registry.import_js("lx.on('request',()=>Promise.reject(Error('upstream unavailable')));"+INIT,'bad.js')['source']
        good=engine.source_registry.import_js(SIMPLE,'good.js')['source']
        ids=[bad['id'],good['id']]+[r['id'] for r in engine.source_registry.rows() if r['kind']!='lx']
        engine.source_registry.set_order(ids)
        jid=engine.store.enqueue('download','song',{'track_id':'one','quality':'lossless'})
        engine.store.update_job(jid,status='blocked',error='old access error')
        engine.store.bulk_jobs('start',[jid])
        job=engine.store.claim(manual_only=True);job['_worker']=True
        part=Path(self.temp.name)/'partial';part.write_bytes(b'old partial')
        src=[{'platform':'qq','source_id':'MID','meta':'{"duration_ms":200000}'}]
        with patch('hub_providers.public_audio_url'),patch.object(engine.providers,'resolve_native') as native,patch.object(engine,'transfer_audio',return_value='valid'):
            result=engine.download_from_sources(job,part,src,200000)
        native.assert_not_called();self.assertEqual(result[1],'valid')
        log=json.loads(engine.store.rows('SELECT source_attempts FROM jobs WHERE id=?',(jid,))[0]['source_attempts'])
        self.assertEqual([r['id'] for r in log],[bad['id'],good['id']])
        self.assertEqual([r['status'] for r in log],['failed','done'])
        self.assertFalse(part.exists())
    def test_all_failures_are_reported_without_leaking_urls_or_tokens(self):
        from types import SimpleNamespace as NS
        from hub_engine import Engine
        from hub_errors import describe,SourcesExhausted
        rows=[{'kind':'lx','id':'a','name':'A'}, {'kind':'lx','id':'b','name':'B'}]
        engine=NS(source_registry=NS(options=lambda p:rows),check=lambda j:None,
            settings=lambda:{},store=NS(update_claimed=lambda *a,**k:None))
        with patch('hub_js_sources.resolve_js',side_effect=[ValueError('会员权限不足'),RuntimeError('https://example.com/?token=PRIVATE')]):
            with self.assertRaises(SourcesExhausted) as error:
                Engine.download_from_sources(engine,{'payload':{}},Path(self.temp.name)/'part',[{'platform':'qq','source_id':'MID','meta':'{}'}],200000)
        self.assertEqual(describe(error.exception)[0],'sources')
        self.assertEqual(len(error.exception.attempts),2)
        self.assertNotIn('PRIVATE',json.dumps(error.exception.attempts))
    def test_priority_applies_across_linked_platforms_and_transfer_failures(self):
        from types import SimpleNamespace as NS
        from hub_engine import Engine
        seen=[]
        def resolve(platform,*args):seen.append(platform);return {'url':platform}
        engine=NS(source_registry=NS(options=lambda p:[{'id':p,'kind':'builtin','priority':1 if p=='qq' else 2}]),
            check=lambda j:None,settings=lambda:{},store=NS(update_claimed=lambda *a,**k:None),
            providers=NS(resolve_native=resolve),transfer_audio=lambda *args:'valid')
        sources=[{'platform':p,'source_id':'song','meta':'{}'} for p in ('netease','qq')]
        Engine.download_from_sources(engine,{'payload':{}},Path(self.temp.name)/'part',sources,200000)
        self.assertEqual(seen,['qq'])
        seen.clear()
        with patch.object(engine,'transfer_audio',side_effect=[ValueError('incomplete file'),'valid']):
            result=Engine.download_from_sources(engine,{'payload':{}},Path(self.temp.name)/'part',sources,200000)
        self.assertEqual(seen,['qq','netease']);self.assertEqual(result[1],'valid')
    def test_tampered_file_rejected(self):
        row=self.registry.import_js(SIMPLE,'example.js')['source']
        (self.registry.path.parent/'scripts'/(row['sha256']+'.js')).write_text('tampered')
        with self.assertRaisesRegex(ValueError,'校验'):self.registry.script(row['id'])
    def test_windows_newlines_roundtrip_restart_and_resolve(self):
        # Same Windows line endings as the user's 西瓜 LX file. Include mixed
        # endings so the regression cannot pass by silently normalizing input.
        code=SIMPLE.replace('\n','\r\n')+'\r\n// 中文\n// mixed\r\n'
        row=self.registry.import_js(code,'西瓜兼容测试.js')['source']
        path=self.registry.path.parent/'scripts'/(row['sha256']+'.js')
        self.assertEqual(path.read_bytes(),code.encode('utf-8'))
        restarted=Sources(self.temp.name,lambda:{})
        self.assertEqual(restarted.script(row['id']),code)
        self.assertEqual(restarted.import_js(code,'renamed.js')['added'],0)
        with patch('hub_providers.public_audio_url'):
            self.assertEqual(resolve_js(restarted,row,'qq','MID','lossless',{})['url'],'https://audio.example/song.mp3')
    def test_reimport_repairs_file_preserving_identity_and_disabled_state(self):
        row=self.registry.import_js(SIMPLE,'example.js')['source']
        self.registry.save({'id':row['id'],'enabled':False})
        path=self.registry.path.parent/'scripts'/(row['sha256']+'.js')
        for broken in ('missing','tampered'):
            if broken=='missing':path.unlink()
            else:path.write_bytes(b'changed')
            result=self.registry.import_js(SIMPLE,'renamed.js')
            self.assertEqual(result['added'],0);self.assertTrue(result['repaired'])
            self.assertEqual(result['source']['id'],row['id'])
            self.assertFalse(result['source']['enabled'])
            self.assertEqual(result['source']['filename'],'example.js')
            self.assertEqual(self.registry.script(row['id']),SIMPLE)
        self.assertEqual(len(self.registry.rows()),5)
        self.assertFalse(self.registry.import_js(SIMPLE,'example.js')['repaired'])
    def test_incompatible_and_oversized_no_write(self):
        for code in ('module.exports={};','while(true){}','x'*262145):
            with self.assertRaises(ValueError):self.registry.import_js(code,'bad.js')
        self.assertEqual(len(self.registry.rows()),4)
    def test_no_host_apis_and_inspection_does_not_request(self):
        code="if(typeof require!=='undefined'||typeof process!=='undefined'||typeof std!=='undefined'||typeof os!=='undefined')throw Error('host exposed');lx.request('http://127.0.0.1/secret',{},()=>{});"+SIMPLE
        with patch('lx_worker.request',side_effect=AssertionError('network on import')):
            self.assertIn('sources',run({'code':code,'operation':'inspect'}))
        self.assertEqual(inspect_script(code,'example.js')['kind'],'lx')
    def test_remote_callback_and_real_promise(self):
        seen=[]
        def transport(item):seen.append(item);return {'statusCode':200,'body':{'url':'https://audio.example/song.mp3'}}
        value=run({'code':REMOTE,'operation':'resolve','request':{'info':{'musicInfo':{'id':'song'}}}},transport)
        self.assertEqual(value,'https://audio.example/song.mp3')
        self.assertEqual(seen[0]['options']['body']['id'],'song')
    def test_callback_errors_timer_and_parallel_fallback(self):
        code="lx.on('request',()=>Promise.any(['bad','good'].map(k=>new Promise((resolve,reject)=>lx.request('https://example.com/'+k,{},(e,r)=>e?reject(e):setTimeout(()=>resolve(r.body),5))))));"+INIT
        def transport(item):
            if item['url'].endswith('bad'):raise ValueError('failed')
            return {'body':'https://audio.example/ok.mp3'}
        self.assertEqual(run({'code':code,'operation':'resolve','request':{}},transport),'https://audio.example/ok.mp3')
    def test_cancel_subprocess_promptly(self):
        code="lx.on('request',()=>new Promise(()=>{}));"+INIT
        started=time.monotonic()
        class Stop(Exception):pass
        def check():
            if time.monotonic()-started>0.3:raise Stop()
        with self.assertRaises(Stop):invoke(code,'resolve',{},check)
        self.assertLess(time.monotonic()-started,2)
    def test_buffer_and_urlparams_compatibility(self):
        code="if(lx.utils.buffer.bufToString(lx.utils.buffer.from('中文'),'base64')!=='5Lit5paH')throw Error('buffer');let p=new URLSearchParams();p.set('a','中文 x');if(p.get('a')!=='中文 x')throw Error('params');"+SIMPLE
        self.assertEqual(inspect_script(code,'source.js')['kind'],'lx')
    def test_resolve_whitelist_and_unknown_quality(self):
        row=self.registry.import_js(SIMPLE,'source.js')['source']
        meta={'title':'Song','artists':['Singer'],'duration_ms':180000,'cookie':'SECRET','token':'SECRET'}
        info=music_info('qq','MID',meta)
        self.assertNotIn('SECRET',json.dumps(info))
        self.assertEqual(info['interval'],'03:00')
        with patch('hub_providers.public_audio_url'):
            result=resolve_js(self.registry,row,'qq','MID','lossless',meta)
        self.assertEqual(result['quality'],'unknown')
        self.assertTrue(result['requires_duration'])
    def test_registry_falls_through_js_failure_and_cancel(self):
        from types import SimpleNamespace as NS
        from hub_engine import Engine,Cancelled
        rows=[{'kind':'lx','id':'bad'}, {'kind':'lx','id':'good'}]
        engine=NS(source_registry=NS(options=lambda p:rows),check=lambda j:None,
            settings=lambda:{'download_quality':'standard'},store=NS(update_claimed=lambda *a,**k:None),
            transfer_audio=lambda *a:'audio')
        part=Path(self.temp.name)/'part';part.write_text('partial')
        src=[{'platform':'qq','source_id':'MID','meta':'{}'}]
        with patch('hub_js_sources.resolve_js',side_effect=[ValueError('source failed'),{'url':'ok'}]) as resolve:
            value=Engine.download_from_sources(engine,{'payload':{'quality':'standard'}},part,src,200000)
        self.assertEqual(value,({'url':'ok'},'audio'));self.assertEqual(resolve.call_count,2);self.assertFalse(part.exists())
        with patch('hub_js_sources.resolve_js',side_effect=Cancelled) as resolve:
            with self.assertRaises(Cancelled):Engine.download_from_sources(engine,{'payload':{'quality':'standard'}},part,src,200000)
        self.assertEqual(resolve.call_count,1)


class BrokerTests(unittest.TestCase):
    def test_private_dns_and_mixed_dns_rejected(self):
        public=(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.215.14',443))
        private=(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))
        for records in ([private],[public,private]):
            with patch('socket.getaddrinfo',return_value=records):
                with self.assertRaisesRegex(ValueError,'内网'):destination('https://example.com')
        with patch('socket.getaddrinfo',return_value=[public]):
            self.assertEqual(destination('https://example.com')[2],'93.184.215.14')
    def test_bad_scheme_port_credentials_rejected(self):
        for url in ('file:///etc/passwd','https://user:pass@example.com','http://example.com:8600','http://example.com/\nsecret'):
            with self.assertRaises(ValueError):destination(url)
    def test_redirect_public_to_private_not_followed(self):
        from types import SimpleNamespace as NS
        reply=NS(status=302,getheader=lambda key,*a:'http://127.0.0.1/secret' if key=='Location' else '')
        conn=NS(sock=None,request=lambda *a:None,getresponse=lambda:reply,close=lambda:None)
        with patch('lx_worker.destination',side_effect=[(NS(scheme='http',hostname='source.example',netloc='source.example',path='/',query=''),80,'93.184.215.14'),ValueError('内网')]),patch('socket.create_connection',return_value=NS(close=lambda:None)) as connect,patch('http.client.HTTPConnection',return_value=conn):
            with self.assertRaisesRegex(ValueError,'内网'):request({'url':'http://source.example','options':{}})
        self.assertEqual(connect.call_count,1)

if __name__=='__main__':unittest.main()
