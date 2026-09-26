"""Standalone Diskwala-only resolver API."""
import logging
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
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


@app.get("/", include_in_schema=False)
def root():
    return {
        "status": True,
        "creator": "Diskwala API",
        "message": "Diskwala Resolver API is online",
        "version": "2.0.0",
        "endpoints": {
            "resolve": "/api/resolve?url=<DISKWALA_URL>",
            "meta": "/api/meta?url=<DISKWALA_URL>",
            "extract": "/api/extract?text=<TEXT>",
            "health": "/health"
        }
    }

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
