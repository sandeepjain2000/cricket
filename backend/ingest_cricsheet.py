import os
import sys
import json
import sqlite3
import zipfile
import urllib.request
import logging
import argparse
from datetime import datetime
from db_schema import init_db

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
RAW_DIR = os.path.join(DATA_DIR, 'raw')
DB_PATH = os.path.join(DATA_DIR, 'cricket_test.db')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
URL = "https://cricsheet.org/downloads/tests_json.zip"

os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Logger setup
log_filename = os.path.join(LOG_DIR, f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def download_data():
    logger.info(f"Downloading data from {URL}...")
    zip_path = os.path.join(RAW_DIR, 'tests_json.zip')
    try:
        urllib.request.urlretrieve(URL, zip_path)
        logger.info("Download completed.")
        return zip_path
    except Exception as e:
        logger.error(f"Error downloading data: {e}")
        sys.exit(1)

def extract_data(zip_path):
    logger.info("Extracting data...")
    extract_to = os.path.join(RAW_DIR, 'tests_json')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_to)
        logger.info(f"Data extracted to {extract_to}")
        return extract_to
    except Exception as e:
        logger.error(f"Error extracting data: {e}")
        sys.exit(1)

def process_match_json(file_path, conn):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {file_path}: {e}")
        return False

    cursor = conn.cursor()
    info = data.get('info', {})
    
    # Exclude non-test matches if any slip through
    if info.get('match_type') != 'Test':
        return False

    match_id = os.path.basename(file_path).replace('.json', '')
    
    # Check if match exists
    cursor.execute('SELECT 1 FROM matches WHERE match_id = ?', (match_id,))
    if cursor.fetchone():
        return False # Existing

    city = info.get('city', '')
    dates = info.get('dates', [])
    date_str = dates[0] if dates else ''
    venue = info.get('venue', '')
    teams = info.get('teams', [])
    team1 = teams[0] if len(teams) > 0 else ''
    team2 = teams[1] if len(teams) > 1 else ''
    
    toss = info.get('toss', {})
    toss_winner = toss.get('winner', '')
    toss_decision = toss.get('decision', '')
    
    outcome = info.get('outcome', {})
    winner = outcome.get('winner', '')
    result = outcome.get('result', '')
    
    by = outcome.get('by', {})
    result_margin = by.get('runs') or by.get('wickets') or 0
    
    pom = info.get('player_of_match', [])
    player_of_match = pom[0] if pom else ''

    # Insert Match
    cursor.execute('''
    INSERT INTO matches (match_id, city, date, venue, team1, team2, toss_winner, toss_decision, winner, result, result_margin, player_of_match)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (match_id, city, date_str, venue, team1, team2, toss_winner, toss_decision, winner, result, result_margin, player_of_match))

    # Players and Match mapping
    registry = info.get('registry', {}).get('people', {})
    for name, pid in registry.items():
        cursor.execute('INSERT OR IGNORE INTO players (player_id, name) VALUES (?, ?)', (pid, name))
        
    for team, team_players in info.get('players', {}).items():
        for player_name in team_players:
            cursor.execute('INSERT OR IGNORE INTO match_players (match_id, player_name, team) VALUES (?, ?, ?)', (match_id, player_name, team))

    # Deliveries
    innings = data.get('innings', [])
    delivery_records = []
    for inning_idx, inning in enumerate(innings):
        team = inning.get('team', '')
        inning_num = inning_idx + 1
        cursor.execute('INSERT INTO innings (match_id, inning_number, team) VALUES (?, ?, ?)', (match_id, inning_num, team))
        
        overs = inning.get('overs', [])
        for over_data in overs:
            over_number = over_data.get('over', 0)
            for d_idx, delivery in enumerate(over_data.get('deliveries', [])):
                delivery_number = d_idx + 1
                batter = delivery.get('batter', '')
                bowler = delivery.get('bowler', '')
                non_striker = delivery.get('non_striker', '')
                
                runs = delivery.get('runs', {})
                r_batter = runs.get('batter', 0)
                r_extras = runs.get('extras', 0)
                r_total = runs.get('total', 0)
                
                wickets = delivery.get('wickets', [])
                w_player_out = wickets[0].get('player_out', '') if wickets else ''
                w_kind = wickets[0].get('kind', '') if wickets else ''
                
                delivery_records.append((
                    match_id, inning_num, over_number, delivery_number, batter, bowler, non_striker,
                    r_batter, r_extras, r_total, w_player_out, w_kind
                ))

    if delivery_records:
        cursor.executemany('''
        INSERT INTO deliveries (match_id, inning_number, over_number, delivery_number, batter, bowler, non_striker, runs_batter, runs_extras, runs_total, wicket_player_out, wicket_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', delivery_records)

    conn.commit()
    return True

def main():
    clear_screen()
    parser = argparse.ArgumentParser(description="Download and ingest Cricsheet Test Match data.")
    parser.add_argument('--mode', choices=['full', 'incremental'], default='incremental', help='Ingestion mode (full repopulates from zip, incremental only adds new)')
    args = parser.parse_args()

    logger.info(f"Starting Cricsheet Data Ingestion. Mode: {args.mode}")

    # Ensure DB exists
    init_db(DB_PATH)
    
    conn = sqlite3.connect(DB_PATH)
    
    if args.mode == 'full':
        logger.info("Full mode - wiping existing data...")
        curr = conn.cursor()
        for table in ['deliveries', 'innings', 'match_players', 'matches']:
            curr.execute(f"DELETE FROM {table}")
        conn.commit()

    zip_path = download_data()
    extract_to = extract_data(zip_path)
    
    logger.info("Processing JSON files...")
    files = [f for f in os.listdir(extract_to) if f.endswith('.json')]
    
    new_matches = 0
    for idx, f in enumerate(files):
        file_path = os.path.join(extract_to, f)
        if process_match_json(file_path, conn):
            new_matches += 1
        if (idx + 1) % 100 == 0:
            logger.info(f"Processed {idx + 1} / {len(files)} files...")

    conn.close()
    logger.info(f"Ingestion complete. {new_matches} new matches added.")

if __name__ == "__main__":
    main()
