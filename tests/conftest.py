import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
os.environ.setdefault('FNMUSIC_HOME',tempfile.mkdtemp(prefix='hub-tests-upstream-'))
