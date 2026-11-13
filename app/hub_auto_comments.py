"""Durable, bounded comment backfill independent of audio download success."""
import json
import time


def initialize(engine):
    with engine.store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS auto_comments(
            track_id TEXT PRIMARY KEY, task_id TEXT, due REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending')''')


def enqueue(engine, tid):
    with engine.store.connect() as db:
        db.execute('INSERT OR IGNORE INTO auto_comments(track_id) VALUES(?)', (tid,))


def seed(engine):
    import hub_folders
    for row in engine.store.rows('SELECT * FROM tracks WHERE path IS NOT NULL'):
        if hub_folders.destinations(engine, row['id']) and hub_folders.saved_file(engine, row):
            enqueue(engine, row['id'])


def pump(engine):
    bridge = engine.bridge
    if not bridge or not bridge.get('BRIDGE', {}).get('store'):
        return
    active = engine.store.rows('SELECT DISTINCT task_id FROM auto_comments WHERE task_id IS NOT NULL')
    for row in active:
        task = bridge['BRIDGE']['store'].get(row['task_id'])
        if task and task['status'] in ('queued', 'running'):
            return
        # Partial results retain provider backoff and are retried later, never in a tight loop.
        success = task and task['status'] == 'succeeded'
        with engine.store.connect() as db:
            db.execute('''UPDATE auto_comments SET task_id=NULL,status=?,due=?
                WHERE task_id=?''', ('done' if success else 'pending', time.time()+3600, row['task_id']))
    if engine.comment_directories.pump():return
    rows = engine.store.rows("SELECT track_id FROM auto_comments WHERE status='pending' AND due<=? ORDER BY attempts,track_id", (time.time(),))
    directories=engine.store.rows('SELECT path FROM comment_directories')
    candidates=[]
    for row in rows:
        if engine.comment_directories.automatic_allowed(row['track_id'],directories):candidates.append(row)
        if len(candidates)==3:break
    rows=candidates
    if not rows:
        return
    ids = [r['track_id'] for r in rows]
    try:
        task = bridge['submit_task']('comment_refresh', {'hub_track_ids': ids, 'comment_count': 100, 'batch_size': 3})
    except Exception:
        return  # Busy bridge retains durable intent.
    with engine.store.connect() as db:
        db.executemany('UPDATE auto_comments SET task_id=?,attempts=attempts+1 WHERE track_id=?', [(task['id'], tid) for tid in ids])


def catalog(engine, ids):
    from hub_comments import query
    import os
    from pathlib import Path
    path = Path(os.environ.get('COMMENTHUB_DB', str(engine.store.home/'commenthub.db')))
    result = []
    for tid in list(dict.fromkeys(ids))[:3]:
        rows = engine.store.rows('SELECT * FROM tracks WHERE id=?', (tid,))
        if not rows:
            continue
        row = rows[0]
        meta = json.loads(row['meta'])
        existing = query(path, title=meta['title'], artist='/'.join(meta.get('artists') or []), limit=1)
        result.append({'guid': existing.get('trackGuid') or 'hub-'+tid, 'title': meta['title'],
                       'artists': meta.get('artists') or [], 'album': meta.get('album') or '',
                       'duration_ms': row['duration'], 'eligible': True})
    return result
