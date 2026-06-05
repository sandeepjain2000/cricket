import sqlite3
import os

def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Matches table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS matches (
        match_id TEXT PRIMARY KEY,
        city TEXT,
        date TEXT,
        venue TEXT,
        team1 TEXT,
        team2 TEXT,
        toss_winner TEXT,
        toss_decision TEXT,
        winner TEXT,
        result TEXT,
        result_margin INTEGER,
        player_of_match TEXT
    )
    ''')

    # Players table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS players (
        player_id TEXT PRIMARY KEY,
        name TEXT UNIQUE
    )
    ''')

    # Match Players Mapping
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS match_players (
        match_id TEXT,
        player_name TEXT,
        team TEXT,
        PRIMARY KEY (match_id, player_name),
        FOREIGN KEY (match_id) REFERENCES matches (match_id)
    )
    ''')

    # Innings table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS innings (
        match_id TEXT,
        inning_number INTEGER,
        team TEXT,
        PRIMARY KEY (match_id, inning_number),
        FOREIGN KEY (match_id) REFERENCES matches (match_id)
    )
    ''')

    # Deliveries table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS deliveries (
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
    )
    ''')
    
    # Indexes for faster queries
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_deliveries_batter ON deliveries (batter)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_deliveries_bowler ON deliveries (bowler)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_deliveries_match ON deliveries (match_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_matches_date ON matches (date)')

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db(os.path.join(os.path.dirname(__file__), "..", "data", "cricket_test.db"))
    print("Database initialized.")
