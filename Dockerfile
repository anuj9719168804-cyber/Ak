# Optional image. The API works with just Python (see requirements.txt) —
# Node/Chromium here are ONLY for diskwala_engine.js, the slowest fallback
# tier (_resolve_diskwala_browser_engine). If you don't need that specific
# tier, delete the Node/Chromium block below and use a plain
# python:3.11-slim image instead — everything else still works.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg chromium git xvfb \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# FlareSolverr (Cloudflare JS-challenge bypass — see cf_bypass.py's
# FLARESOLVERR_URL) runs in this same container as its own background
# process, started by entrypoint.sh below, driven by the Xvfb + chromium
# already installed above. Its own venv, NOT pip-installed alongside this
# project's requirements.txt — FlareSolverr pins its own
# selenium/undetected-chromedriver versions, and mixing those into this
# project's environment risks a real version conflict with something else
# here needing a different pinned version of a shared dependency. Pinned
# to the v3.5.0 tag rather than a branch so this build doesn't silently
# pick up FlareSolverr's own future breaking changes. Ported from
# fbot-tera_api's Dockerfile — see entrypoint.sh for how it's started.
RUN git clone --branch v3.5.0 --depth 1 https://github.com/FlareSolverr/FlareSolverr.git /opt/flaresolver \
    && python3 -m venv /opt/flaresolver/venv \
    && /opt/flaresolver/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/flaresolver/venv/bin/pip install --no-cache-dir -r /opt/flaresolver/requirements.txt

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# diskwala.py's _resolve_diskwala_browser_engine() looks for this exact
# path: <dir of diskwala.py>/diskwala_engine/diskwala_engine.js — the JS
# file (and its own package.json, for the `ws` WebSocket polyfill Node 20
# doesn't have built in) must live in this subfolder, not next to it.
RUN cd diskwala_engine && npm install --omit=dev

RUN chmod +x entrypoint.sh

ENV PORT=8000
EXPOSE 8000
CMD ["/app/entrypoint.sh"]
