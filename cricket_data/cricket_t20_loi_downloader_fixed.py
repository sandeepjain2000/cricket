#!/usr/bin/env python3
"""
Cricket T20 & LOI Data Downloader - FIXED VERSION
Properly downloads and loads deliveries and player data
"""

import os
import sys
import json
import sqlite3
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile
from configparser import ConfigParser

SCRIPT_NAME = "cricket_t20_loi_downloader_fixed.py"
VERSION = "1.1.0"
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
log(f"  T20 & LOI Downloader (DELIVERIES FIXED)")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 70)
log("")

CONFIG_PATH = SCRIPT_DIR / "config" / "settings.ini"
config = ConfigParser()

try:
    config.read(CONFIG_PATH)
    CRICSHEET_URL_T20 = config.get("cricsheet", "url_t20", fallback="https://cricsheet.org/downloads/t20s_json.zip")
    CRICSHEET_URL_LOI = config.get("cricsheet", "url_loi", fallback="https://cricsheet.org/downloads/odis_json.zip")
    REQUEST_TIMEOUT = config.getint("cricsheet", "request_timeout_s", fallback=120)
except:
    CRICSHEET_URL_T20 = "https://cricsheet.org/downloads/t20s_json.zip"
    CRICSHEET_URL_LOI = "https://cricsheet.org/downloads/odis_json.zip"
    REQUEST_TIMEOUT = 120

T20_DB_PATH = SCRIPT_DIR.parent / "data" / "cricket_t20.db"
LOI_DB_PATH = SCRIPT_DIR.parent / "data" / "cricket_loi.db"

log(f"T20 Database: {T20_DB_PATH}")
log(f"LOI Database: {LOI_DB_PATH}")
log("")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS matches (match_id TEXT PRIMARY KEY, match_type TEXT, gender TEXT, start_date TEXT, end_date TEXT, dates_json TEXT, venue TEXT, city TEXT, team1 TEXT, team2 TEXT, toss_winner TEXT, toss_decision TEXT, outcome_winner TEXT, outcome_result TEXT, outcome_by_type TEXT, outcome_by_value INTEGER, balls_per_over INTEGER, event_name TEXT, event_match_number INTEGER, season TEXT, umpires_json TEXT, match_referees_json TEXT, json_file_path TEXT, data_version TEXT, imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS innings (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL, inning_number INTEGER NOT NULL, team TEXT NOT NULL, FOREIGN KEY (match_id) REFERENCES matches(match_id));
CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT NOT NULL, inning_number INTEGER NOT NULL, over_number INTEGER NOT NULL, ball_number INTEGER NOT NULL, batter TEXT, bowler TEXT, batting_team TEXT, bowling_team TEXT, runs_batter INTEGER, runs_extras INTEGER, runs_total INTEGER, wicket_player_out TEXT, wicket_kind TEXT, wicket_fielder TEXT, FOREIGN KEY (match_id) REFERENCES matches(match_id));
CREATE TABLE IF NOT EXISTS players (identifier TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS match_players (match_id TEXT NOT NULL, team TEXT NOT NULL, player_name TEXT NOT NULL, player_id TEXT, FOREIGN KEY (match_id) REFERENCES matches(match_id));
CREATE TABLE IF NOT EXISTS download_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_timestamp TEXT NOT NULL, mode TEXT NOT NULL, format TEXT NOT NULL, filter_start_date TEXT, filter_end_date TEXT, zip_url TEXT, matches_scanned INTEGER DEFAULT 0, matches_inserted INTEGER DEFAULT 0, matches_skipped INTEGER DEFAULT 0, matches_filtered INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, status TEXT DEFAULT 'running');
CREATE INDEX IF NOT EXISTS idx_matches_start_date ON matches(start_date);
CREATE INDEX IF NOT EXISTS idx_matches_team1 ON matches(team1);
CREATE INDEX IF NOT EXISTS idx_matches_team2 ON matches(team2);
CREATE INDEX IF NOT EXISTS idx_deliveries_match_id ON deliveries(match_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_batter ON deliveries(batter);
CREATE INDEX IF NOT EXISTS idx_deliveries_bowler ON deliveries(bowler);
"""

stats = {'matches_inserted': 0, 'deliveries_inserted': 0, 'players_inserted': 0, 'errors': 0}

def process_format(format_name, db_path, zip_url):
    global stats

    log(f"{'=' * 70}")
    log(f"DOWNLOADING {format_name}")
    log(f"{'=' * 70}")
    log("")

    local_stats = {'matches': 0, 'deliveries': 0, 'players': 0, 'errors': 0}

    try:
        log(f"Downloading from {zip_url}...")
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / f"{format_name.lower()}.zip"
            with urlopen(zip_url, timeout=REQUEST_TIMEOUT) as response:
                with open(zip_path, 'wb') as f:
                    f.write(response.read())
            log(f"✓ Downloaded {zip_path.stat().st_size / (1024*1024):.1f} MB")

            extract_dir = Path(temp_dir) / f"{format_name.lower()}_extract"
            with ZipFile(zip_path, 'r') as z:
                z.extractall(extract_dir)
            json_files = list(extract_dir.glob("**/*.json"))
            log(f"✓ Extracted {len(json_files)} JSON files")

            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.executescript(SCHEMA_SQL)
            log(f"✓ Initialized database: {db_path}")

            for file_idx, json_file in enumerate(sorted(json_files), 1):
                try:
                    with open(json_file, 'r', encoding='utf-8') as f:
                        match_data = json.load(f)

                    info = match_data.get("info", {})
                    match_id = info.get("match_type_number", f"{format_name.lower()}_{file_idx}")
                    dates = info.get("dates", [])
                    start_date = dates[0] if dates else None

                    # Insert match
                    conn.execute("""
                        INSERT OR REPLACE INTO matches (
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
                    local_stats['matches'] += 1

                    # Insert deliveries
                    for inning_num, inning_data in enumerate(match_data.get("innings", []), 1):
                        batting_team = inning_data.get("team")

                        for delivery in inning_data.get("deliveries", []):
                            try:
                                over_ball = list(delivery.keys())[0]
                                over, ball = over_ball.split('.')
                                del_data = delivery[over_ball]

                                conn.execute("""
                                    INSERT INTO deliveries (
                                        match_id, inning_number, over_number, ball_number,
                                        batter, bowler, batting_team, bowling_team,
                                        runs_batter, runs_extras, runs_total,
                                        wicket_player_out, wicket_kind
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (
                                    match_id, inning_num, int(over), int(ball),
                                    del_data.get("batter"), del_data.get("bowler"),
                                    batting_team, info.get("teams", [None, None])[1] if batting_team == info.get("teams", [None])[0] else info.get("teams", [None])[0],
                                    del_data.get("runs", {}).get("batter", 0),
                                    del_data.get("runs", {}).get("extras", 0),
                                    del_data.get("runs", {}).get("total", 0),
                                    del_data.get("wickets", [{}])[0].get("player_out") if del_data.get("wickets") else None,
                                    del_data.get("wickets", [{}])[0].get("kind") if del_data.get("wickets") else None
                                ))
                                local_stats['deliveries'] += 1
                            except Exception as e:
                                log(f"    ⚠ Delivery error in {json_file.name}: {str(e)[:50]}")
                                local_stats['errors'] += 1

                    # Extract and insert players
                    players = set()
                    for inning_data in match_data.get("innings", []):
                        team = inning_data.get("team")
                        for delivery in inning_data.get("deliveries", []):
                            del_data = delivery[list(delivery.keys())[0]]
                            if del_data.get("batter"):
                                players.add((del_data.get("batter"), team))
                            if del_data.get("bowler"):
                                players.add((del_data.get("bowler"), info.get("teams", [None, None])[1] if team == info.get("teams", [None])[0] else info.get("teams", [None])[0]))

                    for player_name, team in players:
                        conn.execute("INSERT OR IGNORE INTO players (identifier, name) VALUES (?, ?)", (player_name, player_name))
                        conn.execute("INSERT OR IGNORE INTO match_players (match_id, team, player_name) VALUES (?, ?, ?)", (match_id, team, player_name))
                        local_stats['players'] += 1

                    if file_idx % 100 == 0:
                        log(f"  Processed {file_idx}/{len(json_files)} - {local_stats['matches']} matches, {local_stats['deliveries']} deliveries")
                        conn.commit()

                except Exception as e:
                    log(f"  ⚠ Error in {json_file.name}: {str(e)[:80]}")
                    local_stats['errors'] += 1

            conn.commit()
            conn.close()

            log(f"✓ {format_name}: {local_stats['matches']} matches, {local_stats['deliveries']} deliveries, {local_stats['players']} players")

            stats['matches_inserted'] += local_stats['matches']
            stats['deliveries_inserted'] += local_stats['deliveries']
            stats['players_inserted'] += local_stats['players']
            stats['errors'] += local_stats['errors']

    except Exception as e:
        log(f"❌ FAILED: {e}")
        stats['errors'] += 1

try:
    process_format("T20", T20_DB_PATH, CRICSHEET_URL_T20)
    log("")
    process_format("LOI", LOI_DB_PATH, CRICSHEET_URL_LOI)

    log("")
    log("=" * 70)
    log("✅ SUCCESS — All data downloaded and loaded")
    log("=" * 70)
except Exception as e:
    log(f"ERROR: {e}")
    stats['errors'] = 1

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
  - Matches inserted: {stats['matches_inserted']}
  - Deliveries inserted: {stats['deliveries_inserted']}
  - Players inserted: {stats['players_inserted']}
  - Errors encountered: {stats['errors']}
{"-" * 70}
{"=" * 70}
"""

with open(REPORT_PATH, 'w', encoding='utf-8') as f:
    f.write(report)

log("")
log(report)
log(f"Log file: {LOG_PATH}")
log(f"Report file: {REPORT_PATH}")
log("Done.")
log_file.close()

sys.exit(0 if stats['errors'] == 0 else 1)
