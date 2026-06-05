import psycopg2

conn = psycopg2.connect('postgresql://postgres.lwvejufzfbcnetrfgnuz:tnvgsL50YYLR0DPh@aws-1-ap-northeast-1.pooler.supabase.com:5432/postgres')
cur = conn.cursor()

print("DELIVERIES:")
cur.execute("SELECT batter, COUNT(1) FROM deliveries WHERE batter LIKE '%Bumrah%' OR batter LIKE '%Suryakumar%' OR batter LIKE '%Yadav%' OR batter LIKE '%Hardik%' OR batter LIKE '%Pandya%' GROUP BY batter")
for row in cur.fetchall():
    print(row)

print("\nPLAYER PROFILES:")
cur.execute("SELECT name, full_name FROM player_profiles WHERE name LIKE '%Bumrah%' OR name LIKE '%Suryakumar%' OR name LIKE '%Hardik%' OR full_name LIKE '%Bumrah%' OR full_name LIKE '%Suryakumar%' OR full_name LIKE '%Hardik%' OR name LIKE '%Yadav%'")
for row in cur.fetchall():
    print(row)
