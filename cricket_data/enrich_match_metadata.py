#!/usr/bin/env python3
"""
================================================================================
  enrich_match_metadata.py  v1.0.0
  T20 Match Metadata Enricher — OpenAI edition
================================================================================

HOW IT WORKS
  For each match in the Supabase `matches` table we know: team1, team2,
  start_date, venue, city.  We ask GPT-4o-mini to identify:

    event_name        — e.g. "ICC T20 World Cup 2024", "India vs Australia
                        T20I Series 2023", "IPL 2024"
    competition_type  — "Bilateral" | "Tournament" | "Domestic"
    match_stage       — "Group" | "Super 4" | "Super 8" | "Qualifier" |
                        "Semi-final" | "Final" | "N/A"
    confidence        — "high" | "medium" | "low"

  Matches are sent in batches of 25.  Results are written back to two new
  columns in the `matches` table:  event_name, competition_type, match_stage.
  The `delivery_dimensions` table can then be rebuilt with correct competition
  and stage filters.

SETUP (one-time)
  pip install openai psycopg2-binary --break-system-packages

USAGE
  python enrich_match_metadata.py            # all matches
  python enrich_match_metadata.py --test     # first 50 matches only
  python enrich_match_metadata.py --limit 200
  python enrich_match_metadata.py --skip-existing   # skip already-enriched rows

CONFIG  (auto-read from cricket_ui/.env.local)
  OPENAI_API_KEY   — required
  SUPABASE_URL_T20 — Postgres connection string
================================================================================
"""

import os
import sys
import re
import json
import time
import argparse
import traceback
from datetime import datetime
from pathlib import Path

_claudes = next((p for p in Path(__file__).resolve().parents if p.name == "Claudes"), None)
if _claudes is None:
    raise ImportError("Claudes root not found (expected ...\\Claudes\\nvidia_keys)")
sys.path.insert(0, str(_claudes))
from nvidia_llm import NVIDIA_MODEL, create_nvidia_client, discover_nvidia_key_files

try:
    from openai import OpenAI
    OPENAI_OK = True
except ImportError:
    OPENAI_OK = False

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_OK = True
except ImportError:
    PSYCOPG2_OK = False

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_NAME = "enrich_match_metadata.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR  = Path(__file__).parent.resolve()
OUTPUT_DIR  = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"match_meta_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"match_meta_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

BATCH_SIZE  = 25   # matches per LLM call
MODEL       = NVIDIA_MODEL

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
log(f"  T20 Match Metadata Enricher — OpenAI edition")
log(f"  Started: {START_TIME.strftime('%Y-%m-%d %H:%M:%S')}")
log("=" * 72)
log()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  (reads cricket_ui/.env.local automatically)
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
#  VALID VALUES  (normalised before writing to DB)
# ─────────────────────────────────────────────────────────────────────────────

VALID_COMP_TYPES = {"Bilateral", "Tournament", "Domestic"}
VALID_STAGES     = {"Group", "Super 4", "Super 8", "Qualifier",
                    "Semi-final", "Final", "N/A"}

def normalise_competition_type(val: str | None) -> str:
    if not val:
        return "Bilateral"
    v = val.strip().title()
    if v in VALID_COMP_TYPES:
        return v
    # Fuzzy fallbacks
    v_lower = val.lower()
    if any(k in v_lower for k in ("tournament", "cup", "trophy", "championship", "icc")):
        return "Tournament"
    if any(k in v_lower for k in ("ipl", "bbl", "cpl", "psl", "league", "domestic")):
        return "Domestic"
    return "Bilateral"

def normalise_stage(val: str | None) -> str:
    if not val:
        return "N/A"
    v = val.strip()
    # Direct match
    for s in VALID_STAGES:
        if v.lower() == s.lower():
            return s
    # Fuzzy
    v_lower = v.lower()
    if "final" in v_lower and "semi" in v_lower:
        return "Semi-final"
    if "final" in v_lower:
        return "Final"
    if "semi" in v_lower:
        return "Semi-final"
    if any(k in v_lower for k in ("qualify", "qualifier", "eliminator", "play-off", "playoff")):
        return "Qualifier"
    if "super 8" in v_lower or "super eight" in v_lower:
        return "Super 8"
    if "super 4" in v_lower or "super four" in v_lower:
        return "Super 4"
    if any(k in v_lower for k in ("group", "league", "round robin", "preliminary")):
        return "Group"
    if any(k in v_lower for k in ("bilateral", "n/a", "na", "none", "not applicable", "regular")):
        return "N/A"
    return "N/A"

# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI BATCH LOOKUP
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a cricket statistics expert with comprehensive knowledge of all
international and domestic T20 cricket matches, tournaments, and bilateral series.

For each match provided (identified by teams, date, and venue), return:
  - event_name       : Full name of the tournament or series
                       (e.g. "ICC T20 World Cup 2024", "India vs Australia T20I Series 2023",
                        "Indian Premier League 2023", "Big Bash League 2022/23")
  - competition_type : Exactly one of: "Bilateral", "Tournament", "Domestic"
                       Bilateral = 2-team international series
                       Tournament = multi-team international event (World Cup, Asia Cup, etc.)
                       Domestic   = domestic league (IPL, BBL, CPL, PSL, etc.)
  - match_stage      : Exactly one of: "Group", "Super 4", "Super 8", "Qualifier",
                       "Semi-final", "Final", "N/A"
                       Use "N/A" for bilateral series matches
                       Use "Group" for group stage matches in tournaments
  - confidence       : "high" if certain, "medium" if probable, "low" if guessing

Return ONLY a JSON array, one object per match, in the same order as the input.
Use null for event_name if genuinely unknown. Do not add any text outside the JSON."""

USER_PROMPT_TEMPLATE = """Identify the event, competition type, and match stage for these T20 cricket matches:

{match_list}

Return a JSON array with {count} objects in this exact format:
[
  {{
    "match_id": "<exactly as given>",
    "event_name": "<full event name or null>",
    "competition_type": "Bilateral or Tournament or Domestic",
    "match_stage": "Group or Super 4 or Super 8 or Qualifier or Semi-final or Final or N/A",
    "confidence": "high or medium or low"
  }},
  ...
]"""


def ask_openai_batch(client: OpenAI, matches: list[dict]) -> list[dict]:
    """
    Send a batch of match descriptors to GPT-4o-mini.
    Each match dict has: match_id, team1, team2, start_date, venue, city.
    Returns list of dicts with event_name, competition_type, match_stage, confidence.
    """
    lines = []
    for i, m in enumerate(matches):
        venue_str = ", ".join(filter(None, [m.get("city"), m.get("venue")]))
        lines.append(
            f"{i+1}. [{m['match_id']}] {m['team1']} vs {m['team2']}"
            f" | {m.get('start_date','unknown date')}"
            f" | {venue_str or 'unknown venue'}"
        )

    prompt = USER_PROMPT_TEMPLATE.format(
        match_list="\n".join(lines),
        count=len(matches),
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.1,
            )

            raw = response.choices[0].message.content or ""

            # Strip markdown code fences
            raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.I)
            raw = re.sub(r'\s*```$', '', raw.strip())

            # Extract JSON array
            m_arr = re.search(r'\[.*\]', raw, re.S)
            if m_arr:
                data = json.loads(m_arr.group(0))
                if isinstance(data, list):
                    return data

            data = json.loads(raw)
            if isinstance(data, list):
                return data
            for key in ("matches", "results", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]

            log(f"    ⚠ Could not extract list from response")
            return []

        except json.JSONDecodeError as e:
            log(f"    ⚠ JSON parse error (attempt {attempt+1}): {e}")
            log(f"       Raw: {raw[:300]}")
            if attempt < 2:
                time.sleep(2)
        except Exception as e:
            log(f"    ⚠ OpenAI error (attempt {attempt+1}): {e}")
            if attempt < 2:
                time.sleep(5)

    return []


# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def open_pg():
    if not PSYCOPG2_OK:
        log("  ✗ psycopg2 not installed.  Run:")
        log("    pip install psycopg2-binary --break-system-packages")
        sys.exit(1)
    if not SUPABASE_URL_T20:
        log("  ✗ SUPABASE_URL_T20 not found in cricket_ui/.env.local")
        sys.exit(1)
    pg = psycopg2.connect(SUPABASE_URL_T20)
    pg.autocommit = False
    return pg


def ensure_columns(pg):
    """Add event_name / competition_type / match_stage columns if missing."""
    with pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'matches'
        """)
        existing = {r[0] for r in cur.fetchall()}

    new_cols = []
    if "event_name"       not in existing: new_cols.append("event_name       TEXT")
    if "competition_type" not in existing: new_cols.append("competition_type TEXT")
    if "match_stage"      not in existing: new_cols.append("match_stage      TEXT")
    if "meta_confidence"  not in existing: new_cols.append("meta_confidence  TEXT")

    if new_cols:
        with pg.cursor() as cur:
            for col_def in new_cols:
                col_name = col_def.split()[0]
                cur.execute(f"ALTER TABLE matches ADD COLUMN IF NOT EXISTS {col_def}")
                log(f"  + Added column: {col_name}")
        pg.commit()
    else:
        log("  ✓ All metadata columns already exist")


def load_matches(pg, skip_existing: bool, limit: int) -> list[dict]:
    """Fetch matches that need enriching."""
    where = "WHERE event_name IS NULL" if skip_existing else ""
    lim   = f"LIMIT {limit}" if limit else ""

    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(f"""
            SELECT match_id,
                   COALESCE(team1, '')      AS team1,
                   COALESCE(team2, '')      AS team2,
                   COALESCE(start_date::text, '') AS start_date,
                   COALESCE(venue, '')      AS venue,
                   COALESCE(city, '')       AS city
            FROM matches
            {where}
            ORDER BY start_date
            {lim}
        """)
        return [dict(r) for r in cur.fetchall()]


def upsert_batch(pg, results: list[dict]):
    """Write enriched fields back to matches table."""
    if not results:
        return
    with pg.cursor() as cur:
        for r in results:
            cur.execute("""
                UPDATE matches
                SET event_name       = %(event_name)s,
                    competition_type = %(competition_type)s,
                    match_stage      = %(match_stage)s,
                    meta_confidence  = %(confidence)s
                WHERE match_id = %(match_id)s
            """, r)
    pg.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enrich T20 match metadata via OpenAI")
    parser.add_argument("--test",          action="store_true",
                        help="Process only first 50 matches (quick smoke-test)")
    parser.add_argument("--limit",         type=int, default=0,
                        help="Cap total matches processed (e.g. --limit 200)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip matches that already have event_name populated")
    args = parser.parse_args()

    row_limit = 50 if args.test else (args.limit or 0)

    # ── Validate prerequisites ────────────────────────────────────────────────
    if not OPENAI_OK:
        log("  ✗ openai package not installed.  Run:")
        log("    pip install openai --break-system-packages")
        sys.exit(1)
    try:
        discover_nvidia_key_files()
    except FileNotFoundError as e:
        log(f"  ✗ {e}")
        sys.exit(1)

    client, nvidia_key = create_nvidia_client(log)

    mode = "TEST (50 matches)" if args.test else \
           f"LIMIT {row_limit:,}" if row_limit else \
           ("SKIP EXISTING" if args.skip_existing else "FULL RUN")
    log(f"  Mode  : {mode}")
    log(f"  Model : {MODEL}")
    log(f"  Key   : {nvidia_key['key_id']}")
    log(f"  DB    : {(SUPABASE_URL_T20 or '')[:55]}…")
    log()

    # ── Connect and prepare schema ────────────────────────────────────────────
    pg = open_pg()
    ensure_columns(pg)
    log()

    # ── Load matches ──────────────────────────────────────────────────────────
    matches = load_matches(pg, skip_existing=args.skip_existing, limit=row_limit)
    log(f"  Matches to process : {len(matches):,}")
    log()

    if not matches:
        log("  Nothing to do.")
        pg.close()
        return

    # ── Process in batches ────────────────────────────────────────────────────
    stats = {"total": 0, "enriched": 0, "errors": 0,
             "high": 0, "medium": 0, "low": 0}

    for batch_start in range(0, len(matches), BATCH_SIZE):
        batch   = matches[batch_start:batch_start + BATCH_SIZE]
        batch_n = batch_start // BATCH_SIZE + 1
        total_batches = (len(matches) + BATCH_SIZE - 1) // BATCH_SIZE

        log(f"  Batch {batch_n}/{total_batches}  ({batch[0]['team1']} vs {batch[0]['team2']}"
            f" … {batch[-1]['team1']} vs {batch[-1]['team2']})")

        raw_results = ask_openai_batch(client, batch)

        if not raw_results:
            log(f"    ⚠ Empty response — skipping batch")
            stats["errors"] += len(batch)
            stats["total"]  += len(batch)
            continue

        # Build a lookup by match_id in case GPT reorders
        result_by_id = {str(r.get("match_id", "")): r for r in raw_results}

        rows_to_write = []
        for m in batch:
            mid = str(m["match_id"])
            r   = result_by_id.get(mid) or (raw_results[batch.index(m)] if batch.index(m) < len(raw_results) else {})

            comp   = normalise_competition_type(r.get("competition_type"))
            stage  = normalise_stage(r.get("match_stage"))
            conf   = (r.get("confidence") or "low").lower()
            event  = (r.get("event_name") or "").strip() or None

            rows_to_write.append({
                "match_id":        mid,
                "event_name":      event,
                "competition_type": comp,
                "match_stage":     stage,
                "confidence":      conf,
            })

            stats["total"]   += 1
            stats["enriched"] += 1
            stats[conf if conf in ("high","medium","low") else "low"] += 1

            log(f"    [{mid}] {m['team1']} vs {m['team2']} {m['start_date'][:10]}"
                f"  →  {comp} / {stage}  [{conf}]  {event or '(no event name)'}")

        try:
            upsert_batch(pg, rows_to_write)
        except Exception as e:
            log(f"    ✗ DB write error: {e}")
            pg.rollback()
            stats["errors"] += len(rows_to_write)

        # Polite pause between OpenAI calls
        if batch_start + BATCH_SIZE < len(matches):
            time.sleep(0.5)

    pg.close()

    # ── Report ────────────────────────────────────────────────────────────────
    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()

    log()
    log("=" * 72)
    log(f"  Matches processed : {stats['total']:,}")
    log(f"  Enriched          : {stats['enriched']:,}")
    log(f"  Errors            : {stats['errors']:,}")
    log(f"  Confidence — high : {stats['high']:,}  medium : {stats['medium']:,}  low : {stats['low']:,}")
    log(f"  Duration          : {duration:.1f}s")
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
  Matches processed : {stats['total']:,}
  Enriched          : {stats['enriched']:,}
  Errors            : {stats['errors']:,}
  Confidence breakdown:
    High   : {stats['high']:,}
    Medium : {stats['medium']:,}
    Low    : {stats['low']:,}
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
