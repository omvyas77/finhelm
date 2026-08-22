"""Day 0.2 — verify EDGAR access before writing the ingester.

A missing or generic User-Agent returns 403. Over 10 req/sec blocks your IP ~10 min.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

HEADERS = {"User-Agent": os.environ["SEC_USER_AGENT"]}

r = requests.get(
    "https://data.sec.gov/submissions/CIK0000019617.json", headers=HEADERS, timeout=30
)
print(r.status_code, r.json()["name"])  # expect: 200 JPMORGAN CHASE & CO
