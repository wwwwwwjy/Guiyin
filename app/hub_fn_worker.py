"""Runs ONLY inside Bridge's playlist mutex. Bind each group to its own files."""
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import fnsync
from hub_store import Store, norm


def native_paths():
    path=Path(os.environ.get('FNMUSIC_DB_DIR','/fndb'))/'music.db'
    if not path.is_file():
        return None
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        return {str(g):str(p) for g,p in db.execute('''SELECT t.guid,af.path FROM track t
            JOIN audio_file af ON af.id=t.audio_file_id WHERE af.is_physical_file_deleted=0
            AND t.is_audio_file_deleted=0 AND t.is_admin_deleted=0''')}
    finally:
        db.close()


def match(rows, visible, paths, nas_root):
    result={}
    for row in rows:
        if paths is not None and nas_root:
            hits=[]
            for relative in row.get('paths',[row['path']]):
                expected=str(Path(nas_root)/relative)
                hits=sorted({t['guid'] for t in visible if paths.get(t['guid'])==expected})
                if hits:break
            # More than one native index row can refer to the exact same file.
            # Pick one stable identity; aliases below retire the redundant links.
            hits=hits[:1]
        else:
            meta=json.loads(row['meta'])
            artists=sorted(norm(a) for a in meta.get('artists',[]))
            hits=[t['guid'] for t in visible if artists and row['duration'] and
                  norm(t.get('title',''))==norm(meta['title']) and
                  sorted(norm(a['name']) for a in t.get('artists',[]) if a.get('name'))==artists and
                  t.get('duration') and abs(int(t['duration'])-row['duration'])<=2000]
        if len(hits)==1:
            result[row['id']]=hits[0]
    return result


def folder_rows(store, rows, group, directory=None):
    """A shared logical track has a different native file in each destination."""
    if directory is None:
        configured=store.rows('SELECT directory FROM hub_group_dirs WHERE name=?',(group,))
        directory=configured[0]['directory'] if configured else ''
    if not directory:return []
    folder=Path(directory)
    desired={r['track_id'] for r in store.rows('''SELECT DISTINCT m.track_id FROM membership m
        JOIN rules r ON r.id=m.rule_id WHERE r.group_name=?''',(group,))}
    result=[]
    for row in rows:
        if row['id'] not in desired:continue
        candidates=[p for p in row.get('paths',[row['path']]) if Path(p).parent==folder]
        if candidates:result.append({**row,'path':candidates[0],'paths':candidates})
    return result


def file_aliases(rows, paths, nas_root):
    """Only exact known file paths prove two native GUIDs are the same track.

    A title/artist match alone must never remove a user's different recording.
    """
    if paths is None or not nas_root:return {}
    by_path={}
    for guid,path in paths.items():by_path.setdefault(path,set()).add(guid)
    return {row['id']:set().union(*(by_path.get(str(Path(nas_root)/p),set())
            for p in row.get('paths',[row['path']]))) for row in rows}


def target(store, fn, user, group):
    rows=store.rows('SELECT * FROM fn_targets WHERE user=? AND group_name=?',(user,group))
    if not rows:
        # Persist a unique creation intent BEFORE the remote write. On restart, recover only
        # this reserved exact name, never take over an unrelated same-name playlist.
        suffix=hashlib.sha256((user+'\0'+group).encode()).hexdigest()[:8]
        name='归音 · '+group[:35]+' ['+suffix+']'
        with store.connect() as db:
            db.execute('INSERT INTO fn_targets(user,group_name,name) VALUES(?,?,?)',(user,group,name))
        rows=store.rows('SELECT * FROM fn_targets WHERE user=? AND group_name=?',(user,group))
    record=rows[0]
    if not record['guid'] and len(record['name'])>50:
        record['name']='归音 · '+group[:30]+' ['+hashlib.sha256((user+'\0'+group).encode()).hexdigest()[:8]+']'
        with store.connect() as db:db.execute('UPDATE fn_targets SET name=? WHERE user=? AND group_name=?',(record['name'],user,group))
    clean_name=('归音 · '+group)[:50]
    if record['guid']:
        if record['name']!=clean_name:
            fn.playlist_edit(record['guid'],clean_name)
            with store.connect() as db:db.execute('UPDATE fn_targets SET name=? WHERE user=? AND group_name=?',(clean_name,user,group))
        return {'guid':record['guid'],'name':clean_name}
    candidates=[p for p in fn.playlists() if p.get('name')==record['name']]
    if len(candidates)>1:
        raise ValueError('飞牛出现重复的托管歌单，请在飞牛音乐中核对重名歌单')
    playlist=candidates[0] if candidates else fn.playlist_create(record['name'])
    if not playlist.get('guid'):
        raise ValueError('飞牛未返回新歌单 GUID')
    with store.connect() as db:
        db.execute('UPDATE fn_targets SET guid=? WHERE user=? AND group_name=?',(playlist['guid'],user,group))
    # The recovery marker is only a creation intent; once GUID is durable it
    # belongs in our database, never in the user's visible playlist name.
    return target(store,fn,user,group)


def managed_groups(store, user):
    """Never create targets for unclassified/catalog-only subscriptions."""
    scoped=bool(store.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='hub_group_dirs'"))
    query='SELECT name group_name FROM hub_group_dirs' if scoped else 'SELECT group_name FROM rules'
    names={r['group_name'] for r in store.rows(query)}
    names.update(r['group_name'] for r in store.rows("SELECT group_name FROM fn_targets WHERE user=? AND guid IS NOT NULL AND guid!=''",(user,)))
    return names


def owned_delta(store, fn, playlist, user, group, matched, aliases=None, expected_directory=None):
    """Retain one file identity per logical member of this managed playlist.

    BEGIN IMMEDIATE serializes source snapshot commits/config edits with this write.
    Durable snapshots record attempted operations; a crash before ownership commit
    leaves additions external, never guessed-owned on recovery.
    """
    guid=playlist['guid']
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if expected_directory is not None:
            current=db.execute('SELECT directory FROM hub_group_dirs WHERE name=?',(group,)).fetchone()
            if (current[0] if current else '')!=expected_directory:
                raise ValueError('保存目录已变更，稍后重新核对歌单关联')
        desired={r[0] for r in db.execute('SELECT DISTINCT m.track_id FROM membership m JOIN rules r ON r.id=m.rule_id WHERE r.group_name=?',(group,))}
        for tid, native in matched.items():
            db.execute('INSERT OR IGNORE INTO fn_bindings VALUES(?,?,?)',(user,tid,native))
        scoped=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hub_group_dirs'").fetchone()
        if scoped and not db.execute('SELECT 1 FROM hub_group_dirs WHERE name=?',(group,)).fetchone():desired=set()
        bindings=[tuple(r) for r in db.execute('SELECT track_id,track_guid FROM fn_bindings WHERE user=?',(user,))]
        want={matched[tid] for tid in desired if tid in matched}
        # Protect historic identities only while the replacement is unavailable.
        # Protecting every old binding forever accumulated copies after a rescan
        # or after the same song was saved to another fusion directory.
        protected=want|{native for tid,native in bindings if tid in desired and tid not in matched}
        old=[r['guid'] for r in fn.playlist_tracks(guid)]
        owned={r[0] for r in db.execute('SELECT track_guid FROM fn_owned WHERE user=? AND target_guid=?',(user,guid))}
        # If a user removed an owned relation independently, stop claiming it.
        for native in owned-set(old):
            db.execute('DELETE FROM fn_owned WHERE user=? AND target_guid=? AND track_guid=?',(user,guid,native))
        order=[r[0] for r in db.execute('''SELECT m.track_id FROM membership m
            JOIN rules r ON r.id=m.rule_id WHERE r.group_name=?
            GROUP BY m.track_id ORDER BY MIN(m.position),m.track_id''',(group,))]
        add=list(dict.fromkeys(matched[tid] for tid in order
                              if tid in matched and matched[tid] in want-set(old)))
        redundant=set().union(*(aliases.get(tid,set()) for tid in desired if tid in matched)) if aliases else set()
        # An older release may have left a duplicate unowned. Exact path evidence
        # permits retiring that relation too, but never an unrelated manual song.
        remove=sorted(((owned & set(old))-protected)|((redundant & set(old))-want))
        if not add and not remove: return 0,0
        snapshot=fnsync._playlist_snapshot(fn,playlist,old)
        fnsync._snapshot_status(snapshot,'prepared',mode='owned_delta',planned_add=add,planned_remove=remove)
        try:
            # FnMusic prepends each batch; reverse batches to retain source order.
            chunks=[add[offset:offset+100] for offset in range(0,len(add),100)]
            for chunk in reversed(chunks):
                fnsync._checked_track_write(fn,'playlist_add_tracks',guid,chunk)
                now={r['guid'] for r in fn.playlist_tracks(guid)}
                if not set(chunk)<=now: raise ValueError('飞牛新增关联未通过读回验证')
                db.executemany('INSERT OR IGNORE INTO fn_owned VALUES(?,?,?)',[(user,guid,g) for g in chunk])
            for offset in range(0,len(remove),100):
                chunk=remove[offset:offset+100]
                fnsync._checked_track_write(fn,'playlist_remove_tracks',guid,chunk)
                now={r['guid'] for r in fn.playlist_tracks(guid)}
                if set(chunk)&now: raise ValueError('飞牛删除关联未通过读回验证')
                db.executemany('DELETE FROM fn_owned WHERE user=? AND target_guid=? AND track_guid=?',[(user,guid,g) for g in chunk])
            final={r['guid'] for r in fn.playlist_tracks(guid)}
            if not ((set(old)-set(remove))|want)<=final:
                raise ValueError('飞牛歌单同期发生变化，已保留快照供核对')
            fnsync._snapshot_status(snapshot,'verified',mode='owned_delta',added=add,removed=remove)
        except BaseException:
            fnsync._snapshot_status(snapshot,'unconfirmed',mode='owned_delta')
            raise
        return len(add),len(remove)


def local_cover(audio):
    """Prefer same-stem artwork, then the embedded picture. Never follow links."""
    def valid(data):
        if len(data)>8*1024*1024:return None
        if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):return data,'image/jpeg'
        if data.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in data[-12:]:return data,'image/png'
    for ext in ('.jpg','.jpeg','.png'):
        p=audio.with_suffix(ext)
        if not p.is_symlink() and p.is_file() and p.stat().st_size<=8*1024*1024:
            image=valid(p.read_bytes())
            if image:return image
    try:
        import mutagen,base64
        from mutagen.flac import Picture
        raw=mutagen.File(audio)
        for picture in getattr(raw,'pictures',[]):
            image=valid(picture.data)
            if image:return image
        for key,value in (raw.tags or {}).items():
            if str(key).startswith('APIC'):data=value.data
            elif key=='covr':data=bytes(value[0])
            elif key=='metadata_block_picture':data=Picture(base64.b64decode(value[0])).data
            else:continue
            image=valid(data)
            if image:return image
    except Exception:pass
    return None


def update_cover(store, fn, playlist, group, present, matched, root):
    """Use source playlist order, never download time, to select the first cover."""
    ordered=store.rows('SELECT m.track_id,MIN(m.position) position FROM membership m JOIN rules r ON r.id=m.rule_id WHERE r.group_name=? GROUP BY m.track_id ORDER BY position,m.track_id',(group,))
    order={r['track_id']:i for i,r in enumerate(ordered)}
    desired=set(order)
    members={r['guid']:r for r in fn.playlist_tracks(playlist['guid'])}
    candidates=[]
    for row in present:
        native=members.get(matched.get(row['id']))
        if row['id'] in desired and native:
            if not (root/row['path']).is_file():continue
            candidates.append((order[row['id']],row['id'],row['path']))
    if not candidates:return 0
    artwork=next((image for _,_,path in sorted(candidates) if (image:=local_cover(root/path))),None)
    if not artwork:return 0
    fingerprint=hashlib.sha256(artwork[0]).hexdigest()
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS fn_playlist_covers(target_guid TEXT PRIMARY KEY,fingerprint TEXT,cover_id TEXT)')
    cached=store.rows('SELECT * FROM fn_playlist_covers WHERE target_guid=?',(playlist['guid'],))
    cover=cached[0]['cover_id'] if cached and cached[0]['fingerprint']==fingerprint else None
    detail=fn.get('/playlist/detail',guid=playlist['guid'])
    if detail.get('code')!=0:raise ValueError('无法读取飞牛歌单封面')
    if cover and str((detail.get('data') or {}).get('coverId') or '')==cover:return 0
    if not cover:
        cover=fn.upload_playlist_cover(*artwork)
        with store.connect() as db:
            db.execute('INSERT OR REPLACE INTO fn_playlist_covers VALUES(?,?,?)',(playlist['guid'],fingerprint,cover))
    # Native fnOS releases validate name even when only the cover changes.
    result=fn.post('/playlist/edit',{'guid':playlist['guid'],'name':playlist.get('name') or ('归音 · '+group)[:50],'coverId':cover})
    if result.get('code')!=0:raise ValueError(f"飞牛未接受歌单封面（状态 {result.get('code')}）")
    check=fn.get('/playlist/detail',guid=playlist['guid'])
    if check.get('code')!=0 or str((check.get('data') or {}).get('coverId') or '')!=cover:
        raise ValueError('飞牛歌单封面未通过读回验证')
    return 1


def reconcile(store, cfg, fn, revision, timeout=45, pause=time.sleep, clock=time.monotonic, path_reader=native_paths):
    if not cfg.get('fn_user'):
        return {'status':'partial','summary':'等待连接飞牛音乐账号','counts':{}}
    root=Path(cfg.get('music_dir') or os.environ.get('MUSIC_DIR','/music')).resolve()
    scoped=bool(store.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='hub_group_dirs'"))
    rows=store.rows("SELECT DISTINCT t.* FROM tracks t JOIN membership m ON m.track_id=t.id JOIN rules r ON r.id=m.rule_id "+
                   ("JOIN hub_group_dirs d ON d.name=r.group_name " if scoped else '')+
                   "WHERE t.path IS NOT NULL AND r.kind NOT IN ('created','favorited')")
    present=[];missing_files=[]
    for row in rows:
        candidates=[]
        if scoped:
            candidates += [r['path'] for r in store.rows('SELECT f.path FROM hub_group_files f JOIN hub_group_dirs d ON d.name=f.name WHERE f.track_id=?',(row['id'],))]
            candidates += [str(Path(r['directory'])/Path(row['path']).name) for r in store.rows('SELECT DISTINCT d.directory FROM hub_group_dirs d JOIN rules r ON r.group_name=d.name JOIN membership m ON m.rule_id=r.id WHERE m.track_id=?',(row['id'],))]
        candidates.append(row['path'])
        valid=[]
        for path in dict.fromkeys(candidates):
            file=(root/path).resolve()
            if file.is_relative_to(root) and file.is_file():valid.append(str(file.relative_to(root)))
        if not valid:
            missing_files.append(row['id']);continue
        # Prefer configured folder copies, but retain all exact paths for NAS
        # indexing differences; never match an unrelated song by title alone.
        present.append({**row,'path':valid[0],'paths':valid})
    group_names=[r['name'] for r in store.rows('SELECT name FROM hub_group_dirs')] if scoped else []
    required_rows=[r for name in group_names for r in folder_rows(store,present,name)] if scoped else present
    required_paths={p for row in required_rows for p in row['paths']}
    matched, paths, visible = {}, None, []
    if present:
        libraries=fn.get('/shared-library/list',lan='zh-CN')
        if libraries.get('code')!=0 or not isinstance((libraries.get('data') or {}).get('list'),list):
            raise ValueError('无法读取飞牛共享音乐库')
        selected=[x for x in libraries['data']['list'] if not cfg.get('fn_library') or x.get('guid')==cfg['fn_library']]
        if not selected:
            raise ValueError('未找到配置的飞牛音乐库，请检查库 GUID')
        scan_state=store.home/'fn-scan-times.json'
        try:scan_times=json.loads(scan_state.read_text())
        except (OSError,ValueError):scan_times={}
        initial_paths=path_reader()
        known=set((initial_paths or {}).values())
        missing_paths=not cfg.get('fn_music_root') or any(str(Path(cfg['fn_music_root'])/p) not in known for p in required_paths)
        for library in selected:
            # Repeated requests can restart a slow full-library scan. Let the NAS
            # continue its current pass while reconciling already visible files.
            key=library['guid']
            if missing_paths and time.time()-scan_times.get(key,0)>=15:
                if fn.scan_library(key).get('code')!=0:
                    raise ValueError('飞牛未接受扫描请求，稍后自动重试')
                scan_times[key]=time.time()
                temp=scan_state.with_suffix('.tmp');temp.write_text(json.dumps(scan_times));temp.replace(scan_state)
        deadline=clock()+timeout
        while True:
            paths=path_reader()
            # During a large scan the visible catalog grows between pages. Exact
            # physical-path additions do not require an all-or-nothing catalog.
            # Removals still use completed source snapshots and stored bindings.
            try:visible=fn.export_library(strict=not (paths is not None and cfg.get('fn_music_root')))
            except RuntimeError:
                if clock()>=deadline:raise
                pause(5)
                continue
            matched=match(present,visible,paths,cfg.get('fn_music_root',''))
            # All group copies must be indexed, not only the first global copy.
            indexed_paths=set((paths or {}).values())
            copies_ready=(all(str(Path(cfg['fn_music_root'])/p) in indexed_paths for p in required_paths)
                          if paths is not None and cfg.get('fn_music_root') else len(matched)==len(present))
            if copies_ready or clock()>=deadline:
                break
            pause(5)
    # Paused rules retain their last complete snapshot. A failed scan cannot remove
    # references: desired includes logical tracks with no downloaded/indexed file.
    names=managed_groups(store,cfg['fn_user'])
    aliases=file_aliases(present,paths,cfg.get('fn_music_root',''))
    added=removed=covers=group_missing=0;cover_errors=[];group_errors=[]
    for group in sorted(names):
        existing=store.rows('SELECT guid,name FROM fn_targets WHERE user=? AND group_name=?',(cfg['fn_user'],group))
        desired=store.rows('SELECT DISTINCT m.track_id FROM membership m JOIN rules r ON r.id=m.rule_id WHERE r.group_name=?',(group,))
        directories=store.rows('SELECT directory FROM hub_group_dirs WHERE name=?',(group,)) if scoped else []
        group_directory=directories[0]['directory'] if directories else ''
        configured=not scoped or bool(directories)
        if not existing and not configured:continue
        group_present=folder_rows(store,present,group,directory=group_directory) if scoped else present
        # Without native paths, a metadata-only lookup cannot distinguish copies
        # in different folders. Retain prior relations until exact paths return.
        group_matched=(match(group_present,visible,paths,cfg.get('fn_music_root',''))
                       if not scoped or (paths is not None and cfg.get('fn_music_root')) else {})
        if configured:group_missing+=len({r['track_id'] for r in desired}-set(group_matched))
        try:
            if existing and existing[0]['guid']:
                detail=fn.get('/playlist/detail',guid=existing[0]['guid'])
                if detail.get('code')==100005:
                    # A remotely deleted target must not block the remaining
                    # groups. Retire only our local ownership of the missing GUID.
                    with store.connect() as db:
                        db.execute('DELETE FROM fn_owned WHERE user=? AND target_guid=?',(cfg['fn_user'],existing[0]['guid']))
                        db.execute('DELETE FROM fn_targets WHERE user=? AND group_name=?',(cfg['fn_user'],group))
                    configured=not scoped or bool(store.rows('SELECT 1 FROM hub_group_dirs WHERE name=?',(group,)))
                    if not configured:continue
                elif detail.get('code')!=0:
                    raise ValueError('飞牛歌单暂时无法读取，保留原关联')
            pl=target(store,fn,cfg['fn_user'],group)
            counts=owned_delta(store,fn,pl,cfg['fn_user'],group,group_matched,aliases=aliases,
                               expected_directory=group_directory if scoped else None)
        except Exception as exc:
            group_errors.append(group+'：'+str(exc));continue
        added+=counts[0]; removed+=counts[1]
        try:
            covers+=update_cover(store,fn,pl,group,group_present,group_matched,root)
        except Exception as exc:
            cover_errors.append(group+'：'+str(exc))

    missing=len(present)-len(matched)
    return {'status':'partial' if missing or missing_files or group_missing or cover_errors or group_errors else 'succeeded',
            'summary':f'飞牛已入库 {len(matched)}/{len(present)} 首，新增 {added} 条、移除 {removed} 条歌单关联'+('；等待扫描继续' if missing else '')+(f'；{len(missing_files)} 首文件缺失已跳过' if missing_files else '')+(f'；{group_missing} 条关联等待对应保存目录入库' if group_missing else '')+(f'；更新 {covers} 张歌单封面')+('；封面更新待重试：'+'、'.join(cover_errors) if cover_errors else '')+('；部分歌单待重试：'+'、'.join(group_errors) if group_errors else ''),
            'counts':{'expected':len(present),'indexed':len(matched),'added':added,'missing':missing,'groups':len(names),'removed':removed,'missing_files':len(missing_files),'group_missing':group_missing,'covers':covers},
            'skip_reasons':{'verification':'path' if paths is not None and cfg.get('fn_music_root') else 'unique_metadata'}}


def main():
    store=Store(os.environ.get('FNMUSIC_HOME','/data'),recover_running=False)
    try:
        cfg=json.loads((store.home/'hub-settings.json').read_text())
        result=reconcile(store,cfg,fnsync.FnMusic(cfg.get('fn_user')),int(sys.argv[1]))
    except (Exception,SystemExit) as exc:
        from hub_errors import describe
        code,message,_=describe(exc)
        result={'status':'failed','summary':message,'error_type':type(exc).__name__,'error_code':code}
    print('@@R '+json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['status'] in ('succeeded','partial') else 1


if __name__=='__main__':
    sys.exit(main())
