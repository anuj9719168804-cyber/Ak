"""Configuration for the standalone Diskwala API."""
import os
from dotenv import load_dotenv

load_dotenv()

# Optional Telegram USER session for Diskwala's authenticated token-API tier.
API_ID = int(os.getenv("API_ID", "20432885"))
API_HASH = os.getenv("API_HASH", "4fdcfab1c7f5e24ae69f3ce6bb234dec")
SESSION = os.getenv("SESSION", "1BVtsOIUBu1p95DBVItZu_9cKP7_1aJJl9f-sDqeHr4tpVwV3H1XsaQL8U9vKbH_fhf6ov-NBS9MfMykioaeD2vF8ExH7pOkqQ6NQ9klIWi1p4BzCj8Og5VTKUMW6s2tPhjscwH_wSw3zdg6HqEagS7xPihenm71vj-lWo85xGY_wx6vYg8fjoNF_iPO3cWrHdZOMGMZXW6MPPvk4bRF8HodXC8guvQOhFyLmFUvI2irIvpgZCDBRK-oMLMtXKeQxNdSKAii_0ksWA1turWSR6DJLaLT-mslLtQMZFZWolMhkY4zw1AbSnXXGsTUwMW_2w8EEi2MMVMMaF4dIvbWiFnO9npEtj0k=")


# Per-IP request limit.
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

PORT = int(os.getenv("PORT", "8000"))
