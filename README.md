# Diskwala API

A standalone **Diskwala-only** resolver service.

## What remains

- Diskwala single-file/share links
- Diskwala playlist links
- Diskwala authenticated token API
- Diskwala first-party web API
- Diskwala browser-engine fallback
- Diskwala HTML / Next.js fallback
- Cloudflare handling for Diskwala
- `/api/resolve`, `/api/meta`, `/api/extract`
- Rate limiting

## What was removed

All non-Diskwala provider integrations and their dedicated resolver branches were removed. The project no longer exposes or routes requests for other providers.

## Run

```bash
pip install -r requirements.txt
python app.py
```

Default port: `8000`.

## Endpoints

### Health

```text
GET /health
```

### Resolve

```text
GET /api/resolve?url=https://www.diskwala.com/app/...
```


Example response:

```json
{
  "status": true,
  "creator": "Diskwala API",
  "data": {
    "name": "video.mp4",
    "size": "100.00 MB",
    "size_bytes": 104857600,
    "speed_link": "https://...",
    "stream_link": "https://...",
    "m3u8_url": null,
    "thumbnail": "https://..."
  }
}
```

### Metadata

```text
GET /api/meta?url=https://www.diskwala.com/app/...
```

### Extract Diskwala links from text

```text
GET /api/extract?text=...
```

## Optional authenticated Diskwala tier

Set `API_ID`, `API_HASH`, and `SESSION` in `.env` to enable the Telegram USER-session token tier. If it is unavailable, the API continues through the Diskwala no-auth fallbacks.

## Project structure

```text
diskwala_api/
├── app.py
├── config.py
├── diskwala.py
├── cf_bypass.py
├── diskwala_engine/
│   ├── diskwala_engine.js
│   └── package.json
├── requirements.txt
├── Dockerfile
└── .env.example
```
