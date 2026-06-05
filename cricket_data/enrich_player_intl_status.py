#!/usr/bin/env python3
"""
================================================================================
  enrich_player_intl_status.py  v1.0.0
  International Career Status Enricher — OpenAI GPT-4o-mini + DB data
================================================================================

PURPOSE
  For every player in player_profiles, determines their international
  career status across three formats:  Test  |  ODI (LOI)  |  T20I

  Two signals are combined:

  1. DATABASE SIGNAL  — queries each format's Postgres DB to find the
     last calendar year the player appeared (as batter or bowler).
     This is factual: what the Cricsheet records actually show.

  2. GPT SIGNAL  — asks GPT-4o-mini whether the player has officially
     announced retirement from each format, and whether they ever
     played that format internationally at all.

  Merged result → one of four status values per format:

    "Active"        — played within the last 2 years, not retired
    "Retired"       — officially announced retirement from this format
    "Out 2Y+"       — last played 2+ years ago, no official retirement
                      (dropped, unavailable, or quietly phased out)
    "Never Played"  — never represented their country in this format
    "Unknown"       — insufficient data to determine

  Also sets two summary booleans:
    intl_retired_all   — True if retired from ALL formats they ever played
    intl_available     — True if "Active" in at least one format

NEW COLUMNS ADDED TO player_profiles
  intl_test_status      TEXT        -- status for Test cricket
  intl_odi_status       TEXT        -- status for ODI cricket
  intl_t20i_status      TEXT        -- status for T20I cricket
  intl_test_last_year   SMALLINT    -- last year appeared in Tests (from DB)
  intl_odi_last_year    SMALLINT    -- last year appeared in ODIs (from DB)
  intl_t20i_last_year   SMALLINT    -- last year appeared in T20Is (from DB)
  intl_retired_all      BOOLEAN     -- retired from all formats played
  intl_available        BOOLEAN     -- eligible for international selection
  intl_status_notes     TEXT        -- brief GPT notes (e.g. "Retired from Tests 2023")
  intl_status_updated   DATE        -- date this enrichment was last run

USE CASE
  This table feeds the team selector module.  The selector filters:
    WHERE intl_available = TRUE
      AND intl_{format}_status IN ('Active')
  to build a candidate pool for each format.

CUTOFF LOGIC  (as of script run date)
  "2 years" means: last_year <= (current_year - 2)
  e.g. running in 2026 → players last appearing in 2023 or earlier → "Out 2Y+"

SETUP  (one-time)
  pip install openai psycopg2-binary --break-system-packages

USAGE
  python enrich_player_intl_status.py              # all players
  python enrich_player_intl_status.py --test       # first 60 players only
  python enrich_player_intl_status.py --limit 200
  python enrich_player_intl_status.py --skip-existing  # skip already-enriched

CONFIG  (auto-read from cricket_ui/.env.local)
  OPENAI_API_KEY    — required
  SUPABASE_URL_T20  — T20 Postgres (also hosts player_profiles)
  SUPABASE_URL      — Test Postgres
  SUPABASE_URL_LOI  — LOI/ODI Postgres
================================================================================
"""

import os
import sys
import re
import json
import time
import argparse
import traceback
from datetime import datetime, date
from pathlib import Path

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

SCRIPT_NAME = "enrich_player_intl_status.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()
CURRENT_YEAR = START_TIME.year                # e.g. 2026
OUT_2Y_CUTOFF = CURRENT_YEAR - 2              # last_year ≤ this → "Out 2Y+"

SCRIPT_DIR  = Path(__file__).parent.resolve()
OUTPUT_DIR  = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"intl_status_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"intl_status_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

BATCH_SIZE  = 30    # players per LLM call
MODEL       = NVIDIA_MODEL

VALID_STATUSES = {"Active", "Retired", "Out 2Y+", "Never Played", "Unknown"}

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
log(f"  International Career Status Enricher")
log(f"  Started : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log(f"  Cutoff  : players last seen ≤ {OUT_2Y_CUTOFF} are marked 'Out 2Y+'")
log("=" * 72)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def _load_dotenv(env_path: Path) -> dict:
    vals = {}
    if not env_path.exists():
        return vals
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

_dotenv = _load_dotenv(SCRIPT_DIR.parent / "cricket_ui" / ".env.local")

def _cfg(key: str, fallback=None):
    return os.environ.get(key) or _dotenv.get(key) or fallback

SUPABASE_URL_T20 = _cfg("SUPABASE_URL_T20") or _cfg("SUPABASE_URL")
SUPABASE_URL_TEST = _cfg("SUPABASE_URL")
SUPABASE_URL_LOI  = _cfg("SUPABASE_URL_LOI")

# ─────────────────────────────────────────────────────────────────────────────
#  ARGUMENT PARSING
# ─────────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description=SCRIPT_NAME)
parser.add_argument("--test",          action="store_true",
                    help="Process first 60 players only (smoke test)")
parser.add_argument("--limit",         type=int, default=0,
                    help="Cap total players to process (e.g. --limit 200)")
parser.add_argument("--skip-existing", action="store_true",
                    help="Skip players already having intl_test_status populated")
parser.add_argument("--gpt-only",      action="store_true",
                    help="Skip DB last-year queries, use GPT data only")
parser.add_argument("--db-only",       action="store_true",
                    help="Skip GPT, classify purely from DB last-year data")
args = parser.parse_args()

mode_label = "TEST (60 players)" if args.test else \
             f"LIMIT {args.limit}" if args.limit else "FULL"
log(f"  Mode          : {mode_label}")
log(f"  Skip existing : {args.skip_existing}")
log(f"  GPT only      : {args.gpt_only}")
log(f"  DB only       : {args.db_only}")
log()

# ─────────────────────────────────────────────────────────────────────────────
#  DEPENDENCY CHECKS
# ─────────────────────────────────────────────────────────────────────────────

errors = []
if not PSYCOPG2_OK:
    errors.append("psycopg2 not installed. Run: pip install psycopg2-binary --break-system-packages")
if not OPENAI_OK and not args.db_only:
    errors.append("openai not installed. Run: pip install openai --break-system-packages")
if not args.db_only:
    try:
        discover_nvidia_key_files()
    except FileNotFoundError as e:
        errors.append(str(e))
if not SUPABASE_URL_T20:
    errors.append("SUPABASE_URL_T20 missing from .env.local (needed for player_profiles)")

for e in errors:
    log(f"  ❌ {e}")
if errors:
    log()
    log("  Cannot continue. Fix the above and re-run.")
    log_file.close()
    sys.exit(1)

log("  ✓ Dependencies OK")
log()

# ─────────────────────────────────────────────────────────────────────────────
#  DB CONNECTIONS
# ─────────────────────────────────────────────────────────────────────────────

def connect(url: str | None, label: str):
    if not url:
        log(f"  ⚠  No URL for {label} — skipping last-year lookup for this format")
        return None
    try:
        conn = psycopg2.connect(url, connect_timeout=15)
        conn.autocommit = False
        log(f"  ✓ Connected to {label}")
        return conn
    except Exception as e:
        log(f"  ⚠  Could not connect to {label}: {e}")
        return None

log("  Connecting to databases...")
pg_t20  = connect(SUPABASE_URL_T20,  "T20  DB (player_profiles host)")
pg_test = connect(SUPABASE_URL_TEST, "Test DB")
pg_loi  = connect(SUPABASE_URL_LOI,  "LOI  DB")
log()

if not pg_t20:
    log("  ❌ T20 DB is required (hosts player_profiles). Cannot continue.")
    log_file.close()
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI CLIENT
# ─────────────────────────────────────────────────────────────────────────────

client = None
nvidia_key = None
if not args.db_only:
    client, nvidia_key = create_nvidia_client(log)
    log(f"  ✓ NVIDIA client ready  (model: {MODEL}, key: {nvidia_key['key_id']})")
    log()

# ─────────────────────────────────────────────────────────────────────────────
#  SCHEMA MIGRATION  — add new columns to player_profiles
# ─────────────────────────────────────────────────────────────────────────────

NEW_COLUMNS = [
    ("intl_test_status",    "TEXT"),
    ("intl_odi_status",     "TEXT"),
    ("intl_t20i_status",    "TEXT"),
    ("intl_test_last_year", "SMALLINT"),
    ("intl_odi_last_year",  "SMALLINT"),
    ("intl_t20i_last_year", "SMALLINT"),
    ("intl_retired_all",    "BOOLEAN"),
    ("intl_available",      "BOOLEAN"),
    ("intl_status_notes",   "TEXT"),
    ("intl_status_updated", "DATE"),
]

def add_columns(pg):
    log("  Adding new columns to player_profiles (if not already present)...")
    with pg.cursor() as cur:
        for col, dtype in NEW_COLUMNS:
            cur.execute(
                f"ALTER TABLE player_profiles ADD COLUMN IF NOT EXISTS {col} {dtype};"
            )
            log(f"    ✓ {col}  {dtype}")
    pg.commit()
    log()

add_columns(pg_t20)

# ─────────────────────────────────────────────────────────────────────────────
#  LOAD PLAYERS
# ─────────────────────────────────────────────────────────────────────────────

def load_players(pg, skip_existing: bool, limit: int) -> list[dict]:
    """Fetch players from player_profiles."""
    with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = "WHERE (intl_test_status IS NULL)" if skip_existing else ""
        lim   = f"LIMIT {limit}" if limit else ""
        cur.execute(f"""
            SELECT player_name,
                   COALESCE(nationality, '') AS country,
                   COALESCE(playing_role, '') AS role,
                   bowling_type, bowling_arm
            FROM   player_profiles
            {where}
            ORDER  BY nationality, player_name
            {lim}
        """)
        return [dict(r) for r in cur.fetchall()]

row_limit = 60 if args.test else (args.limit or 0)
players   = load_players(pg_t20, skip_existing=args.skip_existing, limit=row_limit)

log(f"  Players to process : {len(players):,}")
if not players:
    log("  Nothing to do — exiting.")
    log_file.close()
    sys.exit(0)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  DB LAST-YEAR LOOKUP  — per format
# ─────────────────────────────────────────────────────────────────────────────

def get_last_year_map(pg, format_label: str) -> dict[str, int]:
    """
    Query a format DB to get the last calendar year each player appeared
    (as batter OR bowler) in the matches table joined with deliveries.
    Returns { player_name: last_year }.
    """
    if pg is None:
        return {}
    try:
        with pg.cursor() as cur:
            # Check if matches table has start_date column
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'matches'
                  AND column_name IN ('start_date', 'date', 'match_date', 'season')
                LIMIT 5
            """)
            date_cols = [r[0] for r in cur.fetchall()]
            if not date_cols:
                log(f"    ⚠  {format_label}: no date column found in matches table")
                return {}

            # Prefer start_date, fall back to date/match_date/season
            date_col = next((c for c in ["start_date","date","match_date","season"] if c in date_cols), date_cols[0])

            # Check if deliveries has match_id to join
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'deliveries'
                  AND column_name = 'match_id'
            """)
            has_match_id = bool(cur.fetchone())

            if has_match_id and date_col != 'season':
                query = f"""
                    SELECT p.player_name,
                           EXTRACT(YEAR FROM MAX(m.{date_col}::date))::int AS last_year
                    FROM   (
                        SELECT match_id, batter AS player_name FROM deliveries
                        UNION ALL
                        SELECT match_id, bowler AS player_name FROM deliveries
                    ) p
                    JOIN matches m USING (match_id)
                    WHERE m.{date_col} IS NOT NULL
                    GROUP BY p.player_name
                """
            else:
                # Fallback: use season column directly on deliveries if available,
                # or join via match_id with season
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'deliveries'
                      AND column_name = 'season'
                """)
                has_season_in_del = bool(cur.fetchone())

                if has_season_in_del:
                    query = """
                        SELECT player_name,
                               MAX(CAST(SUBSTRING(season FROM '^[0-9]+') AS INTEGER)) AS last_year
                        FROM (
                            SELECT batter AS player_name, season FROM deliveries
                            UNION ALL
                            SELECT bowler AS player_name, season FROM deliveries
                        ) p
                        WHERE season IS NOT NULL
                        GROUP BY player_name
                    """
                else:
                    # Last resort: use matches season column
                    query = f"""
                        SELECT p.player_name,
                               MAX(CAST(SUBSTRING(m.season FROM '^[0-9]+') AS INTEGER)) AS last_year
                        FROM (
                            SELECT match_id, batter AS player_name FROM deliveries
                            UNION ALL
                            SELECT match_id, bowler AS player_name FROM deliveries
                        ) p
                        JOIN matches m USING (match_id)
                        WHERE m.season IS NOT NULL
                        GROUP BY p.player_name
                    """

            cur.execute(query)
            rows = cur.fetchall()
            result = {}
            for name, yr in rows:
                if name and yr:
                    result[str(name)] = int(yr)
            log(f"    ✓ {format_label}: {len(result):,} player-year records")
            return result

    except Exception as e:
        log(f"    ⚠  {format_label} last-year query failed: {e}")
        return {}

if not args.gpt_only:
    log("  Querying DBs for last appearance year per player...")
    last_year_t20  = get_last_year_map(pg_t20,  "T20I")
    last_year_test = get_last_year_map(pg_test, "Test")
    last_year_loi  = get_last_year_map(pg_loi,  "ODI ")
    log()
else:
    last_year_t20  = {}
    last_year_test = {}
    last_year_loi  = {}
    log("  Skipping DB last-year queries (--gpt-only mode)")
    log()

# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI RETIREMENT CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a cricket analyst with encyclopedic knowledge of international cricket careers up to your training cutoff.

For each player listed, classify their international career status across THREE formats:
  Test cricket, ODI (One Day International) cricket, T20 International cricket.

For each format, return ONE of these exact status values:
  "Active"       - regularly played within 2 years of your training cutoff, not officially retired
  "Retired"      - officially/publicly announced retirement from this format
  "Out 2Y+"      - not officially retired but has not been selected for 2+ years (dropped, unavailable)
  "Never Played" - never represented their country at international level in this format
  "Unknown"      - you do not have sufficient information

Also return:
  test_last_year  - last calendar year they played Test cricket (integer) or null
  odi_last_year   - last calendar year they played ODI cricket (integer) or null
  t20i_last_year  - last calendar year they played T20I cricket (integer) or null
  notes           - one brief sentence if there's something important (e.g. "Retired from Tests in 2023, still active in T20I as of 2024")

IMPORTANT RULES:
1. Use "Never Played" if the player has NEVER appeared in that format internationally.
   Many specialists only play 1-2 formats. T20I is the most common format players skip.
2. "Out 2Y+" is for players who are clearly no longer selected but haven't formally retired.
3. Lean toward "Active" if you are uncertain and the player was playing recently.
4. Return ONLY valid JSON — a single top-level array of objects, one per player.
5. Every player in the input MUST appear in the output, in the same order.

Output format (JSON array):
[
  {{
    "player_name": "Virat Kohli",
    "test_status": "Active",
    "odi_status": "Active",
    "t20i_status": "Retired",
    "test_last_year": 2024,
    "odi_last_year": 2024,
    "t20i_last_year": 2024,
    "notes": "Retired from T20I internationals after 2024 World Cup"
  }},
  ...
]"""

def _parse_gpt_json(raw: str) -> list[dict]:
    """Extract JSON array from GPT response, handling markdown code fences."""
    raw = raw.strip()
    # Strip markdown fences
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    raw = raw.strip()
    # Find the JSON array
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    return json.loads(raw)

def classify_batch_gpt(batch: list[dict]) -> list[dict]:
    """Send a batch of players to GPT-4o-mini for classification."""
    lines = []
    for i, p in enumerate(batch, 1):
        lines.append(
            f"{i}. {p['player_name']} ({p.get('country','?')}) "
            f"— Role: {p.get('role','?')}"
        )
    user_msg = "Classify the international career status for these cricket players:\n\n" + "\n".join(lines)

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.1,   # low temperature for factual classification
            max_tokens=3000,
            response_format={"type": "json_object"} if False else None,  # plain JSON
        )
        raw = resp.choices[0].message.content.strip()
        parsed = _parse_gpt_json(raw)
        if isinstance(parsed, dict):
            # GPT sometimes wraps in {"players": [...]}
            for key in ("players", "results", "data", "classifications"):
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        log(f"    ⚠  GPT batch failed: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
#  STATUS MERGE LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def merge_status(gpt_status: str | None, db_last_year: int | None) -> str:
    """
    Combine GPT classification with DB last-year evidence.

    Rules (in priority order):
    1. If GPT says "Retired"      → Retired  (official announcement wins)
    2. If GPT says "Never Played" → Never Played (definitional)
    3. If DB shows last_year ≤ OUT_2Y_CUTOFF → Out 2Y+ (data overrides optimistic GPT)
    4. If DB shows last_year > OUT_2Y_CUTOFF → Active  (recent evidence of play)
    5. Fall back to GPT status if DB has no data
    6. Unknown if nothing available
    """
    gpt = (gpt_status or "Unknown").strip()

    if gpt == "Retired":
        return "Retired"
    if gpt == "Never Played":
        return "Never Played"

    # DB evidence available
    if db_last_year:
        if db_last_year <= OUT_2Y_CUTOFF:
            return "Out 2Y+"
        else:
            return "Active"

    # No DB evidence — fall back to GPT
    if gpt in VALID_STATUSES:
        return gpt
    return "Unknown"

def compute_summary_flags(test_s: str, odi_s: str, t20i_s: str) -> tuple[bool, bool]:
    """
    Returns (intl_retired_all, intl_available).

    intl_retired_all  = True if all formats the player played are "Retired"
                        (ignores "Never Played" formats)
    intl_available    = True if at least one format is "Active"
    """
    played_statuses = [s for s in (test_s, odi_s, t20i_s) if s != "Never Played"]
    all_retired = bool(played_statuses) and all(s == "Retired" for s in played_statuses)
    any_active  = any(s == "Active" for s in (test_s, odi_s, t20i_s))
    return all_retired, any_active

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN PROCESSING LOOP
# ─────────────────────────────────────────────────────────────────────────────

log(f"  Processing {len(players):,} players in batches of {BATCH_SIZE}...")
log()

today_str  = date.today().isoformat()
total_batches = (len(players) + BATCH_SIZE - 1) // BATCH_SIZE

stats = {
    "processed":   0,
    "active":      0,
    "retired":     0,
    "out_2y":      0,
    "never":       0,
    "unknown":     0,
    "db_only":     0,
    "gpt_errors":  0,
}

# Build a quick-lookup dict: player_name → result row to write
results: dict[str, dict] = {}

for batch_n in range(total_batches):
    batch_start = batch_n * BATCH_SIZE
    batch       = players[batch_start : batch_start + BATCH_SIZE]

    log(f"  Batch {batch_n+1}/{total_batches}  —  players {batch_start+1}–{batch_start+len(batch)}")

    # ── GPT classification ──────────────────────────────────────────────────
    gpt_map: dict[str, dict] = {}   # player_name → GPT result

    if not args.db_only and client:
        gpt_results = classify_batch_gpt(batch)

        for item in gpt_results:
            name = item.get("player_name", "")
            if name:
                gpt_map[name] = item

        if len(gpt_map) < len(batch):
            log(f"    ⚠  GPT returned {len(gpt_map)}/{len(batch)} results")
            stats["gpt_errors"] += 1
        else:
            log(f"    ✓ GPT classified {len(gpt_map)} players")

        time.sleep(0.5)   # polite rate-limit pause

    # ── Merge DB + GPT per player ───────────────────────────────────────────
    batch_rows = []

    for p in batch:
        name    = p["player_name"]
        gpt     = gpt_map.get(name, {})

        # DB last years (from the per-format queries)
        db_t20  = last_year_t20.get(name)
        db_test = last_year_test.get(name)
        db_loi  = last_year_loi.get(name)

        # GPT last years (use as fallback if DB has none)
        gpt_t20_yr  = gpt.get("t20i_last_year")
        gpt_test_yr = gpt.get("test_last_year")
        gpt_odi_yr  = gpt.get("odi_last_year")

        # Best last year = DB if available, else GPT
        best_t20_yr  = db_t20  or (int(gpt_t20_yr)  if gpt_t20_yr  else None)
        best_test_yr = db_test or (int(gpt_test_yr) if gpt_test_yr else None)
        best_odi_yr  = db_loi  or (int(gpt_odi_yr)  if gpt_odi_yr  else None)

        # Merge status
        t20i_status = merge_status(gpt.get("t20i_status"), best_t20_yr)
        test_status = merge_status(gpt.get("test_status"), best_test_yr)
        odi_status  = merge_status(gpt.get("odi_status"),  best_odi_yr)

        retired_all, available = compute_summary_flags(test_status, odi_status, t20i_status)

        notes = gpt.get("notes", "") or ""

        row = {
            "player_name":       name,
            "intl_test_status":  test_status,
            "intl_odi_status":   odi_status,
            "intl_t20i_status":  t20i_status,
            "intl_test_last_year":  best_test_yr,
            "intl_odi_last_year":   best_odi_yr,
            "intl_t20i_last_year":  best_t20_yr,
            "intl_retired_all":  retired_all,
            "intl_available":    available,
            "intl_status_notes": notes[:500] if notes else None,
            "intl_status_updated": today_str,
        }
        batch_rows.append(row)
        results[name] = row

        # Tally
        for s in (test_status, odi_status, t20i_status):
            if s == "Active":       stats["active"]  += 1
            elif s == "Retired":    stats["retired"] += 1
            elif s == "Out 2Y+":    stats["out_2y"]  += 1
            elif s == "Never Played": stats["never"] += 1
            else:                   stats["unknown"] += 1

        if not gpt:
            stats["db_only"] += 1

    stats["processed"] += len(batch)

    # ── Bulk UPDATE player_profiles ─────────────────────────────────────────
    try:
        with pg_t20.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                """
                UPDATE player_profiles SET
                    intl_test_status      = %(intl_test_status)s,
                    intl_odi_status       = %(intl_odi_status)s,
                    intl_t20i_status      = %(intl_t20i_status)s,
                    intl_test_last_year   = %(intl_test_last_year)s,
                    intl_odi_last_year    = %(intl_odi_last_year)s,
                    intl_t20i_last_year   = %(intl_t20i_last_year)s,
                    intl_retired_all      = %(intl_retired_all)s,
                    intl_available        = %(intl_available)s,
                    intl_status_notes     = %(intl_status_notes)s,
                    intl_status_updated   = %(intl_status_updated)s
                WHERE player_name = %(player_name)s
                """,
                batch_rows,
                page_size=100,
            )
        pg_t20.commit()
        log(f"    ✓ {len(batch_rows)} rows written to DB")
    except Exception as e:
        pg_t20.rollback()
        log(f"    ❌ DB write failed for batch {batch_n+1}: {e}")

    log()

# ─────────────────────────────────────────────────────────────────────────────
#  VERIFICATION SAMPLE
# ─────────────────────────────────────────────────────────────────────────────

log("  ── Sample results (first 15 players) ─────────────────────────────────")
sample = list(results.values())[:15]
log(f"  {'Player':<28} {'Country':<14} {'Test':<12} {'ODI':<12} {'T20I':<12} {'Avail'}")
log("  " + "─" * 86)
for r in sample:
    p   = next((x for x in players if x["player_name"] == r["player_name"]), {})
    log(
        f"  {r['player_name']:<28} "
        f"{p.get('country','?'):<14} "
        f"{r['intl_test_status']:<12} "
        f"{r['intl_odi_status']:<12} "
        f"{r['intl_t20i_status']:<12} "
        f"{'✓' if r['intl_available'] else '✗'}"
    )
log()

# ─────────────────────────────────────────────────────────────────────────────
#  STATUS DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

log("  ── Status distribution (across all formats for all players) ──────────")
log(f"  Active       : {stats['active']:,}")
log(f"  Retired      : {stats['retired']:,}")
log(f"  Out 2Y+      : {stats['out_2y']:,}")
log(f"  Never Played : {stats['never']:,}")
log(f"  Unknown      : {stats['unknown']:,}")
log(f"  DB-only rows : {stats['db_only']:,}  (GPT had no data)")
log(f"  GPT errors   : {stats['gpt_errors']:,}  (batches with partial/failed response)")
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CLOSE CONNECTIONS
# ─────────────────────────────────────────────────────────────────────────────

for pg, label in ((pg_t20, "T20"), (pg_test, "Test"), (pg_loi, "LOI")):
    if pg:
        pg.close()
        log(f"  ✓ {label} DB connection closed")
log()

# ─────────────────────────────────────────────────────────────────────────────
#  FINAL REPORT
# ─────────────────────────────────────────────────────────────────────────────

end_time = datetime.now()
duration = str(end_time - START_TIME).split(".")[0]

report = f"""
============================================================
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
============================================================
  Start Time     : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time       : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration       : {duration}
  Mode           : {mode_label}
  Outcome        : SUCCESS
------------------------------------------------------------
  Players processed : {stats['processed']:,}
  Current year      : {CURRENT_YEAR}  (Out 2Y+ cutoff ≤ {OUT_2Y_CUTOFF})
------------------------------------------------------------
  Status distribution (per-format counts):
    Active        : {stats['active']:,}
    Retired       : {stats['retired']:,}
    Out 2Y+       : {stats['out_2y']:,}
    Never Played  : {stats['never']:,}
    Unknown       : {stats['unknown']:,}
------------------------------------------------------------
  GPT errors       : {stats['gpt_errors']:,}
  DB-only records  : {stats['db_only']:,}
------------------------------------------------------------
  New columns added to player_profiles:
    intl_test_status      intl_odi_status      intl_t20i_status
    intl_test_last_year   intl_odi_last_year   intl_t20i_last_year
    intl_retired_all      intl_available
    intl_status_notes     intl_status_updated
------------------------------------------------------------
  Recommended SQL to verify:
    SELECT intl_test_status, intl_odi_status, intl_t20i_status,
           intl_available, COUNT(*)
    FROM player_profiles
    WHERE intl_status_updated IS NOT NULL
    GROUP BY 1,2,3,4 ORDER BY 4 DESC, 1;
------------------------------------------------------------
  Next step — Team Selector module:
    Filter pool  : WHERE intl_available = TRUE
    Format filter: AND intl_t20i_status = 'Active'
    Exclude      : Retired + Out 2Y+ players
============================================================
"""

with open(REPORT_PATH, "w", encoding="utf-8") as f:
    f.write(report)

log(report)
log(f"  Log    → {LOG_PATH}")
log(f"  Report → {REPORT_PATH}")
log()
log("  Done. ✓")

log_file.close()
sys.exit(0)
