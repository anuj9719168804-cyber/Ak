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
EXAMPLE_PLAYLIST_URL = "https://www.diskwala.com/playlist/6ab7c1882a52418b24108a58"


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
            "meta": f"/api/meta?url={EXAMPLE_URL}",
            "playlist": f"/api/playlist?url={EXAMPLE_PLAYLIST_URL}"
        },
        "endpoints": {
            "resolve": "/api/resolve?url=<DISKWALA_URL>",
            "meta": "/api/meta?url=<DISKWALA_URL>",
            "playlist": "/api/playlist?url=<DISKWALA_PLAYLIST_URL>",
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
        # BUG FIX: this called the raw diskwala._resolve_no_auth() directly —
        # every /api/resolve request did a full fresh resolve (token/HTML-
        # scrape chain and all), even for a URL just resolved seconds ago.
        # get_page_meta() right below already goes through
        # _resolve_no_auth_cached() (a thin TTL-cached wrapper around the
        # same function), so switching this call to the cached version too
        # means a repeat request for the same URL within the cache window
        # returns instantly instead of re-running the whole resolve chain —
        # and since both calls now share one cache entry per URL, the
        # get_page_meta() call below becomes a cache hit as well.
        info = await run_in_threadpool(diskwala._resolve_no_auth_cached, url)

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


@app.get("/api/playlist")
async def playlist(
    url: str = Query(..., description="Diskwala playlist link (diskwala.com/playlist/<id>)"),
):
    """Resolve every episode of a Diskwala playlist.

    BUG FIX: diskwala.py already has a correct playlist resolver —
    fetch_playlist_info_with_auth_retry() / fetch_playlist_info_diskwala()
    — scraping the playlist page for its child /app/<id> links and
    resolving each one through the SAME full per-video chain /api/resolve
    above uses (see that function's own docstring for why: a real
    WebView traffic analysis of the Android app shows it has no bulk
    playlist crawler at all, only ever resolving one episode's media
    right after that specific episode is opened). None of that was ever
    reachable through this API though — there was no /api/playlist route
    calling it at all, so every playlist link 400'd here as "Only
    Diskwala links are supported" via /api/resolve's is_diskwala_link()
    check (a playlist URL doesn't match a single-video link's shape),
    with no other endpoint to try instead. This is that missing route."""
    if not diskwala.is_playlist_link(url):
        return JSONResponse(
            status_code=400,
            content={
                "status": False,
                "error": "Only Diskwala playlist links (diskwala.com/playlist/<id>) are supported here — use /api/resolve for a single video link.",
            },
        )

    try:
        info = await run_in_threadpool(diskwala.fetch_playlist_info_with_auth_retry, url)
    except Exception as e:
        logger.warning("playlist resolve failed for %s: %s", url, e)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": str(e)},
        )

    files = []
    for f in info.get("files") or []:
        size_bytes = f.get("size") or 0
        files.append({
            "name": f.get("name") or "file",
            "download_link": f.get("link"),
            "stream_link": f.get("link"),
            "thumbnail": f.get("thumb"),
            "size_bytes": size_bytes,
            "size": _human_size(size_bytes),
        })

    return {
        "status": True,
        "creator": "Diskwala API",
        "data": {
            "title": info.get("title") or "Diskwala Playlist",
            "thumbnail": info.get("thumb"),
            "count": len(files),
            "files": files,
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
