ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="归音 Music Hub" \
      org.opencontainers.image.source="https://github.com/wwwwwwjy/Guiyin" \
      org.opencontainers.image.version="0.9.23" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt
COPY app/ /app/
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
COPY licenses/ /app/licenses/
COPY third_party/ /app/third_party/
ENV FNMUSIC_HOME=/data FNMUSIC_DB_DIR=/fndb MUSIC_DIR=/music \
    FNMUSIC_API=http://127.0.0.1:5666/music/api/v1 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
RUN mkdir -p /data /music
# Validate route registration/OpenAPI against the actual image dependencies.
RUN FNMUSIC_HOME=/tmp/guiyin-build-check python -c "import webapp, shutil; webapp.app.openapi(); shutil.rmtree('/tmp/guiyin-build-check', ignore_errors=True)"
COPY tests/test_folders_unittest.py /tmp/guiyin_tests/test_folders_unittest.py
COPY tests/test_repair_unittest.py /tmp/guiyin_tests/test_repair_unittest.py
COPY tests/test_unified_unittest.py /tmp/guiyin_tests/test_unified_unittest.py
COPY tests/test_js_sources_unittest.py /tmp/guiyin_tests/test_js_sources_unittest.py
COPY tests/test_comment_dirs_unittest.py /tmp/guiyin_tests/test_comment_dirs_unittest.py
COPY tests/test_support_unittest.py /tmp/guiyin_tests/test_support_unittest.py
COPY tests/test_metadata_unittest.py /tmp/guiyin_tests/test_metadata_unittest.py
COPY tests/test_asset_dirs_unittest.py /tmp/guiyin_tests/test_asset_dirs_unittest.py
RUN PYTHONPATH=/app python -m unittest discover -s /tmp/guiyin_tests -p "test_*unittest.py" -v
VOLUME ["/data", "/music"]
EXPOSE 8600
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/api/health', timeout=4)"
CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8600", "--workers", "1"]
