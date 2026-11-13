"""Create a new comment database once; never migrate or replace an existing one."""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

def ensure_comments(path):
    path=Path(path)
    if path.is_file():return False
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.comment-init-',dir=path.parent) as directory:
        tmp=Path(directory)/'commenthub.db'
        tool=Path(__file__).parent/'commenthub/tools/migrate.py'
        result=subprocess.run([sys.executable,str(tool),'--db',str(tmp),'--apply'],capture_output=True,text=True,timeout=60)
        if result.returncode:raise RuntimeError('评论库初始化失败，请检查数据目录写入权限')
        con=sqlite3.connect(tmp);con.execute('PRAGMA wal_checkpoint(TRUNCATE)');con.close()
        # hard-link publication is atomic and refuses to replace concurrently created data.
        try:os.link(tmp,path)
        except FileExistsError:return False
    return True
