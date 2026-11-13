"""Explicit playlist destinations and account playlist catalog; never removes audio."""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path


def initialize(e):
    with e.store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS hub_catalog_parents(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hub_catalog_children(id TEXT PRIMARY KEY,parent_id TEXT NOT NULL,source_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hub_group_dirs(name TEXT PRIMARY KEY,directory TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hub_group_files(name TEXT,track_id TEXT,path TEXT,PRIMARY KEY(name,track_id));''')
        for r in db.execute("SELECT * FROM rules WHERE kind IN ('created','favorited')").fetchall():
            db.execute('INSERT OR IGNORE INTO hub_catalog_parents VALUES(?,?)',(r['id'],json.dumps(dict(r))))
            db.execute('UPDATE rules SET enabled=0,auto_download=0 WHERE id=?',(r['id'],))
            db.execute("UPDATE jobs SET status='cancelled' WHERE dedupe=? AND status IN ('queued','running')",('sync:'+r['id'],))
    for p in e.store.rows('SELECT * FROM hub_catalog_parents'):
        r=json.loads(p['data'])
        cached=e.store.rows("SELECT source_id id,name FROM source_playlists WHERE rule_id=? AND state='active'",(r['id'],))
        materialize(e,r,cached,False)


def materialize(e,parent,items,authoritative=True):
    ids=set()
    with e.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for pl in items:
            sid=str(pl['id']);rid='catalog-'+hashlib.sha256((parent['id']+'\0'+sid).encode()).hexdigest()[:24];ids.add(rid)
            old=db.execute('SELECT id FROM rules WHERE id=?',(rid,)).fetchone()
            if not old:
                db.execute('INSERT INTO rules(id,name,platform,kind,ref,group_name,interval_hours,enabled,auto_download,quality,next_run) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (rid,pl.get('name') or sid,parent['platform'],'playlist',sid,'待分类',parent['interval_hours'],0,1,parent['quality'],0))
                db.execute('INSERT OR IGNORE INTO membership SELECT ?,track_id,rowid FROM source_members WHERE rule_id=? AND playlist_id=?',(rid,parent['id'],sid))
            else:db.execute('UPDATE rules SET name=? WHERE id=?',(pl.get('name') or sid,rid))
            db.execute('INSERT OR IGNORE INTO hub_catalog_children VALUES(?,?,?)',(rid,parent['id'],sid))
        if authoritative:
            for row in db.execute('SELECT id FROM hub_catalog_children WHERE parent_id=?',(parent['id'],)).fetchall():
                if row['id'] not in ids:
                    db.execute('UPDATE rules SET enabled=0,error=? WHERE id=?',('来源歌单已移除',row['id']))
                    db.execute('DELETE FROM membership WHERE rule_id=?',(row['id'],))
                    db.execute("UPDATE jobs SET status='cancelled' WHERE dedupe=? AND status IN ('queued','running')",('sync:'+row['id'],))
        # Membership now belongs to concrete children; retain parent catalog snapshots.
        if items or authoritative:db.execute('DELETE FROM membership WHERE rule_id=?',(parent['id'],))
        db.execute('UPDATE pipeline SET revision=revision+1 WHERE id=1')


def refresh(e):
    initialize(e)
    count=0;errors=[]
    for p in e.store.rows('SELECT * FROM hub_catalog_parents'):
        r=json.loads(p['data'])
        account=e.accounts.private(r['platform'])
        if r['ref'].startswith('account:') and r['ref']!='account:'+str(account.get('uid','')):continue
        try:
            items=e.providers.discover(r['platform'],r['ref'],r['kind'])
            materialize(e,r,items);count+=len(items)
        except Exception as exc:
            from hub_errors import describe
            errors.append(r['name']+'：'+describe(exc)[1])
    return {'count':count,'errors':errors}


def directory(e,value):
    value=value.strip()
    if not value:raise ValueError('请指定保存目录，例如 我的喜欢')
    p=Path(value)
    if not p.is_absolute():p=e.root/p
    p=p.resolve()
    if not p.is_relative_to(e.root) or p==e.root:raise ValueError('保存目录必须是音乐目录内的子文件夹')
    if p==e.root/'Playlists' or p.is_relative_to(e.root/'Playlists'):raise ValueError('Playlists 用于歌单文件，请选择其他目录')
    p.mkdir(parents=True,exist_ok=True)
    return str(p.relative_to(e.root))


def destinations(e,tid):
    return e.store.rows("SELECT DISTINCT d.name,d.directory FROM hub_group_dirs d JOIN rules r ON r.group_name=d.name JOIN membership m ON m.rule_id=r.id WHERE r.enabled=1 AND r.kind NOT IN ('created','favorited') AND m.track_id=?",(tid,))


def audio_file(e, value):
    """An existing, nonempty audio inside the configured root, never a stale DB flag."""
    if not value:return None
    path=(e.root/value).resolve()
    if not path.is_relative_to(e.root) or path.suffix.lower() not in ('.mp3','.flac','.m4a','.ogg','.opus','.wav','.aac'):return None
    try:return path if path.is_file() and path.stat().st_size>0 else None
    except OSError:return None


def saved_file(e, track, group=None):
    candidates=[]
    if group:
        dirs=e.store.rows('SELECT directory FROM hub_group_dirs WHERE name=?',(group,))
        if not dirs:return None
        dest=(e.root/dirs[0]['directory']).resolve()
        candidates=[r['path'] for r in e.store.rows('SELECT path FROM hub_group_files WHERE name=? AND track_id=?',(group,track['id']))]
        if track.get('path'):candidates.extend([str(dest/Path(track['path']).name),track['path']])
    else:
        dest=None
        candidates=[track.get('path')]+[r['path'] for r in e.store.rows('SELECT path FROM hub_group_files WHERE track_id=?',(track['id'],))]
    for value in candidates:
        file=audio_file(e,value)
        if file and (dest is None or file.parent==dest):return file
    return None


def counts(e, group, rule_id=None):
    sql="SELECT DISTINCT t.id,t.path FROM tracks t JOIN membership m ON m.track_id=t.id JOIN rules r ON r.id=m.rule_id WHERE r.enabled=1 AND r.kind NOT IN ('created','favorited') AND r.group_name=?"
    args=[group]
    if rule_id:sql+=' AND r.id=?';args.append(rule_id)
    rows=e.store.rows(sql,args)
    return {'total':len(rows),'downloaded':sum(saved_file(e,r,group) is not None for r in rows)}


def publish(e):
    for g in e.store.rows('SELECT * FROM hub_group_dirs'):
        dest=e.root/directory(e,g['directory'])
        rows=e.store.rows("SELECT DISTINCT t.* FROM tracks t JOIN membership m ON m.track_id=t.id JOIN rules r ON r.id=m.rule_id WHERE r.enabled=1 AND r.kind NOT IN ('created','favorited') AND r.group_name=? AND t.path IS NOT NULL",(g['name'],))
        for row in rows:
            src=(e.root/row['path']).resolve()
            if not src.is_relative_to(e.root) or not src.is_file():continue
            target=dest/src.name
            if target.resolve()!=src:
                if target.exists():
                    if not target.is_file() or target.stat().st_size!=src.stat().st_size:raise ValueError('分类目录存在同名不同文件，请更换目录')
                else:
                    try:os.link(src,target)
                    except OSError:
                        tmp=target.with_suffix(target.suffix+'.copying')
                        try:shutil.copy2(src,tmp);os.replace(tmp,target)
                        finally:tmp.unlink(missing_ok=True)
                lyric=src.with_suffix('.lrc')
                if lyric.is_file() and not target.with_suffix('.lrc').exists():shutil.copy2(lyric,target.with_suffix('.lrc'))
                for suffix in ('.jpg','.png'):
                    cover=src.with_suffix(suffix)
                    if cover.is_file() and not target.with_suffix(suffix).exists():shutil.copy2(cover,target.with_suffix(suffix))
            with e.store.connect() as db:
                db.execute('INSERT OR REPLACE INTO hub_group_files VALUES(?,?,?)',(g['name'],row['id'],str(target.relative_to(e.root))))


def selected(e,rid):
    return bool(e.store.rows("SELECT r.id FROM rules r JOIN hub_group_dirs d ON d.name=r.group_name WHERE r.id=? AND r.enabled=1 AND r.kind NOT IN ('created','favorited')",(rid,)))


def save_group(e,name,rule_ids,folder,previous=''):
    """Atomically move selected sources and retire only orphaned directory records."""
    if not rule_ids:raise ValueError('请至少选择一条订阅')
    with e.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for rid in rule_ids:
            if not db.execute("SELECT id FROM rules WHERE id=? AND kind NOT IN ('created','favorited')",(rid,)).fetchone():
                raise ValueError('订阅不存在，请刷新后重试')
        if previous and name!=previous and db.execute("SELECT 1 FROM rules WHERE group_name=? AND kind NOT IN ('created','favorited')",(name,)).fetchone():
            raise ValueError('已有同名融合歌单，请使用其他名称或编辑已有歌单')
        # Old renamed groups may still reserve the same physical directory.
        collisions=db.execute('SELECT name FROM hub_group_dirs WHERE directory=? AND name NOT IN (?,?)',(folder,name,previous)).fetchall()
        for row in collisions:
            active=db.execute("SELECT 1 FROM rules WHERE group_name=? AND kind NOT IN ('created','favorited')",(row['name'],)).fetchone()
            if active:raise ValueError('该目录已用于融合歌单“'+row['name']+'”，请选择独立目录')
            db.execute('DELETE FROM hub_group_dirs WHERE name=?',(row['name'],))
        if previous:
            for row in db.execute('SELECT id FROM rules WHERE group_name=?',(previous,)).fetchall():
                if row['id'] not in rule_ids:db.execute("UPDATE rules SET group_name='待分类',enabled=0 WHERE id=?",(row['id'],))
        db.executemany('UPDATE rules SET group_name=?,enabled=1 WHERE id=?',[(name,rid) for rid in rule_ids])
        db.execute('INSERT OR REPLACE INTO hub_group_dirs VALUES(?,?)',(name,folder))
        if previous and previous!=name:
            db.execute('DELETE FROM hub_group_dirs WHERE name=?',(previous,))
        db.execute('UPDATE pipeline SET revision=revision+1,retry_at=0 WHERE id=1')
