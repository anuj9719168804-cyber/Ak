"""Configuration for the standalone Diskwala API."""
import os
from dotenv import load_dotenv

load_dotenv()

# Optional Telegram USER session for Diskwala's authenticated token-API tier.
API_ID = int(os.getenv("API_ID", "20432885"))
API_HASH = os.getenv("API_HASH", "4fdcfab1c7f5e24ae69f3ce6bb234dec")
SESSION = os.getenv("SESSION", "1AZWarzcBu61nyfLZEDg3AIqwNSoq8a22imtuARpbl8g9jknAv-kml_zvbKhELisWFLtE
AktSu9kLfw8wQz_d1dgM1_EkjH6nva5a73d7QL8fSWQrCjoL7o5SB7L0UeoDrDPvSCG98
0uj3DYY5oM7CJCe7I46FGxg2Y3eW6J_rdgahUa8dGjY4p1IZyLzNL8cLapFeSPdTSs3jl
-6C-cBarhuEr3m1AMsshCfqF9dlOKD4b8X2Vv9jyFUsuPp3g5Wb2BvIsM4kqsfOc9rfj9
FgwLrdIqawfFA3Q_O48x4sWKvkSPFkVa9Dyc1Xggn-gNRcxr7WEHyiYhRXP2SWn0zE8Eg
8IDV8sQ=")


# Per-IP request limit.
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

PORT = int(os.getenv("PORT", "8000"))
