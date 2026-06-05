#!/usr/bin/env python3
"""
================================================================================
  compute_batting_stats.py  v1.0.0
  T20 Batting Analytics Pre-Compute
================================================================================

PURPOSE
  Builds two pre-computed tables from the deliveries + matches data:

  batter_innings_stats  — one row per batter per innings per match
    runs, balls_faced, dot_balls, fours, sixes, dismissed
    team_total, match_total, batting_team, team_won

  batter_career_stats   — one aggregated row per batter
    dot_ball_pct, boundary_pct, balls_per_boundary
    contribution_pct_team, contribution_pct_match
    consistency  (std_dev / median score)
    runs_in_wins, runs_in_losses, win_contribution_pct

  These tables are used by the T20 Lab slice-and-dice API to display
  advanced batting metrics alongside averages and strike rates.

USAGE
  python compute_batting_stats.py            # full rebuild
  python compute_batting_stats.py --test     # first 100 matches only
  python compute_batting_stats.py --limit 500
  python compute_batting_stats.py --incremental  # skip already-processed matches

CONFIG  (auto-read from cricket_ui/.env.local)
  SUPABASE_URL_T20  — Postgres connection string
================================================================================
"""

import os
import sys
import argparse
import traceback
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_OK = True
except ImportError:
    PSYCOPG2_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_NAME = "compute_batting_stats.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR  = Path(__file__).parent.resolve()
OUTPUT_DIR  = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"batting_stats_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"batting_stats_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

MATCH_BATCH = 300   # matches per SQL batch

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
log(f"  T20 Batting Analytics Pre-Compute")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 72)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

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

def _cfg(key, fallback=None):
    return os.environ.get(key) or _dotenv.get(key) or fallback

PG_URL = _cfg("SUPABASE_URL_T20") or _cfg("SUPABASE_URL")

# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def open_pg(autocommit=False):
    if not PSYCOPG2_OK:
        log("  ✗ psycopg2 not installed — run: pip install psycopg2-binary --break-system-packages")
        sys.exit(1)
    if not PG_URL:
        log("  ✗ SUPABASE_URL_T20 not found in cricket_ui/.env.local")
        sys.exit(1)
    pg = psycopg2.connect(PG_URL)
    pg.autocommit = autocommit
    return pg


def ensure_tables(pg):
    with pg.cursor() as cur:
        # ── batter_innings_stats ──────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS batter_innings_stats (
                batter          TEXT    NOT NULL,
                match_id        TEXT    NOT NULL,
                inning_number   INTEGER NOT NULL,
                runs            INTEGER NOT NULL DEFAULT 0,
                balls_faced     INTEGER NOT NULL DEFAULT 0,
                dot_balls       INTEGER NOT NULL DEFAULT 0,
                fours           INTEGER NOT NULL DEFAULT 0,
                sixes           INTEGER NOT NULL DEFAULT 0,
                dismissed       BOOLEAN NOT NULL DEFAULT FALSE,
                team_total      INTEGER,
                match_total     INTEGER,
                batting_team    TEXT,
                team_won        BOOLEAN,
                PRIMARY KEY (batter, match_id, inning_number)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bis_batter  ON batter_innings_stats (batter)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bis_match   ON batter_innings_stats (match_id)")

        # ── batter_career_stats ───────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS batter_career_stats (
                batter                  TEXT    PRIMARY KEY,
                innings                 INTEGER NOT NULL DEFAULT 0,
                runs                    INTEGER NOT NULL DEFAULT 0,
                balls_faced             INTEGER NOT NULL DEFAULT 0,
                dot_balls               INTEGER NOT NULL DEFAULT 0,
                fours                   INTEGER NOT NULL DEFAULT 0,
                sixes                   INTEGER NOT NULL DEFAULT 0,
                dismissals              INTEGER NOT NULL DEFAULT 0,
                dot_ball_pct            NUMERIC(6,2),
                boundary_pct            NUMERIC(6,2),
                balls_per_boundary      NUMERIC(8,2),
                avg_contribution_team   NUMERIC(6,2),
                avg_contribution_match  NUMERIC(6,2),
                consistency             NUMERIC(8,4),
                runs_in_wins            INTEGER NOT NULL DEFAULT 0,
                runs_in_losses          INTEGER NOT NULL DEFAULT 0,
                win_contribution_pct    NUMERIC(6,2),
                updated_at              TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    pg.commit()
    log("  ✓ Tables verified / created")


# ─────────────────────────────────────────────────────────────────────────────
#  INNINGS STATS COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

# Per-batch INSERT for batter_innings_stats.
# Processes a list of match_ids in one query — all joins happen server-side.
INNINGS_STATS_SQL = """
INSERT INTO batter_innings_stats
    (batter, match_id, inning_number, runs, balls_faced, dot_balls,
     fours, sixes, dismissed, team_total, match_total, batting_team, team_won)
WITH
  -- Total runs per inning (all deliveries including extras)
  inning_totals AS (
      SELECT match_id, inning_number, SUM(runs_total)::int AS team_total
      FROM   deliveries
      WHERE  match_id = ANY(%s)
      GROUP  BY match_id, inning_number
  ),
  -- Total runs per match (both innings combined)
  match_totals AS (
      SELECT match_id, SUM(runs_total)::int AS match_total
      FROM   deliveries
      WHERE  match_id = ANY(%s)
      GROUP  BY match_id
  ),
  -- Derive which team batted in each inning from toss data
  batting_teams AS (
      SELECT
          m.match_id,
          inn.inning_number,
          CASE
              WHEN LOWER(m.toss_decision) LIKE 'bat%%' AND inn.inning_number = 1
                  THEN m.toss_winner
              WHEN LOWER(m.toss_decision) LIKE 'bat%%' AND inn.inning_number = 2
                  THEN CASE WHEN m.toss_winner = m.team1 THEN m.team2 ELSE m.team1 END
              WHEN LOWER(m.toss_decision) LIKE 'field%%' AND inn.inning_number = 1
                  THEN CASE WHEN m.toss_winner = m.team1 THEN m.team2 ELSE m.team1 END
              WHEN LOWER(m.toss_decision) LIKE 'field%%' AND inn.inning_number = 2
                  THEN m.toss_winner
              ELSE NULL
          END AS batting_team,
          m.winner
      FROM matches m
      CROSS JOIN (SELECT 1 AS inning_number UNION ALL SELECT 2) AS inn
      WHERE m.match_id = ANY(%s)
  ),
  -- Per-batter per-innings aggregation
  batter_agg AS (
      SELECT
          d.batter,
          d.match_id,
          d.inning_number,
          SUM(d.runs_batter)::int                                           AS runs,
          COUNT(d.delivery_id)::int                                         AS balls_faced,
          COUNT(CASE WHEN d.runs_batter = 0 THEN 1 END)::int               AS dot_balls,
          COUNT(CASE WHEN d.runs_batter = 4 THEN 1 END)::int               AS fours,
          COUNT(CASE WHEN d.runs_batter = 6 THEN 1 END)::int               AS sixes,
          COALESCE(BOOL_OR(d.wicket_player_out = d.batter), FALSE)           AS dismissed
      FROM deliveries d
      WHERE d.match_id = ANY(%s)
      GROUP BY d.batter, d.match_id, d.inning_number
  )
SELECT
    ba.batter,
    ba.match_id,
    ba.inning_number,
    ba.runs,
    ba.balls_faced,
    ba.dot_balls,
    ba.fours,
    ba.sixes,
    ba.dismissed,
    it.team_total,
    mt.match_total,
    bt.batting_team,
    CASE WHEN bt.batting_team IS NOT NULL AND bt.batting_team = bt.winner
         THEN TRUE ELSE FALSE END AS team_won
FROM batter_agg ba
LEFT JOIN inning_totals it ON it.match_id = ba.match_id
                           AND it.inning_number = ba.inning_number
LEFT JOIN match_totals  mt ON mt.match_id = ba.match_id
LEFT JOIN batting_teams bt ON bt.match_id = ba.match_id
                           AND bt.inning_number = ba.inning_number
ON CONFLICT (batter, match_id, inning_number) DO UPDATE SET
    runs            = EXCLUDED.runs,
    balls_faced     = EXCLUDED.balls_faced,
    dot_balls       = EXCLUDED.dot_balls,
    fours           = EXCLUDED.fours,
    sixes           = EXCLUDED.sixes,
    dismissed       = EXCLUDED.dismissed,
    team_total      = EXCLUDED.team_total,
    match_total     = EXCLUDED.match_total,
    batting_team    = EXCLUDED.batting_team,
    team_won        = EXCLUDED.team_won
"""


def compute_innings_stats(pg, match_ids: list, incremental: bool) -> int:
    """
    Process a batch of match_ids — compute and upsert batter_innings_stats rows.
    Returns number of rows upserted.
    """
    if not match_ids:
        return 0

    ids_arr = match_ids  # passed as array to ANY(%s)

    with pg.cursor() as cur:
        cur.execute(INNINGS_STATS_SQL, (ids_arr, ids_arr, ids_arr, ids_arr))
        rows = cur.rowcount
    pg.commit()
    return rows if rows and rows > 0 else 0


# ─────────────────────────────────────────────────────────────────────────────
#  CAREER STATS AGGREGATION
# ─────────────────────────────────────────────────────────────────────────────

CAREER_STATS_SQL = """
INSERT INTO batter_career_stats (
    batter, innings, runs, balls_faced, dot_balls, fours, sixes, dismissals,
    dot_ball_pct, boundary_pct, balls_per_boundary,
    avg_contribution_team, avg_contribution_match,
    consistency,
    runs_in_wins, runs_in_losses, win_contribution_pct,
    updated_at
)
SELECT
    batter,
    COUNT(*)::int                                                        AS innings,
    SUM(runs)::int                                                       AS runs,
    SUM(balls_faced)::int                                                AS balls_faced,
    SUM(dot_balls)::int                                                  AS dot_balls,
    SUM(fours)::int                                                      AS fours,
    SUM(sixes)::int                                                      AS sixes,
    COUNT(CASE WHEN dismissed THEN 1 END)::int                           AS dismissals,

    -- Dot ball %
    ROUND(
        (100.0 * SUM(dot_balls) / NULLIF(SUM(balls_faced), 0))::numeric, 2
    )                                                                    AS dot_ball_pct,

    -- Boundary % (balls where batter hit 4 or 6)
    ROUND(
        (100.0 * (SUM(fours) + SUM(sixes)) / NULLIF(SUM(balls_faced), 0))::numeric, 2
    )                                                                    AS boundary_pct,

    -- Balls per boundary
    ROUND(
        (SUM(balls_faced)::numeric / NULLIF(SUM(fours) + SUM(sixes), 0))::numeric, 2
    )                                                                    AS balls_per_boundary,

    -- Average contribution % to own team's innings total
    ROUND(
        AVG(
            CASE WHEN team_total > 0
                 THEN 100.0 * runs / team_total
                 ELSE NULL END
        )::numeric, 2
    )                                                                    AS avg_contribution_team,

    -- Average contribution % to combined match total (both teams)
    ROUND(
        AVG(
            CASE WHEN match_total > 0
                 THEN 100.0 * runs / match_total
                 ELSE NULL END
        )::numeric, 2
    )                                                                    AS avg_contribution_match,

    -- Consistency: STDDEV / MEDIAN  (lower = more consistent)
    -- Only count innings where batter faced ≥ 1 ball
    ROUND(
        (STDDEV(CASE WHEN balls_faced > 0 THEN runs::numeric END)
        / NULLIF(
            PERCENTILE_CONT(0.5) WITHIN GROUP (
                ORDER BY CASE WHEN balls_faced > 0 THEN runs::numeric END
            ), 0
        ))::numeric, 4
    )                                                                    AS consistency,

    -- Runs in wins vs losses
    SUM(CASE WHEN team_won THEN runs ELSE 0 END)::int                   AS runs_in_wins,
    SUM(CASE WHEN NOT team_won THEN runs ELSE 0 END)::int               AS runs_in_losses,

    -- Win contribution %: what % of their total runs came in winning matches
    ROUND(
        (100.0 * SUM(CASE WHEN team_won THEN runs ELSE 0 END)
        / NULLIF(SUM(runs), 0))::numeric, 2
    )                                                                    AS win_contribution_pct,

    NOW()
FROM batter_innings_stats
GROUP BY batter
ON CONFLICT (batter) DO UPDATE SET
    innings                 = EXCLUDED.innings,
    runs                    = EXCLUDED.runs,
    balls_faced             = EXCLUDED.balls_faced,
    dot_balls               = EXCLUDED.dot_balls,
    fours                   = EXCLUDED.fours,
    sixes                   = EXCLUDED.sixes,
    dismissals              = EXCLUDED.dismissals,
    dot_ball_pct            = EXCLUDED.dot_ball_pct,
    boundary_pct            = EXCLUDED.boundary_pct,
    balls_per_boundary      = EXCLUDED.balls_per_boundary,
    avg_contribution_team   = EXCLUDED.avg_contribution_team,
    avg_contribution_match  = EXCLUDED.avg_contribution_match,
    consistency             = EXCLUDED.consistency,
    runs_in_wins            = EXCLUDED.runs_in_wins,
    runs_in_losses          = EXCLUDED.runs_in_losses,
    win_contribution_pct    = EXCLUDED.win_contribution_pct,
    updated_at              = NOW()
"""


def compute_career_stats(pg):
    log("  Computing batter_career_stats from innings data…")
    with pg.cursor() as cur:
        cur.execute(CAREER_STATS_SQL)
        rows = cur.rowcount
    pg.commit()
    log(f"  ✓ {rows:,} career stat rows upserted")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pre-compute T20 batting analytics")
    parser.add_argument("--test",        action="store_true",
                        help="Process only first 100 matches (quick smoke-test)")
    parser.add_argument("--limit",       type=int, default=0,
                        help="Cap total matches processed (e.g. --limit 500)")
    parser.add_argument("--incremental", action="store_true",
                        help="Skip matches already in batter_innings_stats")
    args = parser.parse_args()

    row_limit = 100 if args.test else (args.limit or 0)

    stats = {"matches": 0, "innings_rows": 0, "career_rows": 0, "errors": 0}

    try:
        mode = "TEST (100 matches)" if args.test else \
               f"LIMIT {row_limit:,}" if row_limit else \
               ("INCREMENTAL" if args.incremental else "FULL REBUILD")
        log(f"  Mode  : {mode}")
        log(f"  DB    : {(PG_URL or '')[:55]}…")
        log()

        pg = open_pg(autocommit=False)
        ensure_tables(pg)
        log()

        # ── Load match_ids to process ─────────────────────────────────────────
        with pg.cursor() as cur:
            if args.incremental:
                cur.execute("""
                    SELECT DISTINCT match_id FROM matches
                    WHERE match_id NOT IN (
                        SELECT DISTINCT match_id FROM batter_innings_stats
                    )
                    ORDER BY match_id
                """)
            else:
                cur.execute("SELECT match_id FROM matches ORDER BY match_id")
            all_ids = [r[0] for r in cur.fetchall()]

        if row_limit:
            all_ids = all_ids[:row_limit]

        log(f"  Matches to process : {len(all_ids):,}")
        log()

        # ── Process in batches ────────────────────────────────────────────────
        total_batches = (len(all_ids) + MATCH_BATCH - 1) // MATCH_BATCH

        for i in range(0, len(all_ids), MATCH_BATCH):
            batch     = all_ids[i:i + MATCH_BATCH]
            batch_num = i // MATCH_BATCH + 1

            try:
                rows = compute_innings_stats(pg, batch, args.incremental)
                stats["matches"]      += len(batch)
                stats["innings_rows"] += rows
                log(f"  Batch {batch_num}/{total_batches} — {len(batch)} matches"
                    f" → {rows} innings rows  (total: {stats['matches']:,} matches)")
            except Exception as e:
                pg.rollback()
                stats["errors"] += 1
                log(f"  ✗ Batch {batch_num} failed: {e}")
                if stats["errors"] >= 3:
                    log("  Too many errors — aborting.")
                    break

        # ── Rebuild career stats from complete innings table ──────────────────
        log()
        if not args.test:
            stats["career_rows"] = compute_career_stats(pg)
        else:
            log("  (Skipping career stats rebuild in test mode)")

        pg.close()

    except Exception as exc:
        log(f"\nFATAL: {exc}")
        log(traceback.format_exc())
        stats["errors"] += 1

    # ── Report ────────────────────────────────────────────────────────────────
    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()

    log()
    log("=" * 72)
    log(f"  Matches processed  : {stats['matches']:,}")
    log(f"  Innings rows       : {stats['innings_rows']:,}")
    log(f"  Career rows        : {stats['career_rows']:,}")
    log(f"  Errors             : {stats['errors']:,}")
    log(f"  Duration           : {duration:.1f}s")
    log("=" * 72)

    report = f"""\
{"=" * 72}
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
{"=" * 72}
  Start Time  : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time    : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration    : {duration:.1f} seconds
  Outcome     : {'SUCCESS' if stats['errors'] == 0 else 'COMPLETED WITH ERRORS'}
{"─" * 72}
  Matches processed  : {stats['matches']:,}
  Innings rows       : {stats['innings_rows']:,}
  Career rows        : {stats['career_rows']:,}
  Errors             : {stats['errors']:,}
{"─" * 72}
  Log file : {LOG_PATH}
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
