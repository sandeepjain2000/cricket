#!/usr/bin/env python3
"""
================================================================================
  expand_player_names.py  v1.0.0
  OpenAI-powered Player Name Expander — Cricket T20 Analytics
================================================================================

PURPOSE
  Cricket ball-by-ball data (Cricsheet) stores player names in abbreviated form,
  e.g.  "V Kohli", "JJ Bumrah", "MS Dhoni", "SCJ Broad".
  ESPN Cricinfo uses full names like "Virat Kohli", "Jasprit Bumrah".

  This script:
    1. Reads all unique abbreviated player names from the local SQLite DB
    2. Sends batches to OpenAI (gpt-4o-mini) to expand to full names
    3. Saves the mapping to a local player_name_map table in SQLite
    4. Uploads the mapping table to Supabase
    5. (Optional) Re-runs the ESPN Cricinfo scraper using expanded names

USAGE
  # Full run — expand all names and upload
  python expand_player_names.py

  # Dry run — expand but don't upload to Supabase
  python expand_player_names.py --no-upload

  # Test on first 30 names only
  python expand_player_names.py --limit 30

  # Re-process names already in the table (force refresh)
  python expand_player_names.py --force

  # Filter to a specific team
  python expand_player_names.py --team "India"

CONFIG
  Set your OpenAI key via environment variable or config/settings.ini:
    OPENAI_API_KEY  — your OpenAI API key

  settings.ini example:
    [openai]
    api_key = sk-...
    model   = gpt-4o-mini    (default — cheap and accurate for this task)
    batch_size = 40

================================================================================
"""

import os
import sys
import json
import sqlite3
import argparse
import traceback
import time
import re
from datetime import datetime
from pathlib import Path
from configparser import ConfigParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import urllib.parse

_claudes = next((p for p in Path(__file__).resolve().parents if p.name == "Claudes"), None)
if _claudes is None:
    raise ImportError("Claudes root not found (expected ...\\Claudes\\nvidia_keys)")
sys.path.insert(0, str(_claudes))
from nvidia_llm import NVIDIA_BASE_URL, NVIDIA_MODEL, discover_nvidia_key_files, pick_random_nvidia_key

# ── Optional Supabase client ──────────────────────────────────────────────────
try:
    from supabase import create_client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_NAME = "expand_player_names.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR   = Path(__file__).parent.resolve()
OUTPUT_DIR   = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"expand_names_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"expand_names_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

LOCAL_DB    = SCRIPT_DIR / "db" / "cricket.db"
CONFIG_PATH = SCRIPT_DIR / "config" / "settings.ini"

OPENAI_API_URL = f"{NVIDIA_BASE_URL}/chat/completions"

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
log(f"  OpenAI Cricket Player Name Expander")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 72)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

config = ConfigParser()
config.read(CONFIG_PATH)

# ── Load .env.local (cricket_ui/.env.local) if present ───────────────────────
def _load_dotenv(env_path: Path) -> dict:
    """Parse a simple KEY=VALUE .env file into a dict (no shell escaping)."""
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

def _get(key: str, fallback=None):
    """Check os.environ first, then .env.local, then config, then fallback."""
    return (
        os.environ.get(key) or
        _dotenv.get(key) or
        fallback
    )

OPENAI_MODEL = _get("NVIDIA_MODEL") or config.get("openai", "model", fallback=NVIDIA_MODEL)
BATCH_SIZE     = config.getint("openai", "batch_size", fallback=40)

SUPABASE_URL = _get("SUPABASE_URL") or config.get("supabase", "url", fallback=None)
SUPABASE_KEY = _get("SUPABASE_KEY") or config.get("supabase", "key", fallback=None)

# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL SQLITE SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_name_map (
    short_name      TEXT    NOT NULL PRIMARY KEY,
    -- The abbreviated name exactly as it appears in the deliveries / match_players tables
    -- e.g.  "V Kohli", "JJ Bumrah", "MS Dhoni", "SCJ Broad"

    full_name       TEXT,
    -- Expanded full name for ESPN Cricinfo search
    -- e.g.  "Virat Kohli", "Jasprit Bumrah", "MS Dhoni"

    espn_search_name TEXT,
    -- The name to actually use when calling the ESPN Cricinfo search API.
    -- Usually same as full_name but may differ (e.g. known aliases).

    team_hint       TEXT,
    -- Team name from match_players — helps disambiguate players with same initials
    -- e.g. "India", "Australia"

    confidence      TEXT    NOT NULL DEFAULT 'Unknown',
    -- 'High'    — OpenAI is confident (common player, unambiguous)
    -- 'Medium'  — OpenAI made a reasonable guess (less common player)
    -- 'Low'     — OpenAI guessed but unsure (rare / ambiguous name)
    -- 'Unknown' — OpenAI could not expand (no cricket knowledge of player)

    source          TEXT    NOT NULL DEFAULT 'OpenAI',
    -- 'OpenAI'  — expanded by this script
    -- 'Manual'  — manually corrected by user
    -- 'Exact'   — name was already a full name (no expansion needed)

    notes           TEXT,
    -- Any notes from OpenAI or the user about this mapping

    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pnm_full_name  ON player_name_map (full_name);
CREATE INDEX IF NOT EXISTS idx_pnm_team_hint  ON player_name_map (team_hint);
CREATE INDEX IF NOT EXISTS idx_pnm_confidence ON player_name_map (confidence);
"""


def open_db() -> sqlite3.Connection:
    LOCAL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOCAL_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(TABLE_SCHEMA)
    conn.commit()
    return conn


# ─────────────────────────────────────────────────────────────────────────────
#  PLAYER NAME DETECTION — Is it already a "full" name?
# ─────────────────────────────────────────────────────────────────────────────

def looks_abbreviated(name: str) -> bool:
    """
    Returns True if the name looks like it uses initials and needs expansion.
    Examples that return True:  "V Kohli", "MS Dhoni", "SCJ Broad", "KL Rahul"
    Examples that return False: "Virat Kohli", "Dale Steyn", "Sachin Tendulkar"
    """
    if not name:
        return False
    parts = name.strip().split()
    if len(parts) < 2:
        return True   # single word — probably just a surname

    # If the first part is 1–3 uppercase letters with no lowercase, treat as initials
    first = parts[0]
    if len(first) <= 3 and first.isupper():
        return True

    # If any internal part is a single uppercase letter (e.g. "Hashim A Amla" style)
    for p in parts[1:-1]:
        if len(p) == 1 and p.isupper():
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI CALL
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert cricket historian with encyclopaedic knowledge of all
international and domestic T20 cricketers from every country.

Your task: given a list of abbreviated cricket player names (as they appear
in Cricsheet ball-by-ball data), expand each one to the player's commonly
used full name as it appears on ESPN Cricinfo.

Rules:
- Return ONLY a valid JSON object — no markdown, no explanation, no extra text.
- Each key is the abbreviated name exactly as given.
- Each value is an object with:
    "full_name"   : string  — the full name (e.g. "Virat Kohli")
    "confidence"  : string  — one of "High", "Medium", "Low", "Unknown"
    "notes"       : string or null  — brief note if ambiguous or unsure

Name expansion conventions (Cricsheet format):
- "V Kohli"    → "Virat Kohli"           (single initial + surname)
- "MS Dhoni"   → "MS Dhoni"              (two initials, keep as-is — very well known)
- "JJ Bumrah"  → "Jasprit Bumrah"        (double initial, expand first)
- "KL Rahul"   → "KL Rahul"             (two initials, well-known)
- "SCJ Broad"  → "Stuart Broad"         (three initials, expand)
- "DJ Bravo"   → "Dwayne Bravo"
- "AB de Villiers" → "AB de Villiers"   (already well-known, keep as-is)

If you genuinely don't know who a player is, set confidence to "Unknown"
and use the original name as full_name.

IMPORTANT: For players where the abbreviated form is their commonly used
public name (MS Dhoni, KL Rahul, AB de Villiers, etc.) — keep it as-is
and set confidence to "High".
"""


def call_openai(names_with_teams: list[tuple[str, str]]) -> dict:
    """
    Calls NVIDIA NIM with a batch of (short_name, team_hint) tuples.
    Returns dict mapping short_name → {full_name, confidence, notes}.
    """
    key = pick_random_nvidia_key()
    log(f"    NVIDIA key: {key['key_id']} model={key['model']}")
    # Build the input message — include team hints to help disambiguation
    name_list = []
    for short, team in names_with_teams:
        team_str = f" [{team}]" if team else ""
        name_list.append(f"- {short}{team_str}")

    user_message = (
        "Expand these cricket player names. "
        "Team hints are in brackets where available (use them to disambiguate).\n\n"
        + "\n".join(name_list)
        + "\n\nReturn ONLY the JSON object."
    )

    payload = json.dumps({
        "model":       key["model"],
        "temperature": 0.1,   # low temperature for factual lookups
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {key['api_key']}",
    }

    for attempt in range(1, 4):
        try:
            req  = Request(OPENAI_API_URL, data=payload, headers=headers, method="POST")
            with urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            return json.loads(content)
        except HTTPError as exc:
            if exc.code == 429:
                wait = 20 * attempt
                log(f"    ⚠ OpenAI rate limit (429). Waiting {wait}s…")
                time.sleep(wait)
            elif exc.code in (400, 401, 403):
                body_text = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenAI HTTP {exc.code}: {body_text[:200]}") from exc
            else:
                log(f"    ⚠ OpenAI HTTP {exc.code} (attempt {attempt}/3)")
                time.sleep(5 * attempt)
        except json.JSONDecodeError as exc:
            log(f"    ⚠ Could not parse OpenAI JSON response (attempt {attempt}/3): {exc}")
            time.sleep(3)
        except Exception as exc:
            log(f"    ⚠ OpenAI call error (attempt {attempt}/3): {exc}")
            time.sleep(5 * attempt)

    return {}   # Return empty dict on total failure — caller will record as Unknown


# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_players_to_process(
    conn: sqlite3.Connection,
    force: bool = False,
    team_filter: str | None = None,
) -> list[tuple[str, str]]:
    """
    Returns list of (short_name, team_hint) tuples that still need processing.
    If force=True, returns all players (re-processes existing entries).
    """
    already_done: set[str] = set()
    if not force:
        cur = conn.execute("SELECT short_name FROM player_name_map")
        already_done = {row[0] for row in cur.fetchall()}

    try:
        if team_filter:
            cur = conn.execute("""
                SELECT DISTINCT mp.player_name, mp.team
                FROM match_players mp
                WHERE mp.team LIKE ?
                ORDER BY mp.player_name
            """, (f"%{team_filter}%",))
        else:
            cur = conn.execute("""
                SELECT mp.player_name,
                       (SELECT mp2.team
                        FROM match_players mp2
                        WHERE mp2.player_name = mp.player_name
                        LIMIT 1) AS team
                FROM (SELECT DISTINCT player_name FROM match_players) mp
                ORDER BY mp.player_name
            """)
        all_players = [(row[0], row[1] or "") for row in cur.fetchall() if row[0]]
    except sqlite3.OperationalError:
        log("  ⚠ match_players table not found.")
        return []

    # Filter out already processed (unless --force)
    result = [(n, t) for n, t in all_players if n not in already_done]
    return result


def upsert_name_row(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute("""
        INSERT INTO player_name_map (
            short_name, full_name, espn_search_name, team_hint,
            confidence, source, notes, created_at, updated_at
        ) VALUES (
            :short_name, :full_name, :espn_search_name, :team_hint,
            :confidence, :source, :notes, datetime('now'), datetime('now')
        )
        ON CONFLICT(short_name) DO UPDATE SET
            full_name        = excluded.full_name,
            espn_search_name = excluded.espn_search_name,
            team_hint        = excluded.team_hint,
            confidence       = excluded.confidence,
            source           = excluded.source,
            notes            = excluded.notes,
            updated_at       = datetime('now')
    """, row)


# ─────────────────────────────────────────────────────────────────────────────
#  SUPABASE UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

def upload_to_supabase(conn: sqlite3.Connection) -> int:
    if not SUPABASE_AVAILABLE:
        log("  ✗ supabase-py not installed. Run: pip install supabase --break-system-packages")
        return 0
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("  ⚠ Supabase credentials not configured — skipping upload.")
        return 0

    log()
    log("─" * 72)
    log("  SUPABASE UPLOAD — player_name_map")
    log("─" * 72)

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    cur = conn.execute("""
        SELECT short_name, full_name, espn_search_name, team_hint,
               confidence, source, notes
        FROM player_name_map
    """)
    rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        log("  No rows to upload.")
        return 0

    log(f"  Uploading {len(rows):,} rows…")
    CHUNK = 500
    uploaded = 0

    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        try:
            sb.table("player_name_map").upsert(chunk, on_conflict="short_name").execute()
            uploaded += len(chunk)
            log(f"  ✓ {uploaded:,} / {len(rows):,} rows uploaded")
        except Exception as exc:
            log(f"  ✗ Supabase error (chunk {i}): {exc}")

    log(f"  Upload complete — {uploaded:,} rows.")
    return uploaded


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Expand abbreviated cricket player names using OpenAI"
    )
    parser.add_argument("--no-upload",  action="store_true",
                        help="Skip Supabase upload")
    parser.add_argument("--force",      action="store_true",
                        help="Re-process names already in the table")
    parser.add_argument("--limit",      type=int, default=None,
                        help="Max number of players to process (for testing)")
    parser.add_argument("--team",       type=str, default=None,
                        help="Only process players from this team (e.g. 'India')")
    parser.add_argument("--upload-only", action="store_true",
                        help="Skip OpenAI expansion; only upload existing cache to Supabase")
    args = parser.parse_args()

    # ── Validate NVIDIA keys ──────────────────────────────────────────────────
    if not args.upload_only:
        try:
            discover_nvidia_key_files()
        except FileNotFoundError as e:
            log(f"  ✗ {e}")
            sys.exit(1)

    stats = {
        "total":            0,
        "exact":            0,
        "expanded_high":    0,
        "expanded_medium":  0,
        "expanded_low":     0,
        "unknown":          0,
        "errors":           0,
        "uploaded":         0,
        "batches":          0,
    }

    conn = open_db()

    try:
        if not args.upload_only:
            # ── Get player list ───────────────────────────────────────────────
            players = get_players_to_process(conn, force=args.force, team_filter=args.team)
            log(f"  Players to process : {len(players):,}")

            if args.limit:
                players = players[:args.limit]
                log(f"  Limited to         : {args.limit}")

            stats["total"] = len(players)

            if not players:
                log("  Nothing to process — all names already expanded.")
                log("  Use --force to re-expand, or --upload-only to just push to Supabase.")
            else:
                log(f"  Batch size         : {BATCH_SIZE}")
                log(f"  OpenAI model       : {OPENAI_MODEL}")
                log()
                log("─" * 72)
                log("  EXPANDING NAMES")
                log("─" * 72)

                # ── Split into batches ────────────────────────────────────────
                for batch_start in range(0, len(players), BATCH_SIZE):
                    batch      = players[batch_start: batch_start + BATCH_SIZE]
                    batch_num  = batch_start // BATCH_SIZE + 1
                    total_batches = (len(players) + BATCH_SIZE - 1) // BATCH_SIZE
                    stats["batches"] += 1

                    log(f"\n  Batch {batch_num}/{total_batches}  "
                        f"(players {batch_start+1}–{min(batch_start+len(batch), len(players))})")

                    # Separate names that look abbreviated from those that don't
                    to_expand   = [(n, t) for n, t in batch if looks_abbreviated(n)]
                    already_full = [(n, t) for n, t in batch if not looks_abbreviated(n)]

                    # ── Insert "Exact" rows immediately ───────────────────────
                    for short_name, team in already_full:
                        upsert_name_row(conn, {
                            "short_name":       short_name,
                            "full_name":        short_name,
                            "espn_search_name": short_name,
                            "team_hint":        team,
                            "confidence":       "High",
                            "source":           "Exact",
                            "notes":            "Name appears to be already full",
                        })
                        stats["exact"] += 1
                    conn.commit()

                    if not to_expand:
                        log(f"    All {len(already_full)} names in this batch are already full — skipped OpenAI")
                        continue

                    log(f"    {len(to_expand)} names need expansion, {len(already_full)} are already full")

                    # ── Call OpenAI ───────────────────────────────────────────
                    try:
                        results = call_openai(to_expand)
                    except Exception as exc:
                        log(f"    ✗ OpenAI batch failed: {exc}")
                        stats["errors"] += len(to_expand)
                        # Still record them as Unknown so we don't lose progress
                        for short_name, team in to_expand:
                            upsert_name_row(conn, {
                                "short_name":       short_name,
                                "full_name":        short_name,
                                "espn_search_name": short_name,
                                "team_hint":        team,
                                "confidence":       "Unknown",
                                "source":           "OpenAI",
                                "notes":            f"API error: {str(exc)[:100]}",
                            })
                        conn.commit()
                        continue

                    # ── Process results ───────────────────────────────────────
                    processed_keys = set()
                    for short_name, team in to_expand:
                        # OpenAI may return the key with or without the team hint in brackets
                        candidate_keys = [
                            short_name,
                            f"{short_name} [{team}]",
                            short_name.strip(),
                        ]
                        entry = None
                        for key in candidate_keys:
                            if key in results:
                                entry = results[key]
                                processed_keys.add(key)
                                break

                        if entry and isinstance(entry, dict):
                            full_name  = entry.get("full_name") or short_name
                            confidence = entry.get("confidence") or "Unknown"
                            notes      = entry.get("notes")

                            # Fallback: if OpenAI returned the abbreviated name unchanged
                            # and confidence isn't High, mark as Unknown
                            if full_name.strip() == short_name.strip() and confidence not in ("High",):
                                confidence = "Unknown"

                            # ESPN search name: use full name but strip common suffixes
                            espn_search = full_name
                        else:
                            # OpenAI didn't return this name — record as Unknown
                            full_name   = short_name
                            espn_search = short_name
                            confidence  = "Unknown"
                            notes       = "Not returned by OpenAI"

                        upsert_name_row(conn, {
                            "short_name":       short_name,
                            "full_name":        full_name,
                            "espn_search_name": espn_search,
                            "team_hint":        team,
                            "confidence":       confidence,
                            "source":           "OpenAI",
                            "notes":            notes,
                        })

                        # Tally stats
                        if confidence == "High":
                            stats["expanded_high"] += 1
                        elif confidence == "Medium":
                            stats["expanded_medium"] += 1
                        elif confidence == "Low":
                            stats["expanded_low"] += 1
                        else:
                            stats["unknown"] += 1

                        # Console output
                        arrow = "→" if full_name != short_name else "="
                        conf_icon = {"High": "✓", "Medium": "~", "Low": "?", "Unknown": "✗"}.get(confidence, "?")
                        log(f"    {conf_icon} [{confidence:7s}]  {short_name:30s} {arrow} {full_name}")

                    conn.commit()

                    # Brief pause between batches to respect rate limits
                    if batch_start + BATCH_SIZE < len(players):
                        time.sleep(1.0)

        # ── Summary ───────────────────────────────────────────────────────────
        log()
        log("─" * 72)
        cur = conn.execute("SELECT COUNT(*) FROM player_name_map")
        total_in_db = cur.fetchone()[0]
        log(f"  Expansion complete:")
        log(f"    Total names in DB  : {total_in_db:,}")
        log(f"    Processed this run : {stats['total']:,}")
        log(f"    Already full names : {stats['exact']:,}")
        log(f"    High confidence    : {stats['expanded_high']:,}")
        log(f"    Medium confidence  : {stats['expanded_medium']:,}")
        log(f"    Low confidence     : {stats['expanded_low']:,}")
        log(f"    Unknown            : {stats['unknown']:,}")
        log(f"    Errors             : {stats['errors']:,}")

        # ── Supabase upload ───────────────────────────────────────────────────
        if not args.no_upload:
            stats["uploaded"] = upload_to_supabase(conn)
        else:
            log()
            log("  --no-upload set — skipping Supabase.")

        conn.close()

    except Exception as exc:
        log(f"\nFATAL: {exc}")
        log(traceback.format_exc())
        stats["errors"] += 1
        try:
            conn.close()
        except Exception:
            pass

    # ── Report ────────────────────────────────────────────────────────────────
    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()

    report = f"""\
{"=" * 72}
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
{"=" * 72}
  Start Time   : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time     : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration     : {duration:.1f} seconds
  OpenAI Model : {OPENAI_MODEL}
  Outcome      : {'SUCCESS' if stats['errors'] == 0 else 'COMPLETED WITH ERRORS'}
{"─" * 72}
  Summary:
  - Names processed this run  : {stats['total']:,}
  - Already full names        : {stats['exact']:,}
  - Expanded (High conf)      : {stats['expanded_high']:,}
  - Expanded (Medium conf)    : {stats['expanded_medium']:,}
  - Expanded (Low conf)       : {stats['expanded_low']:,}
  - Unknown / not found       : {stats['unknown']:,}
  - Errors                    : {stats['errors']:,}
  - Supabase rows uploaded    : {stats['uploaded']:,}
{"─" * 72}
  NEXT STEP:
  Use player_name_map.espn_search_name (instead of raw player_name) when
  calling scrape_player_profiles.py to match names on ESPN Cricinfo.

  To fix any wrong expansions, UPDATE the row manually:
    UPDATE player_name_map
       SET full_name = 'Correct Name',
           espn_search_name = 'Correct Name',
           confidence = 'High',
           source = 'Manual',
           updated_at = datetime('now')
     WHERE short_name = 'V X Name';
{"─" * 72}
  Local DB     : {LOCAL_DB}
  Log          : {LOG_PATH}
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
    except Exception as exc:
        log(f"\nFATAL ERROR: {exc}")
        log(traceback.format_exc())
        log_file.close()
        sys.exit(1)
