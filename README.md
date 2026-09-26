# 🚀 Diskwala API

> ⚡ A standalone, production-ready **Diskwala resolver API** built with FastAPI, Cloudflare-aware request handling, authenticated API support, and a real-browser fallback engine.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Node.js](https://img.shields.io/badge/Node.js-20+-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![License](https://img.shields.io/badge/License-Private-lightgrey)](#-license)

## ✨ Overview

**Diskwala API** is a lightweight HTTP service focused exclusively on resolving Diskwala links.

It provides a clean API layer for applications, Telegram bots, automation systems, download managers, and other clients that need Diskwala metadata or resolved media links.

### 🎯 Core capabilities

- 🔗 Resolve Diskwala share/app links
- 📁 Support Diskwala single-file/share links
- 📚 Handle Diskwala playlist-style links
- 🔐 Optional authenticated Diskwala token-API tier
- 🌐 Diskwala first-party web API fallback
- 🧠 HTML / Next.js extraction fallback
- 🖥️ Real Chromium browser fallback through CDP
- 🛡️ Cloudflare-aware request handling
- ⚡ FastAPI-powered REST endpoints
- 🚦 Per-IP rate limiting
- ❤️ Health-check endpoint
- 🧩 Docker deployment support
- 📦 JSON responses designed for bot/API integrations

---

## 🏗️ Architecture

The resolver uses multiple fallback layers so that one unavailable method does not automatically stop the request.

```text
                    ┌─────────────────────┐
                    │   Client / Bot      │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     FastAPI API     │
                    │  /resolve /meta     │
                    │      /extract       │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Diskwala Resolver   │
                    └──────────┬──────────┘
                               │
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
      🔐 Token API       🌐 Web API        🧩 Browser
             │                 │                 │
             └─────────────────┼─────────────────┘
                               ▼
                    🛡️ Cloudflare Handling
                               │
                               ▼
                    📄 HTML / Next.js Fallback
                               │
                               ▼
                    🎬 Resolved Media Data
```

### 🛡️ Cloudflare handling layers

The Cloudflare helper uses a fail-soft strategy:

1. ⚡ **curl_cffi** — browser-like TLS/HTTP fingerprinting
2. 🧩 **cloudscraper** — handles compatible JavaScript challenges
3. 🌐 **FlareSolverr** — real headless-browser challenge handling

If a tier is unavailable, the resolver can continue to the next available fallback.

---

## 📡 API Endpoints

### ❤️ Health Check

```http
GET /health
```

Example:

```bash
curl http://localhost:8000/health
```

Use this endpoint for:

- ✅ Uptime monitoring
- ✅ Docker health checks
- ✅ Render-style service checks
- ✅ Deployment verification

---

### 🔗 Resolve a Diskwala URL

```http
GET /api/resolve?url=<DISKWALA_URL>
```

Example:

```bash
curl "http://localhost:8000/api/resolve?url=https://www.diskwala.com/app/VIDEO_ID"
```

Typical response:

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

> ℹ️ Actual fields can vary depending on the Diskwala source and which resolver fallback succeeds.

---

### 📝 Get Metadata

```http
GET /api/meta?url=<DISKWALA_URL>
```

Example:

```bash
curl "http://localhost:8000/api/meta?url=https://www.diskwala.com/app/VIDEO_ID"
```

Use this when your application needs information about the media without treating the request as a full download operation.

---

### 🔎 Extract Diskwala Links

```http
GET /api/extract?text=<TEXT>
```

Example:

```bash
curl "http://localhost:8000/api/extract?text=Check%20this%20https%3A%2F%2Fwww.diskwala.com%2Fapp%2FVIDEO_ID"
```

This endpoint is useful when a message contains one or more Diskwala URLs and you want to detect/extract them before resolving.

---

## 🧰 Requirements

### Local development

- 🐍 Python **3.11+**
- 📦 pip
- 🟢 Node.js **20+** — required for the browser-engine fallback
- 🌐 Chromium/Chrome — required for browser fallback when it is not automatically available
- 🐧 Linux is recommended for the complete Docker/browser stack

### Python dependencies

Install the pinned project dependencies with:

```bash
pip install -r requirements.txt
```

---

## 🚀 Quick Start

### 1️⃣ Clone the project

```bash
git clone <YOUR_REPOSITORY_URL>
cd Ak-main
```

### 2️⃣ Create a virtual environment

```bash
python3 -m venv .venv
```

Activate it:

```bash
source .venv/bin/activate
```

### 3️⃣ Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4️⃣ Configure environment variables

Create a `.env` file:

```env
PORT=8000

# Optional authenticated Diskwala tier
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH
SESSION=YOUR_TELEGRAM_USER_SESSION

# Optional rate limit
RATE_LIMIT_PER_MINUTE=30

# Optional Cloudflare / FlareSolverr configuration
FLARESOLVERR_URL=http://127.0.0.1:8191/v1
FLARESOLVERR_TIMEOUT_MS=60000
```

> 🔐 **Never commit real API credentials, Telegram sessions, tokens, cookies, or secrets to GitHub.** Use environment variables or your deployment platform's secret manager.

### 5️⃣ Start the API

```bash
python app.py
```

The API listens on:

```text
http://0.0.0.0:8000
```

Open the interactive FastAPI documentation:

```text
http://localhost:8000/docs
```

Alternative documentation:

```text
http://localhost:8000/redoc
```

---

## 🐳 Docker Deployment

The included `Dockerfile` prepares the complete environment, including:

- 🐍 Python 3.11
- 🟢 Node.js 20
- 🌐 Chromium
- 🖥️ Xvfb
- 🛡️ FlareSolverr
- 📦 Python dependencies
- 🔌 Diskwala browser engine dependencies

### Build

```bash
docker build -t diskwala-api .
```

### Run

```bash
docker run -d \
  --name diskwala-api \
  -p 8000:8000 \
  -e PORT=8000 \
  -e API_ID="YOUR_API_ID" \
  -e API_HASH="YOUR_API_HASH" \
  -e SESSION="YOUR_SESSION" \
  -e RATE_LIMIT_PER_MINUTE=30 \
  diskwala-api
```

Check the container:

```bash
docker logs -f diskwala-api
```

Health check:

```bash
curl http://localhost:8000/health
```

---

## ☁️ Render / Cloud Deployment

For platforms that provide a dynamic `$PORT`, keep:

```env
PORT=8000
```

or allow the platform to inject its own `PORT`.

Recommended environment variables:

```text
API_ID
API_HASH
SESSION
RATE_LIMIT_PER_MINUTE
FLARESOLVERR_URL
FLARESOLVERR_TIMEOUT_MS
```

The included `entrypoint.sh`:

1. 🚀 Starts the FastAPI service
2. 🖥️ Starts Xvfb
3. 🛡️ Starts FlareSolverr
4. ❤️ Waits briefly for the FlareSolverr health endpoint
5. 🔄 Keeps the API process running

> ℹ️ The API is intentionally started before FlareSolverr so the web service can bind to the platform-provided port as early as possible.

---

## 🔐 Optional Telegram User-Session Tier

The authenticated Diskwala tier can use a Telegram user session to obtain the required API authentication data.

Configure:

```env
API_ID=YOUR_API_ID
API_HASH=YOUR_API_HASH
SESSION=YOUR_SESSION_STRING
```

If the authenticated tier is unavailable, the resolver is designed to continue through the other Diskwala resolution paths.

### ⚠️ Session security

A Telegram user-session string is effectively a sensitive credential.

**Do not:**

- ❌ Upload it to GitHub
- ❌ Put it directly in `README.md`
- ❌ Share it in public chats
- ❌ Hard-code it into source code
- ❌ Publish it in screenshots or logs

If a real session string has been exposed publicly, revoke/replace it and generate a new one.

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---:|---|
| `PORT` | `8000` | API listening port |
| `API_ID` | project config | Telegram API ID for authenticated tier |
| `API_HASH` | project config | Telegram API hash |
| `SESSION` | project config | Telegram user-session string |
| `RATE_LIMIT_PER_MINUTE` | `30` | Per-IP request limit |
| `FLARESOLVERR_URL` | `http://127.0.0.1:8191/v1` | FlareSolverr API endpoint |
| `FLARESOLVERR_TIMEOUT_MS` | `60000` | FlareSolverr request timeout |
| `CURL_CFFI_IMPERSONATE` | `chrome124` | Preferred curl_cffi browser fingerprint |

Additional Cloudflare startup tuning variables are supported by `cf_bypass.py`, including:

```env
FLARESOLVERR_STARTUP_TIMEOUT_SECONDS=45
FLARESOLVERR_STARTUP_POLL_INTERVAL=3
FLARESOLVERR_STARTUP_COOLDOWN_SECONDS=60
```

---

## 📊 Rate Limiting

The API includes per-IP request limiting.

Default:

```env
RATE_LIMIT_PER_MINUTE=30
```

Change it according to your deployment:

```env
RATE_LIMIT_PER_MINUTE=60
```

For public production deployments, use a sensible limit appropriate to your server resources and expected traffic.

---

## 📁 Project Structure

```text
Ak-main/
│
├── 🚀 app.py
│   └── FastAPI application and HTTP endpoints
│
├── ⚙️ config.py
│   └── Environment/configuration handling
│
├── 🎬 diskwala.py
│   └── Main Diskwala resolution logic and fallbacks
│
├── 🛡️ cf_bypass.py
│   └── Cloudflare-aware request/challenge handling
│
├── 🧩 diskwala_engine/
│   ├── diskwala_engine.js
│   │   └── Chromium/CDP browser resolver
│   └── package.json
│       └── Node.js dependencies
│
├── 🐳 Dockerfile
│   └── Full container build
│
├── 🚀 entrypoint.sh
│   └── Starts API + FlareSolverr/Xvfb
│
├── 📦 requirements.txt
│   └── Python dependencies
│
└── 📖 README.md
    └── Project documentation
```

---

## 🧪 Testing

### Health

```bash
curl http://localhost:8000/health
```

### Resolve

```bash
curl "http://localhost:8000/api/resolve?url=https://www.diskwala.com/app/VIDEO_ID"
```

### Metadata

```bash
curl "http://localhost:8000/api/meta?url=https://www.diskwala.com/app/VIDEO_ID"
```

### Extract

```bash
curl "http://localhost:8000/api/extract?text=https://www.diskwala.com/app/VIDEO_ID"
```

### API documentation

After starting the server:

```text
http://localhost:8000/docs
```

---

## 🧠 Resolver Strategy

The project intentionally keeps multiple resolution paths:

```text
🔗 Diskwala URL
      │
      ▼
🔐 Authenticated token API
      │
      ├── success ───────────────► ✅ Result
      │
      ▼
🌐 First-party Diskwala web/API path
      │
      ├── success ───────────────► ✅ Result
      │
      ▼
🛡️ Cloudflare-aware request layer
      │
      ├── curl_cffi
      ├── cloudscraper
      └── FlareSolverr
      │
      ▼
🖥️ Chromium / CDP browser engine
      │
      ▼
📄 HTML / Next.js fallback
      │
      ▼
🎬 Final media metadata / URL
```

This architecture allows the service to continue working when an individual resolution method is unavailable.

---

## 🖥️ Browser Engine

The optional browser engine is located at:

```text
diskwala_engine/diskwala_engine.js
```

It uses:

- 🟢 Node.js
- 🌐 Chromium
- 🔌 Chrome DevTools Protocol (CDP)
- 🔗 WebSocket communication
- 🧹 Temporary browser profiles
- ⚡ Direct media/network inspection

Install its dependencies:

```bash
cd diskwala_engine
npm install --omit=dev
```

The Docker image performs this installation automatically.

---

## 🛡️ Cloudflare Module

`cf_bypass.py` provides a reusable Cloudflare-aware request layer.

### Supported approaches

| Layer | Technology | Purpose |
|---|---|---|
| 1️⃣ | `curl_cffi` | Browser-like TLS/HTTP fingerprint |
| 2️⃣ | `cloudscraper` | Compatible JS challenge handling |
| 3️⃣ | FlareSolverr | Real browser-based challenge handling |

Cached cookies are reused for a limited period to reduce unnecessary challenge-solving requests.

> ⚠️ Cloudflare behavior can change at any time. No automated challenge-handling system can guarantee access to every protected page.

---

## 🔧 Troubleshooting

### ❌ Port already in use

Check:

```bash
lsof -i :8000
```

Run on another port:

```bash
PORT=8080 python app.py
```

---

### ❌ `ModuleNotFoundError`

Install dependencies:

```bash
pip install -r requirements.txt
```

Make sure the correct Python environment is active.

---

### ❌ Browser/CDP resolver fails

Check:

```bash
node --version
chromium --version
```

The Docker build installs Node.js and Chromium automatically.

---

### ❌ FlareSolverr unavailable

Check:

```bash
curl http://127.0.0.1:8191/health
```

For Docker:

```bash
docker logs <container_name>
```

The API can still use its other resolver paths when FlareSolverr is unavailable.

---

### ❌ Authenticated tier is not working

Verify:

```env
API_ID=...
API_HASH=...
SESSION=...
```

Then restart the service.

Also make sure the Telegram session is valid and has not been revoked.

---

## 📌 Production Checklist

Before deploying publicly:

- [ ] 🔐 Move all secrets to environment variables
- [ ] 🔄 Replace any exposed Telegram session
- [ ] 🚦 Configure a suitable rate limit
- [ ] ❤️ Verify `/health`
- [ ] 🐳 Test the Docker image
- [ ] 🌐 Test `/api/resolve`
- [ ] 📝 Test `/api/meta`
- [ ] 🔎 Test `/api/extract`
- [ ] 🛡️ Verify Cloudflare fallback behavior
- [ ] 📊 Monitor application logs
- [ ] 💾 Avoid logging sensitive credentials
- [ ] 🔒 Put the API behind HTTPS in production
- [ ] 🧰 Configure platform secrets instead of committing `.env`

---

## 🔒 Security

Please treat the following as secrets:

```text
API_HASH
SESSION
Bot tokens
Authentication tokens
Cookies
Private API credentials
```

Use:

```text
.env
```

locally and your hosting provider's secret/environment-variable manager in production.

Add `.env` to `.gitignore`:

```gitignore
.env
.venv/
__pycache__/
*.pyc
node_modules/
browser/
```

---

## 📦 Dependencies

### Python

```text
FastAPI
Uvicorn
Requests
BeautifulSoup4
Cryptography
Python-dotenv
curl_cffi
cloudscraper
Telethon
```

### Node.js

```text
ws
```

### System / Docker

```text
Chromium
Node.js 20+
Xvfb
FlareSolverr
```

---

## 🤝 Integration Example

A simple Python client:

```python
import requests

url = "https://your-domain.example/api/resolve"
params = {
    "url": "https://www.diskwala.com/app/VIDEO_ID"
}

response = requests.get(url, params=params, timeout=60)
response.raise_for_status()

data = response.json()
print(data)
```

JavaScript example:

```javascript
const params = new URLSearchParams({
  url: "https://www.diskwala.com/app/VIDEO_ID"
});

const response = await fetch(
  `https://your-domain.example/api/resolve?${params}`
);

const data = await response.json();
console.log(data);
```

---

## 📜 License

This project is provided for authorized development and integration purposes.

Before deploying or distributing it, make sure your use of Diskwala content, links, APIs, automation, and any third-party services complies with their applicable terms and laws.

---

## 👨‍💻 Developer

**Diskwala API**

Built with:

```text
🐍 Python
⚡ FastAPI
🛡️ Cloudflare-aware networking
🟢 Node.js
🌐 Chromium
🐳 Docker
```

---

## ⭐ Support

If you are deploying this project and encounter an issue, collect:

```text
1. Python version
2. Node.js version
3. Deployment platform
4. Endpoint being tested
5. Relevant non-sensitive logs
6. HTTP status/error message
```

Never share:

```text
❌ SESSION
❌ API_HASH
❌ Bot tokens
❌ Private credentials
❌ Authentication cookies
```

---

### 🚀 Built for speed. Designed for fallback. Ready for deployment.
