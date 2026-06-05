import sqlite3
import psycopg2
from psycopg2.extras import execute_values
import os
import time

import urllib.parse
password_safe = urllib.parse.quote_plus("A5rBv!eJWL4W%ZD")
PG_URL = f"postgresql://postgres.jebrowpqtuofvlohqlcf:{password_safe}@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"

# The local SQLite path
SQLITE_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "cricket.db")

# Table schema creation in Postgres
TABLE_SCHEMAS = {
    "matches": """
        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY,
            match_type TEXT,
            gender TEXT,
            start_date TEXT,
            end_date TEXT,
            dates_json TEXT,
            venue TEXT,
            city TEXT,
            team1 TEXT,
            team2 TEXT,
            toss_winner TEXT,
            toss_decision TEXT,
            outcome_winner TEXT,
            outcome_result TEXT,
            outcome_by_type TEXT,
            outcome_by_value INTEGER,
            balls_per_over INTEGER DEFAULT 6,
            event_name TEXT,
            event_match_number INTEGER,
            season TEXT,
            umpires_json TEXT,
            match_referees_json TEXT,
            json_file_path TEXT,
            data_version TEXT,
            imported_at TEXT NOT NULL
        );
    """,
    "innings": """
        CREATE TABLE IF NOT EXISTS innings (
            id SERIAL PRIMARY KEY,
            match_id TEXT NOT NULL,
            innings_number INTEGER NOT NULL,
            team TEXT,
            declared INTEGER DEFAULT 0,
            forfeited INTEGER DEFAULT 0,
            target_runs INTEGER,
            target_overs REAL,
            FOREIGN KEY (match_id) REFERENCES matches(match_id)
        );
    """,
    "overs": """
        CREATE TABLE IF NOT EXISTS overs (
            id SERIAL PRIMARY KEY,
            innings_id INTEGER NOT NULL,
            match_id TEXT NOT NULL,
            over_number INTEGER NOT NULL,
            FOREIGN KEY (innings_id) REFERENCES innings(id)
        );
    """,
    "deliveries": """
        CREATE TABLE IF NOT EXISTS deliveries (
            id SERIAL PRIMARY KEY,
            over_id INTEGER NOT NULL,
            innings_id INTEGER NOT NULL,
            match_id TEXT NOT NULL,
            ball_in_over INTEGER,
            batter TEXT,
            non_striker TEXT,
            bowler TEXT,
            runs_batter INTEGER DEFAULT 0,
            runs_extras INTEGER DEFAULT 0,
            runs_total INTEGER DEFAULT 0,
            extras_wides INTEGER DEFAULT 0,
            extras_noballs INTEGER DEFAULT 0,
            extras_byes INTEGER DEFAULT 0,
            extras_legbyes INTEGER DEFAULT 0,
            extras_penalty INTEGER DEFAULT 0,
            is_wicket INTEGER DEFAULT 0,
            wicket_kind TEXT,
            wicket_player_out TEXT,
            wickets_json TEXT,
            review_json TEXT,
            replacements_json TEXT,
            FOREIGN KEY (over_id) REFERENCES overs(id)
        );
    """,
    "players": """
        CREATE TABLE IF NOT EXISTS players (
            identifier TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
    """,
    "match_players": """
        CREATE TABLE IF NOT EXISTS match_players (
            match_id TEXT NOT NULL,
            team TEXT NOT NULL,
            player_name TEXT NOT NULL,
            player_id TEXT,
            PRIMARY KEY (match_id, team, player_name),
            FOREIGN KEY (match_id) REFERENCES matches(match_id)
        );
    """,
    "download_runs": """
        CREATE TABLE IF NOT EXISTS download_runs (
            id SERIAL PRIMARY KEY,
            run_timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            filter_start_date TEXT,
            filter_end_date TEXT,
            zip_url TEXT,
            matches_scanned INTEGER DEFAULT 0,
            matches_inserted INTEGER DEFAULT 0,
            matches_skipped INTEGER DEFAULT 0,
            matches_filtered INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            status TEXT,
            error_message TEXT,
            duration_seconds REAL
        );
    """
}

TABLES = ["matches", "players", "match_players", "innings", "overs", "deliveries", "download_runs"]

def migrate():
    print("Connecting to local SQLite...")
    sl_conn = sqlite3.connect(SQLITE_DB_PATH)
    sl_cursor = sl_conn.cursor()

    print("Connecting to Supabase PostgreSQL...")
    try:
        pg_conn = psycopg2.connect(PG_URL)
        pg_cursor = pg_conn.cursor()
    except Exception as e:
        print(f"Failed to connect to Supabase: {e}")
        return

    # Create tables
    print("Creating tables in PostgreSQL...")
    for table_name in TABLES:
        pg_cursor.execute(TABLE_SCHEMAS[table_name])
    pg_conn.commit()

    # Migrate data
    for table_name in TABLES:
        # Check if table already has data
        pg_cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = pg_cursor.fetchone()[0]
        if count > 0:
            print(f"Table '{table_name}' already contains {count} rows in Postgres. Skipping migration for this table to prevent duplicates.")
            continue

        print(f"Migrating table '{table_name}'...")
        sl_cursor.execute(f"SELECT * FROM {table_name}")
        
        # Get column names
        col_names = [description[0] for description in sl_cursor.description]
        col_names_str = ", ".join(col_names)
        
        # We need to batch inject to prevent memory/payload limits
        batch_size = 5000
        total_migrated = 0
        start_time = time.time()
        
        while True:
            rows = sl_cursor.fetchmany(batch_size)
            if not rows:
                break
                
            insert_query = f"INSERT INTO {table_name} ({col_names_str}) VALUES %s"
            
            # Handle ON CONFLICT DO NOTHING appropriately based on primary keys
            if table_name == "matches":
                insert_query += " ON CONFLICT (match_id) DO NOTHING"
            elif table_name == "players":
                insert_query += " ON CONFLICT (identifier) DO NOTHING"
            elif table_name == "match_players":
                insert_query += " ON CONFLICT (match_id, team, player_name) DO NOTHING"
            elif table_name in ["innings", "overs", "deliveries", "download_runs"]:
                insert_query += " ON CONFLICT (id) DO NOTHING"
            
            # Using execute_values for fast bulk insert
            execute_values(pg_cursor, insert_query, rows)
            pg_conn.commit()
            
            total_migrated += len(rows)
            print(f"  ... inserted {total_migrated} rows into {table_name}")
            
        print(f"Finished migrating '{table_name}' in {time.time() - start_time:.2f} seconds.")

    print("Migration completed successfully!")
    pg_conn.close()
    sl_conn.close()

if __name__ == "__main__":
    if not os.path.exists(SQLITE_DB_PATH):
        print(f"ERROR: SQLite DB not found at {SQLITE_DB_PATH}")
    else:
        migrate()
