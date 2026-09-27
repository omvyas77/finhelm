"""Check EDGAR access before running the ingester.

EDGAR returns 403 for a missing or generic User-Agent, and blocks an IP for about ten
minutes above 10 requests a second. Both surface as ordinary HTTP errors much later in a
long ingest, so this checks them first.

    python scripts/verify_edgar.py
"""

from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

PROBE = "https://data.sec.gov/submissions/CIK0000019617.json"  # JPMorgan Chase


def main() -> int:
    load_dotenv()
    agent = os.getenv("SEC_USER_AGENT")
    if not agent:
        print("SEC_USER_AGENT is not set; see .env.example", file=sys.stderr)
        return 2

    response = requests.get(PROBE, headers={"User-Agent": agent}, timeout=30)
    if response.status_code != 200:
        print(f"{response.status_code} from EDGAR; check SEC_USER_AGENT identifies you",
              file=sys.stderr)
        return 1

    print(response.status_code, response.json()["name"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
