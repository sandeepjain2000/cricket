#!/usr/bin/env python3
"""
Cricket T20 & LOI Downloader — POSTGRES TARGET
Writes directly to per-format Postgres databases (one DB for T20, one for LOI).
Connection strings come from config/settings.ini [postgres] section, with
env-var overrides:
    PG_URL_T20
    PG_URL_LOI
Later these URLs can point to separate Supabase projects — no code change needed.

Schema (per DB) aligns with what the Next.js rankings-t20 / rankings-loi
API routes expect:
    matches         (match_id, city, start_date, venue, team1, team2,
                     toss_winner, toss_decision, winner, result,
                     result_margin, player_of_match)
    match_players   (match_id, player_name, team)
    deliveries      (delivery_id serial, match_id, inning_number, over_number,
                     delivery_number, batter, bowler, non_striker,
                     runs_batter, runs_extras, runs_total,
                     wicket_player_out, wicket_kind)

Usage:
    python cricket_t20_loi_postgres.py            # T20 and LOI
    python cricket_t20_loi_postgres.py --only t20
    python cricket_t20_loi_postgres.py --only loi
"""

import os
import sys
import json
import argparse
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile
from configparser import ConfigParser

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("ERROR: psycopg2 is required. Install with:")
    print("    pip install psycopg2-binary")
    sys.exit(2)

SCRIPT_NAME = "cricket_t20_loi_postgres.py"
VERSION = "1.0.0"
START_TIME = datetime.now()

os.system('cls' if os.name == 'nt' else 'clear')

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH = OUTPUT_DIR / f"{SCRIPT_NAME.replace('.py', '')}_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"{SCRIPT_NAME.replace('.py', '')}_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

log_file = open(LOG_PATH, 'w', encoding='utf-8')


def log(msg):
    print(msg)
    log_file.write(msg + '\n')
    log_file.flush()


log("=" * 70)
log(f"  {SCRIPT_NAME}  v{VERSION}")
log(f"  T20 & LOI Downloader — Postgres target")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 70)
log("")

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = SCRIPT_DIR / "config" / "settings.ini"
config = ConfigParser()
config.read(CONFIG_PATH)

CRICSHEET_URL_T20 = config.get("cricsheet", "url_t20",
                              fallback="https://cricsheet.org/downloads/t20s_json.zip")
CRICSHEET_URL_LOI = config.get("cricsheet", "url_loi",
                              fallback="https://cricsheet.org/downloads/odis_json.zip")
REQUEST_TIMEOUT = config.getint("cricsheet", "request_timeout_s", fallback=120)

# Postgres connection strings — env vars take priority over config
PG_URL_T20 = os.environ.get("PG_URL_T20") or config.get("postgres", "url_t20", fallback=None)
PG_URL_LOI = os.environ.get("PG_URL_LOI") or config.get("postgres", "url_loi", fallback=None)

# Tuning: how many rows to batch-insert into deliveries at a time
BATCH_SIZE = config.getint("postgres", "batch_size", fallback=5000)

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
DROP TABLE IF EXISTS deliveries CASCADE;
DROP TABLE IF EXISTS match_players CASCADE;
DROP TABLE IF EXISTS matches CASCADE;

CREATE TABLE matches (
    match_id          TEXT PRIMARY KEY,
    city              TEXT,
    start_date        TEXT,
    venue             TEXT,
    team1             TEXT,
    team2             TEXT,
    toss_winner       TEXT,
    toss_decision     TEXT,
    winner            TEXT,
    result            TEXT,
    result_margin     INTEGER,
    player_of_match   TEXT
);

CREATE TABLE match_players (
    match_id          TEXT NOT NULL,
    player_name       TEXT NOT NULL,
    team              TEXT,
    PRIMARY KEY (match_id, player_name),
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE TABLE deliveries (
    delivery_id       SERIAL PRIMARY KEY,
    match_id          TEXT NOT NULL,
    inning_number     INTEGER,
    over_number       INTEGER,
    delivery_number   INTEGER,
    batter            TEXT,
    bowler            TEXT,
    non_striker       TEXT,
    runs_batter       INTEGER,
    runs_extras       INTEGER,
    runs_total        INTEGER,
    wicket_player_out TEXT,
    wicket_kind       TEXT,
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX idx_deliveries_match     ON deliveries (match_id);
CREATE INDEX idx_deliveries_batter    ON deliveries (batter);
CREATE INDEX idx_deliveries_bowler    ON deliveries (bowler);
CREATE INDEX idx_matches_start_date   ON matches (start_date);
CREATE INDEX idx_mp_player            ON match_players (player_name);
CREATE INDEX idx_mp_team              ON match_players (team);
"""


# ── Processing ───────────────────────────────────────────────────────────────

def process_format(format_name: str, pg_url: str, zip_url: str, stats: dict):
    """Download one Cricsheet zip and load it into the target Postgres DB."""
    log("=" * 70)
    log(f"DOWNLOADING {format_name}")
    log("=" * 70)
    log("")

    if not pg_url:
        log(f"⚠ No Postgres URL configured for {format_name}. Set env PG_URL_{format_name} "
            f"or add [postgres] url_{format_name.lower()} to settings.ini. Skipping.")
        stats['errors'] += 1
        return

    local_stats = {'matches': 0, 'deliveries': 0, 'match_players': 0, 'errors': 0}

    try:
        log(f"Connecting to Postgres: {_safe_url(pg_url)}")
        conn = psycopg2.connect(pg_url)
        conn.autocommit = False
        cur = conn.cursor()

        log("Creating schema (DROP + CREATE)...")
        cur.execute(SCHEMA_SQL)
        conn.commit()

        log(f"Downloading from {zip_url}...")
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / f"{format_name.lower()}.zip"
            with urlopen(zip_url, timeout=REQUEST_TIMEOUT) as resp:
                with open(zip_path, 'wb') as f:
                    f.write(resp.read())
            log(f"✓ Downloaded {zip_path.stat().st_size / (1024 * 1024):.1f} MB")

            extract_dir = Path(temp_dir) / "extract"
            with ZipFile(zip_path) as z:
                z.extractall(extract_dir)
            json_files = sorted(extract_dir.glob("**/*.json"))
            log(f"✓ Extracted {len(json_files)} JSON files")
            log("")

            # Batch buffers
            match_rows = []
            mp_rows = []
            delivery_rows = []

            def flush_deliveries():
                if not delivery_rows:
                    return
                execute_values(cur, """
                    INSERT INTO deliveries (
                        match_id, inning_number, over_number, delivery_number,
                        batter, bowler, non_striker,
                        runs_batter, runs_extras, runs_total,
                        wicket_player_out, wicket_kind
                    ) VALUES %s
                """, delivery_rows, page_size=1000)
                delivery_rows.clear()

            def flush_matches_and_players():
                if match_rows:
                    execute_values(cur, """
                        INSERT INTO matches (
                            match_id, city, start_date, venue, team1, team2,
                            toss_winner, toss_decision, winner, result
                        ) VALUES %s
                        ON CONFLICT (match_id) DO NOTHING
                    """, match_rows, page_size=500)
                    match_rows.clear()
                if mp_rows:
                    execute_values(cur, """
                        INSERT INTO match_players (match_id, player_name, team)
                        VALUES %s
                        ON CONFLICT (match_id, player_name) DO NOTHING
                    """, mp_rows, page_size=1000)
                    mp_rows.clear()

            for idx, json_file in enumerate(json_files, 1):
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        match_data = json.load(f)

                    info = match_data.get("info", {})
                    match_id = str(info.get("match_type_number", f"{format_name.lower()}_{idx}"))
                    dates = info.get("dates", [])
                    start_date = dates[0] if dates else None
                    teams = info.get("teams", [None, None])
                    if len(teams) < 2:
                        teams = (teams + [None, None])[:2]

                    match_rows.append((
                        match_id,
                        info.get("city"),
                        start_date,
                        info.get("venue"),
                        teams[0],
                        teams[1],
                        (info.get("toss") or {}).get("winner"),
                        (info.get("toss") or {}).get("decision"),
                        (info.get("outcome") or {}).get("winner"),
                        (info.get("outcome") or {}).get("result"),
                    ))
                    local_stats['matches'] += 1

                    for team_name, player_names in (info.get("players") or {}).items():
                        for pname in player_names or []:
                            mp_rows.append((match_id, pname, team_name))
                            local_stats['match_players'] += 1

                    # innings -> overs -> deliveries
                    for inning_num, inning_data in enumerate(match_data.get("innings", []), 1):
                        for over_data in inning_data.get("overs", []):
                            over_num = over_data.get("over", 0)
                            for ball_idx, d in enumerate(over_data.get("deliveries", []), 1):
                                runs = d.get("runs", {}) or {}
                                wickets = d.get("wickets") or []
                                w0 = wickets[0] if wickets else {}
                                delivery_rows.append((
                                    match_id,
                                    inning_num,
                                    int(over_num),
                                    int(ball_idx),
                                    d.get("batter"),
                                    d.get("bowler"),
                                    d.get("non_striker"),
                                    int(runs.get("batter", 0) or 0),
                                    int(runs.get("extras", 0) or 0),
                                    int(runs.get("total", 0) or 0),
                                    w0.get("player_out"),
                                    w0.get("kind"),
                                ))
                                local_stats['deliveries'] += 1

                    # Periodic flush + commit
                    if len(delivery_rows) >= BATCH_SIZE:
                        flush_matches_and_players()
                        flush_deliveries()
                        conn.commit()

                    if idx % 250 == 0:
                        log(f"  {idx:5d}/{len(json_files)} - "
                            f"{local_stats['matches']:6d} matches, "
                            f"{local_stats['deliveries']:8d} deliveries")

                except Exception as e:
                    log(f"  ⚠ Error in {json_file.name}: {str(e)[:80]}")
                    local_stats['errors'] += 1

            # Final flush
            flush_matches_and_players()
            flush_deliveries()
            conn.commit()

            log("")
            log(f"✓ {format_name}: {local_stats['matches']} matches, "
                f"{local_stats['match_players']} match_players rows, "
                f"{local_stats['deliveries']} deliveries")

            # Vacuum + analyze for query plans
            old_iso = conn.isolation_level
            conn.set_isolation_level(0)  # autocommit for VACUUM
            try:
                cur.execute("VACUUM ANALYZE matches;")
                cur.execute("VACUUM ANALYZE match_players;")
                cur.execute("VACUUM ANALYZE deliveries;")
            finally:
                conn.set_isolation_level(old_iso)

        cur.close()
        conn.close()

        stats['matches_inserted'] += local_stats['matches']
        stats['deliveries_inserted'] += local_stats['deliveries']
        stats['match_players_inserted'] += local_stats['match_players']
        stats['errors'] += local_stats['errors']

    except Exception as e:
        log(f"❌ ERROR processing {format_name}: {e}")
        stats['errors'] += 1


def _safe_url(url: str) -> str:
    """Mask the password when logging a Postgres URL."""
    try:
        if "://" not in url or "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        if ":" in creds:
            user, _pw = creds.split(":", 1)
            return f"{scheme}://{user}:***@{host}"
        return url
    except Exception:
        return "<unparseable url>"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["t20", "loi"], default=None,
                        help="Only load one format (default: both)")
    args = parser.parse_args()

    log(f"T20 Postgres URL: {_safe_url(PG_URL_T20) if PG_URL_T20 else '(not configured)'}")
    log(f"LOI Postgres URL: {_safe_url(PG_URL_LOI) if PG_URL_LOI else '(not configured)'}")
    log("")

    stats = {
        'matches_inserted': 0,
        'deliveries_inserted': 0,
        'match_players_inserted': 0,
        'errors': 0,
    }

    try:
        if args.only in (None, "t20"):
            process_format("T20", PG_URL_T20, CRICSHEET_URL_T20, stats)
            log("")
        if args.only in (None, "loi"):
            process_format("LOI", PG_URL_LOI, CRICSHEET_URL_LOI, stats)
            log("")
        log("=" * 70)
        log("✅ DONE" if stats['errors'] == 0 else "⚠ DONE WITH ERRORS")
        log("=" * 70)
    except KeyboardInterrupt:
        log("Interrupted by user.")
        stats['errors'] += 1
    except Exception as e:
        log(f"FATAL: {e}")
        stats['errors'] += 1

    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()

    report = f"""{"=" * 70}
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
{"=" * 70}
  Start Time : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time   : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration   : {duration:.1f} seconds
  Outcome    : {'SUCCESS' if stats['errors'] == 0 else 'FAILURE'}
{"-" * 70}
  Summary:
  - Matches inserted        : {stats['matches_inserted']}
  - Deliveries inserted     : {stats['deliveries_inserted']}
  - match_players inserted  : {stats['match_players_inserted']}
  - Errors encountered      : {stats['errors']}
{"-" * 70}
{"=" * 70}
"""

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)

    log("")
    log(report)
    log(f"Log file    : {LOG_PATH}")
    log(f"Report file : {REPORT_PATH}")
    log("Done.")
    log_file.close()

    sys.exit(0 if stats['errors'] == 0 else 1)


if __name__ == "__main__":
    main()
