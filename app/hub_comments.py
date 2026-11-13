"""归音直接查询同一份 v2 评论库，复用已有匹配、去重和排序逻辑。"""
import json
from pathlib import Path


def query(path, title='', artist='', guid='', limit=60):
    from commenthub.apiv2 import Handler, SOURCES, build_items, ro_connect
    result = {'trackGuid': None, 'count': 0, 'comments': [], 'resolve': None}
    if not Path(path).is_file():
        return {**result, 'message': '尚未采集评论，请先试采一小批。'}
    con = ro_connect(str(path))
    try:
        track = con.execute('SELECT guid,title,artists_json FROM tracks WHERE guid=?', (guid,)).fetchone() if guid else None
        if track is None and title.strip():
            found = Handler._v1_find_track(con, title.strip(), artist.strip())
            if found:
                guid, name, singer, score = found
                result['resolve'] = {'title': name, 'artist': singer, 'score': round(score, 3)}
                track = con.execute('SELECT guid,title,artists_json FROM tracks WHERE guid=?', (guid,)).fetchone()
        if track is None:
            return {**result, 'message': '这首歌尚未采集评论。'}
        items, _ = build_items(con, track['guid'], SOURCES, 'hot', limit, 0, 1)
        fields = ('source','sources','content','author','likes','reply_count','ctime','ip_location','is_hot','other_likes')
        comments = [{k: row[k] for k in fields} for row in items[:limit]]
        result.update(trackGuid=track['guid'], count=len(comments), comments=comments,
                      title=track['title'], artists=json.loads(track['artists_json'] or '[]'),
                      platforms=Handler._v1_platforms(con,track['guid']),
                      mappings=Handler._v1_mappings(con,track['guid']),
                      expansion=Handler._v1_expansion(con,track['guid']), expanding=False)
        if not comments:
            result['message'] = '这首歌还没有抓到评论，可先运行评论采集。'
        return result
    finally:
        con.close()
