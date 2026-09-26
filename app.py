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

EXAMPLE_URL = "https://www.diskwala.com/app/6ab7c1882a52418b24108a58"


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
        "example": {
            "url": EXAMPLE_URL,
            "resolve": f"/api/resolve?url={EXAMPLE_URL}",
            "meta": f"/api/meta?url={EXAMPLE_URL}"
        },
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
    """Resolve a Diskwala app URL and return metadata + playable/download URLs."""

    if not diskwala.is_diskwala_link(url):
        return JSONResponse(
            status_code=400,
            content={
                "status": False,
                "error": "Only Diskwala links are supported.",
            },
        )

    try:
        # Resolve the actual file/stream information.
        info = await run_in_threadpool(diskwala._resolve_no_auth, url)

        # Fetch page metadata separately. Some Diskwala pages may not expose
        # every metadata field, so failures here should not break resolution.
        try:
            page_info = await run_in_threadpool(diskwala.get_page_meta, url)
        except Exception as meta_error:
            logger.warning("metadata lookup failed for %s: %s", url, meta_error)
            page_info = {}

    except Exception as e:
        logger.warning("resolve failed for %s: %s", url, e)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": str(e)},
        )

    download_url = info.get("downloadUrl") or info.get("download_url")
    stream_url = info.get("streamUrl") or info.get("stream_url") or download_url

    if not download_url and not stream_url:
        return JSONResponse(
            status_code=502,
            content={
                "status": False,
                "error": "Resolved, but no download or stream link was returned.",
            },
        )

    # Prefer resolver values, then fall back to page metadata.
    name = info.get("name") or page_info.get("title") or "file"
    extension = info.get("extension") or page_info.get("extension")

    if extension:
        extension = str(extension).lstrip(".")
        if not name.lower().endswith(f".{extension.lower()}"):
            name = f"{name}.{extension}"

    size_bytes = (
        info.get("size")
        or info.get("size_bytes")
        or page_info.get("file_size")
        or 0
    )

    thumbnail = (
        info.get("thumb")
        or info.get("thumbnail")
        or page_info.get("poster_url")
    )

    m3u8_url = (
        stream_url
        if stream_url and ".m3u8" in str(stream_url).lower()
        else None
    )

    data = {
        # Requested Diskwala metadata response
        "title": page_info.get("title") or info.get("title") or name,
        "author": page_info.get("author") or info.get("author"),
        "duration_seconds": page_info.get("duration") or info.get("duration"),
        "thumbnail": thumbnail,
        "size_bytes": size_bytes,
        "size": _human_size(size_bytes),
        "extension": extension,
        "category": page_info.get("category") or info.get("category"),

        # Resolved file information
        "name": name,
        "download_link": download_url,
        "stream_link": stream_url,
        "m3u8_url": m3u8_url,
    }

    # Do not include fields whose value is None/null.
    data = {key: value for key, value in data.items() if value is not None}

    return {
        "status": True,
        "creator": "Diskwala API",
        "data": data,
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
