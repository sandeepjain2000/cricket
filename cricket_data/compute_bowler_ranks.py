#!/usr/bin/env python3
"""
================================================================================
  compute_bowler_ranks.py  v1.0.0
  T20 Bowler Match Rank Pre-Compute
================================================================================

PURPOSE
  For every innings in every T20 match, ranks each bowler by the formula:

    rank_score = economy  −  (0.5 × wickets)
                 ↑ lower score = better bowler in that innings

  Results are stored in two places:

  bowler_match_ranks   — one row per bowler per innings
    match_id, inning_number, bowler
    balls, runs_conceded, wickets
    economy, rank_score, rank_in_innings   (1 = best bowler that innings)

  delivery_dimensions  — new column `bowler_match_rank` (INTEGER)
    For each delivery, the rank of that delivery's bowler in that innings.
    Allows T20 Lab to filter: "how did batter X bat against the #1 ranked
    bowler in each match?"

FILTER TIERS exposed in T20 Lab
  Rank 1        — faced the best-ranked bowler in the innings
  Top 2         — faced a top-2 ranked bowler
  Top 3         — faced a top-3 ranked bowler
  Rank 4+       — faced a lower-ranked bowler

QUALIFICATION
  Bowlers must have bowled ≥ 6 balls (1 full over) to receive a rank.
  Bowlers with fewer balls are excluded from rankings (rank = NULL).

USAGE
  python compute_bowler_ranks.py            # full rebuild
  python compute_bowler_ranks.py --test     # first 100 matches only
  python compute_bowler_ranks.py --limit 500
  python compute_bowler_ranks.py --incremental  # skip already-ranked matches

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

SCRIPT_NAME = "compute_bowler_ranks.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR  = Path(__file__).parent.resolve()
OUTPUT_DIR  = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"bowler_ranks_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"bowler_ranks_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

MATCH_BATCH     = 500    # matches per SQL batch
MIN_BALLS_RANK  = 6      # minimum balls to qualify for ranking (1 over)

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
log(f"  T20 Bowler Match Rank Pre-Compute")
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

def _cfg(key: str, fallback=None):
    return os.environ.get(key) or _dotenv.get(key) or fallback

SUPABASE_URL_T20 = _cfg("SUPABASE_URL_T20") or _cfg("SUPABASE_URL")

# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def open_pg(autocommit=False):
    if not PSYCOPG2_OK:
        log("  ✗ psycopg2 not installed. Run: pip install psycopg2-binary --break-system-packages")
        sys.exit(1)
    if not SUPABASE_URL_T20:
        log("  ✗ SUPABASE_URL_T20 not found in cricket_ui/.env.local")
        sys.exit(1)
    pg = psycopg2.connect(SUPABASE_URL_T20)
    pg.autocommit = autocommit
    return pg


def detect_columns(pg) -> dict:
    """Detect actual column names in deliveries table (handle schema variants)."""
    with pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'deliveries'
        """)
        cols = {r[0] for r in cur.fetchall()}

    inning_col = next((c for c in ["inning_number", "inning", "innings", "innings_number"]
                       if c in cols), "inning_number")
    bowler_col = next((c for c in ["bowler", "bowling"] if c in cols), "bowler")
    pk_col     = next((c for c in ["delivery_id", "id", "ball_id"] if c in cols), "delivery_id")

    log(f"    deliveries PK={pk_col}  inning={inning_col}  bowler={bowler_col}")
    return {"inning": inning_col, "bowler": bowler_col, "pk": pk_col}


def ensure_tables(pg):
    """Create bowler_match_ranks table and add bowler_match_rank to delivery_dimensions."""
    with pg.cursor() as cur:

        # ── bowler_match_ranks ────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bowler_match_ranks (
                match_id        TEXT    NOT NULL,
                inning_number   INTEGER NOT NULL,
                bowler          TEXT    NOT NULL,
                balls           INTEGER NOT NULL DEFAULT 0,
                runs_conceded   INTEGER NOT NULL DEFAULT 0,
                wickets         INTEGER NOT NULL DEFAULT 0,
                economy         NUMERIC(6,2),
                rank_score      NUMERIC(6,2),
                rank_in_innings INTEGER,
                PRIMARY KEY (match_id, inning_number, bowler)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bmr_match
            ON bowler_match_ranks (match_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bmr_bowler
            ON bowler_match_ranks (bowler)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bmr_rank
            ON bowler_match_ranks (rank_in_innings)
        """)

        # ── bowler_match_rank column in delivery_dimensions ───────────────────
        cur.execute("""
            ALTER TABLE delivery_dimensions
            ADD COLUMN IF NOT EXISTS bowler_match_rank INTEGER
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dd_bowler_rank
            ON delivery_dimensions (bowler_match_rank)
        """)

    pg.commit()
    log("  ✓ Tables and columns verified / created")


def load_match_ids(pg, incremental: bool, limit: int) -> list[str]:
    """Return list of match_ids to process."""
    if incremental:
        query = """
            SELECT DISTINCT match_id FROM deliveries
            WHERE match_id NOT IN (SELECT DISTINCT match_id FROM bowler_match_ranks)
            ORDER BY match_id
        """
    else:
        query = "SELECT DISTINCT match_id FROM deliveries ORDER BY match_id"

    if limit:
        query += f" LIMIT {limit}"

    with pg.cursor() as cur:
        cur.execute(query)
        return [r[0] for r in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
#  RANK COMPUTATION SQL
# ─────────────────────────────────────────────────────────────────────────────

def build_rank_sql(cols: dict) -> str:
    """
    Build the CTE INSERT that computes bowler ranks for a batch of match_ids.
    Uses RANK() OVER PARTITION to rank bowlers within each innings.
    Minimum MIN_BALLS_RANK balls to qualify.
    """
    return f"""
        WITH bowler_stats AS (
            SELECT
                match_id,
                {cols['inning']}                                  AS inning_number,
                {cols['bowler']}                                   AS bowler,
                COUNT({cols['pk']})                                AS balls,
                COALESCE(SUM(runs_batter), 0)::int                AS runs_conceded,
                COUNT(CASE
                    WHEN wicket_player_out IS NOT NULL
                     AND wicket_player_out != ''
                    THEN 1 END)::int                               AS wickets
            FROM deliveries
            WHERE match_id = ANY(%s)
            GROUP BY match_id, {cols['inning']}, {cols['bowler']}
            HAVING COUNT({cols['pk']}) >= {MIN_BALLS_RANK}
        ),
        ranked AS (
            SELECT
                match_id,
                inning_number,
                bowler,
                balls,
                runs_conceded,
                wickets,
                ROUND(
                    (runs_conceded::numeric / NULLIF(balls, 0)) * 6,
                    2
                ) AS economy,
                ROUND(
                    (runs_conceded::numeric / NULLIF(balls, 0)) * 6
                    - 0.5 * wickets,
                    2
                ) AS rank_score,
                RANK() OVER (
                    PARTITION BY match_id, inning_number
                    ORDER BY (
                        (runs_conceded::numeric / NULLIF(balls, 0)) * 6
                        - 0.5 * wickets
                    ) ASC
                )::int AS rank_in_innings
            FROM bowler_stats
        )
        INSERT INTO bowler_match_ranks
            (match_id, inning_number, bowler, balls, runs_conceded,
             wickets, economy, rank_score, rank_in_innings)
        SELECT
            match_id, inning_number, bowler, balls, runs_conceded,
            wickets, economy, rank_score, rank_in_innings
        FROM ranked
        ON CONFLICT (match_id, inning_number, bowler) DO UPDATE SET
            balls           = EXCLUDED.balls,
            runs_conceded   = EXCLUDED.runs_conceded,
            wickets         = EXCLUDED.wickets,
            economy         = EXCLUDED.economy,
            rank_score      = EXCLUDED.rank_score,
            rank_in_innings = EXCLUDED.rank_in_innings
    """


def update_delivery_dimensions(pg, cols: dict) -> int:
    """
    Bulk-update delivery_dimensions.bowler_match_rank from bowler_match_ranks.
    Returns number of rows updated.
    """
    log("  Updating delivery_dimensions.bowler_match_rank…")
    update_sql = f"""
        UPDATE delivery_dimensions dd
        SET    bowler_match_rank = bmr.rank_in_innings
        FROM   deliveries d
        JOIN   bowler_match_ranks bmr
            ON  bmr.match_id      = d.match_id
            AND bmr.inning_number = d.{cols['inning']}
            AND bmr.bowler        = d.{cols['bowler']}
        WHERE  dd.delivery_id = d.{cols['pk']}
    """
    with pg.cursor() as cur:
        cur.execute(update_sql)
        rows = cur.rowcount
    pg.commit()
    log(f"    ✓ {rows:,} delivery_dimensions rows updated")
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Compute T20 bowler match ranks")
    parser.add_argument("--test",        action="store_true",
                        help="Process first 100 matches only")
    parser.add_argument("--limit",       type=int, default=0,
                        help="Cap total matches processed (e.g. --limit 500)")
    parser.add_argument("--incremental", action="store_true",
                        help="Skip matches already in bowler_match_ranks")
    args = parser.parse_args()

    match_limit = 100 if args.test else (args.limit or 0)

    mode = "TEST (100 matches)" if args.test else \
           f"LIMIT {match_limit:,}" if match_limit else \
           ("INCREMENTAL" if args.incremental else "FULL REBUILD")

    log(f"  Mode  : {mode}")
    log(f"  DB    : {(SUPABASE_URL_T20 or '')[:55]}…")
    log()

    stats = {"matches": 0, "rank_rows": 0, "dd_rows": 0, "errors": 0}

    pg = open_pg(autocommit=False)

    # ── Schema setup ──────────────────────────────────────────────────────────
    ensure_tables(pg)
    log()

    # ── Detect deliveries column names ────────────────────────────────────────
    cols = detect_columns(pg)
    log()

    # ── Load match IDs ────────────────────────────────────────────────────────
    match_ids = load_match_ids(pg, incremental=args.incremental, limit=match_limit)
    log(f"  Matches to process : {len(match_ids):,}")
    log()

    if not match_ids:
        log("  Nothing to do.")
        pg.close()
        _write_report(stats, datetime.now())
        return

    # ── Process in batches ────────────────────────────────────────────────────
    rank_sql     = build_rank_sql(cols)
    total_batches = (len(match_ids) + MATCH_BATCH - 1) // MATCH_BATCH

    for i in range(0, len(match_ids), MATCH_BATCH):
        batch    = match_ids[i:i + MATCH_BATCH]
        batch_n  = i // MATCH_BATCH + 1

        try:
            with pg.cursor() as cur:
                cur.execute(rank_sql, (batch,))
                rows = cur.rowcount
            pg.commit()
            stats["rank_rows"] += max(rows, 0)
            stats["matches"]   += len(batch)
            log(f"  Batch {batch_n}/{total_batches} — {len(batch)} matches "
                f"→ {max(rows,0)} rank rows  (total: {stats['matches']:,})")

        except Exception as e:
            pg.rollback()
            stats["errors"] += 1
            log(f"  ✗ Batch {batch_n} failed: {e}")
            if stats["errors"] >= 5:
                log("  Too many errors — aborting.")
                break

    # ── Update delivery_dimensions ────────────────────────────────────────────
    log()
    if stats["errors"] == 0 or stats["rank_rows"] > 0:
        try:
            stats["dd_rows"] = update_delivery_dimensions(pg, cols)
        except Exception as e:
            pg.rollback()
            log(f"  ✗ delivery_dimensions update failed: {e}")
            stats["errors"] += 1
    else:
        log("  ⚠ Skipping delivery_dimensions update due to errors.")

    pg.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()

    log()
    log("=" * 72)
    log(f"  Matches processed        : {stats['matches']:,}")
    log(f"  Bowler rank rows created : {stats['rank_rows']:,}")
    log(f"  Delivery dimensions rows : {stats['dd_rows']:,}")
    log(f"  Errors                   : {stats['errors']:,}")
    log(f"  Duration                 : {duration:.1f}s")
    log("=" * 72)

    _write_report(stats, end_time)
    log_file.close()

    try:
        import winsound
        winsound.Beep(1000, 400)
        winsound.Beep(1200, 400)
    except Exception:
        print('\a', end='', flush=True)

    sys.exit(0 if stats["errors"] == 0 else 1)


def _write_report(stats: dict, end_time: datetime):
    duration = (end_time - START_TIME).total_seconds()
    errors   = stats.get("errors", 0)
    report = f"""\
{"=" * 72}
  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}
{"=" * 72}
  Start Time  : {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}
  End Time    : {end_time.strftime('%Y-%m-%d %H:%M:%S')}
  Duration    : {duration:.1f} seconds
  Outcome     : {'SUCCESS' if errors == 0 else 'COMPLETED WITH ERRORS'}
{"─" * 72}
  Matches processed        : {stats.get('matches', 0):,}
  Bowler rank rows created : {stats.get('rank_rows', 0):,}
  Delivery dimensions rows : {stats.get('dd_rows', 0):,}
  Errors                   : {errors:,}
{"─" * 72}
  Log file : {LOG_PATH}
{"=" * 72}
"""
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)
    log()
    log(report)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n  ⚠ Interrupted by user.")
        log_file.close()
        sys.exit(1)
    except Exception as e:
        log(f"\nFATAL: {e}\n{traceback.format_exc()}")
        log_file.close()
        sys.exit(1)
