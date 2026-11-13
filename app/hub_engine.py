import json
import os
import threading
import time
from pathlib import Path

from hub_store import Store, safe_name
from hub_providers import Providers, open_audio


class Paused(Exception):
    pass


class Cancelled(Exception):
    pass


class JobControlled(Cancelled):
    """A newer user action owns the state; the old worker must not overwrite it."""
    pass


class Engine:
    def __init__(self, home, music_root, discover, bridge=None):
        self.store = Store(home)
        self.mount_root = Path(music_root).resolve()
        self.root = self.mount_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(home) / 'hub-settings.json'
        self.config_lock = threading.RLock()
        self.export_lock = threading.Lock()
        self.pipeline_lock = threading.Lock()
        configured = self.settings().get('music_dir')
        if configured:
            candidate=Path(configured).resolve()
            if not candidate.is_relative_to(self.mount_root):
                raise ValueError('音乐目录必须位于 MUSIC_DIR 挂载范围内')
            self.root=candidate
            self.root.mkdir(parents=True,exist_ok=True)
        from hub_accounts import Accounts
        self.accounts = Accounts(self)
        from hub_sources import Sources
        self.source_registry = Sources(home,self.settings)
        self.providers = Providers(self.settings, discover, self.accounts.private)
        self.providers.source_registry = self.source_registry
        self.bridge = bridge
        self.stop = threading.Event()
        self.threads = []
        from hub_library import Library
        self.library = Library(self)
        import hub_folders
        hub_folders.initialize(self)
        from hub_assets import Assets
        self.assets=Assets(self)
        from hub_asset_dirs import AssetDirectories
        self.asset_directories=AssetDirectories(self)
        from hub_metadata import MetadataDirectories
        self.metadata_directories=MetadataDirectories(self)
        import hub_auto_comments
        hub_auto_comments.initialize(self)
        from hub_comment_dirs import CommentDirectories
        self.comment_directories=CommentDirectories(self)

    def settings(self):
        with self.config_lock:
            if self.config_path.exists():
                return json.loads(self.config_path.read_text())
            return {'netease_cookie':'', 'gateways':{}}

    def save_settings(self, value):
        # Blank secrets preserve their previous values; explicit clear toggles remove them.
        with self.config_lock:
            current = self.settings()
            if value.get('clear_cookie'):
                current['netease_cookie'] = ''
            elif value.get('netease_cookie'):
                current['netease_cookie'] = value['netease_cookie']
            for platform, item in value.get('gateways',{}).items():
                previous = current.setdefault('gateways',{}).get(platform,{})
                current['gateways'][platform] = {
                    'url':item.get('url','').strip(), 'account_url':item.get('account_url','').strip(),
                    'search_url':item.get('search_url','').strip(),
                    'token':'' if item.get('clear_token') else item.get('token') or previous.get('token','')}
            if 'music_dir' in value and value['music_dir']:
                candidate = Path(value['music_dir']).resolve()
                if not candidate.is_relative_to(self.mount_root):
                    raise ValueError('音乐目录必须位于挂载目录 '+str(self.mount_root)+' 内')
                if candidate!=self.root and self.store.rows('SELECT id FROM tracks LIMIT 1'):
                    raise ValueError('曲库已有歌曲；请先迁移数据并保持原容器路径，避免文件关联失效')
                candidate.mkdir(parents=True,exist_ok=True)
                self.root=candidate
                current['music_dir']=str(candidate)
            if 'library_dirs' in value:
                current['library_dirs'] = list(dict.fromkeys(str(self.library.confined(p)) for p in value['library_dirs']))
                if not current['library_dirs']:raise ValueError('请至少选择一个音乐目录')
            for key in ('fn_auto','fn_user','fn_music_root','fn_library','downloads_paused','sync_enabled','download_quality'):
                if key in value:
                    current[key]=value[key]
            temp = self.config_path.with_suffix('.tmp')
            fd = os.open(temp,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(current,f,ensure_ascii=False)
            temp.replace(self.config_path)
            self.store.dirty()
            if 'library_dirs' in value:self.library.start()

    def public_settings(self):
        cfg = self.settings()
        return {'cookie_configured':bool(self.providers.cookie()), 'music_root':str(self.root), 'mount_root':str(self.mount_root),
                'download_quality':cfg.get('download_quality','lossless'), 'library_dirs':self.library.directories(), 'downloads_paused':cfg.get('downloads_paused',True), 'sync_enabled':cfg.get('sync_enabled',False),
                'fn_auto':cfg.get('fn_auto',False), 'fn_user':cfg.get('fn_user',''),
                'fn_music_root':cfg.get('fn_music_root',''), 'fn_library':cfg.get('fn_library',''),
                'gateways':{p:{'url':v.get('url',''),'account_url':v.get('account_url',''),'search_url':v.get('search_url',''),
                               'token_configured':bool(v.get('token'))} for p,v in cfg.get('gateways',{}).items()}}

    def pump_pipeline(self):
        with self.pipeline_lock:
            return self._pump_pipeline()

    def _pump_pipeline(self):
        if not self.bridge or not self.bridge.get('BRIDGE',{}).get('store'):
            return
        state=self.store.pipeline()
        if state['task_id']:
            task=self.bridge['BRIDGE']['store'].get(state['task_id'])
            if task and task['status'] in ('queued','running'):
                return
            result=(task or {}).get('result') or {}
            if isinstance(result,str):
                result=json.loads(result)
            args=(task or {}).get('args') or {}
            complete=task and task['status']=='succeeded'
            revision=int(args.get('revision',0)) if complete else state['completed']
            with self.store.connect() as db:
                db.execute('UPDATE pipeline SET completed=MAX(completed,?),task_id=NULL,retry_at=?,status=?,message=? WHERE id=1',
                    (revision,time.time()+(2 if complete else 10 if (task or {}).get('status')=='partial' else 60),'done' if complete else 'waiting',
                     result.get('summary') or (task or {}).get('summary') or '等待飞牛任务重试'))
            return
        cfg=self.settings()
        if not cfg.get('fn_user'):
            return
        if state['revision']<=state['completed'] or state['retry_at']>time.time():
            return
        try:
            task=self.bridge['submit_task']('playlist_sync',{'action':'hub_reconcile','revision':state['revision']})
        except Exception:
            # Busy is transient. Persist intent and retry without losing a download event.
            with self.store.connect() as db:
                db.execute("UPDATE pipeline SET retry_at=?,status='waiting',message='等待飞牛任务空闲或连接恢复' WHERE id=1",(time.time()+30,))
            return
        with self.store.connect() as db:
            db.execute("UPDATE pipeline SET task_id=?,status='running',message='正在扫描并核对飞牛入库歌曲' WHERE id=1",(task['id'],))

    def start(self):
        self.accounts.recover()
        self.reconcile_download_records()
        self.store.dirty()
        import hub_auto_comments
        hub_auto_comments.seed(self)
        self.library.start()
        for target in (self.worker,self.scheduler):
            thread = threading.Thread(target=target,daemon=True,name='hub-'+target.__name__)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.accounts.close()
        self.stop.set()
        self.assets.stop.set()
        self.asset_directories.close()
        self.metadata_directories.close()
        self.comment_directories.pool.shutdown(wait=False,cancel_futures=True)
        for thread in self.threads:
            thread.join(timeout=5)

    def check(self, job):
        if job.get('_worker') and not job['payload'].get('manual') and not self.settings().get('sync_enabled',False):raise Paused()
        if job.get('_worker') and not job['payload'].get('manual'):
            import hub_folders
            if job['kind']=='sync':
                if not hub_folders.selected(self,job['payload']['rule_id']):raise Cancelled()
            elif not hub_folders.destinations(self,job['payload']['track_id']):raise Cancelled()
        rows = self.store.rows('SELECT status,generation FROM jobs WHERE id=?',(job['id'],))
        if job.get('_worker') and (not rows or rows[0]['generation']!=job['generation'] or rows[0]['status']!='running'):
            raise JobControlled()
        if self.stop.is_set() or not rows or rows[0]['status']=='cancelled':
            raise Cancelled()

    def scheduler(self):
        while not self.stop.is_set():
            try:
                self.pump_pipeline()
                self.asset_directories.pump()
                self.metadata_directories.pump()
                import hub_auto_comments
                hub_auto_comments.pump(self)
                if time.time()-self.library.last_scan>300 and not self.library.running:self.library.start()
                if not self.settings().get('sync_enabled',False):
                    self.stop.wait(5)
                    continue
                if time.time()-getattr(self,'catalog_checked',0)>3600:
                    import hub_folders
                    hub_folders.refresh(self)
                    self.catalog_checked=time.time()
                for rule in self.store.rows('SELECT * FROM rules WHERE enabled=1 AND next_run<=?',(time.time(),)):
                    self.store.enqueue('sync','sync:'+rule['id'],{'rule_id':rule['id']})
                    with self.store.connect() as db:
                        db.execute('UPDATE rules SET next_run=? WHERE id=?',(time.time()+rule['interval_hours']*3600,rule['id']))
            except Exception as exc:
                print('[hub scheduler]',type(exc).__name__,flush=True)
            self.stop.wait(5)

    def worker(self):
        while not self.stop.is_set():
            cfg=self.settings()
            manual_only=not cfg.get('sync_enabled',False)
            job=self.store.claim(manual_only=manual_only,downloads_paused=cfg.get('downloads_paused',False))
            if not job:
                self.stop.wait(1)
                continue
            job['_worker']=True
            try:
                self.check(job)
                if job['kind']=='sync':
                    warning=self.sync(job)
                else:
                    self.download(job)
                    warning=''
                self.check(job)
                self.store.update_claimed(job,status='done',progress=100,error=warning or '')
            except JobControlled:
                pass
            except Paused:
                self.store.update_claimed(job,status='queued',error='同步已停止，等待手动开始')
            except Cancelled:
                if self.stop.is_set():
                    self.store.update_claimed(job,status='queued',error='已暂停，启动后继续')
                else:
                    self.store.update_claimed(job,status='cancelled',error='已取消')
            except (Exception,SystemExit) as exc:
                # Do not persist transport URLs / cookies / signed query strings.
                from hub_errors import describe
                code,message,transient = describe(exc)
                retry = transient and job['attempts'] < 3
                self.store.update_claimed(job,status='queued' if retry else ('failed' if transient else 'blocked'),
                                      due=time.time()+30*2**job['attempts'],error=f'[{code}] {message}')
                if job['kind']=='sync':
                    with self.store.connect() as db:
                        db.execute('UPDATE rules SET error=? WHERE id=?',(message,job['payload']['rule_id']))

    def sync(self, job):
        rule = self.store.rule(job['payload']['rule_id'])
        playlists = self.providers.discover(rule['platform'],rule['ref'],rule['kind'])
        if len(playlists)>1000:
            raise ValueError('账号歌单超过 1000 张，请拆分订阅')
        tracks, seen, source_members = [], set(), []
        complete, warnings = True, []
        for i, pl in enumerate(playlists):
            self.check(job)
            result = self.providers.playlist(rule['platform'],str(pl['id']))
            if result.get('snapshot_complete') is False:
                complete=False
                warnings.append(result.get('warning') or '平台返回不完整，保留未返回的旧记录')
            if result.get('name'): pl['name']=result['name']
            for track in result['tracks']:
                tid = self.store.upsert_track(rule['platform'],track)
                source_members.append((rule['id'],str(pl['id']),tid))
                if tid not in seen:
                    seen.add(tid)
                    tracks.append(tid)
            self.store.update_claimed(job,progress=round((i+1)/max(1,len(playlists))*90))
        self.check(job)
        # Only commit new membership when all upstream pages/playlists succeeded.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT * FROM rules WHERE id=?',(rule['id'],)).fetchone()
            active=db.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if not current or current['enabled']!=rule['enabled'] or not active or active['status']=='cancelled' or any(current[k]!=rule[k] for k in ('platform','kind','ref','group_name')):
                raise Cancelled()
            if not complete:
                for old in db.execute('SELECT track_id FROM membership WHERE rule_id=? ORDER BY position',(rule['id'],)):
                    if old[0] not in seen:
                        seen.add(old[0]);tracks.append(old[0])
                source_members.extend(tuple(r) for r in db.execute('SELECT rule_id,playlist_id,track_id FROM source_members WHERE rule_id=?',(rule['id'],)))
            if complete:
                db.execute("UPDATE source_playlists SET state='removed',updated=? WHERE rule_id=?",(time.time(),rule['id']))
            db.executemany('''INSERT INTO source_playlists VALUES(?,?,?,?,?,?) ON CONFLICT(rule_id,platform,source_id) DO UPDATE SET name=excluded.name,state='active',updated=excluded.updated''',
                [(rule['id'],rule['platform'],str(pl['id']),pl.get('name',''),'active',time.time()) for pl in playlists])
            db.execute('DELETE FROM source_members WHERE rule_id=?',(rule['id'],))
            db.executemany('INSERT OR IGNORE INTO source_members VALUES(?,?,?)',source_members)
            db.execute('UPDATE pipeline SET revision=revision+1 WHERE id=1')
            db.execute('DELETE FROM membership WHERE rule_id=?',(rule['id'],))
            db.executemany('INSERT INTO membership VALUES(?,?,?)',[(rule['id'],tid,i) for i,tid in enumerate(tracks)])
            if complete: db.execute('DELETE FROM rule_playlists WHERE rule_id=?',(rule['id'],))
            db.executemany('INSERT OR IGNORE INTO rule_playlists VALUES(?,?,?,?)',
                           [(rule['id'],rule['platform'],str(pl['id']),pl.get('name','')) for pl in playlists])
            db.execute('UPDATE rules SET last_run=?,error=? WHERE id=?',(time.time(),'；'.join(warnings),rule['id']))
            if not complete: db.execute('UPDATE rules SET next_run=? WHERE id=?',(time.time()+900,rule['id']))
        if rule['auto_download']:
            for tid in tracks:
                self.check(job)
                self.enqueue_download(tid,rule['quality'])
        self.export()
        return ';'.join(warnings)

    def enqueue_download(self, tid, quality='lossless', force=False):
        rows = self.store.rows('SELECT path FROM tracks WHERE id=?',(tid,))
        if not rows:
            raise ValueError('歌曲不存在')
        import hub_folders
        existing=hub_folders.saved_file(self,{'id':tid,**rows[0]})
        path=str(existing.relative_to(self.root)) if existing else None
        if rows[0]['path']!=path:
            with self.store.connect() as db:db.execute('UPDATE tracks SET path=?,bytes=? WHERE id=?',(path,existing.stat().st_size if existing else 0,tid))
        if not path:
            from hub_store import norm
            track=self.store.rows('SELECT meta,duration FROM tracks WHERE id=?',(tid,))[0]
            meta=json.loads(track['meta'])
            if track['duration'] and meta.get('artists'):
                candidates=self.store.rows('SELECT * FROM local_files WHERE title=? COLLATE NOCASE AND ABS(duration-?)<=2000',(meta['title'],track['duration']))
                candidates=[r for r in candidates if sorted(norm(a) for a in json.loads(r['artists']))==sorted(norm(a) for a in meta['artists']) and (self.mount_root/r['path']).resolve().is_relative_to(self.root)]
                if len(candidates)==1:
                    file=(self.mount_root/candidates[0]['path']).resolve()
                    if file.is_file():
                        path=str(file.relative_to(self.root))
                        with self.store.connect() as db:db.execute('UPDATE tracks SET path=?,bytes=?,quality=? WHERE id=?',(path,file.stat().st_size,'local',tid))
        if path and (self.root/path).is_file():
            self.export()
            return {'skipped':True,'reason':'本地文件已存在'}
        if not force and self.store.rows("SELECT id FROM jobs WHERE dedupe=? AND status='blocked' LIMIT 1",('download:'+tid,)):
            return {'skipped':True,'reason':'此曲等待授权或配置修复，可在任务中心手动重试'}
        jid=self.store.enqueue('download','download:'+tid,{'track_id':tid,'quality':quality,'manual':force,'user_started':force})
        if force:
            # A click in search should not wait behind thousands of automatic items.
            # Pause still applies; sync tasks keep their higher priority.
            with self.store.connect() as db:db.execute("UPDATE jobs SET due=0 WHERE id=? AND status='queued'",(jid,))
        return {'job_id':jid}

    def export(self):
        with self.export_lock:
            if self.settings().get('sync_enabled',False):
                import hub_folders
                hub_folders.publish(self)
            self.store.export_playlists(self.root)
            self.store.dirty()

    def export_after_download(self):
        # Audio is already validated and atomically published. A later playlist
        # export must not turn a successful transfer into a failed download.
        try:
            self.export()
        except Exception as exc:
            from hub_errors import describe
            with self.store.connect() as db:
                db.execute("UPDATE pipeline SET status='waiting',message=?,revision=revision+1 WHERE id=1",
                           ('音频已保存；分类歌单导出待处理：'+describe(exc)[1],))
        self.library.start()

    def reconcile_download_records(self):
        """Repair only the known post-download export error, without downloading."""
        import hub_folders
        import mutagen
        jobs=self.store.rows("SELECT * FROM jobs WHERE kind='download' AND status IN ('blocked','failed') AND error LIKE '%分类目录存在同名不同文件%'")
        for job in jobs:
            payload=json.loads(job['payload']);tid=payload.get('track_id')
            if not payload.get('manual') and not hub_folders.destinations(self,tid):
                self.store.update_claimed(job,status='cancelled',error='来源未选中，已停止同步；现有音乐文件保留')
                continue
            rows=self.store.rows('SELECT path,duration FROM tracks WHERE id=?',(tid,))
            if not rows or not rows[0]['path']:continue
            file=(self.root/rows[0]['path']).resolve()
            if not file.is_relative_to(self.root) or not file.is_file():continue
            try:
                audio=mutagen.File(file)
                valid=audio and getattr(audio,'info',None) and audio.info.length>=1
                if not valid or (rows[0]['duration'] and audio.info.length*1000<rows[0]['duration']-5000):continue
            except Exception:continue
            self.store.update_claimed(job,status='done',progress=100,error='')

    def transfer_audio(self, job, part, resolved, duration=0):
        import mutagen
        self.check(job)
        if resolved.get('requires_duration') and not (duration or resolved.get('duration_ms')):
            raise ValueError('歌曲缺少已知时长，无法验证 JS 音源返回的是完整音频')
        limit=512*1024*1024
        with open_audio(resolved['url']) as response, part.open('wb') as file:
            total=int(response.headers.get('Content-Length') or 0)
            if total>limit:raise ValueError('音频超过单文件 512 MB 上限')
            size=0
            while True:
                self.check(job)
                chunk=response.read(128*1024)
                if not chunk:break
                size+=len(chunk)
                if size>limit:raise ValueError('音频超过单文件 512 MB 上限')
                file.write(chunk)
                if total:self.store.update_claimed(job,progress=min(95,int(size*95/total)))
            if not size or (total and total!=size):raise ValueError('音频为空或下载不完整')
        audio=mutagen.File(part)
        if audio is None or not getattr(audio,'info',None) or audio.info.length<1:
            raise ValueError('下载内容不是有效音频')
        from hub_audio_validation import validate_quality
        validate_quality(audio, resolved.get('quality'))
        expected=duration or resolved.get('duration_ms',0)
        if expected and audio.info.length*1000<expected-5000:
            raise ValueError('下载时长短于曲目，未保存不完整音频')
        return audio

    def transfer_with_fallback(self,job,part,resolved,source,duration):
        from hub_errors import UpstreamFailure
        try:return resolved,self.transfer_audio(job,part,resolved,duration)
        except (Paused,Cancelled):raise
        except Exception as primary:
            if source['platform']!='netease' or resolved.get('provider')=='musicjx' or not self.source_registry.enabled('builtin-netease-public'):
                stage='备用音频传输或完整性检查' if resolved.get('provider')=='musicjx' else '音频传输或完整性检查'
                raise UpstreamFailure(stage,primary) from primary
            self.check(job)
            part.unlink(missing_ok=True)
            self.store.update_claimed(job,progress=0)
            try:
                alternate=self.providers.public_netease_audio(source['source_id'],json.loads(source['meta']))
            except Exception as fallback:
                raise UpstreamFailure('网易云公开备用解析',fallback) from fallback
            try:return alternate,self.transfer_audio(job,part,alternate,duration)
            except (Paused,Cancelled):raise
            except Exception as fallback:
                raise UpstreamFailure('备用音频传输或完整性检查',fallback) from fallback

    def download_from_sources(self, job, part, sources, duration):
        """A URL is not success: try the next known source after transfer validation.

        Strategy inspired by Lemon Music's sourceFallback/downloadQuality modules.
        Never broad-match a different recording or persist a partial file.
        """
        from hub_sources import resolve_http
        from hub_errors import describe,SourcesExhausted
        candidates=[(option,source) for source in sources for option in self.source_registry.options(source['platform'])]
        candidates.sort(key=lambda pair:pair[0].get('priority',999))
        attempts=[]
        def record():
            self.store.update_claimed(job,source_attempts=json.dumps(attempts,ensure_ascii=False))
        for option,source in candidates:
            self.check(job)
            attempt={'id':option['id'],'name':option.get('name',option['id']),
                     'platform':source['platform'],'status':'running','message':'正在解析并校验下载'}
            attempts.append(attempt);record()
            try:
                quality=self.settings().get('download_quality',job['payload'].get('quality','lossless'))
                meta=json.loads(source['meta'])
                if option['kind'] in ('builtin','native'):
                    resolved=self.providers.resolve_native(source['platform'],source['source_id'],quality,meta)
                elif option['kind']=='fallback':
                    resolved=self.providers.public_netease_audio(source['source_id'],meta)
                elif option['kind']=='legacy':
                    resolved=self.providers.resolve_gateway(option,source['platform'],source['source_id'],quality)
                elif option['kind']=='lx':
                    from hub_js_sources import resolve_js
                    resolved=resolve_js(self.source_registry,option,source['platform'],source['source_id'],quality,meta,lambda:self.check(job))
                else:
                    resolved=resolve_http(option,source['platform'],source['source_id'],quality,meta)
                audio=self.transfer_audio(job,part,resolved,duration)
                attempt.update(status='done',message='音频下载与完整性校验通过');record()
                return resolved,audio
            except (Paused,Cancelled):
                raise
            except Exception as exc:
                code,message,retryable=describe(exc)
                attempt.update(status='failed',code=code,message=message,retryable=retryable);record()
                part.unlink(missing_ok=True)
                self.store.update_claimed(job,progress=0)
        if attempts:raise SourcesExhausted(attempts)
        raise ValueError('歌曲没有已启用的可用音源，请检查设置')

    def download(self, job):
        import mutagen
        tid = job['payload']['track_id']
        import hub_folders
        if not job['payload'].get('manual') and not hub_folders.destinations(self,tid):raise Cancelled()
        row = self.store.rows('SELECT * FROM tracks WHERE id=?',(tid,))[0]
        existing=hub_folders.saved_file(self,row)
        if existing:
            with self.store.connect() as db:db.execute('UPDATE tracks SET path=?,bytes=? WHERE id=?',(str(existing.relative_to(self.root)),existing.stat().st_size,tid))
            self.export()
            import hub_auto_comments
            hub_auto_comments.enqueue(self,tid)
            return
        sources = self.store.rows('SELECT * FROM sources WHERE track_id=? ORDER BY CASE platform WHEN \'netease\' THEN 0 ELSE 1 END',(tid,))
        meta = json.loads(row['meta'])
        import hub_folders
        places=hub_folders.destinations(self,tid)
        directory = self.root/(places[0]['directory'] if places else 'Library')
        directory.mkdir(exist_ok=True,parents=True)
        stem = safe_name('、'.join(meta.get('artists',[]))+' - '+meta['title'])+'--'+tid[:10]
        target = directory/(stem+'.mp3')
        part = directory/(stem+'.part')
        try:
            resolved,audio=self.download_from_sources(job,part,sources,row['duration'])
            # Detect the actual container rather than trusting the resolver extension.
            module = type(audio).__module__
            detected = {'mutagen.mp3':'mp3','mutagen.flac':'flac','mutagen.mp4':'m4a',
                        'mutagen.oggvorbis':'ogg','mutagen.oggopus':'opus','mutagen.wave':'wav','mutagen.aac':'aac'}.get(module)
            if not detected:
                raise ValueError('不支持该音频容器格式')
            target = target.with_suffix('.'+detected)
            # Write tags before atomic publication; a tag error must not corrupt the library.
            easy = mutagen.File(part,easy=True)
            if easy is not None and detected not in ('wav','aac'):
                if easy.tags is None:
                    easy.add_tags()
                easy['title'] = [meta['title']]
                easy['artist'] = meta.get('artists') or ['未知歌手']
                easy['album'] = [meta.get('album') or '未知专辑']
                easy.save()
            import hub_artwork
            artwork = hub_artwork.fetch(resolved.get('cover_url') or meta.get('cover_url'))
            if artwork:
                hub_artwork.embed(part, artwork)
            self.check(job)
            if artwork:
                cover = target.with_suffix(artwork[2])
                cover_part = cover.with_suffix(cover.suffix + '.part')
                try:
                    cover_part.write_bytes(artwork[0])
                    cover_part.replace(cover)
                finally:
                    cover_part.unlink(missing_ok=True)
            part.replace(target)
            lyric = resolved.get('lyric')
            if isinstance(lyric,str) and lyric:
                target.with_suffix('.lrc').write_text(lyric[:500000],encoding='utf-8')
            with self.store.connect() as db:
                db.execute('UPDATE tracks SET path=?,bytes=?,quality=? WHERE id=?',
                           (str(target.relative_to(self.root)),target.stat().st_size,
                            str(resolved.get('quality') or 'unknown'),tid))
            self.export_after_download()
            import hub_auto_comments
            hub_auto_comments.enqueue(self,tid)
        finally:
            part.unlink(missing_ok=True)

    def stats(self):
        scalar = lambda sql: self.store.rows(sql)[0]['n']
        return {
            'library':self.library.stats(),
            'accounts':self.accounts.public(),
            'tracks':scalar('SELECT COUNT(*) n FROM tracks'),
            'downloaded':sum(__import__('hub_folders').saved_file(self,r) is not None for r in self.store.rows('SELECT id,path FROM tracks')),
            'bytes':scalar('SELECT COALESCE(SUM(bytes),0) n FROM tracks'),
            'rules':scalar('SELECT COUNT(*) n FROM rules'),
            'groups':scalar('SELECT COUNT(DISTINCT group_name) n FROM rules'),
            'saved_copies':scalar('SELECT COUNT(*) n FROM sources')-scalar('SELECT COUNT(*) n FROM tracks'),
            'sources':self.store.rows('SELECT platform,COUNT(*) count FROM sources GROUP BY platform'),
            'jobs':self.store.rows("SELECT status,COUNT(*) count FROM jobs WHERE status!='cleared' GROUP BY status"),
            'daily':self.store.rows("SELECT strftime('%Y-%m-%d',updated,'unixepoch','+8 hours') day,COUNT(*) count FROM jobs WHERE kind='download' AND status='done' AND updated>? GROUP BY day ORDER BY day",(time.time()-7*86400,)),
            'music_root':str(self.root)
        }
