"""Standalone Diskwala-only resolver API."""
import logging
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import config
import diskwala

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("diskwala_api")

app = FastAPI(
    title="Diskwala Resolver API",
    description="Diskwala-only link resolver API.",
    version="2.0.0",
)

_hits: dict[str, deque] = defaultdict(deque)

EXAMPLE_URL = "https://www.diskwala.com/app/6ab531a02a52418b24f34358"


def _check_rate_limit(client_ip: str):
    now = time.time()
    window = _hits[client_ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= config.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded, try again in a bit.")
    window.append(now)


def _human_size(num_bytes) -> str:
    try:
        num_bytes = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "Unknown"
    if num_bytes <= 0:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} TB"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Diskwala Resolver API</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; padding: 2rem 1rem; }}
  .container {{ max-width: 860px; margin: 0 auto; }}
  h1 {{ font-size: 2rem; font-weight: 700; color: #60a5fa; margin-bottom: .4rem; }}
  .badge {{ display: inline-block; background: #1e3a5f; color: #93c5fd; font-size: .75rem; padding: .2rem .6rem; border-radius: 99px; margin-bottom: 1.5rem; }}
  .card {{ background: #1a1d27; border: 1px solid #2d3148; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; }}
  .card h2 {{ font-size: 1.1rem; color: #a78bfa; margin-bottom: 1rem; display: flex; align-items: center; gap: .5rem; }}
  .endpoint {{ background: #0f1117; border: 1px solid #2d3148; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; }}
  .method {{ display: inline-block; background: #064e3b; color: #6ee7b7; font-size: .72rem; font-weight: 700; padding: .15rem .5rem; border-radius: 4px; margin-right: .5rem; font-family: monospace; }}
  .path {{ color: #f0abfc; font-family: monospace; font-size: .95rem; font-weight: 600; }}
  .desc {{ color: #94a3b8; font-size: .85rem; margin: .5rem 0; }}
  .param {{ display: flex; gap: .5rem; align-items: baseline; margin: .3rem 0; }}
  .pname {{ color: #fbbf24; font-family: monospace; font-size: .82rem; min-width: 60px; }}
  .ptype {{ color: #60a5fa; font-size: .75rem; }}
  .pdesc {{ color: #94a3b8; font-size: .8rem; }}
  .try-link {{ display: inline-block; margin-top: .75rem; background: #1e3a5f; color: #60a5fa; text-decoration: none; font-size: .82rem; padding: .35rem .8rem; border-radius: 6px; font-family: monospace; word-break: break-all; transition: background .2s; }}
  .try-link:hover {{ background: #1e40af; }}
  code {{ background: #0f1117; border: 1px solid #2d3148; border-radius: 4px; padding: .1rem .35rem; font-family: monospace; font-size: .82rem; color: #f0abfc; word-break: break-all; }}
  .resp {{ background: #0a0d14; border-left: 3px solid #6ee7b7; border-radius: 0 6px 6px 0; padding: .75rem 1rem; font-family: monospace; font-size: .78rem; color: #a5f3fc; white-space: pre; overflow-x: auto; margin-top: .75rem; }}
  .note {{ background: #1c1a0d; border: 1px solid #78350f; border-radius: 6px; padding: .6rem .9rem; color: #fcd34d; font-size: .82rem; margin-top: .5rem; }}
  .health-dot {{ width: 8px; height: 8px; background: #4ade80; border-radius: 50%; display: inline-block; margin-right: .4rem; }}
</style>
</head>
<body>
<div class="container">
  <h1>🔗 Diskwala Resolver API</h1>
  <span class="badge">v2.0.0 &nbsp;·&nbsp; <span class="health-dot"></span>Online</span>

  <div class="card">
    <h2>📖 Endpoints</h2>

    <!-- /api/resolve -->
    <div class="endpoint">
      <span class="method">GET</span><span class="path">/api/resolve</span>
      <p class="desc">Resolve a Diskwala share link → direct download URL, stream URL, file size.</p>
      <div class="param"><span class="pname">url</span><span class="ptype">string (required)</span><span class="pdesc">— Diskwala share link</span></div>
      <a class="try-link" href="/api/resolve?url={EXAMPLE_URL}" target="_blank">
        Try → /api/resolve?url={EXAMPLE_URL}
      </a>
      <div class="resp">{{
  "status": true,
  "creator": "Diskwala API",
  "data": {{
    "name": "video.mp4",
    "size": "304.00 MB",
    "size_bytes": 318767104,
    "speed_link": "https://cdn.diskwala.com/...",
    "stream_link": "https://cdn.diskwala.com/...",
    "m3u8_url": null,
    "thumbnail": "https://..."
  }}
}}</div>
    </div>

    <!-- /api/meta -->
    <div class="endpoint">
      <span class="method">GET</span><span class="path">/api/meta</span>
      <p class="desc">Get metadata only — title, author, duration, thumbnail, file size. Faster than /api/resolve (no CDN link resolution).</p>
      <div class="param"><span class="pname">url</span><span class="ptype">string (required)</span><span class="pdesc">— Diskwala share link</span></div>
      <a class="try-link" href="/api/meta?url={EXAMPLE_URL}" target="_blank">
        Try → /api/meta?url={EXAMPLE_URL}
      </a>
      <div class="resp">{{
  "status": true,
  "creator": "Diskwala API",
  "data": {{
    "title": "Video Title",
    "author": "Uploader Name",
    "duration_seconds": 2562,
    "thumbnail": "https://...",
    "size_bytes": 318767104,
    "size": "304.00 MB",
    "extension": "mp4",
    "category": "video"
  }}
}}</div>
    </div>

    <!-- /api/extract -->
    <div class="endpoint">
      <span class="method">GET</span><span class="path">/api/extract</span>
      <p class="desc">Extract all Diskwala links found in a block of text.</p>
      <div class="param"><span class="pname">text</span><span class="ptype">string (required)</span><span class="pdesc">— Any text that may contain Diskwala links</span></div>
      <a class="try-link" href="/api/extract?text=Check+this+out+{EXAMPLE_URL}+and+more" target="_blank">
        Try → /api/extract?text=Check this out {EXAMPLE_URL}
      </a>
      <div class="resp">{{
  "status": true,
  "data": {{
    "links": ["https://www.diskwala.com/app/6ab531a02a52418b24f34358"],
    "count": 1
  }}
}}</div>
    </div>

    <!-- /health -->
    <div class="endpoint">
      <span class="method">GET</span><span class="path">/health</span>
      <p class="desc">Health check — use this for uptime monitoring (UptimeRobot etc.).</p>
      <a class="try-link" href="/health" target="_blank">Try → /health</a>
    </div>
  </div>

  <div class="card">
    <h2>⚡ Quick Usage</h2>
    <p class="desc" style="margin-bottom:.75rem">Python example:</p>
    <div class="resp">import requests

BASE = "https://YOUR-SERVICE.onrender.com"
URL  = "{EXAMPLE_URL}"

# Resolve → download link
r = requests.get(f"{{BASE}}/api/resolve", params={{"url": URL}})
data = r.json()["data"]
print(data["speed_link"])   # direct CDN download URL
print(data["size"])         # "304.00 MB"

# Meta only (faster)
m = requests.get(f"{{BASE}}/api/meta", params={{"url": URL}})
print(m.json()["data"]["title"])</div>

    <p class="desc" style="margin:.75rem 0 .4rem">curl example:</p>
    <div class="resp">curl "https://YOUR-SERVICE.onrender.com/api/resolve?url={EXAMPLE_URL}"</div>

    <div class="note">⚠️ Rate limit: {config.RATE_LIMIT_PER_MINUTE} requests / minute per IP. Exceed it → HTTP 429.</div>
  </div>

  <div class="card">
    <h2>🔗 Supported Links</h2>
    <p class="desc">All <code>diskwala.com</code> share formats:</p>
    <ul style="margin-top:.6rem;padding-left:1.2rem;color:#94a3b8;font-size:.85rem;line-height:1.8">
      <li><code>https://www.diskwala.com/app/&lt;id&gt;</code></li>
      <li><code>https://diskwala.com/app/&lt;id&gt;</code></li>
      <li><code>https://www.diskwala.com/playlist/&lt;id&gt;</code></li>
      <li><code>https://www.diskwala.com/folder/&lt;id&gt;</code></li>
    </ul>
  </div>

  <p style="text-align:center;color:#475569;font-size:.78rem;margin-top:1rem">
    Diskwala Resolver API &nbsp;·&nbsp; <a href="/docs" style="color:#60a5fa">Swagger UI</a> &nbsp;·&nbsp; <a href="/redoc" style="color:#60a5fa">ReDoc</a>
  </p>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/health")
def health():
    return {"status": True, "service": "diskwala-api", "provider": "Diskwala"}


@app.get("/api/extract")
def extract(text: str = Query(..., description="Text containing Diskwala links")):
    try:
        links = diskwala.extract_diskwala_links(text)
        return {"status": True, "data": {"links": links, "count": len(links)}}
    except Exception as e:
        logger.exception("extract failed")
        return JSONResponse(status_code=500, content={"status": False, "error": str(e)})


@app.get("/api/meta")
async def meta(url: str = Query(..., description="Diskwala share link")):
    if not diskwala.is_diskwala_link(url):
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Only Diskwala links are supported."},
        )
    try:
        info = await run_in_threadpool(diskwala.get_page_meta, url)
        return {
            "status": True,
            "creator": "Diskwala API",
            "data": {
                "title": info.get("title"),
                "author": info.get("author"),
                "duration_seconds": info.get("duration"),
                "thumbnail": info.get("poster_url"),
                "size_bytes": info.get("file_size"),
                "size": _human_size(info.get("file_size")),
                "extension": info.get("extension"),
                "category": info.get("category"),
            },
        }
    except Exception as e:
        logger.exception("meta failed for %s", url)
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})


@app.get("/api/resolve")
async def resolve(
    url: str = Query(..., description="Diskwala share link"),
):

    if not diskwala.is_diskwala_link(url):
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Only Diskwala links are supported."},
        )

    try:
        info = await run_in_threadpool(diskwala._resolve_no_auth, url)
    except Exception as e:
        logger.warning("resolve failed for %s: %s", url, e)
        return JSONResponse(status_code=502, content={"status": False, "error": str(e)})

    download_url = info.get("downloadUrl")
    stream_url = info.get("streamUrl") or download_url
    if not download_url:
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": "Resolved, but no download link was returned."},
        )

    name = info.get("name") or "file"
    ext = info.get("extension")
    if ext and not name.lower().endswith(f".{ext}"):
        name = f"{name}.{ext}"

    m3u8_url = stream_url if stream_url and ".m3u8" in stream_url.lower() else None

    return {
        "status": True,
        "creator": "Diskwala API",
        "data": {
            "name": name,
            "size": _human_size(info.get("size")),
            "size_bytes": info.get("size") or 0,
            "speed_link": download_url,
            "stream_link": stream_url,
            "m3u8_url": m3u8_url,
            "thumbnail": info.get("thumb"),
        },
    }


@app.middleware("http")
async def rate_limit_mw(request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    try:
        _check_rate_limit(client_ip)
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"status": False, "error": e.detail})
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, reload=False)
