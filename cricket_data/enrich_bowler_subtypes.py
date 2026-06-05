#!/usr/bin/env python3
"""
================================================================================
  enrich_bowler_subtypes.py  v1.0.0
  T20 Bowler Subtype Enricher — OpenAI GPT-4o-mini edition
================================================================================

HOW IT WORKS
  Reads all bowlers from the Supabase `player_profiles` table and asks
  GPT-4o-mini to classify each one with a detailed bowling subtype:

  PACE bowlers  →  "Pure Pace" | "Swing" | "Seam" | "Fast-medium"
  SPIN bowlers  →  "Off-spin" | "Leg-spin" | "Left-arm orthodox" | "Chinaman"
  Others        →  "Medium-pace" | "N/A"

  We already have `bowling_style` (raw text) and `bowling_type` (broad category)
  in the DB.  This script adds:

    bowling_subtype_detail      — detailed subtype (one of 8 values above)
    bowling_subtype_confidence  — "high" | "medium" | "low"

  Batches of 50 bowlers are sent per OpenAI call.  Non-bowlers and batters-only
  are skipped.  A --skip-existing flag lets you re-run without overwriting work.

SETUP (one-time)
  pip install openai psycopg2-binary --break-system-packages

USAGE
  python enrich_bowler_subtypes.py              # all bowlers
  python enrich_bowler_subtypes.py --test       # first 50 bowlers only
  python enrich_bowler_subtypes.py --limit 200
  python enrich_bowler_subtypes.py --skip-existing   # skip already-enriched

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

SCRIPT_NAME = "enrich_bowler_subtypes.py"
VERSION     = "1.0.0"
START_TIME  = datetime.now()

SCRIPT_DIR  = Path(__file__).parent.resolve()
OUTPUT_DIR  = SCRIPT_DIR / "logs_and_reports"
OUTPUT_DIR.mkdir(exist_ok=True)

LOG_PATH    = OUTPUT_DIR / f"bowler_subtypes_{START_TIME.strftime('%Y%m%d_%H%M%S')}.log"
REPORT_PATH = OUTPUT_DIR / f"bowler_subtypes_report_{START_TIME.strftime('%Y%m%d_%H%M%S')}.txt"

BATCH_SIZE  = 50    # bowlers per LLM call
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
log(f"  T20 Bowler Subtype Enricher — OpenAI GPT-4o-mini edition")
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
#  VALID VALUES
# ─────────────────────────────────────────────────────────────────────────────

# Pace subtypes
PACE_SUBTYPES = {"Pure Pace", "Swing", "Seam", "Fast-medium"}
# Spin subtypes
SPIN_SUBTYPES = {"Off-spin", "Leg-spin", "Left-arm orthodox", "Chinaman"}
# Other
OTHER_SUBTYPES = {"Medium-pace", "N/A"}

ALL_SUBTYPES = PACE_SUBTYPES | SPIN_SUBTYPES | OTHER_SUBTYPES


def normalise_subtype(val: str | None, bowling_type: str | None) -> str:
    """
    Normalise GPT's free-text subtype to one of the canonical 9 values.
    Falls back to rule-based derivation from bowling_type if GPT gives rubbish.
    """
    if not val:
        return _derive_from_bowling_type(bowling_type)

    v = val.strip()

    # Direct match (case-insensitive)
    for s in ALL_SUBTYPES:
        if v.lower() == s.lower():
            return s

    # Fuzzy mapping
    vl = v.lower()

    # Pace fuzzy
    if any(k in vl for k in ("pure pace", "express pace", "express", "raw pace")):
        return "Pure Pace"
    if "swing" in vl:
        return "Swing"
    if "seam" in vl:
        return "Seam"
    if any(k in vl for k in ("fast-medium", "fast medium", "medium-fast", "medium fast")):
        return "Fast-medium"

    # Spin fuzzy
    if any(k in vl for k in ("off-spin", "off spin", "off break", "off-break")):
        return "Off-spin"
    if any(k in vl for k in ("leg-spin", "leg spin", "leg break", "leg-break", "googly")):
        return "Leg-spin"
    if any(k in vl for k in ("left-arm orthodox", "left arm orthodox", "slow left", "orthodox")):
        return "Left-arm orthodox"
    if any(k in vl for k in ("chinaman", "left-arm wrist", "left arm wrist", "unorthodox")):
        return "Chinaman"
    if "medium" in vl:
        return "Medium-pace"

    # Last resort: derive from bowling_type column
    return _derive_from_bowling_type(bowling_type)


def _derive_from_bowling_type(bowling_type: str | None) -> str:
    """Rule-based fallback using the existing bowling_type column."""
    if not bowling_type:
        return "N/A"
    bt = bowling_type.lower()

    if "off-break" in bt or "off break" in bt:
        return "Off-spin"
    if "leg-break" in bt or "leg break" in bt:
        return "Leg-spin"
    if "unorthodox" in bt or "chinaman" in bt:
        return "Chinaman"
    if "orthodox" in bt:
        return "Left-arm orthodox"
    if "fast-medium" in bt or "fast medium" in bt:
        return "Fast-medium"
    if "fast" in bt:
        return "Pure Pace"   # default for pure fast
    if "medium" in bt:
        return "Medium-pace"
    return "N/A"


# ─────────────────────────────────────────────────────────────────────────────
#  OPENAI BATCH CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a cricket expert with encyclopaedic knowledge of every international
and domestic T20 cricketer.

For each bowler provided (given as name + known bowling_style text), classify
their primary bowling subtype from EXACTLY these options:

PACE subtypes (for fast/medium-fast/medium bowlers):
  "Pure Pace"     — bowler whose primary weapon is raw pace/speed
                    (e.g. Shoaib Akhtar, Mitchell Starc when attacking)
  "Swing"         — bowler who swings the ball as primary weapon
                    (e.g. James Anderson, Trent Boult)
  "Seam"          — bowler who moves the ball off the seam as primary weapon
                    (e.g. Tim Southee, Mohammed Shami)
  "Fast-medium"   — bowler who relies on control + variation more than pure pace
                    (e.g. Jasprit Bumrah's yorker style, Chris Woakes)

SPIN subtypes:
  "Off-spin"      — right-arm finger spin (turns from off to leg for right-handers)
                    (e.g. Ravichandran Ashwin, Moeen Ali)
  "Leg-spin"      — right-arm wrist spin (leg-break/googly)
                    (e.g. Shane Warne, Rashid Khan, Yuzvendra Chahal)
  "Left-arm orthodox" — left-arm finger spin (turns from off to leg for left-handers)
                    (e.g. Rangana Herath, Axar Patel)
  "Chinaman"      — left-arm wrist spin (unorthodox)
                    (e.g. Kuldeep Yadav, Brad Hogg)

OTHER:
  "Medium-pace"   — genuine medium-pace, not seam/swing-focused
  "N/A"           — batting specialist, no significant bowling

Return ONLY a JSON array, one object per bowler, in the same input order.
Do not add any text outside the JSON."""

USER_PROMPT_TEMPLATE = """\
Classify the bowling subtype for these {count} cricketers:

{bowler_list}

Return a JSON array with {count} objects in this exact format:
[
  {{
    "player_name": "<exactly as given>",
    "bowling_subtype_detail": "<one of the exact subtype strings above>",
    "confidence": "high or medium or low",
    "notes": "<one sentence explaining why, optional>"
  }},
  ...
]"""


def ask_openai_batch(client: OpenAI, bowlers: list[dict]) -> list[dict]:
    """
    Send a batch of bowler descriptors to GPT-4o-mini.
    Each dict has: player_name, bowling_style, bowling_type.
    Returns list of dicts with player_name, bowling_subtype_detail, confidence.
    """
    lines = []
    for i, b in enumerate(bowlers):
        style_str = b.get("bowling_style") or b.get("bowling_type") or "unknown"
        lines.append(f"{i+1}. {b['player_name']}  |  {style_str}")

    prompt = USER_PROMPT_TEMPLATE.format(
        bowler_list="\n".join(lines),
        count=len(bowlers),
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
            for key in ("bowlers", "results", "data"):
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
    """Add bowling_subtype_detail / bowling_subtype_confidence columns if missing."""
    with pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'player_profiles'
        """)
        existing = {r[0] for r in cur.fetchall()}

    new_cols = []
    if "bowling_subtype_detail"     not in existing:
        new_cols.append("bowling_subtype_detail     TEXT")
    if "bowling_subtype_confidence" not in existing:
        new_cols.append("bowling_subtype_confidence TEXT")

    if new_cols:
        with pg.cursor() as cur:
            for col_def in new_cols:
                col_name = col_def.split()[0]
                cur.execute(
                    f"ALTER TABLE player_profiles "
                    f"ADD COLUMN IF NOT EXISTS {col_def}"
                )
                log(f"  + Added column: {col_name}")
        pg.commit()
    else:
        log("  ✓ Subtype columns already exist")


def load_bowlers(pg, skip_existing: bool, limit: int) -> list[dict]:
    """
    Load players who bowl (is_bowler=1 OR has bowling_style).
    Exclude pure batting specialists with no bowling info at all.
    """
    skip_clause = (
        "AND (bowling_subtype_detail IS NULL OR bowling_subtype_detail = '')"
        if skip_existing else ""
    )
    limit_clause = f"LIMIT {limit}" if limit else ""

    with pg.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(f"""
            SELECT player_name,
                   COALESCE(bowling_style, '')  AS bowling_style,
                   COALESCE(bowling_type,  '')  AS bowling_type,
                   is_bowler
            FROM player_profiles
            WHERE (
                is_bowler IS TRUE
                OR (bowling_style IS NOT NULL AND bowling_style NOT IN ('', '-', 'N/A', 'NA'))
            )
            {skip_clause}
            ORDER BY player_name
            {limit_clause}
        """)
        return [dict(r) for r in cur.fetchall()]


def upsert_batch(pg, results: list[dict]):
    """Write subtype fields back to player_profiles."""
    if not results:
        return
    with pg.cursor() as cur:
        for r in results:
            cur.execute("""
                UPDATE player_profiles
                SET bowling_subtype_detail     = %(bowling_subtype_detail)s,
                    bowling_subtype_confidence = %(bowling_subtype_confidence)s
                WHERE player_name = %(player_name)s
            """, r)
    pg.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Classify bowler subtypes in player_profiles via OpenAI"
    )
    parser.add_argument("--test",          action="store_true",
                        help="Process only first 50 bowlers (quick smoke-test)")
    parser.add_argument("--limit",         type=int, default=0,
                        help="Cap total bowlers processed (e.g. --limit 200)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip bowlers that already have bowling_subtype_detail")
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

    mode = "TEST (50 bowlers)" if args.test else \
           f"LIMIT {row_limit:,}" if row_limit else \
           ("SKIP EXISTING" if args.skip_existing else "FULL RUN")
    log(f"  Mode  : {mode}")
    log(f"  Model : {MODEL}")
    log(f"  Key   : {nvidia_key['key_id']}")
    log(f"  DB    : {(SUPABASE_URL_T20 or '')[:55]}…")
    log()

    # ── Connect + prepare schema ──────────────────────────────────────────────
    pg = open_pg()
    ensure_columns(pg)
    log()

    # ── Load bowlers ──────────────────────────────────────────────────────────
    bowlers = load_bowlers(pg, skip_existing=args.skip_existing, limit=row_limit)
    log(f"  Bowlers to classify : {len(bowlers):,}")
    log()

    if not bowlers:
        log("  Nothing to do.")
        pg.close()
        _write_report(stats={}, end_time=datetime.now())
        return

    # ── Subtype distribution counters ─────────────────────────────────────────
    stats = {
        "total": 0, "classified": 0, "errors": 0,
        "high": 0, "medium": 0, "low": 0,
        # subtype counts
        "Pure Pace": 0, "Swing": 0, "Seam": 0, "Fast-medium": 0,
        "Off-spin": 0, "Leg-spin": 0, "Left-arm orthodox": 0, "Chinaman": 0,
        "Medium-pace": 0, "N/A": 0,
    }

    total_batches = (len(bowlers) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_start in range(0, len(bowlers), BATCH_SIZE):
        batch   = bowlers[batch_start:batch_start + BATCH_SIZE]
        batch_n = batch_start // BATCH_SIZE + 1

        log(f"  Batch {batch_n}/{total_batches}  "
            f"({batch[0]['player_name']} … {batch[-1]['player_name']})")

        raw_results = ask_openai_batch(client, batch)

        if not raw_results:
            log(f"    ⚠ Empty response — using rule-based fallback for batch")
            # Fallback: derive subtype from existing bowling_type
            raw_results = [
                {
                    "player_name":           b["player_name"],
                    "bowling_subtype_detail": None,   # will trigger fallback
                    "confidence":            "low",
                }
                for b in batch
            ]

        # Build lookup by player_name (GPT might reorder)
        result_by_name = {}
        for r in raw_results:
            key = str(r.get("player_name", "")).strip()
            result_by_name[key] = r

        rows_to_write = []
        for b in batch:
            name = b["player_name"]
            r    = result_by_name.get(name) or {}

            raw_subtype = r.get("bowling_subtype_detail") or r.get("subtype") or None
            subtype     = normalise_subtype(raw_subtype, b.get("bowling_type"))
            conf        = (r.get("confidence") or "low").lower()
            if conf not in ("high", "medium", "low"):
                conf = "low"

            rows_to_write.append({
                "player_name":              name,
                "bowling_subtype_detail":   subtype,
                "bowling_subtype_confidence": conf,
            })

            stats["total"]      += 1
            stats["classified"] += 1
            stats[conf]         += 1
            stats[subtype]       = stats.get(subtype, 0) + 1

            log(f"    {name:35s}  →  {subtype:20s}  [{conf}]")

        try:
            upsert_batch(pg, rows_to_write)
        except Exception as e:
            log(f"    ✗ DB write error: {e}")
            pg.rollback()
            stats["errors"] += len(rows_to_write)
            stats["classified"] -= len(rows_to_write)

        # Polite pause between OpenAI calls
        if batch_start + BATCH_SIZE < len(bowlers):
            time.sleep(0.4)

    pg.close()

    # ── Final summary ─────────────────────────────────────────────────────────
    end_time = datetime.now()
    duration = (end_time - START_TIME).total_seconds()

    log()
    log("=" * 72)
    log(f"  Bowlers processed  : {stats['total']:,}")
    log(f"  Classified         : {stats['classified']:,}")
    log(f"  Errors             : {stats['errors']:,}")
    log()
    log(f"  ── Pace subtypes ──────────────────────────")
    log(f"    Pure Pace        : {stats['Pure Pace']:,}")
    log(f"    Swing            : {stats['Swing']:,}")
    log(f"    Seam             : {stats['Seam']:,}")
    log(f"    Fast-medium      : {stats['Fast-medium']:,}")
    log()
    log(f"  ── Spin subtypes ──────────────────────────")
    log(f"    Off-spin         : {stats['Off-spin']:,}")
    log(f"    Leg-spin         : {stats['Leg-spin']:,}")
    log(f"    Left-arm orthodox: {stats['Left-arm orthodox']:,}")
    log(f"    Chinaman         : {stats['Chinaman']:,}")
    log()
    log(f"  ── Other ──────────────────────────────────")
    log(f"    Medium-pace      : {stats['Medium-pace']:,}")
    log(f"    N/A              : {stats['N/A']:,}")
    log()
    log(f"  ── Confidence ─────────────────────────────")
    log(f"    High             : {stats['high']:,}")
    log(f"    Medium           : {stats['medium']:,}")
    log(f"    Low              : {stats['low']:,}")
    log(f"  Duration           : {duration:.1f}s")
    log("=" * 72)

    _write_report(stats, end_time)
    log_file.close()

    # Beep to signal completion
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
  Bowlers processed  : {stats.get('total', 0):,}
  Classified         : {stats.get('classified', 0):,}
  Errors             : {errors:,}

  Pace subtypes:
    Pure Pace         : {stats.get('Pure Pace', 0):,}
    Swing             : {stats.get('Swing', 0):,}
    Seam              : {stats.get('Seam', 0):,}
    Fast-medium       : {stats.get('Fast-medium', 0):,}

  Spin subtypes:
    Off-spin          : {stats.get('Off-spin', 0):,}
    Leg-spin          : {stats.get('Leg-spin', 0):,}
    Left-arm orthodox : {stats.get('Left-arm orthodox', 0):,}
    Chinaman          : {stats.get('Chinaman', 0):,}

  Other:
    Medium-pace       : {stats.get('Medium-pace', 0):,}
    N/A               : {stats.get('N/A', 0):,}

  Confidence:
    High   : {stats.get('high', 0):,}
    Medium : {stats.get('medium', 0):,}
    Low    : {stats.get('low', 0):,}
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
        log()
        log("  ⚠ Interrupted by user.")
        log_file.close()
        sys.exit(1)
    except Exception as e:
        log(f"\nFATAL: {e}\n{traceback.format_exc()}")
        log_file.close()
        sys.exit(1)
