import psycopg2
import os

PG_URL = "postgresql://postgres.jebrowpqtuofvlohqlcf:A5rBv%21eJWL4W%25ZD@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres"

def drop():
    conn = psycopg2.connect(PG_URL)
    cursor = conn.cursor()
    print("Dropping old tables...")
    cursor.execute("DROP TABLE IF EXISTS matches, innings, overs, deliveries, players, match_players, download_runs CASCADE;")
    conn.commit()
    print("Dropped")
    conn.close()

if __name__ == "__main__":
    drop()
