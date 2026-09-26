# Optional image. The API works with just Python (see requirements.txt) —
# Node/Chromium here are ONLY for diskwala_engine.js, the slowest fallback
# tier (_resolve_diskwala_browser_engine). If you don't need that specific
# tier, delete the Node/Chromium block below and use a plain
# python:3.11-slim image instead — everything else still works.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg chromium \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# diskwala.py's _resolve_diskwala_browser_engine() looks for this exact
# path: <dir of diskwala.py>/diskwala_engine/diskwala_engine.js — the JS
# file (and its own package.json, for the `ws` WebSocket polyfill Node 20
# doesn't have built in) must live in this subfolder, not next to it.
RUN cd diskwala_engine && npm install --omit=dev

ENV PORT=8000
EXPOSE 8000
CMD ["python", "app.py"]
