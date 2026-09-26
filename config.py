"""Configuration for the standalone Diskwala API."""
import os
from dotenv import load_dotenv

load_dotenv()

# Optional Telegram USER session for Diskwala's authenticated token-API tier.
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION = os.getenv("SESSION", "")


# Per-IP request limit.
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

PORT = int(os.getenv("PORT", "8000"))
