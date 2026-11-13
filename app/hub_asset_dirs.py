"""Persistent independent directory scopes for conservative sidecar completion.

One worker serializes writes even for overlapping directories. Pausing interrupts
at safe checkpoints; subsequent runs inspect disk again before any publication.
"""
import hashlib
import json
import threading
import time
from hub_assets import Assets


class AssetDirectories:
    table = "asset_directories"
    worker_type = Assets
    success_status = "已补全"
    interval = 300
    def __init__(self, engine):
        self.e = engine
        self.lock = threading.RLock()
        self.active = None
        self.thread = None
        self.workers = {}
        with engine.store.connect() as db:
            db.execute(f'CREATE TABLE IF NOT EXISTS {self.table} (id TEXT PRIMARY KEY, path TEXT UNIQUE, enabled INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, snapshot TEXT NOT NULL DEFAULT "{{}}")')

    def add(self, path):
        path = str(self.e.library.confined(path))
        key = hashlib.sha256(path.encode()).hexdigest()[:24]
        with self.lock, self.e.store.connect() as db:
            db.execute(f'INSERT OR IGNORE INTO {self.table}(id,path) VALUES (?,?)', (key,path))
        return key

    def control(self, key, action):
        if action not in ('pause','resume','scan'):
            raise ValueError('未知操作')
        with self.lock:
            rows = self.e.store.rows(f'SELECT * FROM {self.table} WHERE id=?', (key,))
            if not rows:
                raise ValueError('目录不存在')
            if action != 'pause':
                self.e.library.confined(rows[0]['path'])
            with self.e.store.connect() as db:
                if action == 'pause':
                    db.execute(f'UPDATE {self.table} SET enabled=0 WHERE id=?', (key,))
                    if key == self.active:
                        self.workers[key].stop.set()
                else:
                    db.execute(f'UPDATE {self.table} SET enabled=1,due=0 WHERE id=?', (key,))
            # A paused worker must exit before a resume can start another worker.
        self.pump()

    def pump(self):
        with self.lock:
            if self.e.stop.is_set() or self.active is not None or self.e.assets.running:
                return
            rows = self.e.store.rows(f'SELECT * FROM {self.table} WHERE enabled=1 AND due<=? ORDER BY due,id LIMIT 1', (time.time(),))
            if not rows:
                return
            row = rows[0]
            worker = self.worker_type(self.e, [row['path']])
            last_checkpoint = [0]
            def save_progress():
                if time.monotonic()-last_checkpoint[0] >= 2:
                    self.checkpoint(row['id'],worker)
                    last_checkpoint[0] = time.monotonic()
            worker.checkpoint = save_progress
            self.workers[row['id']] = worker
            self.active = row['id']
            worker.running = True
            self.thread = threading.Thread(target=self.run, args=(row['id'],worker), daemon=True, name='hub-asset-directories')
            self.thread.start()

    def checkpoint(self, key, worker):
        with worker.lock:
            snapshot = dict(phase=worker.phase,scanned=worker.scanned,error=worker.error,rows=worker.rows)
            payload = json.dumps(snapshot, ensure_ascii=False)
        with self.e.store.connect() as db:
            db.execute(f'UPDATE {self.table} SET snapshot=? WHERE id=?', (payload,key))

    def run(self, key, worker):
        try:
            worker.phase = '正在扫描'
            worker.scan()
            if not worker.cancelled():
                worker.phase = '正在匹配'
                self.checkpoint(key,worker)
                worker.match()
            worker.phase = '已暂停' if worker.cancelled() else '本轮完成，等待检查新增文件'
        except Exception as exc:
            from hub_errors import describe
            worker.error = describe(exc)[1]
            worker.phase = '处理失败，稍后重试'
        finally:
            with worker.lock:
                if worker.cancelled():
                    for item in worker.rows:
                        if item.get('status') == '正在匹配':
                            item['status'] = '待匹配'
                worker.running = False
            with self.lock:
                self.checkpoint(key,worker)
                with self.e.store.connect() as db:
                    # Resume while stopping requests an immediate fresh run.
                    db.execute(f'UPDATE {self.table} SET due=? WHERE id=?', (0 if worker.cancelled() else time.time()+self.interval,key))
                self.active = None

    def status(self, key='', offset=0):
        result = []
        details = []
        total = 0
        with self.lock:
            for row in self.e.store.rows(f'SELECT * FROM {self.table} ORDER BY path'):
                worker = self.workers.get(row['id'])
                if worker:
                    with worker.lock:
                        snap = dict(phase=worker.phase,scanned=worker.scanned,error=worker.error,rows=[dict(x) for x in worker.rows])
                else:
                    snap = json.loads(row['snapshot'])
                items = snap.pop('rows',[])
                if not row['enabled'] or row['id'] != self.active:
                    for item in items:
                        if item.get('status') == '正在匹配':
                            item['status'] = '待匹配'
                done = sum(x.get('status') not in ('待匹配','正在匹配') for x in items)
                completed = sum(x.get('status') == self.success_status for x in items)
                running = row['id'] == self.active
                result.append(dict(id=row['id'],path=row['path'],enabled=bool(row['enabled']),running=running,
                    phase=snap.get('phase','尚未扫描'),scanned=snap.get('scanned',0),error=snap.get('error',''),
                    total=len(items),processed=done,completed=completed,unmatched=done-completed,
                    current=next((x.get('title','') for x in items if x.get('status')=='正在匹配'),'')))
                if row['id']==key:
                    total=len(items);details=items[offset:offset+100]
        return dict(directories=result,rows=details,total=total,offset=offset)

    def close(self):
        with self.lock:
            for worker in self.workers.values():
                worker.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
