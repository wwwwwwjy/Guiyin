"""Expose completed 归音 source snapshots to the integrated native playlist engine."""
import json
import os
import sqlite3
from pathlib import Path


def playlist(platform, source_id):
    path=Path(os.environ.get('FNMUSIC_HOME','/data'))/'hub.sqlite3'
    if not path.is_file():return None
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='source_playlists'").fetchone():return None
        db.execute('BEGIN')
        source=db.execute('''SELECT p.* FROM source_playlists p JOIN rules r ON r.id=p.rule_id
            WHERE p.platform=? AND p.source_id=? ORDER BY p.updated DESC LIMIT 1''',(platform,str(source_id))).fetchone()
        if not source:return None
        tracks=[]
        if source['state']=='active':
            tracks=[json.loads(r[0]) for r in db.execute('''SELECT t.meta FROM source_members m
                JOIN tracks t ON t.id=m.track_id WHERE m.rule_id=? AND m.playlist_id=? ORDER BY m.rowid''',
                (source['rule_id'],source['source_id']))]
        return {'source':platform,'source_id':str(source_id),'name':source['name'] or str(source_id),
                'tracks':tracks,'declared_count':len(tracks),'snapshot_at':source['updated']}
    finally:db.close()
