import re
import time
import json
import html
import logging
import os
import requests
import subprocess
from urllib.parse import quote, urljoin
from bs4 import BeautifulSoup

logger = logging.getLogger("faphouse_bot")

HTML_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# Optional: a logged-in Diskwala account cookie. Without it, the Diskwala
# fallback can still confirm the file exists and report its name/size, but
# cannot produce an actual download URL (Diskwala only serves direct links to
# saved/logged-in accounts). Set this in .env if you have one.
# Optional Cloudflare-JS-challenge bypass, shared with ytdlp_downloader.py
# (see that module's own _CF_BYPASS_AVAILABLE guard for the full story).
# diskwala.net's first-party /web/api/* endpoints (see
# _resolve_diskwala_web_api() below) sit behind Cloudflare and reject
# plain requests with a challenge page once IP/traffic looks suspicious —
# this reuses whatever clearance/impersonation cf_bypass.py provides
# instead of duplicating that logic here.
#
# BUG FIX: this import was missing entirely (not even wrapped in the
# try/except below) — _resolve_diskwala_web_api's _cf_aware() referenced
# the bare name `cf_bypass` with nothing importing it anywhere in this
# file, so every single call NameError'd immediately, before Cloudflare
# (or cf_bypass) ever entered the picture at all. Caught by
# _resolve_no_auth's `except Exception`, so it never crashed the bot —
# it just meant this whole first-party resolver silently never worked.
try:
    import cf_bypass
    _CF_BYPASS_AVAILABLE = True
except ImportError:
    _CF_BYPASS_AVAILABLE = False
    logger.warning(
        "cf_bypass module not found (deleted?) — diskwala.net's direct "
        "API won't get an automatic Cloudflare-challenge retry; the "
        "existing external/token/HTML resolver chain is unaffected."
    )

# BUG FIX: DISKWALA_COOKIE was read everywhere below (_get_diskwala_cookie(),
# refresh_diskwala_cookie(), _diskwala_save_and_resolve()) as a bare module-
# level name, but nothing in this file ever actually defined or imported
# it — every one of those call sites would have raised a NameError the
# instant it was reached (masked only because _resolve_no_auth's Diskwala
# branch is now routed to _fetch_diskwala_mainpy() instead, which never
# touches these functions at all). Defined here, the same way every other
# optional credential in this file is: an env var, defaulting to empty.

# ─────────────────────────────────────────────────────────────────────────
#  Telethon auth-token tier (ported from ultra-main's main.py)
#
#  ultra-main's get_auth_token()/_get_persistent_telethon_client() lived in
#  main.py and ran on the bot's own asyncio event loop — every caller was
#  already an `async def` on that loop. fbot-main's adapter functions
#  below are plain sync `def`s (this module's whole contract with
#  fbot-main's main.py — see the INTEGRATION ADAPTER section further
#  down), called via asyncio.to_thread(), so there's no running event loop
#  in the thread they execute on for Telethon's async client calls to use.
#
#  Fix: run Telethon on ONE persistent background thread with its own
#  event loop, started lazily on first use, and give every sync caller a
#  blocking get_auth_token_sync() that hands work to that loop via
#  asyncio.run_coroutine_threadsafe() and waits for the result. The
#  Telethon client itself is created once on that loop and reused (same
#  as ultra-main's _telethon_client global) rather than reconnecting on
#  every call.
# ─────────────────────────────────────────────────────────────────────────
import threading
import asyncio

_auth_cache = {"token": None, "expires": 0}
_telethon_client = None
_telethon_async_lock = None  # created lazily, ON the Telethon loop (see below)

_telethon_loop = None
_telethon_loop_thread = None
_telethon_loop_start_lock = threading.Lock()


def _ensure_telethon_loop():
    """Start the dedicated Telethon event-loop thread on first use, or
    return the already-running one. Safe to call from any thread."""
    global _telethon_loop, _telethon_loop_thread
    with _telethon_loop_start_lock:
        if _telethon_loop is None:
            _telethon_loop = asyncio.new_event_loop()

            def _run_forever():
                asyncio.set_event_loop(_telethon_loop)
                _telethon_loop.run_forever()

            _telethon_loop_thread = threading.Thread(
                target=_run_forever, name="telethon-auth-loop", daemon=True
            )
            _telethon_loop_thread.start()
    return _telethon_loop


async def _get_persistent_telethon_client():
    """Runs ON the Telethon loop. Returns a connected Telethon client,
    creating/reconnecting if needed — same logic as ultra-main's."""
    global _telethon_client, _telethon_async_lock
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from config import API_ID, API_HASH, SESSION

    if _telethon_async_lock is None:
        _telethon_async_lock = asyncio.Lock()
    if not SESSION:
        raise RuntimeError(
            "SESSION env var not set — the Telethon token-API tier needs a "
            "user-session string (see config.py's comment on SESSION)."
        )
    if _telethon_client is None:
        _telethon_client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    if not _telethon_client.is_connected():
        await _telethon_client.connect()
    return _telethon_client


async def _async_get_auth_token() -> str:
    """Runs ON the Telethon loop — identical flow to ultra-main's
    get_auth_token(): logs into the "sky577bot" Mini App as the Telethon
    user session and pulls the bearer token out of the WebApp URL.

    Diskwala-only — see _async_get_diskwala_auth_token() just below for why
    Diskwala links need a completely separate token from a different bot,
    not this one reused with a different X-Bot-Id header."""
    global _telethon_async_lock
    if _auth_cache["token"] and time.time() < _auth_cache["expires"]:
        return _auth_cache["token"]

    # BUG FIX: _telethon_async_lock used to only get created inside
    # _get_persistent_telethon_client() below — but the `async with` right
    # here runs BEFORE that function is ever called, so on the very first
    # request (or any time this races back to None) it was still None,
    # and `async with None:` blew up with "'NoneType' object does not
    # support the asynchronous context manager protocol". Every token
    # fetch hit this, which is why the token-API tier never actually
    # worked even with SESSION correctly configured. Create it here,
    # before it's needed, instead.
    if _telethon_async_lock is None:
        _telethon_async_lock = asyncio.Lock()

    async with _telethon_async_lock:
        if _auth_cache["token"] and time.time() < _auth_cache["expires"]:
            return _auth_cache["token"]

        from telethon.tl.functions.messages import RequestAppWebViewRequest
        from telethon.tl.types import InputBotAppShortName, InputPeerSelf, DataJSON
        from urllib.parse import urlparse, unquote

        client = await _get_persistent_telethon_client()
        bot = await client.get_input_entity("sky577bot")
        r = await client(RequestAppWebViewRequest(
            peer=InputPeerSelf(),
            app=InputBotAppShortName(bot_id=bot, short_name="open"),
            platform="android",
            write_allowed=True,
            start_param="",
            theme_params=DataJSON("{}"),
        ))
        token = unquote(urlparse(r.url).fragment.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion=", 1)[0])
        _auth_cache["token"] = token
        _auth_cache["expires"] = time.time() + 3600  # 1 hour — same as ultra-main
        return token


# BUG FIX: every Diskwala token-API call was using the SAME bearer token as
# Diskwala — fetched from "sky577bot" above — just with the X-Bot-Id
# header swapped to "diskwala". That token is scoped to whatever bot issued
# it; a working reference implementation for this exact endpoint pair
# (api2.diskwala.net/api/diskwala/downloadw + .../statusw) fetches its
# token from a COMPLETELY DIFFERENT bot — "diskwalanbot" — via a plain
# RequestWebViewRequest (a webapp URL, not a short-named bot app like
# sky577bot's "open"), not sky577bot with a different header. This is
# almost certainly why the Diskwala token-API tier has never actually
# returned a file in any of this session's testing: it was never really
# authenticated for Diskwala at all, just presenting a Diskwala-scoped
# token with a header claiming otherwise, which the backend most likely
# just silently rejects/finds nothing for — a 401/403 would have surfaced
# as DiskwalaAuthError already, so a silent "not found"-shaped failure is
# the more likely symptom, indistinguishable from the file genuinely not
# existing without knowing this.









def get_auth_token_sync(timeout: float = 30) -> str:
    """Blocking sync entry point for the adapter functions below — the
    Telethon equivalent of just calling get_auth_token() directly, minus
    needing to be inside an async def on the Telethon loop. Raises
    whatever _async_get_auth_token() raised (including the SESSION-not-set
    RuntimeError above) if the token-API tier isn't usable."""
    loop = _ensure_telethon_loop()
    fut = asyncio.run_coroutine_threadsafe(_async_get_auth_token(), loop)
    return fut.result(timeout=timeout)


def _invalidate_auth_token():
    _auth_cache["token"] = None
    _auth_cache["expires"] = 0


def resolve_diskwala_with_auth_retry(link: str) -> dict:
    """Sync equivalent of ultra-main's resolve_diskwala_with_retry(): try
    the token-API tier (auto-refreshing the token once on a 401/403
    instead of failing until the 1-hour cache naturally expires), and let
    the caller (_resolve_no_auth below) fall back to HTML-scrape if this
    raises at all — including when SESSION isn't configured, so nothing
    breaks for anyone who hasn't set it up.

    BUG FIX: this always fetched the Diskwala/sky577bot token, even for
    Diskwala links, and fetch_diskwala_video() passed that SAME token
    straight through to Diskwala's api2 calls — see the BUG FIX comment on
    _diskwala_auth_cache above for why that token was never actually valid
    for Diskwala's endpoints in the first place. Now picks the right
    token function for the link, and retries with the right invalidation
    function too."""
    is_diskwala = "diskwala.com" in link.lower()
    get_token = get_diskwala_auth_token_sync if is_diskwala else get_auth_token_sync
    invalidate_token = _invalidate_diskwala_auth_token if is_diskwala else _invalidate_auth_token

    auth = get_token()
    try:
        return fetch_diskwala_video(link, auth)
    except DiskwalaAuthError as e:
        logger.warning(f"{'Diskwala' if is_diskwala else 'Diskwala'} auth token rejected ({e}), refreshing and retrying once...")
        invalidate_token()
        auth = get_token()
        return fetch_diskwala_video(link, auth)




def fetch_playlist_info_with_auth_retry(playlist_url: str) -> dict:
    """Same job as resolve_diskwala_with_auth_retry() above, but for
    playlists: fetch_playlist_info() needs an auth token same as
    fetch_diskwala_video() does, and until fetch_playlist_info_diskwala()
    above was added, there was no no-auth fallback at all for playlists —
    so unlike single-video links, there was nothing to fall back to if
    this raised. Bug fix: this wrapper didn't exist at all — main.py
    called diskwala.fetch_playlist_info_with_auth_retry(...) expecting the
    same auth-token-plus-one-retry contract every other entry point in
    this file has, and got AttributeError instead, on every single
    playlist link (SESSION configured or not — fetch_playlist_info() was
    simply unreachable from main.py).

    FIX: now tries fetch_playlist_info_diskwala() (no SESSION/Telethon
    needed at all) first — playlists work even on a deployment that
    intentionally leaves SESSION unset (see config.py's own comment on
    why a shared hardcoded SESSION default was removed). Falls back to
    the token-API tier below only if that fails."""
    try:
        return fetch_playlist_info_diskwala(playlist_url)
    except Exception as e:
        logger.info(f"diskwalaplayer.com playlist resolver unavailable ({e}), trying token-API tier...")

    auth = get_auth_token_sync()
    try:
        return fetch_playlist_info(playlist_url, auth)
    except DiskwalaAuthError as e:
        logger.warning(f"Diskwala auth token rejected ({e}), refreshing and retrying once...")
        _invalidate_auth_token()
        auth = get_auth_token_sync()
        return fetch_playlist_info(playlist_url, auth)


class DiskwalaAuthError(Exception):
    """Raised when the Diskwala miniapp API rejects the bearer token itself
    (HTTP 401/403) — distinct from a normal 'not found' / processing error,
    so callers know a fresh token (not a retry) is what's needed."""
    pass

API_DOWNLOAD = "https://api2.diskwala.net/api/diskwala/downloadw"
API_STATUS = "https://api2.diskwala.net/api/diskwala/statusw"

# Diskwala file-service endpoints.  The API host can be overridden with
# DISKWALA_FILE_API_BASE when a deployment uses a different API gateway.
DISKWALA_FILE_API_BASE = os.getenv(
    "DISKWALA_FILE_API_BASE", "https://api2.diskwala.net"
).rstrip("/")
DISKWALA_FILE_PLAYLIST_INFO = f"{DISKWALA_FILE_API_BASE}/file/playlist_info"
DISKWALA_FILE_UPDATE_INFO = f"{DISKWALA_FILE_API_BASE}/file/update_file_info"
DISKWALA_FILE_MULTIPART = f"{DISKWALA_FILE_API_BASE}/file/multipart"


def _diskwala_file_headers(auth: str | None = None, multipart: bool = False) -> dict:
    """Common headers for Diskwala /file/* endpoints."""
    headers = {
        "Accept": "application/json",
        "User-Agent": HTML_USER_AGENT,
        "Origin": "https://miniapp.diskwala.net",
        "Referer": "https://miniapp.diskwala.net/",
    }
    if not multipart:
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    return headers


def file_playlist_info(playlist_url: str, auth: str | None = None) -> dict:
    """Call /file/playlist_info and return its JSON response.

    Kept separate from get_all_playlist_files() so callers can use the
    file-service metadata endpoint without changing the existing playlist
    resolver contract.
    """
    payload = {"link": playlist_url, "url": playlist_url}
    response = requests.post(
        DISKWALA_FILE_PLAYLIST_INFO,
        headers=_diskwala_file_headers(auth),
        json=payload,
        timeout=60,
    )
    if response.status_code in (401, 403):
        raise DiskwalaAuthError(
            f"Diskwala file API auth rejected (HTTP {response.status_code})"
        )
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Diskwala /file/playlist_info returned non-JSON data (HTTP {response.status_code})."
        )
    if response.status_code >= 400:
        raise RuntimeError(data.get("error") or data.get("message") or str(data))
    return data


def update_file_info(file_id, info: dict, auth: str | None = None) -> dict:
    """Update metadata through /file/update_file_info."""
    payload = dict(info or {})
    if file_id is not None:
        payload.setdefault("file_id", file_id)
        payload.setdefault("id", file_id)
    response = requests.post(
        DISKWALA_FILE_UPDATE_INFO,
        headers=_diskwala_file_headers(auth),
        json=payload,
        timeout=60,
    )
    if response.status_code in (401, 403):
        raise DiskwalaAuthError(
            f"Diskwala file API auth rejected (HTTP {response.status_code})"
        )
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Diskwala /file/update_file_info returned non-JSON data (HTTP {response.status_code})."
        )
    if response.status_code >= 400:
        raise RuntimeError(data.get("error") or data.get("message") or str(data))
    return data


def multipart_file_api(action: str, *, files=None, data=None, json_data=None, auth: str | None = None) -> dict:
    """Call any /file/multipart/<action> endpoint.

    ``action`` is the path after /file/multipart/; it is intentionally
    configurable because the server exposes multiple multipart operations.
    """
    action = str(action or "").strip("/")
    if not action:
        raise ValueError("multipart action is required")
    url = f"{DISKWALA_FILE_MULTIPART}/{action}"
    kwargs = {"headers": _diskwala_file_headers(auth, multipart=files is not None),
              "timeout": 120}
    if files is not None:
        kwargs["files"] = files
        kwargs["data"] = data or {}
    else:
        kwargs["json"] = json_data if json_data is not None else (data or {})
    response = requests.post(url, **kwargs)
    if response.status_code in (401, 403):
        raise DiskwalaAuthError(
            f"Diskwala multipart API auth rejected (HTTP {response.status_code})"
        )
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(
            f"Diskwala /file/multipart/{action} returned non-JSON data (HTTP {response.status_code})."
        )
    if response.status_code >= 400:
        raise RuntimeError(result.get("error") or result.get("message") or str(result))
    return result

# BUG FIX: get_all_playlist_files() (below) was POSTing playlist URLs to
# API_DOWNLOAD and getting back {"ok": false, "error": "invalid diskwala
# link"} for every playlist — confirmed against a real working reference
# implementation (TeraBox-Video-Downloader's diskwalaDL/diskwala_dl.py):
# its DISKWALA_DOWNLOAD_API is "{base}/download/d" — an extra "/d" path
# segment this project's API_DOWNLOAD doesn't have. Kept as its own
# constant rather than changing API_DOWNLOAD itself: API_DOWNLOAD is also
# used by the single-file path (_fetch_diskwala_video_via_api), which is
# confirmed working as-is — only get_all_playlist_files() below switches
# to this "/d" variant, so a single-file link's request shape is
# completely unaffected by this fix.
API_DOWNLOAD_PLAYLIST = API_DOWNLOAD + "/d"
# Bug fix / replacement: api2.diskwala.net's Diskwala routes needed a
# bearer token from the Diskwala miniapp AND a two-payload-shape workaround
# (see the old _fetch_diskwala_video_via_api docstring below) just to get a
# flat "not found" for most links. DISKWALA_WORKER_API (see
# _resolve_diskwala_new below) is a dedicated, Diskwala-specific
# Cloudflare Worker — no auth token needed — that replaces those two
# endpoints entirely; Diskwala links no longer touch api2.diskwala.net at
# all (see _resolve_no_auth's Diskwala branch). Kept here (unused by
# Diskwala now) only because _get_endpoints() below still returns them for
# any caller that imports these names directly.
# AES-GCM key for decrypt_file() below — overridable via env var so a
# key rotation on Diskwala's side doesn't require a code deploy, just a
# restart with the new value set.
_DISKWALA_AES_KEY_HEX = os.environ.get(
    "DISKWALA_AES_KEY_HEX",
    "e7109544dab612bd5b80b8a427ac474ba5541b9efff7a4ca1c8ef85df2489c23",
)
ENCRYPTION_KEY = _DISKWALA_AES_KEY_HEX


def _get_endpoints(link: str) -> tuple[str, str]:
    """Return Diskwala's authenticated download/status API endpoints."""
    return API_DOWNLOAD, API_STATUS + "?link="


def _pick(d: dict, *keys):
    """First non-empty value among `keys` in dict `d` — module-level (not
    nested inside _fetch_diskwala_video_via_api like it used to be) so
    terabox_downloader.py's _fetch_terabox_via_token_api can share the
    exact same field-name fallback list instead of maintaining its own,
    shorter one that can silently drift out of sync (see that function's
    own comment for the real mismatch this fixed: it was only checking
    2-3 field-name variants per value where this checks 4-6, including
    snake_case alternates like "download_url"/"stream_url" this API is
    known to use sometimes)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def decrypt_file(file_data: dict) -> dict:
    """Decrypt AES-GCM encrypted file response from Diskwala API.

    BUG FIX: both attempts below used to construct the EXACT SAME bytes
    (`p + h`, ciphertext-then-tag) — one via `bytes.fromhex(p) +
    bytes.fromhex(h)`, the other via `bytes.fromhex(p + h)` (hex-string
    concatenation then a single decode). Those are mathematically
    identical for any valid (even-length) hex strings, which p/h always
    are here — so the "fallback" never actually tried anything
    different from the first attempt; if the first failed, the second
    was guaranteed to fail with the exact same error, confirmed live
    ("AES-GCM decryption failed (tried both byte orderings)" firing
    on the very first real attempt). The genuine alternative worth
    trying — and what the docstring already claimed this did — is the
    fields reversed (`h + p`, tag-before-ciphertext), in case a given
    API response has "p"/"h" swapped from the usual ciphertext/tag
    convention.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = bytes.fromhex(ENCRYPTION_KEY)
    iv  = bytes.fromhex(file_data["s"])
    p   = bytes.fromhex(file_data["p"])
    h   = bytes.fromhex(file_data["h"])
    aesgcm = AESGCM(key)

    # Attempt 1: ciphertext + tag (p + h) — the standard AES-GCM
    # convention, and what "p"/"h" (presumably "payload"/"hash"?) most
    # likely mean.
    try:
        plaintext = aesgcm.decrypt(iv, p + h, None)
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        pass

    # Attempt 2: fields reversed — tag + ciphertext (h + p).
    try:
        plaintext = aesgcm.decrypt(iv, h + p, None)
        return json.loads(plaintext.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"AES-GCM decryption failed (tried both byte orderings): {e}") from e


def extract_diskwala_links(text: str) -> list[str]:
    """Extract Diskwala URLs from arbitrary text."""
    if not text:
        return []
    patterns = [
        r"https?://(?:www\.)?diskwala\.[a-z]{2,}/\S+",
        r"https?://thediskwala\.[a-z]{2,}/\S+",
        r"https?://miniapp\.diskwala\.net/\S+",
        r"https?://t\.me/(?:sky577bot|diskwalabot)(?:/[a-zA-Z0-9_-]+)?\?(?:startapp|start)=\S+",
    ]
    links=[]
    for pattern in patterns:
        links.extend(re.findall(pattern,text,re.IGNORECASE))
    return list(dict.fromkeys(l.rstrip(").,!?>'\"") for l in links))
# General-purpose Diskwala share-ID shape (24 hex chars) — Diskwala's own
# IDs are always this shape regardless of which URL path style wraps them.
# Used as a companion to the URL patterns above: extract_diskwala_id()
# below pulls one out of ANY diskwala.com URL (or bare text) that contains
# one, even a path shape none of the patterns above were written for yet.
DISKWALA_LINK_ID_RE = re.compile(r"[a-fA-F0-9]{24}")


def extract_diskwala_id(link_or_text: str) -> str | None:
    m = DISKWALA_LINK_ID_RE.search(link_or_text or "")
    return m.group(0) if m else None










# Free public third-party resolver for diskwala.com/net/app links — no
# auth, no cookies, single GET request. Ported from terabox-diskwala-bot's
# src/providers/diskwala.js. Not affiliated with Diskwala; can go down or
# rate-limit independently of Diskwala's own site, so it's tried as a fast
# FIRST attempt in _resolve_no_auth below, falling through to the existing
# token-API/HTML-scrape chain unchanged if it fails — never a replacement
# for that chain, just a quicker path when it happens to work.




# diskwala.net's OWN public, no-auth, no-encryption web API — confirmed
# live via a HAR capture of diskwala.net's own frontend resolving a
# diskwala.com/app/<id> link:
#   POST https://diskwala.net/web/api/download/n   {"link": <url>}
#     -> {"ok": true, "status": "done"}                    (starts the job)
#   GET  https://diskwala.net/web/api/status?link=<url>
#     -> {"ok": true, "status": "done", "file": {
#            "name": ..., "extension": ..., "size": ...,
#            "thumb": ..., "downloadUrl": ...
#         }}
# No Authorization header and no AES-GCM encryption at all (unlike
# api2.diskwala.net's authenticated miniapp API — see
# fetch_diskwala_video()/decrypt_file()) — this is diskwala.net's own
# public website using it directly, so no token/SESSION/Telethon setup
# is needed here either. Tried as a fast, first-party, no-auth attempt
# — ahead of the third-party _resolve_diskwala_external above, since a
# first-party endpoint is inherently more likely to stay in sync with
# whatever Diskwala's backend actually does than an unaffiliated
# third-party scraper is. diskwala.net itself is Cloudflare-protected
# (same as terabox_downloader.py's _api_call and this file's HTML-fetch
# helpers), so this goes through cf_bypass the same way those do.
_DISKWALA_WEB_API_BASE = "https://diskwala.net/web/api"


def _resolve_diskwala_web_api(link: str) -> dict:
    headers = {
        "User-Agent": HTML_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://diskwala.net",
        "Referer": "https://diskwala.net/",
    }

    def _cf_aware(method: str, url: str, **kwargs):
        if not _CF_BYPASS_AVAILABLE:
            r = requests.request(method, url, headers=headers, timeout=20, **kwargs)
            r.raise_for_status()
            return r

        # cf_bypass.request() uses curl_cffi's Chrome-TLS impersonation
        # for the actual call (not just a plain `requests` call with
        # cached cookies bolted on) — see that function's own docstring
        # for why the TLS handshake itself matters here, not just cookies.
        r = cf_bypass.request(method, url, headers=headers, **kwargs)
        content_type = r.headers.get("Content-Type", "")
        if r.status_code == 403 or "text/html" in content_type:
            if not cf_bypass.try_solve("https://diskwala.net/"):
                raise Exception("diskwala.net web API: Cloudflare challenge, cf_bypass couldn't solve it.")
            r = cf_bypass.request(method, url, headers=headers, **kwargs)
        r.raise_for_status()
        return r

    _cf_aware("POST", f"{_DISKWALA_WEB_API_BASE}/download/n", json={"link": link})

    last_data = None
    for _ in range(15):  # poll for up to ~15s
        r = _cf_aware("GET", f"{_DISKWALA_WEB_API_BASE}/status", params={"link": link})
        try:
            data = r.json()
        except ValueError:
            raise Exception("diskwala.net web API: status endpoint returned a non-JSON response")
        last_data = data
        if not data.get("ok"):
            raise Exception(f"diskwala.net web API: {data}")

        status = data.get("status")
        if status == "done":
            file = data.get("file") or {}
            dl = file.get("downloadUrl")
            if not dl:
                raise Exception(f"diskwala.net web API: no downloadUrl in a 'done' response: {data}")
            ext = re.sub(r"[^a-z0-9]", "", str(file.get("extension") or "mp4").lower()) or "mp4"
            name = str(file.get("name") or "diskwala").strip() or "diskwala"
            if not name.lower().endswith(f".{ext}"):
                name = f"{name}.{ext}"
            return {
                "name": name, "extension": ext, "size": file.get("size") or 0,
                "downloadUrl": dl, "streamUrl": dl,
                "thumb": file.get("thumb") or None, "creator": None, "duration_seconds": None,
                "views": None, "likes": None, "description": None,
                "upload_date": None, "category": "Video",
            }
        if status == "error":
            raise Exception(f"diskwala.net web API reported an error: {data}")
        time.sleep(1)

    raise Exception(f"diskwala.net web API: status polling timed out (last response: {last_data})")


_DISKWALA_ENGINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diskwala_engine", "diskwala_engine.js")


def _resolve_diskwala_browser_engine(link: str) -> dict:
    """Real-headless-Chrome fallback for diskwala.net links, for when
    _resolve_diskwala_web_api() above can't get past Cloudflare even with
    cf_bypass's help. Spawns diskwala_engine.js (Node, via CDP) which
    launches an actual Chrome, navigates to https://diskwala.net/, types
    the link into the site's own input box, clicks its own "Get" button,
    and sniffs the resulting /web/api/status network response for
    downloadUrl — i.e. it drives the real website exactly as a human
    would, rather than replicating its API calls itself. Sidesteps
    Cloudflare entirely this way: it IS a real browser, with a real TLS
    handshake and a real JS engine, so there's no challenge to solve or
    fingerprint to fake in the first place.

    Ported from a reference project's standalone diskwala_engine.js
    (originally meant to be run as `node diskwala_engine.js <linkId>` and
    read off stdout — this calls it exactly that way as a subprocess).
    Slower than the API-based tiers above (a real browser has to actually
    launch and navigate, typically several seconds, up to the ~28s the
    script itself gives up at), so it's tried after them, not before —
    but far more likely to succeed against Cloudflare specifically,
    since nothing about it needs solving or impersonating.

    Raises on any failure (node/the script missing, timeout, non-zero
    exit, unparseable output, or the script's own {"success": false}) —
    the caller (_resolve_no_auth) falls through to the external/token-API/
    HTML chain exactly as it does when this tier isn't reachable at all."""
    m = re.search(r"/app/([A-Za-z0-9]+)", link) or re.search(r"/watch/([A-Za-z0-9]+)", link)
    link_id = m.group(1) if m else (link.rstrip("/").split("/")[-1] or "")
    if not link_id:
        raise Exception("diskwala browser-engine: couldn't extract a link ID from this URL")

    if not os.path.exists(_DISKWALA_ENGINE_PATH):
        raise Exception("diskwala browser-engine: diskwala_engine.js not found in this deployment")

    try:
        proc = subprocess.run(
            ["node", _DISKWALA_ENGINE_PATH, link_id],
            capture_output=True, text=True, timeout=45,
        )
    except subprocess.TimeoutExpired:
        raise Exception("diskwala browser-engine: timed out after 45s (browser launch/navigation took too long)")
    except FileNotFoundError:
        raise Exception("diskwala browser-engine: 'node' executable not found on PATH")

    if proc.returncode != 0:
        raise Exception(f"diskwala browser-engine: exited with code {proc.returncode}: {(proc.stderr or '')[-500:]}")

    # The script only ever prints one JSON line (console.log(JSON.stringify(...)))
    # but take the last non-empty line defensively in case Node or a
    # dependency ever logs a stray line to stdout first.
    lines = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
    if not lines:
        raise Exception("diskwala browser-engine: produced no output")
    try:
        data = json.loads(lines[-1])
    except ValueError:
        raise Exception(f"diskwala browser-engine: couldn't parse output as JSON: {lines[-1][:500]}")

    if not data.get("success"):
        raise Exception(f"diskwala browser-engine: {data.get('error') or 'unknown failure'}")

    dl = data.get("stream_url") or data.get("download_url")
    if not dl:
        raise Exception("diskwala browser-engine: reported success but gave no usable URL")

    raw_name = str(data.get("title") or f"diskwala_{link_id}").strip() or f"diskwala_{link_id}"
    ext = os.path.splitext(raw_name)[1].lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{2,4}", ext or ""):
        ext = "mp4"
        name = f"{raw_name}.{ext}"
    else:
        name = raw_name

    return {
        "name": name, "extension": ext, "size": data.get("size_bytes") or 0,
        "downloadUrl": dl, "streamUrl": dl,
        "thumb": data.get("thumbnail") or None, "creator": None, "duration_seconds": None,
        "views": None, "likes": None, "description": None,
        "upload_date": None, "category": "Video",
    }














def _parse_duration_string(raw: str) -> int | None:
    """Turns '7:17' / '1:07:17' / a bare '437' into total seconds. Returns
    None (rather than 0) for anything unparseable, so callers can tell
    "found nothing" apart from a genuine 0-second value."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        if ":" in raw:
            parts = [int(p) for p in raw.split(":")]
            return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
        return int(float(raw))
    except (ValueError, TypeError):
        return None






def resolve_diskwala_html(link: str) -> dict:
    """Fallback resolver: scrape the Diskwala share page directly for
    a video URL, bypassing the bearer-token miniapp API entirely. Used when
    api2.diskwala.net returns an error (e.g. 404 "not found") for a link
    that otherwise loads fine in a browser.
    """
    if "diskwala" in link.lower():
        headers = {
            "User-Agent": HTML_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://diskwala.in/",
            "Origin": "https://diskwala.in",
        }
    elif "diskwala" in link.lower():
        # Bug fix: this function is also used as fetch_diskwala_video's
        # generic last-resort fallback for Diskwala links (after
        # resolve_diskwala_html and the token-API both fail) — without this
        # branch it fell into the diskwala.com else-case below and sent a
        # diskwala.com Referer/Origin to diskwala.com, which can make the
        # scrape less likely to succeed.
        headers = {
            "User-Agent": HTML_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://diskwala.com/",
            "Origin": "https://diskwala.com",
        }
    else:
        headers = {
            "User-Agent": HTML_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.diskwala.com/",
            "Origin": "https://www.diskwala.com",
        }

    r = requests.get(link, headers=headers, timeout=20, allow_redirects=True)
    r.raise_for_status()
    # Named html_text, not html — this function's local scope used to
    # shadow the module-level `import html` (stdlib, used for
    # html.unescape elsewhere in this file). Harmless today since nothing
    # in this function calls html.unescape, but a landmine for any future
    # edit that adds one here.
    html_text = r.text
    soup = BeautifulSoup(html_text, "html.parser")

    name = None
    thumb = None
    download_url = None

    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        name = og_title["content"].strip()

    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        thumb = urljoin(link, og_image["content"].strip())

    # <video>/<source> tags
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src")
        if src:
            download_url = urljoin(link, src.strip().strip("\"'"))
            break

    # Embedded JSON / JS blobs
    if not download_url:
        json_url_patterns = [
            r'"downloadUrl"\s*:\s*"([^"]+)"',
            r'"download_url"\s*:\s*"([^"]+)"',
            r'"directUrl"\s*:\s*"([^"]+)"',
            r'"direct_url"\s*:\s*"([^"]+)"',
            r'"contentUrl"\s*:\s*"([^"]+)"',
            r'"content_url"\s*:\s*"([^"]+)"',
        ]
        for script in soup.find_all("script"):
            content = script.string
            if not content:
                continue
            matched = False
            for pattern in json_url_patterns:
                m = re.search(pattern, content)
                if m:
                    download_url = urljoin(link, m.group(1))
                    matched = True
                    break
            if not matched:
                m2 = re.search(r'(https?://[^\s"\'<>]+\.(?:mp4|mkv|webm|m3u8)[^\s"\'<>]*)', content)
                if m2:
                    download_url = m2.group(1)
                    matched = True
            if matched:
                break

    # Last resort: raw media-URL scan of the full page text
    if not download_url:
        m3 = re.search(r'(https?://[^\s"\'<>]+\.(?:mp4|mkv|webm|m3u8)[^\s"\'<>]*)', html_text)
        if m3:
            download_url = m3.group(1)

    # __NEXT_DATA__ — Diskwala, Diskwala and Diskwala are Next.js apps.
    # The video URL is embedded in window.__NEXT_DATA__ JSON, not in
    # <video> tags or plain script variables. This is the most reliable
    # extraction path when the API token is expired or unavailable.
    if not download_url:
        nd_match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(\{.*?\})</script>', html_text, re.DOTALL)
        if nd_match:
            try:
                nd = json.loads(nd_match.group(1))
                nd_text = json.dumps(nd)
                # Search entire JSON blob for any video URL
                for pat in [
                    r'"(?:downloadUrl|download_url|directUrl|direct_url|contentUrl|content_url|streamUrl|stream_url|url|videoUrl|video_url)"\s*:\s*"(https?://[^"]+\.(?:mp4|mkv|webm|m3u8)[^"]*)"',
                    r'"(?:src|source|file|link)"\s*:\s*"(https?://[^"]+\.(?:mp4|mkv|webm|m3u8)[^"]*)"',
                ]:
                    m4 = re.search(pat, nd_text, re.IGNORECASE)
                    if m4:
                        download_url = m4.group(1).replace('\\/', '/')
                        break
                # Also grab title and thumbnail from __NEXT_DATA__ if not found yet
                if not name:
                    for pat in [r'"(?:title|name|filename)"\s*:\s*"([^"]{3,200})"']:
                        tm = re.search(pat, nd_text)
                        if tm:
                            name = tm.group(1)
                            break
                if not thumb:
                    for pat in [r'"(?:thumbnail|thumb|poster|image|cover)"\s*:\s*"(https?://[^"]+)"']:
                        im = re.search(pat, nd_text)
                        if im:
                            thumb = im.group(1).replace('\\/', '/')
                            break
            except Exception as e:
                logger.debug(f"__NEXT_DATA__ parse failed: {e}")

    # Last-ditch: internal metadata endpoint — extended to cover Diskwala
    # and Diskwala IDs in addition to Diskwala app IDs.
    if not download_url:
        id_match = (
            re.search(r"diskwala\.com/app/([A-Za-z0-9]+)", link)
            or re.search(r"diskwala\.in/watch/([A-Za-z0-9_-]+)", link)
            or re.search(r"diskwala\.com/(?:s|share|f|v|d)/([A-Za-z0-9_-]+)", link)
        )
        if id_match:
            try:
                # Try all known metadata endpoints
                endpoints_to_try = [
                    ("POST", "https://api2.diskwala.net/api/v1/file/temp_info", {"id": id_match.group(1)}),
                    ("POST", "https://api2.diskwala.net/api/diskwala/temp_info", {"id": id_match.group(1)}),
                    ("POST", "https://api2.diskwala.net/api/diskwala/temp_info",   {"id": id_match.group(1)}),
                ]
                for method, url_ep, payload in endpoints_to_try:
                    try:
                        api_resp = requests.post(url_ep, json=payload, headers=headers, timeout=15)
                        if api_resp.status_code == 200:
                            data = api_resp.json()
                            payload_data = data.get("data") if isinstance(data.get("data"), dict) else data
                            for key in ("downloadUrl", "download_url", "url", "video_url", "streamUrl"):
                                if payload_data.get(key):
                                    download_url = payload_data[key]
                                    break
                            if download_url:
                                break
                    except Exception:
                        continue
            except Exception as e:
                logger.info(f"HTML-fallback metadata endpoint also failed: {e}")

    if not download_url:
        # Diagnostic breadcrumb for whoever reads the server logs next —
        # the generic "not found" message alone doesn't say WHY the
        # scrape came up empty (dead file vs. a changed page structure
        # vs. a bot-detection challenge page), and there's no way to
        # re-fetch this exact response later to check. A short snippet
        # plus a couple of common tells is usually enough to tell those
        # apart at a glance without needing to reproduce the request.
        page_title_tag = soup.find("title")
        page_title_text = page_title_tag.get_text(strip=True) if page_title_tag else None
        lower_html = html_text.lower()
        looks_challenged = any(
            marker in lower_html
            for marker in ("just a moment", "checking your browser", "cf-challenge", "captcha", "cloudflare")
        )
        logger.warning(
            f"resolve_diskwala_html: no media URL found for {link} — "
            f"page <title>: {page_title_text!r}, "
            f"looks_like_bot_challenge={looks_challenged}, "
            f"html_len={len(html_text)}, snippet={html_text[:300]!r}"
        )
        raise Exception("not found (HTML fallback also found no media URL)")

    if not name:
        fn_match = re.search(r"/([^/?#]+?)(?:\?|#|$)", download_url)
        name = fn_match.group(1) if fn_match else "video.mp4"
    if "." not in name:
        ext_match = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", download_url)
        name += "." + ext_match.group(1) if ext_match else ".mp4"

    # Extra metadata: this fallback previously returned creator/upload_date/
    # views/likes/description/category as entirely absent, no matter what
    # the page or Diskwala's own API actually had — a token-API failure
    # (the only reason this HTML fallback ever runs) meant those fields
    # silently disappeared even though two other sources for them already
    # exist elsewhere in this file. Reuses both, same combination
    # fetch_diskwala_preview already relies on: the page's own
    # __NEXT_DATA__/JSON-LD (_scrape_preview_from_html) plus Diskwala's
    # internal temp_info endpoint, which — unlike for Diskwala links — DOES
    # recognize genuine diskwala.com/app/<id> file IDs.
    scraped_meta = {}
    try:
        scraped_meta = _scrape_preview_from_html(link, html_text)
    except Exception as e:
        logger.info(f"resolve_diskwala_html: metadata scrape failed: {e}")

    temp_meta = {}
    try:
        temp_meta = fetch_diskwala_temp_info(link)
    except Exception as e:
        logger.info(f"resolve_diskwala_html: temp_info fetch failed: {e}")

    if not thumb:
        thumb = scraped_meta.get("thumb") or temp_meta.get("thumb")

    logger.info(f"HTML fallback resolved: {name} -> {download_url[:120]}")

    return {
        "name": name,
        "size": 0,
        "downloadUrl": download_url,
        "streamUrl": download_url,
        "thumb": thumb,
        "creator": temp_meta.get("creator") or scraped_meta.get("author"),
        "duration_seconds": temp_meta.get("duration_seconds") or scraped_meta.get("duration_seconds"),
        "views": temp_meta.get("views"),
        "likes": temp_meta.get("likes"),
        "description": temp_meta.get("description"),
        "upload_date": temp_meta.get("upload_date") or scraped_meta.get("upload_date"),
        "category": temp_meta.get("category"),
    }


def fetch_diskwala_video(link: str, auth: str) -> dict:
    """Fetch video info, routing Diskwala and Diskwala links differently:

    - Diskwala links now go through the token-API tier FIRST
      (api2.diskwala.net/api/diskwala/*, via _get_endpoints() — the same
      infra Diskwala links use), falling back to resolve_diskwala_html()
      (direct scrape + cookie) only if that fails, then a generic HTML
      scrape as the last resort.

      REVERSED from the previous ordering (direct-scrape first): that
      assumed diskwala.com's own /user/files page could be scraped for a
      real download link once logged in, which turned out to be false
      even with a valid, freshly-generated cookie — confirmed live
      (2026-09-25): save/files calls succeeded but returned zero usable
      links, because /user/files is a client-rendered dashboard shell
      with no file data in its server response at all. The token-API
      tier is a genuinely different, independently-working path (same
      one Diskwala links use) rather than another guess at scraping
      diskwala.com itself, so it goes first now. resolve_diskwala_html()
      still runs as tier 2 — it's gained real value since this ordering
      was first written (the share-page/​__NEXT_DATA__/​/user/save-body
      tiers added to it since can occasionally succeed where the plain
      /user/files href scrape couldn't), so it's still worth trying, just
      no longer assumed to be the more reliable of the two.

    - Diskwala links are unaffected — still token-API first, with the
      existing HTML-scrape fallback if that fails, same as before."""
    if "diskwala." in link.lower():
        try:
            return _fetch_diskwala_video_via_api(link, auth)
        except Exception as api_error:
            logger.warning(f"Diskwala token-API fetch failed ({api_error}), trying direct-scrape/cookie fallback...")
            try:
                return resolve_diskwala_html(link)
            except Exception as diskwala_error:
                logger.warning(f"Diskwala direct scrape also failed ({diskwala_error}), trying generic HTML fallback...")
                try:
                    return resolve_diskwala_html(link)
                except Exception:
                    # All three attempts failed — the token-API's own
                    # error (Diskwala's live backend response) is more
                    # useful to the user than a generic HTML "not found".
                    raise api_error

    try:
        return _fetch_diskwala_video_via_api(link, auth)
    except Exception as api_error:
        logger.warning(f"Token-API fetch failed ({api_error}), trying HTML fallback...")
        try:
            return resolve_diskwala_html(link)
        except Exception as html_error:
            logger.warning(f"HTML fallback also failed: {html_error}")
            raise api_error


def _fetch_diskwala_video_via_api(link: str, auth: str) -> dict:
    """Original bearer-token miniapp API path.

    BUG FIX — Diskwala: the download endpoint was always called with
    {"link": link}, the same payload shape Diskwala's endpoints
    take. But this file's OWN last-ditch temp_info fallback further up
    (resolve_diskwala_html's metadata lookup) calls ITS diskwala
    endpoint with {"id": <share id>} instead — suggesting
    api2.diskwala.net's diskwala routes key off the bare share id, not
    the full watch URL. Every Diskwala link was coming back a flat
    "not found" from this endpoint, including freshly-shared links that
    open fine in a browser — that pattern (100% failure regardless of
    which file) matches an endpoint being sent a payload shape it
    doesn't recognize, not every link actually being missing.

    So for Diskwala specifically, {"id": id} is now tried FIRST —
    falling back to the original {"link": link} if that also comes back
    not-ok, so this can't make a previously-working case worse. The
    status-poll identifier below follows whichever payload the download
    call actually succeeded with, on the assumption a REST API keys its
    status lookup the same way it keyed the request that created it."""
    is_diskwala = "diskwala.com" in link.lower()
    headers = {
        "Authorization": f"Bearer {auth}",
        "X-Bot-Id": "diskwala" if is_diskwala else "diskwala",
        "Content-Type": "application/json",
        # BUG FIX: this Origin/Referer pair (miniapp.diskwala.net) is
        # sky577bot's own webapp — correct for Diskwala, but Diskwala's
        # token now comes from diskwalanbot's own webapp instead (see
        # _async_get_diskwala_auth_token's docstring), so a Diskwala request
        # presents that webapp's own Origin/Referer too, matching a
        # confirmed-working reference implementation for this exact
        # endpoint pair rather than guessing they don't matter.
        "Origin": "https://diskwala-downloader.pages.dev" if is_diskwala else "https://miniapp.diskwala.net",
        "Referer": "https://diskwala-downloader.pages.dev/" if is_diskwala else "https://miniapp.diskwala.net/",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
    }

    download_api, status_api_prefix = _get_endpoints(link)

    vb_id = _extract_diskwala_id(link) if "diskwala.in" in link.lower() else None
    payloads_to_try = [{"id": vb_id}, {"link": link}] if vb_id else [{"link": link}]

    data = {}
    status_identifier = link
    for payload in payloads_to_try:
        logger.info(f"Calling API: {download_api} payload_keys={list(payload.keys())}")
        r = requests.post(download_api, headers=headers, json=payload, timeout=60)
        logger.info(f"Download response: {r.status_code} - {r.text[:200]}")
        if r.status_code in (401, 403):
            raise DiskwalaAuthError(f"Diskwala auth token rejected (HTTP {r.status_code})")
        try:
            data = r.json()
        except Exception:
            data = {"ok": False, "error": f"non-JSON response (HTTP {r.status_code})"}
        # WIRING FIX: the response can come back fully encrypted at the
        # TOP level ({"_x": true, "s":..., "h":..., "p":...}), not just
        # nested under "file" (that case is already handled below, at
        # the "done" branch). An encrypted top-level response has no
        # "ok" key, so without this it fell straight into "not
        # data.get('ok')" below and raised a generic, useless "API
        # Error: {...}" — the real content was sitting right there,
        # just never decrypted.
        if data.get("_x"):
            try:
                data = decrypt_file(data)
            except Exception as e:
                logger.warning(f"Top-level response looked encrypted but decrypt failed: {e}")
        if data.get("ok"):
            status_identifier = payload.get("id") or payload.get("link")
            break

    if not data.get("ok"):
        raise Exception(data.get("error", f"API Error: {data}"))

    status_url = status_api_prefix + quote(status_identifier, safe="")

    poll_interval = 0.5   # adaptive backoff: 0.5s → 1s → 2s (capped)
    for _ in range(90):
        r = requests.get(status_url, headers=headers, timeout=60)
        if r.status_code in (401, 403):
            raise DiskwalaAuthError(f"Diskwala auth token rejected while polling (HTTP {r.status_code})")
        data = r.json()
        # Same top-level-encryption case as the initial POST response above.
        if data.get("_x"):
            try:
                data = decrypt_file(data)
            except Exception as e:
                logger.warning(f"Top-level status response looked encrypted but decrypt failed: {e}")

        if not data.get("ok"):
            raise Exception(data.get("error", f"API Error: {data}"))

        status = data.get("status", "").lower()

        if status == "pending":
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 2.0)
            continue

        # BUG FIX: "error" is a real, documented status this API returns
        # (confirmed from a real response: {"ok": True, "status": "error"})
        # — not a malformed/unrecognized value, so it shouldn't fall into
        # the generic "Unexpected status" catch-all below, which produced
        # a confusing raw-dict error message for what's actually a clean,
        # known failure signal (Diskwala itself couldn't process this
        # link — expired, removed, or conversion failed on their end).
        if status == "error":
            reason = data.get("error") or data.get("message") or data.get("reason")
            raise Exception(
                f"Diskwala couldn't process this link{f': {reason}' if reason else ''} "
                "(link may be invalid, expired, or removed)."
            )

        if status == "done":
            file = data.get("file")
            if not file:
                raise Exception(f"No file returned: {data}")

            # Decrypt if encrypted
            if file.get("_x"):
                logger.info("File is encrypted, decrypting...")
                file = decrypt_file(file)
                logger.info(f"Decrypted file: {json.dumps(file)[:300]}")

            def _parse_duration(raw):
                """Parse duration from int (seconds) or 'HH:MM:SS' string."""
                if raw is None:
                    return None
                try:
                    if isinstance(raw, str) and ":" in raw:
                        parts = [int(p) for p in raw.split(":")]
                        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
                    return int(float(raw))
                except (ValueError, TypeError):
                    return None

            # Also check top-level data dict for metadata (some API versions
            # put creator/duration at root level, not inside 'file')
            meta_src = {**file, **{k: v for k, v in data.items()
                                   if k not in ("file", "ok", "status")}}

            # Extension — present in decrypted API response as "extension": "mp4"
            raw_name_api = _pick(file, "name", "fileName", "filename", "title") or "video.mp4"
            api_ext = _pick(file, "extension", "ext", "fileExtension", "file_extension")
            if not api_ext and "." in raw_name_api:
                api_ext = raw_name_api.rsplit(".", 1)[-1].lower()
            api_ext = api_ext.lower().strip(".") if api_ext else None

            # Category — infer from extension if API doesn't provide it
            _VIDEO_EXTS = {"mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts", "3gp", "m2ts", "vob"}
            _AUDIO_EXTS = {"mp3", "flac", "aac", "ogg", "m4a", "wav", "opus", "wma", "alac", "aiff"}
            _IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "tif", "svg", "heic", "heif", "avif"}
            _DOC_EXTS   = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "zip", "rar", "7z", "tar", "gz"}
            api_category = _pick(meta_src, "category", "genre", "tag", "type")
            if not api_category and api_ext:
                if api_ext in _VIDEO_EXTS:
                    api_category = "Video"
                elif api_ext in _AUDIO_EXTS:
                    api_category = "Audio"
                elif api_ext in _IMAGE_EXTS:
                    api_category = "Photo"
                elif api_ext in _DOC_EXTS:
                    api_category = "Document"

            return {
                "name":             raw_name_api,
                "extension":        api_ext,
                "size":             _pick(file, "size", "fileSize", "length") or 0,
                "downloadUrl":      _pick(file, "downloadUrl", "download_url", "url", "link"),
                "streamUrl":        _pick(file, "streamUrl", "stream_url", "hls")
                                    or _pick(file, "downloadUrl", "download_url", "url", "link"),
                "thumb":            _pick(meta_src, "thumb", "thumbnail", "thumbnailUrl", "poster", "image"),
                # ── Extra metadata from API response ───────────────────────
                "creator":          _pick(meta_src, "creator", "uploader", "author", "uploaderName",
                                          "uploader_name", "channel", "creatorName", "username"),
                "duration_seconds": _parse_duration(
                                        _pick(meta_src, "duration", "duration_seconds",
                                              "durationSeconds", "video_duration", "length_seconds")
                                    ),
                "views":            _pick(meta_src, "views", "view_count", "viewCount",
                                          "watchCount", "watch_count"),
                "likes":            _pick(meta_src, "likes", "like_count", "likeCount"),
                "description":      _pick(meta_src, "description", "desc", "caption", "details"),
                "upload_date":      _pick(meta_src, "upload_date", "uploadDate", "created_at",
                                          "createdAt", "date", "uploadedAt"),
                "category":         api_category,
            }

        raise Exception(f"Unexpected status: {status} - {data}")

    raise Exception("Timeout waiting for Diskwala API response")

# ─────────────────────────────────────────────────────────────────────────────
#  PLAYLIST SUPPORT
# ─────────────────────────────────────────────────────────────────────────────
#
# BUG FIX — root cause: API_PLAYLIST_INFO below (api2.diskwala.net/api/
# diskwala/playlist) was a GUESSED endpoint that doesn't actually exist.
# Verified against a real working reference implementation
# (TeraBox-Video-Downloader's diskwalaDL/diskwala_dl.py): Diskwala has NO
# separate playlist/folder API at all — a playlist link gets POSTed to
# the exact same download endpoint _fetch_diskwala_video_via_api() above
# already uses for a single file (_get_endpoints() already returns the
# right (download_api, status_api) pair for a playlist URL — it isn't
# diskwala.com or diskwala.in, so it falls through to the plain
# API_DOWNLOAD/API_STATUS pair, same as any other diskwala.net link).
# The only real difference: a playlist's status response holds a LIST of
# files (under "files"/"items"/"children"/"contents"/"entries"/etc. —
# Diskwala deployments aren't consistent about which key) instead of a
# single "file" object, so it needs recursively flattening rather than
# picking one entry out.
#
# get_all_playlist_files() below is the fix, deliberately named and
# shaped to match terabox_downloader.py's own get_all_folder_files():
# same flat [{"name","url","size"}, ...] contract, "url" already a
# direct download URL (not something that needs re-resolving through
# this whole module again) — so a caller that already knows how to walk
# a TeraBox folder's file list knows how to walk this one too.
# fetch_playlist_info() below is kept, now implemented as a thin wrapper
# around get_all_playlist_files(), purely so
# fetch_playlist_info_with_auth_retry() (and anything else already
# calling it) keeps working unchanged.

_FOLDER_KEYS = (
    "files", "items", "children", "contents", "entries",
    "results", "fileList", "file_list", "videos"
)


def _looks_like_file(obj: dict) -> bool:
    """True when a dict already carries a usable direct-download URL —
    ported from the reference implementation's identically-named check."""
    if not isinstance(obj, dict):
        return False
    return any(obj.get(k) for k in ("downloadUrl", "download_url", "streamUrl", "stream_url", "url", "link"))


def _normalise_diskwala_files(obj, parent_path: str = "") -> list:
    """Recursively flattens a Diskwala status response into a flat file
    list — same recursion shape as the reference implementation's own
    _normalise_diskwala_files(), adapted to reuse THIS file's _pick() and
    decrypt_file() (rather than duplicating separate field-name-fallback
    and decrypt logic) so both stay in sync with the single-file path's
    own handling of the exact same response fields."""
    found = []

    if isinstance(obj, dict) and obj.get("_x"):
        try:
            obj = decrypt_file(obj)
        except Exception:
            return found

    if isinstance(obj, dict):
        if _looks_like_file(obj):
            name = _pick(obj, "name", "fileName", "filename", "title") or "diskwala_file"
            folder = parent_path.strip("/ ")
            if folder and folder.lower() not in str(name).lower():
                name = f"{folder}/{name}"
            url = _pick(obj, "downloadUrl", "download_url", "streamUrl", "stream_url", "url", "link")
            found.append({
                "name": str(name),
                "url": url,
                "size": int(_pick(obj, "size", "fileSize", "length") or 0),
            })
            return found

        node_name = obj.get("name") or obj.get("folderName") or obj.get("title")
        next_path = parent_path
        if node_name and any(k in obj for k in _FOLDER_KEYS):
            node_name = str(node_name).strip("/ ")
            if node_name:
                next_path = f"{parent_path}/{node_name}".strip("/")

        for key in _FOLDER_KEYS:
            value = obj.get(key)
            if value is not None:
                found.extend(_normalise_diskwala_files(value, next_path))
        if "data" in obj and not any(obj.get(k) is not None for k in _FOLDER_KEYS):
            found.extend(_normalise_diskwala_files(obj.get("data"), next_path))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(_normalise_diskwala_files(item, parent_path))

    return found


def get_all_playlist_files(playlist_url: str, auth: str) -> list:
    """Diskwala equivalent of terabox_downloader.py's
    get_all_folder_files(url) — same contract: returns
    [{"name","url","size"}, ...], one entry per file, "url" already a
    direct downloadable link. A playlist that (unusually) only holds one
    file still comes back as a 1-item list, same as TeraBox's function
    does for a plain single-file share.

    Goes through the SAME start-download/poll-status calls
    _fetch_diskwala_video_via_api() uses for a single file — see this
    section's module-level comment for why there's no separate playlist
    API to call instead."""
    headers = {
        "Authorization": f"Bearer {auth}",
        "X-Bot-Id": "diskwala",
        "Content-Type": "application/json",
        "Origin": "https://miniapp.diskwala.net",
        "Referer": "https://miniapp.diskwala.net/",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
    }
    _, status_api_prefix = _get_endpoints(playlist_url)
    download_api = API_DOWNLOAD_PLAYLIST

    r = requests.post(download_api, headers=headers, json={"link": playlist_url}, timeout=60)
    if r.status_code in (401, 403):
        raise DiskwalaAuthError(f"Diskwala auth token rejected (HTTP {r.status_code})")
    try:
        data = r.json()
    except Exception:
        data = {"ok": False, "error": f"non-JSON response (HTTP {r.status_code})"}
    if not data.get("ok"):
        raise Exception(data.get("error", f"API Error: {data}"))

    status_url = status_api_prefix + quote(playlist_url, safe="")
    poll_interval = 0.5
    data = None
    for _ in range(90):
        r = requests.get(status_url, headers=headers, timeout=60)
        if r.status_code in (401, 403):
            raise DiskwalaAuthError(f"Diskwala auth token rejected while polling (HTTP {r.status_code})")
        data = r.json()
        if not data.get("ok"):
            raise Exception(data.get("error", f"API Error: {data}"))

        status = data.get("status", "").lower()
        if status == "pending":
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 2.0)
            continue
        if status == "error":
            reason = data.get("error") or data.get("message") or data.get("reason")
            raise Exception(
                f"Diskwala couldn't process this playlist{f': {reason}' if reason else ''} "
                "(link may be invalid, expired, or removed)."
            )
        if status == "done":
            break
        raise Exception(f"Unexpected status: {status} - {data}")
    else:
        raise Exception("Timeout waiting for Diskwala API response")

    candidates = []
    if data.get("file") is not None:
        candidates.append(data.get("file"))
    for key in _FOLDER_KEYS:
        if data.get(key) is not None:
            candidates.append(data.get(key))
    if not candidates and data.get("data") is not None:
        candidates.append(data.get("data"))

    files = []
    for candidate in candidates:
        files.extend(_normalise_diskwala_files(candidate))

    unique = []
    seen = set()
    for item in files:
        key = (item.get("url"), item.get("name"))
        if key in seen or not item.get("url"):
            continue
        seen.add(key)
        unique.append(item)

    if not unique:
        raise Exception("Playlist resolved but contained no downloadable files")
    return unique



PLAYLIST_ID_RE = re.compile(
    r"diskwala\.com/playlist/([A-Za-z0-9]{24})",
    re.IGNORECASE,
)

# Full-URL version of PLAYLIST_ID_RE (scheme + optional subdomain + optional
# trailing path/query) — used by extract_playlist_links() below to pull
# whole matching URLs out of a message's text, not just the ID out of a
# single already-known URL (that's PLAYLIST_ID_RE's own job, via
# extract_playlist_id() right below it).
_PLAYLIST_URL_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?diskwala\.[a-z]{2,}/playlist/[A-Za-z0-9]{24}\S*",
    re.IGNORECASE,
)


def extract_playlist_id(url: str) -> str | None:
    m = PLAYLIST_ID_RE.search(url)
    return m.group(1) if m else None


def extract_playlist_links(text: str) -> list[str]:
    """Bug fix: main.py's link_handler calls this expecting the same
    contract as extract_diskwala_links(text) — scan arbitrary message
    text and return every matching URL, in order, de-duplicated — but it
    never actually existed here (only the single-URL extract_playlist_id()
    and is_playlist_link() did), so every message main.py processed raised
    AttributeError before it could even look for a playlist link, let
    alone anything else in the same message."""
    if not text:
        return []
    seen = set()
    links = []
    for match in _PLAYLIST_URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links



def is_playlist_link(url: str) -> bool:
    return bool(PLAYLIST_ID_RE.search(url))


def fetch_playlist_info(playlist_url: str, auth: str) -> dict:
    """Fetch playlist metadata + list of file links from Diskwala.

    BUG FIX: used to POST/GET against API_PLAYLIST_INFO
    (api2.diskwala.net/api/diskwala/playlist) — a guessed endpoint,
    confirmed against a real working reference implementation not to
    exist. Now a thin wrapper around get_all_playlist_files() (this
    section's real fix — see its own docstring/the module comment above
    it), just reshaped into this function's existing return contract so
    fetch_playlist_info_with_auth_retry() and anything else already
    calling fetch_playlist_info() keeps working unchanged:
      {
        "title": str,
        "thumb": str | None,
        "files": [{"name": str, "link": str}, ...]
      }

    IMPORTANT for whoever wires this into main.py's send/download step:
    each "link" here is ALREADY a direct downloadable URL (a playlist
    entry has no diskwala.net share URL of its own to re-resolve) — hand
    it to download_video() via its existing stream_url= direct-URL
    override (same pattern every other backend's direct-URL case already
    uses), not back through fetch_diskwala_video()/this module's normal
    link-resolution path again."""
    try:
        files_flat = get_all_playlist_files(playlist_url, auth)
    except DiskwalaAuthError:
        raise
    except Exception as e:
        logger.info(f"get_all_playlist_files failed ({e}), trying HTML scrape as a last resort...")
        return _scrape_playlist_html(playlist_url)
    return {
        "title": "Diskwala Playlist",
        "thumb": None,
        "files": [{"name": f["name"], "link": f["url"]} for f in files_flat],
    }


def _scrape_playlist_html(playlist_url: str) -> dict:
    """HTML fallback: scrape playlist page for app links."""
    headers = {
        "User-Agent": HTML_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.diskwala.com/",
    }
    r = requests.get(playlist_url, headers=headers, timeout=30, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    title_tag = soup.find("h1") or soup.find("meta", property="og:title")
    title = (
        (title_tag.get("content") if title_tag and title_tag.name == "meta" else
         title_tag.get_text(strip=True) if title_tag else None)
        or "Diskwala Playlist"
    )

    thumb_tag = soup.find("meta", property="og:image")
    thumb = thumb_tag.get("content") if thumb_tag else None

    # Find all /app/ links in the page
    file_links = []
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"/app/[A-Za-z0-9]{24}")):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = urljoin("https://www.diskwala.com", href)
        if href not in seen:
            seen.add(href)
            name = a.get_text(strip=True) or href.split("/")[-1]
            file_links.append({"name": name, "link": href})

    # Also scan script tags for JSON with app links
    if not file_links:
        for script in soup.find_all("script"):
            text = script.string or ""
            for m in re.finditer(r'diskwala\.com/app/([A-Za-z0-9]{24})', text):
                link = f"https://www.diskwala.com/app/{m.group(1)}"
                if link not in seen:
                    seen.add(link)
                    file_links.append({"name": f"video_{m.group(1)[:8]}.mp4", "link": link})

    # BUG FIX: neither of the two attempts above ever actually finds
    # anything, because diskwala.com is a Next.js app the same way single
    # video pages are (see resolve_diskwala_html's own __NEXT_DATA__
    # comment) — the playlist's file list is hydrated client-side from a
    # window.__NEXT_DATA__ JSON blob embedded in a <script> tag, not
    # rendered as plain <a href> links or matched by the generic
    # script-text regex above (which only catches a BARE "diskwala.com/
    # app/..." string sitting directly in some script's source, not one
    # buried inside a JSON literal alongside a hundred other fields).
    # Confirmed from a real failure: "Could not find any video links in
    # playlist page" on every single playlist link, every time — the
    # __NEXT_DATA__ step was simply missing. Same regex-over-the-whole-
    # JSON-blob technique as the single-video path, just scanning for
    # every /app/<24-char-id> occurrence instead of one download URL.
    if not file_links:
        nd_match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(\{.*?\})</script>', r.text, re.DOTALL)
        if nd_match:
            try:
                nd_text = json.dumps(json.loads(nd_match.group(1)))
            except Exception as e:
                logger.debug(f"playlist __NEXT_DATA__ parse failed: {e}")
                nd_text = nd_match.group(1)  # fall back to raw text; regex below still works on it
            for m in re.finditer(r'(?:diskwala\.[a-z]{2,})?/?app/([A-Za-z0-9]{24})', nd_text, re.IGNORECASE):
                file_id = m.group(1)
                link = f"https://www.diskwala.com/app/{file_id}"
                if link not in seen:
                    seen.add(link)
                    file_links.append({"name": f"video_{file_id[:8]}.mp4", "link": link})
            # Try to pick up real filenames alongside the ids, best-effort —
            # falls back to the generic "video_<id8>.mp4" names above if
            # this doesn't find anything (still fully functional either way).
            if file_links:
                names = re.findall(r'"(?:name|title|fileName|filename)"\s*:\s*"([^"]{3,200})"', nd_text)
                for i, nm in enumerate(names[:len(file_links)]):
                    file_links[i]["name"] = nm

    if not file_links:
        raise Exception("Could not find any video links in playlist page")

    return {"title": title, "thumb": thumb, "files": file_links}


# ─────────────────────────────────────────────────────────────────────────────
#  PREVIEW SCRAPER — title, author, duration, thumb from HTML (no auth needed)
# ─────────────────────────────────────────────────────────────────────────────

# Generic/site-level titles to ignore
_GENERIC_TITLES = {
    "diskwala", "diskwala", "free unlimited cloud", "cloud storage",
    "creator platform", "upload files", "share with",
}

def _is_generic_title(t: str) -> bool:
    if not t:
        return True
    tl = t.lower()
    return any(g in tl for g in _GENERIC_TITLES)


def _scrape_preview_from_html(link: str, html_text: str) -> dict:
    """Extract title/author/duration/thumb.
    Priority: __NEXT_DATA__ > JSON-LD > og:meta > page text.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    title = None
    author = None
    duration_seconds = 0
    thumb = None
    size = 0

    # 1. __NEXT_DATA__ (Next.js) — most reliable for Diskwala
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script and next_script.string:
        try:
            nd = json.loads(next_script.string)
            page_props = nd.get("props", {}).get("pageProps", {})
            file_data = (
                page_props.get("file")
                or page_props.get("video")
                or page_props.get("data")
                or page_props.get("fileData")
                or {}
            )
            if not file_data:
                for v in page_props.values():
                    if isinstance(v, dict) and (v.get("name") or v.get("title")):
                        file_data = v
                        break

            nd_title = (file_data.get("title") or file_data.get("name") or file_data.get("fileName"))
            if nd_title and not _is_generic_title(nd_title):
                title = str(nd_title).strip()

            nd_author = (
                file_data.get("creator") or file_data.get("author")
                or file_data.get("uploader") or file_data.get("username")
                or file_data.get("creatorName")
                or (file_data.get("user") or {}).get("username")
                or (file_data.get("user") or {}).get("name")
            )
            if nd_author and isinstance(nd_author, str):
                author = nd_author.strip()

            nd_dur = file_data.get("duration") or file_data.get("videoDuration")
            if nd_dur:
                try:
                    duration_seconds = int(float(nd_dur))
                except Exception:
                    pass

            nd_thumb = (
                file_data.get("thumb") or file_data.get("thumbnail")
                or file_data.get("coverImage") or file_data.get("poster")
            )
            if nd_thumb:
                thumb = urljoin(link, str(nd_thumb))

            nd_size = file_data.get("size") or file_data.get("fileSize")
            if nd_size:
                try:
                    size = int(nd_size)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"__NEXT_DATA__ parse failed: {e}")

    description = None

    # 2. JSON-LD: fill gaps
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if not title:
                ld_t = data.get("name") or data.get("headline")
                if ld_t and not _is_generic_title(ld_t):
                    title = str(ld_t).strip()
            if not author:
                ao = data.get("author") or data.get("creator") or data.get("uploadedBy")
                if ao:
                    author = (ao.get("name") if isinstance(ao, dict) else str(ao)).strip()
            if not duration_seconds:
                dur_str = data.get("duration")
                if dur_str:
                    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", str(dur_str))
                    if m:
                        h, mi, s = (int(x or 0) for x in m.groups())
                        duration_seconds = h * 3600 + mi * 60 + s
            if not thumb:
                img = data.get("thumbnailUrl") or data.get("image")
                if img:
                    thumb = urljoin(link, img if isinstance(img, str) else img[0])
            if not description:
                # schema.org's VideoObject (and the generic Article/
                # CreativeWork types some pages use instead) both define
                # "description" as a standard property — this isn't a
                # guess about Diskwala's own markup, it's the same
                # standard field every other JSON-LD value above already
                # reads from this same script tag.
                ld_desc = data.get("description")
                if ld_desc and isinstance(ld_desc, str) and ld_desc.strip():
                    description = ld_desc.strip()
        except Exception:
            pass

    # 3. og:image for thumb (og:title skipped — usually generic on Diskwala)
    if not thumb:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            thumb = urljoin(link, og_img["content"].strip())

    # og:description — same universal convention as og:image above, and
    # the only other source this function has for description now that
    # fetch_diskwala_temp_info's API endpoints are unreliable (see
    # fetch_diskwala_preview's merge logic below, which prefers this
    # HTML-scraped value over temp_info's, same "HTML first" priority
    # every other field in this function already uses).
    if not description:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content") and og_desc["content"].strip():
            description = og_desc["content"].strip()

    # 4. Plain HTML fallbacks.
    if not title:
        h1 = soup.find("h1")
        if h1:
            t = h1.get_text(strip=True)
            if not _is_generic_title(t):
                title = t

    upload_date = None

    # 5. Author / duration from raw JSON blobs in page scripts
    if not author:
        for pat in [
            r'"creatorName"\s*:\s*"([^"]+)"',
            r'"username"\s*:\s*"([^"]+)"',
            r'"creator"\s*:\s*"([^"]+)"',
            r'"author"\s*:\s*"([^"]+)"',
            r'"uploader"\s*:\s*"([^"]+)"',
        ]:
            am = re.search(pat, html_text)
            if am:
                c = am.group(1).strip()
                if c.lower() not in ("admin","system","bot","diskwala","diskwala","null","undefined"):
                    author = c
                    break

    if not duration_seconds:
        dm = re.search(r'"duration"\s*:\s*(\d+)', html_text)
        if dm:
            duration_seconds = int(dm.group(1))

    return {"title": title, "author": author, "duration_seconds": duration_seconds, "thumb": thumb,
            "size": size, "upload_date": upload_date, "description": description}


def fetch_diskwala_temp_info(link: str) -> dict:
    """Calls the same api2.diskwala.net/api/v1/file/temp_info
    endpoint already used elsewhere in this file (as a last-resort
    download-URL fallback in resolve_diskwala_html) — same request shape,
    just reading more of the payload this time: creator, duration, view/
    like counts, description, upload date, category, size, and thumbnail,
    not just the media URL.

    This is an undocumented internal endpoint (found via reverse
    engineering, not an official API) with no confirmed field-naming —
    each attribute below tries several plausible key spellings the way
    the existing downloadUrl/download_url/url/... fallback chain already
    does, since that's the only naming convention evidence available for
    this endpoint from within this codebase. Fields the response doesn't
    have (or that don't match any of the tried spellings) just come back
    None — callers already treat any None field as "unknown", same as
    the HTML-scrape path.

    Returns a dict with keys: creator, duration_seconds, views, likes,
    description, upload_date, category, size, thumb. Returns all-None on
    any failure (link doesn't look like a diskwala.com/app/<id> link,
    network error, non-200, unexpected JSON shape, etc.) rather than
    raising — this is meant to be a best-effort enrichment layered on
    top of fetch_diskwala_preview's existing HTML scrape, not something
    that should ever block showing what the HTML scrape already found."""
    empty = {"creator": None, "duration_seconds": None, "views": None, "likes": None,
             "description": None, "upload_date": None, "category": None,
             "size": None, "thumb": None}

    file_id_match = re.search(r"diskwala\.com/app/([A-Za-z0-9]+)", link)
    if file_id_match:
        file_id = file_id_match.group(1)
    else:
        # Diskwala shares the same api2.diskwala.net backend as Diskwala
        # (see _get_endpoints() above) — worth trying its share id here
        # too rather than assuming this endpoint is Diskwala-only just
        # because that's the only pattern this file's other temp_info
        # call site (in resolve_diskwala_html) happened to check for.
        # Diskwala also goes through api2.diskwala.net (see
        # _get_endpoints() above), so its share id is worth trying too —
        # without this, temp_info always returned `empty` for diskwala
        # links and views/likes/creator/etc. never got filled in.
        file_id = _extract_diskwala_id(link) or _extract_diskwala_id(link)
    if not file_id:
        return empty

    headers = {
        "User-Agent": HTML_USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.diskwala.com/",
        "Origin": "https://www.diskwala.com",
    }

    # Try multiple endpoints — api2.diskwala.net has several info paths,
    # and which one works depends on the file type and API version.
    # temp_info returns 404 for some files; info/meta/file_info may work instead.
    _INFO_ENDPOINTS = [
        ("POST", "https://api2.diskwala.net/api/v1/file/temp_info", {"id": file_id}),
        ("POST", "https://api2.diskwala.net/api/v1/file/info",      {"id": file_id}),
        ("POST", "https://api2.diskwala.net/api/v1/file/meta",      {"id": file_id}),
        ("GET",  f"https://api2.diskwala.net/api/v1/file/{file_id}", None),
        ("POST", "https://api2.diskwala.net/api/diskwala/file_info", {"id": file_id}),
    ]

    data = None
    for method, endpoint, body in _INFO_ENDPOINTS:
        try:
            if method == "POST":
                resp = requests.post(endpoint, json=body, headers=headers, timeout=15)
            else:
                resp = requests.get(endpoint, headers=headers, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                break
            else:
                logger.info(f"temp_info returned status {resp.status_code} for {endpoint[:60]}")
        except Exception as e:
            logger.info(f"temp_info call failed for {endpoint[:40]}: {e}")

    if data is None:
        return empty

    payload = data.get("data") if isinstance(data.get("data"), dict) else data

    def _first(*keys):
        for key in keys:
            val = payload.get(key)
            if val not in (None, ""):
                return val
        return None

    duration_raw = _first("duration", "duration_seconds", "durationSeconds", "length", "video_duration")
    duration_seconds = None
    if duration_raw is not None:
        try:
            # Some of these endpoints hand back "HH:MM:SS" instead of a
            # raw number — this covers both without needing to know in
            # advance which shape this particular response used.
            if isinstance(duration_raw, str) and ":" in duration_raw:
                parts = [int(p) for p in duration_raw.split(":")]
                duration_seconds = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
            else:
                duration_seconds = int(float(duration_raw))
        except (ValueError, TypeError):
            duration_seconds = None

    return {
        "creator": _first("creator", "uploader", "author", "uploaderName", "uploader_name", "channel"),
        "duration_seconds": duration_seconds,
        "views": _first("views", "view_count", "viewCount", "watchCount", "watch_count"),
        "likes": _first("likes", "like_count", "likeCount"),
        "description": _first("description", "desc", "caption"),
        "upload_date": _first("upload_date", "uploadDate", "created_at", "createdAt", "date"),
        "category": _first("category", "genre", "tag", "type"),
        "size": _first("size", "fileSize", "file_size"),
        "thumb": _first("thumbnail", "thumb", "thumbnailUrl", "thumbnail_url", "poster", "image"),
    }


def fetch_diskwala_preview(link: str) -> dict:
    """Scrape Diskwala OR Diskwala share page to get title, author, duration,
    and thumbnail WITHOUT needing a bearer token.  Returns a dict with keys:
        title, author, duration_seconds, thumb, size,
        views, likes, description, upload_date, category
    Any field that can't be found is None / 0.

    The last five keys come from fetch_diskwala_temp_info() (Option B —
    the api2.diskwala.net file-info endpoint) layered on top of this
    function's own HTML scrape below — that scrape has no way to surface
    view/like counts, a description, upload date, or category at all, so
    those five are always temp_info's alone; author/duration_seconds/
    thumb/size only get overwritten by temp_info's version if the HTML
    scrape came back empty for that particular field.
    """
    headers = {
        "User-Agent": HTML_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Route Diskwala links to their own page URL
    if "diskwala" in link.lower():
        share_id = _extract_diskwala_id(link)
        if share_id:
            link = f"https://diskwala.com/s/{share_id}"
        headers["Referer"] = "https://diskwala.com/"
    elif "diskwala" in link.lower():
        headers["Referer"] = "https://diskwala.in/"
    else:
        headers["Referer"] = "https://www.diskwala.com/"

    try:
        r = requests.get(link, headers=headers, timeout=20, allow_redirects=True)
        r.raise_for_status()
        html_text = r.text
        result = _scrape_preview_from_html(link, html_text)
    except Exception as e:
        logger.warning(f"fetch_diskwala_preview GET failed: {e}")
        result = {"title": None, "author": None, "duration_seconds": 0, "thumb": None, "size": 0,
                  "upload_date": None, "description": None}

    temp_info = fetch_diskwala_temp_info(link)
    result["views"] = temp_info["views"]
    result["likes"] = temp_info["likes"]
    # FIX: this used to unconditionally overwrite with temp_info's
    # description, same bug the upload_date fix below already covers —
    # temp_info's api2.diskwala.net endpoints have been 404ing (confirmed
    # in production logs), which made description ALWAYS come back None
    # with no way to recover it even though the page's own JSON-LD/
    # og:description usually has it (see _scrape_preview_from_html's new
    # description extraction above). views/likes/category genuinely have
    # no HTML-scrape source in this function yet, so those three still
    # depend entirely on temp_info and will stay None while it's down —
    # that's a real, known gap, not something this fix silently papers
    # over.
    result["description"] = result.get("description") or temp_info["description"]
    # Bug fix: this used to unconditionally overwrite with temp_info's
    # upload_date — for Diskwala links that's always None (temp_info's
    # api2.diskwala.net endpoints don't recognize Diskwala file IDs at all,
    # it's a Diskwala-only internal API), which threw away the real value
    # _scrape_preview_from_html above already found from the page's own
    # data-datetime attribute. Same "HTML scrape first, temp_info fills
    # the gap" priority the other fields below already use.
    result["upload_date"] = result.get("upload_date") or temp_info["upload_date"]
    result["category"] = temp_info["category"]
    if not result.get("author"):
        result["author"] = temp_info["creator"]
    if not result.get("duration_seconds"):
        result["duration_seconds"] = temp_info["duration_seconds"] or 0
    if not result.get("thumb"):
        result["thumb"] = temp_info["thumb"]
    if not result.get("size"):
        result["size"] = temp_info["size"] or 0

    logger.info(f"Preview scraped ({link[:60]}): title={result['title']!r} author={result['author']!r} dur={result['duration_seconds']}s")
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  AUTO DISKWALA COOKIE GENERATOR
#  (ported from TeraBox-Video-Downloader/scripts/auto_diskwala_cookie.py)
#
#  Creates a disposable mail.tm inbox → registers on Diskwala → verifies email
#  → completes onboarding → returns a ready-to-use cookie string.
#
#  BUG FIX: this function itself always worked, but nothing in the
#  codebase ever called it — this comment used to (still incorrectly)
#  claim it was "used by the /refresh_diskwala_cookie admin command", which
#  never actually existed either. Every Diskwala link without a manually-
#  set DISKWALA_COOKIE just hit "Diskwala only serves direct links to a
#  logged-in account" and stopped there. Now wired up two ways: automatic
#  on-demand via _get_diskwala_cookie() (called from resolve_diskwala_html()),
#  and manually via refresh_diskwala_cookie() — which the /refresh_diskwala_cookie
#  command in main.py actually does now call.
# ─────────────────────────────────────────────────────────────────────────────



# In-memory cache for an auto-generated Diskwala cookie — kept separate from
# DISKWALA_COOKIE itself, which stays a fixed env-var override for anyone
# who wants to supply their own real logged-in account instead. Same
# "recognize the resource isn't up, (re)build it, cache it" shape as
# cf_bypass.py's own per-domain cache, just for a Diskwala account instead
# of a Cloudflare clearance cookie.








# ═════════════════════════════════════════════════════════════════════════
#  fbot-main INTEGRATION ADAPTER

# ── Per-URL resolve cache ────────────────────────────────────────────────────
# Both get_page_meta() and get_available_qualities() call _resolve_no_auth().
# Without a cache they make TWO separate API calls for the same URL within
# a few seconds of each other (quality-menu render, then download confirm).
# Cache the result for 5 minutes — long enough to cover both calls for a
# single user interaction, short enough not to serve stale download URLs.
_resolve_cache: dict = {}
_RESOLVE_CACHE_TTL = 300   # seconds


def _resolve_no_auth_cached(link: str) -> dict:
    """Like _resolve_no_auth() but caches the result for _RESOLVE_CACHE_TTL
    seconds to avoid double-resolving for get_page_meta + get_available_qualities."""
    now = time.time()
    cached = _resolve_cache.get(link)
    if cached and (now - cached[1]) < _RESOLVE_CACHE_TTL:
        logger.debug(f"[diskwala] resolve cache hit: {link[:60]}")
        return cached[0]
    result = _resolve_no_auth(link)
    _resolve_cache[link] = (result, now)
    # Keep cache small — prune oldest 50 entries when over 100
    if len(_resolve_cache) > 100:
        for k in sorted(_resolve_cache, key=lambda k: _resolve_cache[k][1])[:50]:
            _resolve_cache.pop(k, None)
    return result

#
#  Everything above this point is ultra-main's diskwala.py, ported over
#  as-is. Everything below is new — a thin adapter exposing the exact
#  is_X_link / extract_X_links / get_page_meta / get_available_qualities /
#  download_video / get_stream_url interface every other backend module
#  in fbot-main's main.py (_downloader_for, show_quality_menu,
#  download_video) already duck-types against, so this module slots in
#  the same way terabox_downloader.py or faphouse_downloader.py do,
#  without touching that shared dispatch code at all.
#
#  TOKEN-API TIER — now wired up. ultra-main's get_auth_token() tier
#  needed a real Telegram *user* session (Telethon, phone-number login —
#  separate from this bot's own Pyrogram *bot* token) logged into
#  Telegram's "sky577bot" Mini App to pull a bearer token out of the
#  WebApp URL. That infrastructure is now ported too (see the Telethon
#  auth-token block near the top of this file: get_auth_token_sync(),
#  resolve_diskwala_with_auth_retry()) — it runs on its own dedicated
#  background event-loop thread so these sync adapter functions can call
#  it without needing an `async def`.
#
#  It only actually activates if the SESSION env var is set (see
#  config.py's comment on it) — a manual one-time Telethon login is still
#  required, this port just can't skip that step for you. Without
#  SESSION, or if the Telethon call fails for any reason (bad/expired
#  session, network issue, etc.), _resolve_no_auth() below catches it and
#  falls straight through to the same no-token HTML-scrape path
#  (resolve_diskwala_html / resolve_diskwala_html) this adapter always used
#  — so nothing breaks for a deploy that hasn't configured SESSION.
# ═════════════════════════════════════════════════════════════════════════

_DISKWALA_DOMAINS_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?(?:diskwala\.[a-z]{2,}|thediskwala\.[a-z]{2,})",
    re.IGNORECASE,
)


def is_diskwala_link(url: str) -> bool:
    """Return True only for recognised Diskwala URLs."""
    if not url:
        return False
    return bool(_DISKWALA_DOMAINS_RE.search(url)) or bool(
        re.search(r"https?://t\.me/(?:sky577bot|diskwalabot)(?:/[^?]+)?\?(?:startapp|start)=", url, re.I)
    )



def _looks_like_not_found(msg: str) -> bool:
    """True if an error message from either the token-API tier or an
    HTML scrape reads like the file genuinely doesn't exist server-side
    (deleted/expired/invalid share link) rather than a transient network
    or auth problem. Used only to phrase the combined error below more
    clearly for that — very common — case; doesn't change control flow."""
    m = (msg or "").lower()
    return "not found" in m or " 404" in m or m.strip() == "404"



# ─────────────────────────────────────────────────────────────────────────
# Diskwala: exact flow from the supplied standalone main.py
# ─────────────────────────────────────────────────────────────────────────




def _resolve_no_auth(link: str) -> dict:
    """Resolve a Diskwala link using Diskwala-only resolver tiers."""
    if not is_diskwala_link(link):
        raise ValueError("Only Diskwala links are supported.")

    token_api_error = None
    try:
        return resolve_diskwala_with_auth_retry(link)
    except RuntimeError as e:
        token_api_error = e
        logger.info("Diskwala token-API tier unavailable: %s", e)
    except ImportError as e:
        logger.info("Diskwala token-API tier unavailable: %s", e)
    except Exception as e:
        token_api_error = e
        logger.info("Diskwala token-API tier failed: %s", e)

    try:
        return _resolve_diskwala_web_api(link)
    except Exception as e:
        logger.info("Diskwala first-party web API unavailable: %s", e)

    try:
        return _resolve_diskwala_browser_engine(link)
    except Exception as e:
        logger.info("Diskwala browser-engine resolver unavailable: %s", e)

    try:
        return resolve_diskwala_html(link)
    except Exception as e:
        if token_api_error and _looks_like_not_found(str(token_api_error)) and _looks_like_not_found(str(e)):
            raise Exception(
                "This Diskwala file could not be found. The link may be expired, "
                "deleted, or invalid. Double-check the share link and try again."
            ) from e
        raise


def get_page_meta(url: str) -> dict:
    """Matches terabox_downloader.get_page_meta()'s contract: {poster_url,
    duration, title, file_size, extension, category}.

    FIX: Previously used fetch_diskwala_preview() (HTML scrape only) which
    returned thumb=None for Diskwala and often missed title/author for Diskwala.
    Now calls _resolve_no_auth_cached() — the same full resolve that
    get_available_qualities() uses — so the real API filename, thumbnail, and
    creator are all available. The cached result means no extra API call is
    made when get_available_qualities() runs immediately after.
    """
    VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
                  ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts"}

    try:
        # Primary: use the full resolve result (has real name + thumbnail)
        info = _resolve_no_auth_cached(url)

        raw_name = info.get("name") or info.get("title") or ""
        # Strip file extension for the display title (🎬 Title line)
        if raw_name and "." in raw_name:
            title_display = os.path.splitext(raw_name)[0]
            ext = os.path.splitext(raw_name)[1].lower()
        else:
            title_display = raw_name or "Video"
            slug = url.rstrip("/").split("/")[-1].split("?")[0]
            ext = os.path.splitext(slug)[1].lower() or ".mp4"

        # Thumbnail: resolve result > fetch_diskwala_preview fallback
        thumb = info.get("thumb")
        if not thumb:
            try:
                preview = fetch_diskwala_preview(url)
                thumb = preview.get("thumb")
            except Exception:
                pass

        return {
            "title":      title_display or "Video",
            "author":     info.get("creator") or info.get("author") or "Unknown",
            "duration":   info.get("duration_seconds") or 0,
            "poster_url": thumb,
            "file_size":  info.get("size") or 0,
            "extension":  ext,
            "category":   "Video" if ext in VIDEO_EXTS else info.get("category") or "File",
        }

    except Exception as e:
        # Fallback to HTML scrape if resolve fails completely
        logger.warning(f"[diskwala] get_page_meta resolve failed ({e}), using HTML scrape")
        info = fetch_diskwala_preview(url)
        title = info.get("title") or "Video"
        ext = os.path.splitext(title)[1].lower()
        if not ext:
            slug = url.rstrip("/").split("/")[-1].split("?")[0]
            ext = os.path.splitext(slug)[1].lower() or ".mp4"
        return {
            "title":      title,
            "author":     info.get("author") or "Unknown",
            "duration":   info.get("duration_seconds") or 0,
            "poster_url": info.get("thumb"),
            "file_size":  info.get("size") or 0,
            "extension":  ext,
            "category":   "Video" if ext in VIDEO_EXTS else info.get("category") or "File",
        }


def get_available_qualities(url: str) -> list:
    """Matches terabox_downloader.get_available_qualities()'s contract —
    same "just one real link, no actual quality ladder" shape as Terabox
    (see that function's own docstring): Diskwala hand
    back one downloadUrl each, never a resolution choice. A length-1 list
    is what makes main.py's show_quality_menu() skip straight to
    download_video() instead of showing a pointless one-button menu."""
    # Playlist links (e.g. diskwala.com/playlist/...) match the same
    # domain check as a single file (is_diskwala_link doesn't distinguish
    # them), but resolve_diskwala_html/resolve_diskwala_html — and this
    # adapter's single-downloadUrl return shape below — are built for one
    # file at a time. fetch_playlist_info() (ported further up, in the
    # ultra-main section) CAN now resolve a playlist's contents using the
    # same Telethon auth token this adapter just gained, but wiring that
    # into a real multi-file download flow is a separate change to
    # main.py's dispatch/UI (it expects one file per link here), not
    # something this function's contract can express — so this stays a
    # clear, honest "not supported yet" rather than a silent wrong-file
    # download or a half-working single-file-of-a-playlist result.
    if is_playlist_link(url):
        raise RuntimeError(
            "This looks like a Diskwala playlist link — playlist support isn't "
            "wired up in this bot yet. Send a single file's share link instead."
        )
    # FIX: use cached resolve — if get_page_meta() was called first (it always
    # is, for the quality menu), the result is already cached and this is free.
    result = _resolve_no_auth_cached(url)
    download_link = result.get("downloadUrl")
    if not download_link:
        raise RuntimeError("Diskwala: could not resolve a download link for this URL.")
    return [{"label": "Best", "url": download_link}]
def get_stream_url(url: str):
    """Matches terabox_downloader.get_stream_url()'s contract — used for
    main.py's Stream button."""
    try:
        result = _resolve_no_auth(url)
        return result.get("streamUrl") or result.get("downloadUrl")
    except Exception as e:
        logger.warning(f"diskwala get_stream_url failed: {e}")
        return None


def download_video(url: str, out_path: str, on_progress=None, stream_url: str = None) -> str:
    """Matches terabox_downloader.download_video()'s contract: downloads
    to out_path, reports progress the same {"pct", "downloaded_bytes"}
    shape every other backend's on_progress callback already expects, and
    returns the final path. stream_url (the already-resolved link from
    get_available_qualities(), passed back in once the user picks/
    confirms) is used directly when given, so this doesn't re-resolve
    from scratch on every download — same reasoning as Terabox's
    download_video accepting a pre-resolved stream_url."""
    download_link = stream_url
    if not download_link:
        result = _resolve_no_auth(url)
        download_link = result.get("downloadUrl")
    if not download_link:
        raise RuntimeError("Diskwala: no download link available.")

    headers = {"User-Agent": HTML_USER_AGENT}
    if "diskwala" in url.lower():
        headers["Referer"] = "https://diskwala.in/"
    elif "diskwala" in url.lower():
        headers["Referer"] = "https://diskwala.com/"
    else:
        headers["Referer"] = "https://www.diskwala.com/"

    resp = requests.get(download_link, headers=headers, stream=True, timeout=30)
    resp.raise_for_status()
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    last_report = 0.0
    try:
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if on_progress and (now - last_report) >= 1.0:
                    last_report = now
                    pct = (downloaded / total * 100) if total else 0
                    try:
                        on_progress({"pct": pct, "downloaded_bytes": downloaded})
                    except Exception:
                        pass
    except IOError as e:
        raise RuntimeError(f"Disk write error: {e}") from e
    finally:
        resp.close()

    return out_path
