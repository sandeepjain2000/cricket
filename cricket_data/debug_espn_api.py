#!/usr/bin/env python3
"""
debug_espn_api.py — ESPN Cricinfo API response inspector
Run this locally, paste the output back so the scraper can be fixed.

Usage:
    python debug_espn_api.py
    python debug_espn_api.py "Rohit Sharma"
"""

import sys
import json
import urllib.request
import urllib.parse

PLAYER = sys.argv[1] if len(sys.argv) > 1 else "Andrew Flintoff"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":    "https://www.espncricinfo.com/",
    "Origin":     "https://www.espncricinfo.com",
}

def fetch(url, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  URL: {url}")
    print(f"{'='*70}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        print(f"  HTTP status : {resp.status}")
        print(f"  Content-Type: {resp.headers.get('Content-Type','')}")
        print(f"  Body length : {len(raw)} chars")
        print()
        try:
            data = json.loads(raw)
            print("  Parsed JSON — top-level keys:", list(data.keys()))
            print()
            print(json.dumps(data, indent=2)[:3000])
            if len(raw) > 3000:
                print(f"\n  ... (truncated, full length {len(raw)} chars)")
        except json.JSONDecodeError:
            print("  Not JSON. Raw body (first 2000 chars):")
            print(raw[:2000])
    except Exception as e:
        print(f"  ERROR: {e}")


q = urllib.parse.quote(PLAYER)
q_plus = urllib.parse.quote_plus(PLAYER)

# Try every plausible ESPN Cricinfo endpoint
fetch(
    f"https://hs-consumer-api.espncricinfo.com/v1/search?search={q}&type=player",
    "1. hs-consumer-api /v1/search  (type=player)"
)
fetch(
    f"https://hs-consumer-api.espncricinfo.com/v1/search?search={q}&type=player&size=10",
    "2. hs-consumer-api /v1/search  (type=player, size=10)"
)
fetch(
    f"https://hs-consumer-api.espncricinfo.com/v1/search?search={q}",
    "3. hs-consumer-api /v1/search  (no type filter)"
)
fetch(
    f"https://hs-consumer-api.espncricinfo.com/v1/pages/player/list?q={q}",
    "4. hs-consumer-api /v1/pages/player/list"
)
fetch(
    f"https://www.espncricinfo.com/player/search/?search={q_plus}",
    "5. espncricinfo.com /player/search/ (HTML page)"
)

print("\n" + "="*70)
print("  DONE — paste this entire output so the scraper can be fixed.")
print("="*70)
