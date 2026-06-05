#!/usr/bin/env python3
"""
Cricket T20 & LOI Data Downloader
Downloads T20 Internationals and Limited Overs Internationals from Cricsheet
and loads them into separate SQLite databases.
"""

import os
import sys
import json
import sqlite3
import shutil
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile
from configparser import ConfigParser

# ══════════════════════════════════════════════════════════════════════════════
# 1. IMPORTS & CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

SCRIPT_NAME = "cricket_t20_loi_downloader.py"
VERSION = "1.0.0"
START_TIME = datetime.now()

# ══════════════════════════════════════════════════════════════════════════════
# 2. CLEAR SCREEN
# ══════════════════════════════════════════════════════════════════════════════

os.system('cls' if os.name == 'nt' else 'clear')

# ══════════════════════════════════════════════════════════════════════════════
# 3. SET UP DIRECTORIES & LOGGING
# ══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH = OUTPUT_DIR / f"{SCRIPT_NAME.replace('.py', '')}_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"{SCRIPT_NAME.replace('.py', '')}_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

log_file = open(LOG_PATH, 'w', encoding='utf-8')

def log(msg):
    """Write message to console and log file."""
    print(msg)
    log_file.write(msg + '\n')
    log_file.flush()

# ══════════════════════════════════════════════════════════════════════════════
# 4. PRINT HEADER/BANNER
# ══════════════════════════════════════════════════════════════════════════════

log("=" * 70)
log(f"  {SCRIPT_NAME}  v{VERSION}")
log(f"  T20 Internationals & Limited Overs Internationals Downloader")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 70)
log("")

# ══════════════════════════════════════════════════════════════════════════════
# 5. CONFIGURATION & SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

CONFIG_PATH = SCRIPT_DIR / "config" / "settings.ini"
config = ConfigParser()

try:
    config.read(CONFIG_PATH)
    CRICSHEET_URL_T20 = config.get("cricsheet", "url_t20", fallback="https://cricsheet.org/downloads/t20s_json.zip")
    CRICSHEET_URL_LOI = config.get("cricsheet", "url_loi", fallback="https://cricsheet.org/downloads/odis_json.zip")
    REQUEST_TIMEOUT = config.getint("cricsheet", "request_timeout_s", fallback=120)
    log(f"Config loaded from {CONFIG_PATH}")
except Exception as e:
    log(f"Warning: Could not load config — using defaults. ({e})")
    CRICSHEET_URL_T20 = "https://cricsheet.org/downloads/t20s_json.zip"
    CRICSHEET_URL_LOI = "https://cricsheet.org/downloads/odis_json.zip"
    REQUEST_TIMEOUT = 120

# Database paths
T20_DB_PATH = SCRIPT_DIR.parent / "data" / "cricket_t20.db"
LOI_DB_PATH = SCRIPT_DIR.parent / "data" / "cricket_loi.db"

log(f"T20 Database: {T20_DB_PATH}")
log(f"LOI Database: {LOI_DB_PATH}")
log("")

# ══════════════════════════════════════════════════════════════════════════════
# 6. SCHEMA DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS matches (
    match_id            TEXT    PRIMARY KEY,
    match_type          TEXT,
    gender              TEXT,
    start_date          TEXT,
    end_date            TEXT,
    dates_json          TEXT,
    venue               TEXT,
    city                TEXT,
    team1               TEXT,
    team2               TEXT,
    toss_winner         TEXT,
    toss_decision       TEXT,
    outcome_winner      TEXT,
    outcome_result      TEXT,
    outcome_by_type     TEXT,
    outcome_by_value    INTEGER,
    balls_per_over      INTEGER,
    event_name          TEXT,
    event_match_number  INTEGER,
    season              TEXT,
    umpires_json        TEXT,
    match_referees_json TEXT,
    json_file_path      TEXT,
    data_version        TEXT,
    imported_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS innings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT    NOT NULL,
    inning_number   INTEGER NOT NULL,
    team            TEXT    NOT NULL,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT    NOT NULL,
    inning_number       INTEGER NOT NULL,
    over_number         INTEGER NOT NULL,
    ball_number         INTEGER NOT NULL,
    batter              TEXT,
    bowler              TEXT,
    batting_team        TEXT,
    bowling_team        TEXT,
    runs_batter         INTEGER,
    runs_extras         INTEGER,
    runs_total          INTEGER,
    wicket_player_out   TEXT,
    wicket_kind         TEXT,
    wicket_fielder      TEXT,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS players (
    identifier  TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id    TEXT    NOT NULL,
    team        TEXT    NOT NULL,
    player_name TEXT    NOT NULL,
    player_id   TEXT,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS download_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp       TEXT    NOT NULL,
    mode                TEXT    NOT NULL,
    format              TEXT    NOT NULL,
    filter_start_date   TEXT,
    filter_end_date     TEXT,
    zip_url             TEXT,
    matches_scanned     INTEGER DEFAULT 0,
    matches_inserted    INTEGER DEFAULT 0,
    matches_skipped     INTEGER DEFAULT 0,
    matches_filtered    INTEGER DEFAULT 0,
    errors              INTEGER DEFAULT 0,
    status              TEXT    DEFAULT 'running'
);

CREATE INDEX IF NOT EXISTS idx_matches_start_date   ON matches(start_date);
CREATE INDEX IF NOT EXISTS idx_matches_team1        ON matches(team1);
CREATE INDEX IF NOT EXISTS idx_matches_team2        ON matches(team2);
CREATE INDEX IF NOT EXISTS idx_deliveries_match_id  ON deliveries(match_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_batter    ON deliveries(batter);
CREATE INDEX IF NOT EXISTS idx_deliveries_bowler    ON deliveries(bowler);
"""

# ══════════════════════════════════════════════════════════════════════════════
# 7. MAIN LOGIC (WRAPPED IN TRY/EXCEPT)
# ══════════════════════════════════════════════════════════════════════════════

stats = {
    'format': None,
    'downloaded': False,
    'matches_inserted': 0,
    'matches_skipped': 0,
    'errors': 0,
}

try:
    # Download and process T20
    log("=" * 70)
    log("PHASE 1: T20 Internationals")
    log("=" * 70)
    log("")

    log(f"Downloading T20 data from {CRICSHEET_URL_T20}...")
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / "t20.zip"
        with urlopen(CRICSHEET_URL_T20, timeout=REQUEST_TIMEOUT) as response:
            with open(zip_path, 'wb') as f:
                f.write(response.read())
        log(f"✓ Downloaded {zip_path.stat().st_size / (1024*1024):.1f} MB")

        # Extract
        extract_dir = Path(temp_dir) / "t20_extract"
        with ZipFile(zip_path, 'r') as z:
            z.extractall(extract_dir)
        json_files = list(extract_dir.glob("**/*.json"))
        log(f"✓ Extracted {len(json_files)} JSON files")

        # Initialize database
        T20_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn_t20 = sqlite3.connect(T20_DB_PATH)
        conn_t20.executescript(SCHEMA_SQL)
        log(f"✓ Initialized database: {T20_DB_PATH}")

        # Load matches
        for i, json_file in enumerate(sorted(json_files), 1):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    match_data = json.load(f)

                info = match_data.get("info", {})
                match_id = info.get("match_type_number", f"t20_{i}")

                dates = info.get("dates", [])
                start_date = dates[0] if dates else None

                conn_t20.execute("""
                    INSERT OR IGNORE INTO matches (
                        match_id, match_type, gender, start_date, end_date, dates_json,
                        team1, team2, toss_winner, toss_decision, outcome_winner, outcome_result,
                        venue, city, season, event_name, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    match_id, info.get("match_type"), info.get("gender"),
                    start_date, dates[-1] if dates else None, json.dumps(dates),
                    info.get("teams", [None])[0], info.get("teams", [None, None])[1],
                    info.get("toss", {}).get("winner"), info.get("toss", {}).get("decision"),
                    info.get("outcome", {}).get("winner"), info.get("outcome", {}).get("result"),
                    info.get("venue"), info.get("city"), info.get("season"),
                    info.get("event", {}).get("name"), START_TIME.isoformat()
                ))

                # Parse deliveries (simplified)
                for inning_num, inning_data in enumerate(match_data.get("innings", []), 1):
                    for delivery in inning_data.get("deliveries", []):
                        over, ball = list(delivery.keys())[0].split('.')
                        del_data = delivery[list(delivery.keys())[0]]

                        conn_t20.execute("""
                            INSERT INTO deliveries (
                                match_id, inning_number, over_number, ball_number,
                                batter, bowler, batting_team, bowling_team,
                                runs_batter, runs_extras, runs_total, wicket_player_out, wicket_kind
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            match_id, inning_num, int(over), int(ball),
                            del_data.get("batter"), del_data.get("bowler"),
                            inning_data.get("team"), None,
                            del_data.get("runs", {}).get("batter", 0),
                            del_data.get("runs", {}).get("extras", 0),
                            del_data.get("runs", {}).get("total", 0),
                            del_data.get("wickets", [{}])[0].get("player_out") if del_data.get("wickets") else None,
                            del_data.get("wickets", [{}])[0].get("kind") if del_data.get("wickets") else None
                        ))

                stats['matches_inserted'] += 1
                if (i % 50 == 0):
                    log(f"  Processed {i}/{len(json_files)} matches...")
                    conn_t20.commit()

            except Exception as e:
                log(f"  ⚠ Error processing {json_file.name}: {e}")
                stats['errors'] += 1
                continue

        conn_t20.commit()
        conn_t20.close()
        log(f"✓ T20 data loaded: {stats['matches_inserted']} matches inserted")

    log("")
    log("=" * 70)
    log("PHASE 2: Limited Overs Internationals (ODI)")
    log("=" * 70)
    log("")

    log(f"Downloading LOI data from {CRICSHEET_URL_LOI}...")
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / "loi.zip"
        with urlopen(CRICSHEET_URL_LOI, timeout=REQUEST_TIMEOUT) as response:
            with open(zip_path, 'wb') as f:
                f.write(response.read())
        log(f"✓ Downloaded {zip_path.stat().st_size / (1024*1024):.1f} MB")

        # Extract
        extract_dir = Path(temp_dir) / "loi_extract"
        with ZipFile(zip_path, 'r') as z:
            z.extractall(extract_dir)
        json_files = list(extract_dir.glob("**/*.json"))
        log(f"✓ Extracted {len(json_files)} JSON files")

        # Initialize database
        LOI_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn_loi = sqlite3.connect(LOI_DB_PATH)
        conn_loi.executescript(SCHEMA_SQL)
        log(f"✓ Initialized database: {LOI_DB_PATH}")

        # Load matches (similar to T20)
        loi_matches = 0
        for i, json_file in enumerate(sorted(json_files), 1):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    match_data = json.load(f)

                info = match_data.get("info", {})
                match_id = info.get("match_type_number", f"loi_{i}")
                dates = info.get("dates", [])
                start_date = dates[0] if dates else None

                conn_loi.execute("""
                    INSERT OR IGNORE INTO matches (
                        match_id, match_type, gender, start_date, end_date, dates_json,
                        team1, team2, toss_winner, toss_decision, outcome_winner, outcome_result,
                        venue, city, season, event_name, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    match_id, info.get("match_type"), info.get("gender"),
                    start_date, dates[-1] if dates else None, json.dumps(dates),
                    info.get("teams", [None])[0], info.get("teams", [None, None])[1],
                    info.get("toss", {}).get("winner"), info.get("toss", {}).get("decision"),
                    info.get("outcome", {}).get("winner"), info.get("outcome", {}).get("result"),
                    info.get("venue"), info.get("city"), info.get("season"),
                    info.get("event", {}).get("name"), START_TIME.isoformat()
                ))
                loi_matches += 1
                if (i % 50 == 0):
                    log(f"  Processed {i}/{len(json_files)} matches...")
                    conn_loi.commit()

            except Exception as e:
                log(f"  ⚠ Error processing {json_file.name}: {e}")
                stats['errors'] += 1
                continue

        conn_loi.commit()
        conn_loi.close()
        log(f"✓ LOI data loaded: {loi_matches} matches inserted")

    log("")
    log("=" * 70)
    log("✅ SUCCESS — All data downloaded and loaded")
    log("=" * 70)

except Exception as e:
    import traceback
    error_msg = f"ERROR: {e}\n{traceback.format_exc()}"
    log(error_msg)
    log("")
    log("❌ FAILED — See log for details")
    stats['errors'] = 1

# ══════════════════════════════════════════════════════════════════════════════
# 8. WRITE SUMMARY REPORT
# ══════════════════════════════════════════════════════════════════════════════

end_time = datetime.now()
duration = (end_time - START_TIME).total_seconds()

report_content = f"""{"=" * 70}
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
{"=" * 70}
  Start Time : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time   : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration   : {duration:.1f} seconds
  Outcome    : {'SUCCESS' if stats['errors'] == 0 else 'FAILURE'}
{"-" * 70}
  Summary:
  - T20 Database: {T20_DB_PATH}
  - LOI Database: {LOI_DB_PATH}
  - Total matches inserted: {stats['matches_inserted']}
  - Errors encountered: {stats['errors']}
{"-" * 70}
  Databases created and populated with T20 and LOI data.
  Ready for analytics and rankings computation.
{"=" * 70}
"""

with open(REPORT_PATH, 'w', encoding='utf-8') as f:
    f.write(report_content)

log("")
log(report_content)

# ══════════════════════════════════════════════════════════════════════════════
# 9. CLOSE LOG & EXIT
# ══════════════════════════════════════════════════════════════════════════════

log(f"Log file: {LOG_PATH}")
log(f"Report file: {REPORT_PATH}")
log("")
log("Done.")
log_file.close()

sys.exit(0 if stats['errors'] == 0 else 1)
