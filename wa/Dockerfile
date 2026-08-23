# WA Toolkit — server build.
#
# Same base image as the Report Maker container next door, so the two share
# layers on the host and there is exactly one Chromium version to reason about.
# The tag MUST match the playwright pin in requirements-web.txt.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8010 \
    WA_DATA_DIR=/data \
    WA_ROOT_PATH=/wa \
    WA_FORCE_HEADLESS=1 \
    HOME=/app

WORKDIR /app

COPY requirements-web.txt ./
RUN pip install --no-cache-dir --break-system-packages -r requirements-web.txt

# The toolkit itself. `wa/` is the Python package; `ui/` is the built bundle
# (rebuild it with ui-src/build.sh before `docker compose build`, not in here —
# node is not in this image and the bundle is committed).
COPY wa/ ./wa/
COPY ui/ ./ui/
COPY plugins/ ./plugins/
COPY server.py app.py bot.py wa.py ./
COPY config.json tasks.json file1.txt file2.txt ./

# /data is the writable volume: config.json, tasks.json, plugins/, wa_profile/
# (the WhatsApp login), links.csv, qr.png. wa/paths.py copies the defaults above
# into it on first boot and never overwrites them again.
#
# UID 1000 matches the default non-root user on an Ubuntu VPS, so the bind mount
# is writable without a chown dance — same convention as the report container.
RUN mkdir -p /data \
 && if ! id -u 1000 >/dev/null 2>&1; then useradd -m -u 1000 wauser; fi \
 && chown -R 1000:0 /app /data \
 && chmod -R g+rwX /app /data

USER 1000

EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8010')+'/health').read()"

# One worker: the Playwright session lives in this process's worker thread, and
# a second uvicorn worker would mean a second Chromium fighting for the same
# profile directory.
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8010} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
