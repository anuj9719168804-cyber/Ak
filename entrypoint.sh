#!/bin/sh
# entrypoint.sh — starts the API immediately (so Render sees its bound
# $PORT right away), then starts FlareSolverr (Cloudflare-challenge
# solver — see cf_bypass.py's FLARESOLVERR_URL) in the background.
#
# Ported from fbot-tera_api's entrypoint.sh, trimmed down to just the
# FlareSolverr block: this project doesn't have main.py's other sidecars
# (bgutil-pot, ytnode), just app.py's single FastAPI process, which
# already binds 0.0.0.0:$PORT itself via uvicorn.run() — no separate
# health-server process needed the way main.py's keep_alive.py provides.

# ── 1. Start the API immediately (background) so it binds Render's $PORT ──
python3 app.py &
APP_PID=$!
echo "[entrypoint] API started (pid $APP_PID) — binding \$PORT..."

# ── 2. FlareSolverr headless-Chrome Cloudflare solver ─────────────────────
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &
export DISPLAY=:99

HOST=127.0.0.1 PORT=8191 LOG_LEVEL=info \
    /opt/flaresolver/venv/bin/python /opt/flaresolver/src/flaresolverr.py \
    > /tmp/flaresolverr.log 2>&1 &
FLARESOLVERR_PID=$!

FLARESOLVERR_UP=0
for i in $(seq 1 45); do
    if curl -sf http://127.0.0.1:8191/health >/dev/null 2>&1; then
        FLARESOLVERR_UP=1
        break
    fi
    # Also abort early if the process already died
    kill -0 "$FLARESOLVERR_PID" 2>/dev/null || break
    sleep 1
done

if [ "$FLARESOLVERR_UP" = "1" ] && kill -0 "$FLARESOLVERR_PID" 2>/dev/null; then
    echo "[flaresolverr] OK — Cloudflare-challenge solver is up on 127.0.0.1:8191"
else
    echo "[flaresolverr] WARNING — did not come up after 45s. Last log lines:"
    tail -n 20 /tmp/flaresolverr.log 2>/dev/null
    echo "[flaresolverr] API will still run — cf_bypass.py just won't get an automatic retry on Cloudflare 403s (cloudscraper's in-process solver still works on its own)."
fi

# ── 3. Wait for the API process (it's already running) ───────────────────
wait $APP_PID
