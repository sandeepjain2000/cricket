#!/usr/bin/env python3
"""
================================================================================
  populate_delivery_dimensions.py  v1.0.0
  T20 Delivery Dimensions Populator
================================================================================

PURPOSE
  After running:
    1. cricket_downloader.py  (loads match ball-by-ball data into SQLite)
    2. scrape_player_profiles.py  (loads player bowling/batting styles)

  This script computes and inserts all per-delivery analytics labels into
  the delivery_dimensions table, enabling instant slice-and-dice queries.

  For each delivery it calculates:
    • over_phase          (Powerplay / Middle / Death)
    • innings_role        (Batting First / Batting Second)
    • bowler_bowling_type (from player_profiles)
    • bowler_pace_or_spin (Pace / Spin / NA)
    • batter_is_captain   (from player_match_roles)
    • venue_context       (Home / Away / Neutral)
    • competition_type    (Bilateral / Tournament / Domestic / Unknown)

USAGE
  python populate_delivery_dimensions.py            # full rebuild
  python populate_delivery_dimensions.py --incremental   # only new deliveries

SUPABASE
  Set SUPABASE_URL + SUPABASE_KEY env vars or config/settings.ini [supabase]
  to also push the dimensions table to Supabase after local build.
================================================================================
"""

import os
import sys
import sqlite3
import traceback
import re
import json
import argparse
from datetime import datetime
from pathlib import Path
from configparser import ConfigParser

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_OK = True
except ImportError:
    PSYCOPG2_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_NAME = "populate_delivery_dimensions.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR   = Path(__file__).parent.resolve()
OUTPUT_DIR   = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"populate_dd_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"populate_dd_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

LOCAL_DB    = SCRIPT_DIR / "db" / "cricket.db"
CONFIG_PATH = SCRIPT_DIR / "config" / "settings.ini"

BATCH_SIZE  = 10_000

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
log(f"  T20 Delivery Dimensions Populator")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 72)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  (auto-reads cricket_ui/.env.local)
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

# T20 DB is the primary target; fall back to Test DB
SUPABASE_URL_T20 = _cfg("SUPABASE_URL_T20")
SUPABASE_URL     = _cfg("SUPABASE_URL")
PG_URL           = SUPABASE_URL_T20 or SUPABASE_URL

# ─────────────────────────────────────────────────────────────────────────────
#  DIMENSION COMPUTATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def over_phase(over_number: int) -> str:
    """
    over_number is 0-indexed in the JSON (over 0 = 1st over).
    We convert to 1-indexed for human-readable logic.
    """
    ov = int(over_number) + 1  # convert 0-indexed → 1-indexed over number
    if ov <= 6:
        return 'Powerplay'
    elif ov <= 15:
        return 'Middle'
    else:
        return 'Death'


def innings_role(inning_number: int) -> str:
    return 'Batting First' if int(inning_number) == 1 else 'Batting Second'


def pace_or_spin(bowling_type: str | None) -> str:
    if not bowling_type or bowling_type == 'NA':
        return 'NA'
    bt = bowling_type.lower()
    if any(k in bt for k in ('fast', 'medium')):
        return 'Pace'
    if any(k in bt for k in ('break', 'orthodox', 'unorthodox', 'spin')):
        return 'Spin'
    return 'NA'


# Known ICC / major multi-team tournaments (partial list — extend as needed)
TOURNAMENT_PATTERNS = re.compile(
    r'world cup|world twenty|icc|asia cup|champions trophy|tri.series|'
    r'commonwealth|nidahas|triangular|quadrangular|bilateral.+cup',
    re.I
)
DOMESTIC_PATTERNS = re.compile(
    r'ipl|bbl|cpl|psl|sa20|hundre|blast|t20 league|big bash|supersmash|'
    r'mushrafe|t10|the hundred|lpl|mpl|ctb|slt20|ilts20',
    re.I
)

def competition_type(event_name: str | None, series_name: str | None) -> str:
    label = f"{event_name or ''} {series_name or ''}".strip()
    if not label:
        return 'Bilateral'   # no event name → assume bilateral international T20
    if DOMESTIC_PATTERNS.search(label):
        return 'Domestic'
    if TOURNAMENT_PATTERNS.search(label):
        return 'Tournament'
    # Bilateral heuristic: "X vs Y" with no tournament keywords
    if re.search(r'\bvs\b|\bv\b', label, re.I):
        return 'Bilateral'
    return 'Unknown'


# Country → typical home grounds mapping (extend as needed)
COUNTRY_VENUES = {
    'India':        ['mumbai', 'delhi', 'bangalore', 'kolkata', 'chennai', 'hyderabad',
                     'ahmedabad', 'pune', 'rajkot', 'indore', 'nagpur', 'dharamsala',
                     'lucknow', 'cuttack', 'ranchi', 'kanpur', 'mohali', 'guwahati',
                     'visakhapatnam', 'thiruvananthapuram'],
    'Australia':    ['sydney', 'melbourne', 'brisbane', 'perth', 'adelaide', 'hobart',
                     'canberra', 'gold coast', 'darwin'],
    'England':      ['london', 'manchester', 'birmingham', 'nottingham', 'cardiff',
                     'chester-le-street', 'leeds', 'southampton', 'bristol', 'taunton',
                     'edgbaston', 'lord', 'oval', "headingley"],
    'Pakistan':     ['karachi', 'lahore', 'rawalpindi', 'multan', 'faisalabad', 'peshawar'],
    'South Africa': ['johannesburg', 'cape town', 'durban', 'pretoria', 'centurion',
                     'east london', 'port elizabeth', 'bloemfontein', 'gqeberha'],
    'West Indies':  ['bridgetown', 'kingston', 'port of spain', 'providence', 'basseterre',
                     'north sound', 'gros islet', 'tarouba'],
    'New Zealand':  ['auckland', 'wellington', 'christchurch', 'hamilton', 'napier',
                     'dunedin', 'nelson', 'mount maunganui'],
    'Sri Lanka':    ['colombo', 'kandy', 'pallekele', 'galle', 'dambulla'],
    'Bangladesh':   ['dhaka', 'chittagong', 'mirpur', 'sylhet'],
    'Zimbabwe':     ['harare', 'bulawayo'],
    'Afghanistan':  ['kabul', 'sharjah', 'greater noida', 'lucknow'],
    'UAE':          ['dubai', 'abu dhabi', 'sharjah'],
}


def venue_context(city: str | None, venue: str | None, team: str | None) -> str:
    """
    Returns 'Home', 'Away', or 'Neutral'.
    Neutral = venue not in any team's home city list.
    We only assign Home/Away if we can match the city to the team's country.
    """
    loc = (f"{city or ''} {venue or ''}").lower()
    if not loc.strip():
        return 'Neutral'

    # Find which country owns this venue
    host_country = None
    for country, cities in COUNTRY_VENUES.items():
        if any(c in loc for c in cities):
            host_country = country
            break

    if host_country is None:
        return 'Neutral'

    # Match team name fragment to country
    if not team:
        return 'Neutral'
    t = team.lower()

    # Simplistic matching — covers ICC team names
    country_to_teams = {
        'India':        ['india'],
        'Australia':    ['australia'],
        'England':      ['england'],
        'Pakistan':     ['pakistan'],
        'South Africa': ['south africa'],
        'West Indies':  ['west indies'],
        'New Zealand':  ['new zealand'],
        'Sri Lanka':    ['sri lanka'],
        'Bangladesh':   ['bangladesh'],
        'Zimbabwe':     ['zimbabwe'],
        'Afghanistan':  ['afghanistan'],
        'UAE':          ['united arab emirates', 'u.a.e', 'uae'],
    }

    team_country = None
    for country, aliases in country_to_teams.items():
        if any(a in t for a in aliases):
            team_country = country
            break

    if team_country is None:
        return 'Neutral'

    if team_country == host_country:
        return 'Home'
    else:
        return 'Away'


def derive_batter_team(mm: dict, inn_num: int) -> str:
    """
    When batter_team is unknown (match_players table has no team column),
    infer it from toss data: whichever team batted in the given inning.
      toss_decision='bat'   → toss_winner batted in inning 1
      toss_decision='field' → toss_winner batted in inning 2
    """
    team1        = mm.get("team1", "")
    team2        = mm.get("team2", "")
    toss_winner  = mm.get("toss_winner", "")
    toss_decision = (mm.get("toss_decision") or "").lower()

    if not team1 or not team2 or not toss_winner:
        return ""

    other_team = team2 if toss_winner == team1 else team1

    if toss_decision in ("bat", "batting"):
        batting_first, batting_second = toss_winner, other_team
    elif toss_decision in ("field", "fielding", "bowl", "bowling"):
        batting_first, batting_second = other_team, toss_winner
    else:
        return ""

    return batting_first if int(inn_num) == 1 else batting_second


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEMA SETUP
# ─────────────────────────────────────────────────────────────────────────────

DD_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_dimensions (
    delivery_id             INTEGER  NOT NULL PRIMARY KEY,
    match_id                TEXT     NOT NULL,
    over_phase              TEXT     NOT NULL,
    innings_role            TEXT     NOT NULL,
    bowler_bowling_type     TEXT,
    bowler_bowling_subtype  TEXT,
    bowler_bowling_arm      TEXT,
    bowler_pace_or_spin     TEXT,
    batter_is_captain       INTEGER  NOT NULL DEFAULT 0,
    batter_is_vice_captain  INTEGER  NOT NULL DEFAULT 0,
    batter_is_post_captaincy INTEGER NOT NULL DEFAULT 0,
    batter_team_captain     TEXT,
    competition_type        TEXT,
    venue_context           TEXT,
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dd_match       ON delivery_dimensions (match_id);
CREATE INDEX IF NOT EXISTS idx_dd_phase       ON delivery_dimensions (over_phase);
CREATE INDEX IF NOT EXISTS idx_dd_innings     ON delivery_dimensions (innings_role);
CREATE INDEX IF NOT EXISTS idx_dd_bowl_type   ON delivery_dimensions (bowler_bowling_type);
CREATE INDEX IF NOT EXISTS idx_dd_pace_spin   ON delivery_dimensions (bowler_pace_or_spin);
CREATE INDEX IF NOT EXISTS idx_dd_venue       ON delivery_dimensions (venue_context);
CREATE INDEX IF NOT EXISTS idx_dd_comp_type   ON delivery_dimensions (competition_type);
CREATE INDEX IF NOT EXISTS idx_dd_is_captain  ON delivery_dimensions (batter_is_captain);

CREATE TABLE IF NOT EXISTS player_match_roles (
    match_id                TEXT    NOT NULL,
    player_name             TEXT    NOT NULL,
    team                    TEXT    NOT NULL,
    is_captain              INTEGER NOT NULL DEFAULT 0,
    is_vice_captain         INTEGER NOT NULL DEFAULT 0,
    is_post_captaincy       INTEGER NOT NULL DEFAULT 0,
    team_captain            TEXT,
    PRIMARY KEY (match_id, player_name),
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pmr_player      ON player_match_roles (player_name);
CREATE INDEX IF NOT EXISTS idx_pmr_captain     ON player_match_roles (team_captain);

CREATE TABLE IF NOT EXISTS match_meta_t20 (
    match_id                TEXT    NOT NULL,
    team                    TEXT    NOT NULL,
    captain                 TEXT,
    vice_captain            TEXT,
    competition_type        TEXT,
    competition_name        TEXT,
    series_name             TEXT,
    venue_context           TEXT,
    home_country            TEXT,
    venue                   TEXT,
    city                    TEXT,
    PRIMARY KEY (match_id, team),
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mmt_captain    ON match_meta_t20 (captain);
CREATE INDEX IF NOT EXISTS idx_mmt_comp_type  ON match_meta_t20 (competition_type);
"""

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN BUILD LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def open_pg(autocommit=False):
    """Open a psycopg2 connection to the Supabase T20 Postgres DB."""
    if not PSYCOPG2_OK:
        log("  ✗ psycopg2 not installed. Run:")
        log("    pip install psycopg2-binary --break-system-packages")
        sys.exit(1)
    if not PG_URL:
        log("  ✗ No SUPABASE_URL_T20 or SUPABASE_URL found in cricket_ui/.env.local")
        sys.exit(1)
    pg = psycopg2.connect(PG_URL)
    pg.autocommit = autocommit
    return pg


def ensure_tables(pg):
    """Create delivery_dimensions and supporting tables if they don't exist."""
    with pg.cursor() as cur:
        # Create table (no-op if already exists)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS delivery_dimensions (
                delivery_id             BIGINT   PRIMARY KEY,
                match_id                TEXT     NOT NULL,
                over_phase              TEXT     NOT NULL,
                innings_role            TEXT     NOT NULL,
                bowler_bowling_type     TEXT,
                bowler_bowling_subtype  TEXT,
                bowler_bowling_arm      TEXT,
                bowler_pace_or_spin     TEXT,
                batter_is_captain       BOOLEAN  DEFAULT FALSE,
                batter_is_vice_captain  BOOLEAN  DEFAULT FALSE,
                batter_is_post_captaincy BOOLEAN DEFAULT FALSE,
                batter_team_captain     TEXT,
                competition_type        TEXT,
                venue_context           TEXT,
                match_stage             TEXT
            )
        """)
        # Add any columns that may be missing from older table versions
        cur.execute("""
            ALTER TABLE delivery_dimensions
                ADD COLUMN IF NOT EXISTS match_stage TEXT
        """)
        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_match      ON delivery_dimensions (match_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_phase      ON delivery_dimensions (over_phase)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_bowl_type  ON delivery_dimensions (bowler_bowling_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_pace_spin  ON delivery_dimensions (bowler_pace_or_spin)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_venue      ON delivery_dimensions (venue_context)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_comp_type  ON delivery_dimensions (competition_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dd_stage      ON delivery_dimensions (match_stage)")
    pg.commit()
    log("  ✓ Tables verified / created")


def build_player_profile_cache(pg) -> dict:
    """Returns {player_name: {bowling_type, bowling_subtype, bowling_arm}}"""
    cache = {}
    try:
        with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT player_name, bowling_type, bowling_arm,
                          COALESCE(bowling_subtype_detail, bowling_subtype) AS bowling_subtype
                   FROM player_profiles"""
            )
            for row in cur:
                cache[row["player_name"]] = {
                    "bowling_type":    row["bowling_type"] or "NA",
                    "bowling_subtype": row["bowling_subtype"],
                    "bowling_arm":     row["bowling_arm"],
                }
    except Exception as e:
        log(f"  ⚠ player_profiles not loaded: {e}")
    log(f"    player_profiles  : {len(cache):,} players")
    return cache


def build_match_meta_cache(pg) -> dict:
    """Returns {match_id: {competition_type, city, venue, team1, team2}}"""
    cache = {}

    # First: discover what columns the matches table actually has
    try:
        with pg.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'matches'
                ORDER BY ordinal_position
            """)
            cols = {r[0] for r in cur.fetchall()}
        log(f"    matches columns  : {sorted(cols)}")
    except Exception as e:
        pg.rollback()
        log(f"  ⚠ Could not inspect matches table: {e}")
        log(f"    matches          : 0")
        return cache

    # Build SELECT based on available columns
    select_cols = ["match_id"]
    for c in ["city", "venue", "team1", "team2", "toss_winner", "toss_decision",
              "event_name", "series_name", "competition_name",
              "competition_type", "match_stage"]:
        if c in cols:
            select_cols.append(c)

    try:
        with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(f"SELECT {', '.join(select_cols)} FROM matches")
            for row in cur:
                r = dict(row)
                mid   = r["match_id"]
                event = r.get("event_name") or r.get("series_name") or r.get("competition_name") or ""
                # Prefer pre-enriched competition_type from matches table if available
                comp  = r.get("competition_type") or competition_type(event, None)
                cache[mid] = {
                    "competition_type": comp,
                    "match_stage":      r.get("match_stage") or "N/A",
                    "city":             r.get("city")         or "",
                    "venue":            r.get("venue")        or "",
                    "team1":            r.get("team1")        or "",
                    "team2":            r.get("team2")        or "",
                    "toss_winner":      r.get("toss_winner")  or "",
                    "toss_decision":    r.get("toss_decision") or "",
                }
    except Exception as e:
        pg.rollback()
        log(f"  ⚠ matches not loaded: {e}")

    log(f"    matches          : {len(cache):,}")
    return cache


def populate_dimensions(pg, pg_read, incremental: bool = False, row_limit: int = 0) -> dict:
    stats = {"total": 0, "inserted": 0, "errors": 0}

    log("  Loading lookup caches…")
    pp_cache    = build_player_profile_cache(pg_read)
    match_cache = build_match_meta_cache(pg_read)
    log()

    # Clear existing rows for full rebuild
    if not incremental:
        with pg.cursor() as cur:
            cur.execute("DELETE FROM delivery_dimensions")
        pg.commit()
        log("  Cleared existing delivery_dimensions rows")

    # Discover deliveries table columns
    with pg_read.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'deliveries'
            ORDER BY ordinal_position
        """)
        del_cols = {r[0] for r in cur.fetchall()}
    log(f"    deliveries cols  : {sorted(del_cols)}")

    # Find the primary key / row-id column
    pk_col = next((c for c in ["delivery_id", "id", "ball_id", "row_id"]
                   if c in del_cols), None)
    if not pk_col:
        # Last resort: use ctid (Postgres physical row ID) — always exists
        pk_col = None
        log("  ⚠ No numeric PK found — using ROW_NUMBER() as delivery_id")

    # Find inning / over / batter / bowler column names (handle variants)
    inning_col = next((c for c in ["inning_number","inning","innings","innings_number"] if c in del_cols), "inning_number")
    over_col   = next((c for c in ["over_number","over","over_num"] if c in del_cols), "over_number")
    batter_col = next((c for c in ["batter","batsman","striker"] if c in del_cols), "batter")
    bowler_col = next((c for c in ["bowler","bowling"] if c in del_cols), "bowler")

    log(f"    PK={pk_col}  inning={inning_col}  over={over_col}  batter={batter_col}  bowler={bowler_col}")

    if pk_col:
        id_expr   = f"d.{pk_col}"
        order_by  = f"d.{pk_col}"
    else:
        id_expr   = "ROW_NUMBER() OVER (ORDER BY d.match_id)"
        order_by  = "d.match_id"

    # Check if match_players table has team column
    mp_join = ""
    try:
        with pg_read.cursor() as cur:
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name='match_players'""")
            mp_cols = {r[0] for r in cur.fetchall()}
        if "team" in mp_cols and "player_name" in mp_cols:
            mp_join = f"""LEFT JOIN match_players mp
                   ON mp.match_id = d.match_id AND mp.player_name = d.{batter_col}"""
            team_select = "mp.team AS batter_team"
        else:
            team_select = "NULL AS batter_team"
    except Exception:
        pg_read.rollback()   # clear any aborted transaction before the main fetch
        team_select = "NULL AS batter_team"
        mp_join = ""

    # Fetch deliveries using keyset pagination (WHERE delivery_id > last_id LIMIT N).
    # A single SELECT on 1.1M rows hits Supabase's statement_timeout (~2 min);
    # each paginated chunk completes in seconds regardless of table size.
    log("  Fetching deliveries from Supabase (keyset pagination)…")

    # For incremental: only rows whose delivery_id isn't already in delivery_dimensions
    incremental_and = ""
    if incremental and pk_col:
        incremental_and = f"AND {id_expr} NOT IN (SELECT delivery_id FROM delivery_dimensions)"

    PAGE_SQL = f"""
        SELECT
            {id_expr}            AS delivery_id,
            d.match_id,
            d.{inning_col}       AS inning_number,
            d.{over_col}         AS over_number,
            d.{batter_col}       AS batter,
            d.{bowler_col}       AS bowler,
            {team_select}
        FROM deliveries d
        {mp_join}
        WHERE {id_expr} > %s
        {incremental_and}
        ORDER BY {id_expr}
        LIMIT %s
    """

    UPSERT_SQL = """
        INSERT INTO delivery_dimensions (
            delivery_id, match_id,
            over_phase, innings_role,
            bowler_bowling_type, bowler_bowling_subtype, bowler_bowling_arm, bowler_pace_or_spin,
            batter_is_captain, batter_is_vice_captain, batter_is_post_captaincy, batter_team_captain,
            competition_type, venue_context, match_stage
        ) VALUES (
            %(delivery_id)s, %(match_id)s,
            %(over_phase)s, %(innings_role)s,
            %(bowler_bowling_type)s, %(bowler_bowling_subtype)s,
            %(bowler_bowling_arm)s, %(bowler_pace_or_spin)s,
            %(batter_is_captain)s, %(batter_is_vice_captain)s,
            %(batter_is_post_captaincy)s, %(batter_team_captain)s,
            %(competition_type)s, %(venue_context)s, %(match_stage)s
        )
        ON CONFLICT (delivery_id) DO UPDATE SET
            over_phase              = EXCLUDED.over_phase,
            innings_role            = EXCLUDED.innings_role,
            bowler_bowling_type     = EXCLUDED.bowler_bowling_type,
            bowler_bowling_subtype  = EXCLUDED.bowler_bowling_subtype,
            bowler_bowling_arm      = EXCLUDED.bowler_bowling_arm,
            bowler_pace_or_spin     = EXCLUDED.bowler_pace_or_spin,
            batter_is_captain       = EXCLUDED.batter_is_captain,
            batter_is_vice_captain  = EXCLUDED.batter_is_vice_captain,
            batter_is_post_captaincy = EXCLUDED.batter_is_post_captaincy,
            batter_team_captain     = EXCLUDED.batter_team_captain,
            competition_type        = EXCLUDED.competition_type,
            venue_context           = EXCLUDED.venue_context,
            match_stage             = EXCLUDED.match_stage
    """

    batch = []
    write_cur = pg.cursor()
    last_id   = 0          # keyset pagination cursor

    def flush(rows):
        if not rows:
            return
        psycopg2.extras.execute_batch(write_cur, UPSERT_SQL, rows, page_size=500)
        pg.commit()

    while True:
        # Honour --test / --limit: stop once we've hit the row cap
        if row_limit and stats["total"] >= row_limit:
            log(f"  ⓘ Row limit ({row_limit:,}) reached — stopping early.")
            break

        page_size = BATCH_SIZE
        if row_limit:
            page_size = min(BATCH_SIZE, row_limit - stats["total"])

        with pg_read.cursor(cursor_factory=psycopg2.extras.DictCursor) as fetch_cur:
            fetch_cur.execute(PAGE_SQL, (last_id, page_size))
            chunk = fetch_cur.fetchall()

        if not chunk:
            break

        last_id = chunk[-1]["delivery_id"]   # advance the keyset cursor

        for row in chunk:
            stats["total"] += 1
            try:
                mid         = row["match_id"]
                bowler      = row["bowler"] or ""
                inn_num     = row["inning_number"] or 1
                over_num    = row["over_number"] or 0

                # Use join-supplied batter_team if available; fall back to toss derivation
                batter_team = row["batter_team"] or ""
                mm = match_cache.get(mid, {})
                if not batter_team:
                    batter_team = derive_batter_team(mm, inn_num)

                bp = pp_cache.get(bowler, {})
                vc = venue_context(mm.get("city",""), mm.get("venue",""), batter_team)

                batch.append({
                    "delivery_id":               row["delivery_id"],
                    "match_id":                  mid,
                    "over_phase":                over_phase(over_num),
                    "innings_role":              innings_role(inn_num),
                    "bowler_bowling_type":       bp.get("bowling_type", "NA"),
                    "bowler_bowling_subtype":    bp.get("bowling_subtype"),
                    "bowler_bowling_arm":        bp.get("bowling_arm"),
                    "bowler_pace_or_spin":       pace_or_spin(bp.get("bowling_type")),
                    "batter_is_captain":         False,
                    "batter_is_vice_captain":    False,
                    "batter_is_post_captaincy":  False,
                    "batter_team_captain":       None,
                    "competition_type":          mm.get("competition_type", "Bilateral"),
                    "venue_context":             vc,
                    "match_stage":               mm.get("match_stage", "N/A"),
                })
                stats["inserted"] += 1

            except Exception as exc:
                stats["errors"] += 1
                if stats["errors"] <= 10:
                    log(f"    ⚠ Row error: {exc}")

        flush(batch)
        batch = []
        log(f"    Progress: {stats['total']:,} deliveries processed…")

    write_cur.close()
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Populate T20 delivery dimensions table")
    parser.add_argument("--incremental", action="store_true",
                        help="Only process deliveries not yet in delivery_dimensions")
    parser.add_argument("--no-upload",   action="store_true",
                        help="Skip Supabase upload")
    parser.add_argument("--test",        action="store_true",
                        help="Quick smoke-test: process only the first 5,000 deliveries")
    parser.add_argument("--limit",       type=int, default=0,
                        help="Cap total deliveries processed (e.g. --limit 10000)")
    args = parser.parse_args()

    row_limit = 5_000 if args.test else (args.limit or 0)

    stats = {"total": 0, "inserted": 0, "errors": 0, "uploaded": 0}

    try:
        mode = "TEST (5 k rows)" if args.test else \
               f"LIMIT {row_limit:,}" if row_limit else \
               ("INCREMENTAL" if args.incremental else "FULL REBUILD")
        log(f"  Mode    : {mode}")
        log(f"  DB      : {(PG_URL or '')[:55]}…")
        log()

        pg      = open_pg(autocommit=False)   # write connection — commits in batches
        pg_read = open_pg(autocommit=False)   # read connection — stays in 1 long transaction, never committed
        ensure_tables(pg)

        stats.update(populate_dimensions(pg, pg_read,
                                         incremental=args.incremental,
                                         row_limit=row_limit))

        log()
        log("─" * 72)
        log(f"  Deliveries processed : {stats['total']:,}")
        log(f"  Rows inserted        : {stats['inserted']:,}")
        log(f"  Errors               : {stats['errors']:,}")

        pg.close()
        pg_read.close()

    except Exception as exc:
        log(f"\nFATAL: {exc}")
        log(traceback.format_exc())
        stats["errors"] += 1

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
  Summary:
  - Deliveries processed : {stats['total']:,}
  - Dimensions inserted  : {stats['inserted']:,}
  - Errors               : {stats['errors']:,}
  - Supabase rows        : {stats['uploaded']:,}
{"─" * 72}
  Database    : {LOCAL_DB}
  Log file    : {LOG_PATH}
{"=" * 72}
"""

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)

    log()
    log(report)
    log_file.close()

    # Beep to signal completion
    try:
        import winsound
        winsound.Beep(1000, 400)
        winsound.Beep(1200, 400)
    except Exception:
        print('\a', end='', flush=True)

    sys.exit(0 if stats['errors'] == 0 else 1)


if __name__ == "__main__":
    main()
