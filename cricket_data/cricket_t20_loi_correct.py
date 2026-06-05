#!/usr/bin/env python3
"""
Cricket T20 & LOI Downloader - CORRECT SCHEMA VERSION
Uses exact same schema as Test database for consistency
"""

import os, sys, json, sqlite3, tempfile
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile
from configparser import ConfigParser

SCRIPT_NAME = "cricket_t20_loi_correct.py"
VERSION = "2.0.0"
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
log(f"  T20 & LOI Downloader (CORRECT SCHEMA)")
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

# Schema aligned with Test database expectations (rankings API requires
# matches.start_date + match_players table).
SCHEMA_SQL = """
DROP TABLE IF EXISTS deliveries;
DROP TABLE IF EXISTS match_players;
DROP TABLE IF EXISTS matches;

CREATE TABLE matches (
    match_id TEXT PRIMARY KEY,
    city TEXT,
    start_date TEXT,
    venue TEXT,
    team1 TEXT,
    team2 TEXT,
    toss_winner TEXT,
    toss_decision TEXT,
    winner TEXT,
    result TEXT,
    result_margin INTEGER,
    player_of_match TEXT
);

CREATE TABLE match_players (
    match_id TEXT,
    player_name TEXT,
    team TEXT,
    PRIMARY KEY (match_id, player_name),
    FOREIGN KEY (match_id) REFERENCES matches (match_id)
);

CREATE TABLE deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT,
    inning_number INTEGER,
    over_number INTEGER,
    delivery_number INTEGER,
    batter TEXT,
    bowler TEXT,
    non_striker TEXT,
    runs_batter INTEGER,
    runs_extras INTEGER,
    runs_total INTEGER,
    wicket_player_out TEXT,
    wicket_kind TEXT,
    FOREIGN KEY (match_id) REFERENCES matches (match_id)
);

CREATE INDEX IF NOT EXISTS idx_match_id ON deliveries(match_id);
CREATE INDEX IF NOT EXISTS idx_batter ON deliveries(batter);
CREATE INDEX IF NOT EXISTS idx_bowler ON deliveries(bowler);
CREATE INDEX IF NOT EXISTS idx_start_date ON matches(start_date);
CREATE INDEX IF NOT EXISTS idx_mp_player ON match_players(player_name);
"""

stats = {'matches_inserted': 0, 'deliveries_inserted': 0, 'errors': 0}

def process_format(format_name, db_path, zip_url):
    global stats

    log(f"{'=' * 70}")
    log(f"DOWNLOADING {format_name}")
    log(f"{'=' * 70}")
    log("")

    local_stats = {'matches': 0, 'deliveries': 0, 'errors': 0}

    try:
        log(f"Downloading from {zip_url}...")
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / f"{format_name.lower()}.zip"
            with urlopen(zip_url, timeout=REQUEST_TIMEOUT) as response:
                with open(zip_path, 'wb') as f:
                    f.write(response.read())
            log(f"✓ Downloaded {zip_path.stat().st_size / (1024*1024):.1f} MB")

            extract_dir = Path(temp_dir) / f"{format_name.lower()}_extract"
            with ZipFile(zip_path) as z:
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
                    date = dates[0] if dates else None
                    teams = info.get("teams", [None, None])

                    # Insert match
                    conn.execute("""
                        INSERT OR REPLACE INTO matches (
                            match_id, city, start_date, venue, team1, team2,
                            toss_winner, toss_decision, winner, result
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        match_id, info.get("city"), date,
                        info.get("venue"), teams[0], teams[1],
                        info.get("toss", {}).get("winner"),
                        info.get("toss", {}).get("decision"),
                        info.get("outcome", {}).get("winner"),
                        info.get("outcome", {}).get("result")
                    ))
                    local_stats['matches'] += 1

                    # Populate match_players from info.players: { "Team A": [names], "Team B": [names] }
                    for team_name, player_names in (info.get("players") or {}).items():
                        for pname in player_names or []:
                            conn.execute("""
                                INSERT OR IGNORE INTO match_players (match_id, player_name, team)
                                VALUES (?, ?, ?)
                            """, (match_id, pname, team_name))

                    # Insert deliveries
                    # Cricsheet structure: innings -> overs -> deliveries (3 levels deep)
                    for inning_num, inning_data in enumerate(match_data.get("innings", []), 1):
                        for over_data in inning_data.get("overs", []):
                            over_num = over_data.get("over", 0)
                            for ball_idx, del_data in enumerate(over_data.get("deliveries", []), 1):
                                try:
                                    batter = del_data.get("batter")
                                    bowler = del_data.get("bowler")
                                    non_striker = del_data.get("non_striker")

                                    runs = del_data.get("runs", {})
                                    runs_batter = runs.get("batter", 0)
                                    runs_extras = runs.get("extras", 0)
                                    runs_total = runs.get("total", 0)

                                    wickets = del_data.get("wickets") or []
                                    wicket_player = wickets[0].get("player_out") if wickets else None
                                    wicket_kind = wickets[0].get("kind") if wickets else None

                                    conn.execute("""
                                        INSERT INTO deliveries (
                                            match_id, inning_number, over_number, delivery_number,
                                            batter, bowler, non_striker,
                                            runs_batter, runs_extras, runs_total,
                                            wicket_player_out, wicket_kind
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        match_id, inning_num, int(over_num), int(ball_idx),
                                        batter, bowler, non_striker,
                                        runs_batter, runs_extras, runs_total,
                                        wicket_player, wicket_kind
                                    ))
                                    local_stats['deliveries'] += 1
                                except Exception as e:
                                    local_stats['errors'] += 1

                    if file_idx % 100 == 0:
                        log(f"  {file_idx:4d}/{len(json_files)} - {local_stats['matches']:5d} matches, {local_stats['deliveries']:7d} deliveries")
                        conn.commit()

                except Exception as e:
                    log(f"  ⚠ Error in {json_file.name}: {str(e)[:60]}")
                    local_stats['errors'] += 1

            conn.commit()
            conn.close()

            log(f"✓ {format_name}: {local_stats['matches']} matches, {local_stats['deliveries']} deliveries")
            stats['matches_inserted'] += local_stats['matches']
            stats['deliveries_inserted'] += local_stats['deliveries']
            stats['errors'] += local_stats['errors']

    except Exception as e:
        log(f"❌ ERROR: {e}")
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
