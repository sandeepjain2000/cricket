#!/usr/bin/env python3
"""
================================================================================
  scrape_player_profiles.py  v3.0.0
  Cricket Player Profile Builder — OpenAI edition
================================================================================

HOW IT WORKS
  Instead of scraping ESPN Cricinfo (which blocks bots), we ask OpenAI GPT-4o
  about each player.  OpenAI already knows batting style, bowling style, and
  playing role for every international cricketer from its training data.

  Players are sent in batches of 30.  Each batch is one API call.
  GPT-4o returns structured JSON which we normalise and store.

SETUP (one-time)
  pip install openai --break-system-packages

USAGE
  python scrape_player_profiles.py               # all players
  python scrape_player_profiles.py --player "Andrew Flintoff"
  python scrape_player_profiles.py --limit 50    # test run
  python scrape_player_profiles.py --no-upload   # local only
  python scrape_player_profiles.py --upload-only # push cache → Supabase

CONFIG  (auto-read from cricket_ui/.env.local)
  OPENAI_API_KEY   — required
  SUPABASE_URL     — for upload
  SUPABASE_KEY     — for upload
================================================================================
"""

import os
import sys
import re
import json
import time
import sqlite3
import argparse
import traceback
from datetime import datetime
from pathlib import Path
from configparser import ConfigParser

_claudes = next((p for p in Path(__file__).resolve().parents if p.name == "Claudes"), None)
if _claudes is None:
    raise ImportError("Claudes root not found (expected ...\\Claudes\\nvidia_keys)")
sys.path.insert(0, str(_claudes))
from nvidia_llm import NVIDIA_MODEL, create_nvidia_client, discover_nvidia_key_files

try:
    from openai import OpenAI
    OPENAI_OK = True
except ImportError:
    OPENAI_OK = False

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_OK = True
except ImportError:
    PSYCOPG2_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_NAME = "scrape_player_profiles.py"
VERSION     = "3.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR  = Path(__file__).parent.resolve()
OUTPUT_DIR  = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"profiles_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"profiles_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"
LOCAL_DB    = SCRIPT_DIR / "db" / "cricket.db"
CONFIG_PATH = SCRIPT_DIR / "config" / "settings.ini"

BATCH_SIZE  = 30   # players per LLM call
MODEL       = NVIDIA_MODEL

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

os.system('cls' if os.name == 'nt' else 'clear')
log_file = open(LOG_PATH, 'w', encoding='utf-8')

def log(msg: str = ""):
    print(msg)
    log_file.write(msg + '\n')
    log_file.flush()

log("=" * 72)
log(f"  {SCRIPT_NAME}  v{VERSION}")
log(f"  Cricket Player Profiles — OpenAI GPT-4o edition")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 72)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  (reads cricket_ui/.env.local automatically)
# ─────────────────────────────────────────────────────────────────────────────

config = ConfigParser()
config.read(CONFIG_PATH)

def _load_dotenv(env_path: Path) -> dict:
    vals = {}
    if not env_path.exists():
        return vals
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip()
    return vals

_dotenv = _load_dotenv(SCRIPT_DIR.parent / "cricket_ui" / ".env.local")

def _cfg(key: str, fallback=None):
    return os.environ.get(key) or _dotenv.get(key) or fallback

SUPABASE_URL   = _cfg("SUPABASE_URL") or config.get("supabase", "url", fallback=None)
SUPABASE_URL_T20 = _cfg("SUPABASE_URL_T20")

# ─────────────────────────────────────────────────────────────────────────────
#  BOWLING TYPE NORMALISATION
# ─────────────────────────────────────────────────────────────────────────────

BOWLING_TYPE_MAP = [
    (re.compile(r'left.arm.fast.medium|left.arm.medium.fast', re.I), 'Left arm Fast-Medium'),
    (re.compile(r'right.arm.fast.medium|right.arm.medium.fast', re.I), 'Right arm Fast-Medium'),
    (re.compile(r'left.arm.fast',    re.I), 'Left arm Fast'),
    (re.compile(r'right.arm.fast',   re.I), 'Right arm Fast'),
    (re.compile(r'left.arm.medium',  re.I), 'Left arm Medium'),
    (re.compile(r'right.arm.medium', re.I), 'Right arm Medium'),
    (re.compile(r'left.arm.unorthodox|left.arm.wrist', re.I), 'Left arm Unorthodox'),
    (re.compile(r'left.arm.orthodox|slow.left.arm',    re.I), 'Left arm Orthodox'),
    (re.compile(r'leg.break|right.arm.leg',  re.I), 'Right arm Leg-break'),
    (re.compile(r'off.break|off.spin|right.arm.off|right.arm.spin', re.I), 'Right arm Off-break'),
]

BOWLING_SUBTYPE_MAP = [
    (re.compile(r'googly',      re.I), 'Googly'),
    (re.compile(r'doosra',      re.I), 'Doosra'),
    (re.compile(r'carrom',      re.I), 'Carrom Ball'),
    (re.compile(r'knuckleball', re.I), 'Knuckleball'),
]

def normalise_bowling(bowling_style: str | None) -> tuple[str, str | None]:
    if not bowling_style or bowling_style.strip().lower() in ('', '-', 'n/a', 'na'):
        return 'NA', None
    b_type = 'NA'
    for pattern, category in BOWLING_TYPE_MAP:
        if pattern.search(bowling_style):
            b_type = category
            break
    b_sub = next((lbl for p, lbl in BOWLING_SUBTYPE_MAP if p.search(bowling_style)), None)
    return b_type, b_sub

def derive_batting_hand(s):
    if not s: return None
    return 'Left' if re.search(r'left', s, re.I) else ('Right' if re.search(r'right', s, re.I) else None)

def derive_bowling_arm(s):
    if not s: return None
    return 'Left' if re.search(r'left', s, re.I) else ('Right' if re.search(r'right', s, re.I) else None)

def derive_role_flags(role: str | None) -> dict:
    r = (role or '').lower()
    return {
        'is_bowler':       int('bowl' in r),
        'is_batter':       int(any(k in r for k in ('bat', 'open', 'top', 'mid'))),
        'is_wicketkeeper': int(any(k in r for k in ('keep', 'wicket'))),
        'is_allrounder':   int('allrounder' in r or 'all-rounder' in r),
    }

# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL SQLITE
# ─────────────────────────────────────────────────────────────────────────────

LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_profiles (
    player_name         TEXT    NOT NULL PRIMARY KEY,
    espncricinfo_id     INTEGER,
    full_name           TEXT,
    date_of_birth       TEXT,
    birth_place         TEXT,
    batting_style       TEXT,
    bowling_style       TEXT,
    bowling_type        TEXT    NOT NULL DEFAULT 'NA',
    bowling_subtype     TEXT,
    batting_hand        TEXT,
    bowling_arm         TEXT,
    playing_role        TEXT,
    is_bowler           INTEGER NOT NULL DEFAULT 0,
    is_batter           INTEGER NOT NULL DEFAULT 1,
    is_wicketkeeper     INTEGER NOT NULL DEFAULT 0,
    is_allrounder       INTEGER NOT NULL DEFAULT 0,
    nationality         TEXT,
    teams_json          TEXT,
    espncricinfo_url    TEXT,
    scraped_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pp_bowling_type ON player_profiles (bowling_type);
CREATE INDEX IF NOT EXISTS idx_pp_batting_hand ON player_profiles (batting_hand);
CREATE INDEX IF NOT EXISTS idx_pp_playing_role ON player_profiles (playing_role);
"""

def open_db() -> sqlite3.Connection:
    LOCAL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOCAL_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LOCAL_SCHEMA)
    conn.commit()
    return conn

def get_player_list(conn: sqlite3.Connection) -> list[str]:
    # Try match_players first, fall back to player_name_map
    for query in [
        "SELECT DISTINCT player_name FROM match_players ORDER BY player_name",
        "SELECT DISTINCT short_name  FROM player_name_map  ORDER BY short_name",
    ]:
        try:
            cur = conn.execute(query)
            rows = [r[0] for r in cur.fetchall() if r[0]]
            if rows:
                return rows
        except sqlite3.OperationalError:
            continue
    return []

def get_already_done(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT player_name FROM player_profiles")
    return {r[0] for r in cur.fetchall()}

def load_name_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Returns {short_name → full_name} for display / passing to OpenAI."""
    try:
        cur = conn.execute(
            "SELECT short_name, full_name FROM player_name_map "
            "WHERE full_name IS NOT NULL"
        )
        return {r[0]: r[1] for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}

def upsert_profile(conn: sqlite3.Connection, row: dict):
    conn.execute("""
        INSERT INTO player_profiles (
            player_name, full_name,
            batting_style, bowling_style, bowling_type, bowling_subtype,
            batting_hand, bowling_arm, playing_role,
            is_bowler, is_batter, is_wicketkeeper, is_allrounder,
            nationality, espncricinfo_url, scraped_at, updated_at
        ) VALUES (
            :player_name, :full_name,
            :batting_style, :bowling_style, :bowling_type, :bowling_subtype,
            :batting_hand, :bowling_arm, :playing_role,
            :is_bowler, :is_batter, :is_wicketkeeper, :is_allrounder,
            :nationality, :espncricinfo_url, datetime('now'), datetime('now')
        )
        ON CONFLICT(player_name) DO UPDATE SET
            full_name        = excluded.full_name,
            batting_style    = excluded.batting_style,
            bowling_style    = excluded.bowling_style,
            bowling_type     = excluded.bowling_type,
            bowling_subtype  = excluded.bowling_subtype,
            batting_hand     = excluded.batting_hand,
            bowling_arm      = excluded.bowling_arm,
            playing_role     = excluded.playing_role,
            is_bowler        = excluded.is_bowler,
            is_batter        = excluded.is_batter,
            is_wicketkeeper  = excluded.is_wicketkeeper,
            is_allrounder    = excluded.is_allrounder,
            nationality      = excluded.nationality,
            updated_at       = datetime('now')
    """, row)

# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI BATCH LOOKUP
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a cricket expert with deep knowledge of all international cricketers.
For each player name provided, return their:
  - batting_style  (e.g. "Right hand Bat", "Left hand Bat")
  - bowling_style  (e.g. "Right arm Fast medium", "Left arm Orthodox spin", "Right arm Leg break")
                   Use null if the player does not bowl
  - playing_role   (e.g. "Batter", "Bowler", "Allrounder", "Wicketkeeper Batter", "Wicketkeeper Allrounder")
  - nationality    (country they played for internationally)
  - confidence     ("High" if you are certain, "Low" if uncertain or player is obscure)

Return ONLY a JSON array, one object per player, in the same order as the input list.
Use null for any field you don't know. Do not add any explanation outside the JSON."""

USER_PROMPT_TEMPLATE = """Provide batting style, bowling style, playing role, and nationality for these cricket players:

{player_list}

Return a JSON array with {count} objects in this exact format:
[
  {{
    "player_name": "<exactly as given>",
    "batting_style": "<style or null>",
    "bowling_style": "<style or null>",
    "playing_role": "<role or null>",
    "nationality": "<country or null>",
    "confidence": "High or Low"
  }},
  ...
]"""


def ask_openai_batch(client: OpenAI, players: list[str]) -> list[dict]:
    """
    Send a batch of player names to GPT-4o and get back their profiles.
    Returns a list of dicts, one per player.
    """
    numbered = "\n".join(f"{i+1}. {name}" for i, name in enumerate(players))
    prompt = USER_PROMPT_TEMPLATE.format(
        player_list=numbered,
        count=len(players)
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.1,
            )

            raw = response.choices[0].message.content or ""

            # Extract JSON from the response — handle markdown code fences,
            # plain JSON arrays, or wrapped objects like {"players": [...]}
            # Strip ```json ... ``` fences if present
            raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.I)
            raw = re.sub(r'\s*```$', '', raw.strip())

            # Try to find a JSON array directly
            m = re.search(r'\[.*\]', raw, re.S)
            if m:
                data = json.loads(m.group(0))
                if isinstance(data, list):
                    return data

            # Try parsing as object and unwrapping
            data = json.loads(raw)
            if isinstance(data, list):
                return data
            for key in ("players", "results", "data", "cricketers"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            for v in data.values():
                if isinstance(v, list):
                    return v

            log(f"    ⚠ Could not extract list from response")
            return []

        except json.JSONDecodeError as e:
            log(f"    ⚠ JSON parse error (attempt {attempt+1}): {e}")
            log(f"       Raw response: {raw[:300]}")
            if attempt < 2:
                time.sleep(2)
        except Exception as e:
            log(f"    ⚠ OpenAI error (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(5)

    return []


def build_row(player_name: str, full_name: str, data: dict) -> dict:
    """Turn an OpenAI response dict into a DB row dict."""
    batting_style = (data.get("batting_style") or "").strip() or None
    bowling_style = (data.get("bowling_style") or "").strip() or None
    playing_role  = (data.get("playing_role")  or "").strip() or None
    nationality   = (data.get("nationality")   or "").strip() or None

    bowling_type, bowling_subtype = normalise_bowling(bowling_style)
    role_flags = derive_role_flags(playing_role)

    return {
        "player_name":     player_name,
        "full_name":       full_name or player_name,
        "batting_style":   batting_style,
        "bowling_style":   bowling_style,
        "bowling_type":    bowling_type,
        "bowling_subtype": bowling_subtype,
        "batting_hand":    derive_batting_hand(batting_style),
        "bowling_arm":     derive_bowling_arm(bowling_style),
        "playing_role":    playing_role,
        "is_bowler":       role_flags["is_bowler"],
        "is_batter":       role_flags["is_batter"],
        "is_wicketkeeper": role_flags["is_wicketkeeper"],
        "is_allrounder":   role_flags["is_allrounder"],
        "nationality":     nationality,
        "espncricinfo_url": None,
    }

# ─────────────────────────────────────────────────────────────────────────────
#  SUPABASE UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

def upload_to_supabase(conn: sqlite3.Connection) -> int:
    if not PSYCOPG2_OK:
        log("  ✗ psycopg2 not installed. Run:")
        log("    pip install psycopg2-binary --break-system-packages")
        return 0

    # Use T20 DB if available, otherwise fall back to Test DB
    db_url = SUPABASE_URL_T20 or SUPABASE_URL
    if not db_url:
        log("  ⚠ No SUPABASE_URL found in cricket_ui/.env.local — skipping upload.")
        return 0

    log()
    log("─" * 72)
    log("  SUPABASE UPLOAD — player_profiles")
    log(f"  DB: {db_url[:50]}…")
    log("─" * 72)

    cur = conn.execute("""
        SELECT player_name, full_name,
               batting_style, bowling_style, bowling_type, bowling_subtype,
               batting_hand, bowling_arm, playing_role,
               is_bowler, is_batter, is_wicketkeeper, is_allrounder,
               nationality
        FROM player_profiles
    """)
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        log("  No rows to upload.")
        return 0

    log(f"  Rows to upload : {len(rows):,}")

    try:
        pg = psycopg2.connect(db_url)
        pg.autocommit = False
        pgcur = pg.cursor()

        # Create table if it doesn't exist yet
        pgcur.execute("""
            CREATE TABLE IF NOT EXISTS player_profiles (
                player_name     TEXT    PRIMARY KEY,
                full_name       TEXT,
                batting_style   TEXT,
                bowling_style   TEXT,
                bowling_type    TEXT    DEFAULT 'NA',
                bowling_subtype TEXT,
                batting_hand    TEXT,
                bowling_arm     TEXT,
                playing_role    TEXT,
                is_bowler       BOOLEAN DEFAULT FALSE,
                is_batter       BOOLEAN DEFAULT TRUE,
                is_wicketkeeper BOOLEAN DEFAULT FALSE,
                is_allrounder   BOOLEAN DEFAULT FALSE,
                nationality     TEXT,
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        UPSERT_SQL = """
            INSERT INTO player_profiles (
                player_name, full_name,
                batting_style, bowling_style, bowling_type, bowling_subtype,
                batting_hand, bowling_arm, playing_role,
                is_bowler, is_batter, is_wicketkeeper, is_allrounder,
                nationality, updated_at
            ) VALUES (
                %(player_name)s, %(full_name)s,
                %(batting_style)s, %(bowling_style)s, %(bowling_type)s, %(bowling_subtype)s,
                %(batting_hand)s, %(bowling_arm)s, %(playing_role)s,
                %(is_bowler)s, %(is_batter)s, %(is_wicketkeeper)s, %(is_allrounder)s,
                %(nationality)s, NOW()
            )
            ON CONFLICT (player_name) DO UPDATE SET
                full_name       = EXCLUDED.full_name,
                batting_style   = EXCLUDED.batting_style,
                bowling_style   = EXCLUDED.bowling_style,
                bowling_type    = EXCLUDED.bowling_type,
                bowling_subtype = EXCLUDED.bowling_subtype,
                batting_hand    = EXCLUDED.batting_hand,
                bowling_arm     = EXCLUDED.bowling_arm,
                playing_role    = EXCLUDED.playing_role,
                is_bowler       = EXCLUDED.is_bowler,
                is_batter       = EXCLUDED.is_batter,
                is_wicketkeeper = EXCLUDED.is_wicketkeeper,
                is_allrounder   = EXCLUDED.is_allrounder,
                nationality     = EXCLUDED.nationality,
                updated_at      = NOW()
        """

        CHUNK = 200
        uploaded = 0
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            for r in chunk:
                r["is_bowler"]       = bool(r["is_bowler"])
                r["is_batter"]       = bool(r["is_batter"])
                r["is_wicketkeeper"] = bool(r["is_wicketkeeper"])
                r["is_allrounder"]   = bool(r["is_allrounder"])
            psycopg2.extras.execute_batch(pgcur, UPSERT_SQL, chunk, page_size=100)
            pg.commit()
            uploaded += len(chunk)
            log(f"  ✓ {uploaded:,} / {len(rows):,} rows uploaded")

        pgcur.close()
        pg.close()
        log(f"  Upload complete — {uploaded:,} rows.")
        return uploaded

    except Exception as exc:
        log(f"  ✗ Upload error: {exc}")
        log(traceback.format_exc())
        return 0

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not OPENAI_OK:
        log("  ✗ openai not installed. Run:")
        log("    pip install openai --break-system-packages")
        sys.exit(1)

    try:
        discover_nvidia_key_files()
    except FileNotFoundError as e:
        log(f"  ✗ {e}")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Cricket player profile builder — NVIDIA NIM edition")
    parser.add_argument("--player",        type=str, default=None,
                        help="Single player name to look up")
    parser.add_argument("--no-upload",     action="store_true")
    parser.add_argument("--upload-only",   action="store_true")
    parser.add_argument("--limit",         type=int, default=None)
    parser.add_argument("--include-cached",action="store_true",
                        help="Re-query even players already in the DB")
    args = parser.parse_args()

    stats = {"total": 0, "found": 0, "unknown": 0, "errors": 0, "uploaded": 0}

    conn = open_db()
    client, nvidia_key = create_nvidia_client(log)
    log(f"  NVIDIA key: {nvidia_key['key_id']}  model: {MODEL}")
    log()

    try:
        name_map = load_name_map(conn)
        log(f"  Name map loaded: {len(name_map):,} entries" if name_map
            else "  Name map: not available (run expand_player_names.py first)")
        log()

        if not args.upload_only:
            if args.player:
                raw_players = [args.player]
            else:
                raw_players = get_player_list(conn)
                log(f"  Players in DB  : {len(raw_players):,}")
                if not args.include_cached:
                    already = get_already_done(conn)
                    before  = len(raw_players)
                    raw_players = [p for p in raw_players if p not in already]
                    log(f"  Already done   : {before - len(raw_players):,}  →  "
                        f"{len(raw_players):,} to process")

            if args.limit:
                raw_players = raw_players[:args.limit]
                log(f"  Limited to     : {args.limit}")

            stats["total"] = len(raw_players)
            log(f"  Model          : {MODEL}")
            log(f"  Batch size     : {BATCH_SIZE}")
            log(f"  Est. API calls : {-(-len(raw_players) // BATCH_SIZE)}")
            log()
            log("─" * 72)

            # Process in batches
            for batch_start in range(0, len(raw_players), BATCH_SIZE):
                batch = raw_players[batch_start:batch_start + BATCH_SIZE]
                batch_end = batch_start + len(batch)

                # Use full names from name_map for better OpenAI accuracy
                display_names = [name_map.get(p, p) for p in batch]

                log(f"  Batch {batch_start+1}–{batch_end} / {len(raw_players)}")

                try:
                    results = ask_openai_batch(client, display_names)
                except Exception as exc:
                    log(f"  ✗ Batch failed: {exc}")
                    stats["errors"] += len(batch)
                    continue

                # Match results back to original player_names
                # GPT returns them in the same order as we sent them
                for i, player_name in enumerate(batch):
                    full_name = display_names[i]
                    data = results[i] if i < len(results) else {}

                    row = build_row(player_name, full_name, data)
                    upsert_profile(conn, row)

                    confidence = data.get("confidence", "?")
                    bat   = row["batting_style"]  or "unknown"
                    bowl  = row["bowling_style"]  or "—"
                    btype = row["bowling_type"]
                    role  = row["playing_role"]   or "unknown"

                    if bat != "unknown":
                        stats["found"] += 1
                        log(f"    ✓ [{confidence}] {full_name:30s}  "
                            f"{role:20s}  Bat: {bat}  |  Bowl: {bowl}  [{btype}]")
                    else:
                        stats["unknown"] += 1
                        log(f"    ? [{confidence}] {full_name:30s}  unknown")

                conn.commit()

                # Small pause between batches — be a polite API citizen
                if batch_end < len(raw_players):
                    time.sleep(0.5)

            log()
            log("─" * 72)
            log(f"  Total processed : {stats['total']:,}")
            log(f"  Found           : {stats['found']:,}")
            log(f"  Unknown         : {stats['unknown']:,}")
            log(f"  Errors          : {stats['errors']:,}")

        if not args.no_upload:
            stats["uploaded"] = upload_to_supabase(conn)

        conn.close()

    except Exception as exc:
        log(f"\nFATAL: {exc}")
        log(traceback.format_exc())
        try: conn.close()
        except: pass

    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()
    report = f"""\
{"=" * 72}
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
{"=" * 72}
  Start Time  : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time    : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration    : {duration:.1f} seconds
  Outcome     : {'SUCCESS' if stats['errors'] == 0 else 'COMPLETED WITH ERRORS'}
{"─" * 72}
  Players processed : {stats['total']:,}
  Profiles found    : {stats['found']:,}
  Unknown           : {stats['unknown']:,}
  Errors            : {stats['errors']:,}
  Supabase uploaded : {stats['uploaded']:,}
{"─" * 72}
  Log    : {LOG_PATH}
{"=" * 72}
"""
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)
    log()
    log(report)
    log_file.close()
    sys.exit(0 if stats['errors'] == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\nFATAL: {e}")
        log(traceback.format_exc())
        log_file.close()
        sys.exit(1)
