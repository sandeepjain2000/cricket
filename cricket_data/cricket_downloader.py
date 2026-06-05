#!/usr/bin/env python3
"""
================================================================================
Cricket Data Downloader — Cricsheet Test Match Data
================================================================================
Downloads Test match JSON data from cricsheet.org and loads into SQLite.

Modes:
  full         — Download ALL test matches ever recorded
  incremental  — Download matches within a specific date range

Usage:
  python cricket_downloader.py --mode full
  python cricket_downloader.py --mode incremental --today
  python cricket_downloader.py --mode incremental --last-days 7
  python cricket_downloader.py --mode incremental --start-date 2024-01-01 --end-date 2024-03-31
  python cricket_downloader.py --mode incremental --start-date 2024-01-01

Project structure (auto-created next to this script):
  cricket_data/
  ├── cricket_downloader.py   ← this script
  ├── config/
  │   └── settings.ini        ← URL / tuning config
  ├── data/
  │   └── test_matches/
  │       ├── json/           ← permanent JSON store (one file per match)
  │       └── archives/       ← downloaded ZIP archives (kept for reference)
  ├── db/
  │   └── cricket.db          ← SQLite database
  ├── logs/
  │   └── cricket_YYYYMMDD_HHMMSS.log
  └── temp/                   ← auto-cleaned extraction workspace

Dependencies: Python 3.8+ standard library only (no pip installs needed)
================================================================================
"""

import os
import sys
import json
import logging
import sqlite3
import zipfile
import argparse
import configparser
import shutil
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
#  PATH CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.resolve()

DIRS = {
    "json":     BASE_DIR / "data" / "test_matches" / "json",
    "archives": BASE_DIR / "data" / "test_matches" / "archives",
    "db":       BASE_DIR / "db",
    "logs":     BASE_DIR / "logs",
    "temp":     BASE_DIR / "temp",
    "config":   BASE_DIR / "config",
}

DB_PATH            = DIRS["db"] / "cricket.db"
SETTINGS_PATH      = DIRS["config"] / "settings.ini"

# Default Cricsheet URLs (overridable in settings.ini)
DEFAULT_FULL_URL          = "https://cricsheet.org/downloads/tests_json.zip"
DEFAULT_RECENT_URL_TPL    = "https://cricsheet.org/downloads/recently_added_{days}_json.zip"
RECENT_DAYS_BUFFER        = 3   # extra days buffer when using recently_added


# ─────────────────────────────────────────────────────────────────────────────
#  DIRECTORY + CONFIG SETUP
# ─────────────────────────────────────────────────────────────────────────────

def setup_directories() -> None:
    for path in DIRS.values():
        path.mkdir(parents=True, exist_ok=True)


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["cricsheet"] = {
        "full_url":          DEFAULT_FULL_URL,
        "recent_url_tpl":    DEFAULT_RECENT_URL_TPL,
        "recent_days_limit": "365",
    }
    cfg["download"] = {
        "chunk_size_kb":     "64",
        "request_timeout_s": "120",
    }
    cfg["logging"] = {
        "console_level": "INFO",
        "file_level":    "DEBUG",
    }
    if SETTINGS_PATH.exists():
        cfg.read(SETTINGS_PATH)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(cfg: configparser.ConfigParser):
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file   = DIRS["logs"] / f"cricket_{timestamp}.log"
    logger     = logging.getLogger("cricket")
    logger.setLevel(logging.DEBUG)

    fmt_full   = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S")
    fmt_short  = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                                   datefmt="%H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(getattr(logging, cfg["logging"]["file_level"].upper(), logging.DEBUG))
    fh.setFormatter(fmt_full)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, cfg["logging"]["console_level"].upper(), logging.INFO))
    ch.setFormatter(fmt_short)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger, log_file


# ─────────────────────────────────────────────────────────────────────────────
#  PROGRESS BAR
# ─────────────────────────────────────────────────────────────────────────────

class ProgressBar:
    """Lightweight ASCII progress bar — no external libraries needed."""

    BAR_WIDTH = 50

    def __init__(self, total: int, label: str = "Downloading"):
        self.total   = total
        self.label   = label
        self.current = 0

    def update(self, delta: int) -> None:
        self.current += delta
        if self.total > 0:
            pct    = self.current / self.total
            filled = int(self.BAR_WIDTH * pct)
            bar    = "█" * filled + "░" * (self.BAR_WIDTH - filled)
            done   = self.current / 1_048_576
            total  = self.total   / 1_048_576
            print(f"\r  {self.label}: [{bar}] {pct:5.1%}  {done:.1f}/{total:.1f} MB",
                  end="", flush=True)
        else:
            mb = self.current / 1_048_576
            print(f"\r  {self.label}: {mb:.1f} MB downloaded", end="", flush=True)

    def finish(self) -> None:
        print()   # newline after progress bar


# ─────────────────────────────────────────────────────────────────────────────
#  DOWNLOADER
# ─────────────────────────────────────────────────────────────────────────────

def download_file(url: str, dest: Path, logger, cfg: configparser.ConfigParser) -> bool:
    """Download *url* to *dest* with progress reporting. Returns True on success."""
    logger.info(f"Downloading : {url}")
    logger.info(f"Destination : {dest}")
    chunk_bytes = int(cfg["download"]["chunk_size_kb"]) * 1024
    timeout_s   = int(cfg["download"]["request_timeout_s"])

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CricketDataDownloader/1.0 (github.com/cricsheet)"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            total    = int(resp.headers.get("Content-Length", 0))
            progress = ProgressBar(total, "Downloading")

            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(chunk_bytes)
                    if not chunk:
                        break
                    fh.write(chunk)
                    progress.update(len(chunk))

            progress.finish()

        size_mb = dest.stat().st_size / 1_048_576
        logger.info(f"Download OK : {size_mb:.2f} MB  →  {dest.name}")
        return True

    except urllib.error.HTTPError as exc:
        logger.warning(f"HTTP {exc.code} ({exc.reason}) — {url}")
        return False
    except urllib.error.URLError as exc:
        logger.error(f"URL error : {exc.reason}")
        return False
    except Exception as exc:
        logger.error(f"Download failed : {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  ZIP EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_zip(zip_path: Path, extract_dir: Path, logger) -> bool:
    """Extract all *.json members from *zip_path* into *extract_dir*."""
    logger.info(f"Extracting  : {zip_path.name}  →  {extract_dir}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".json")]
            total   = len(members)
            logger.info(f"Archive contains {total:,} JSON files")
            for i, member in enumerate(members, 1):
                zf.extract(member, extract_dir)
                if i % 500 == 0 or i == total:
                    logger.info(f"  Extracted {i:,}/{total:,}")
        logger.info("Extraction complete")
        return True
    except zipfile.BadZipFile as exc:
        logger.error(f"Bad zip : {exc}")
        return False
    except Exception as exc:
        logger.error(f"Extraction failed : {exc}")
        return False


def find_json_root(extract_dir: Path) -> Path:
    """Return directory that actually holds the *.json match files."""
    files = list(extract_dir.rglob("*.json"))
    if not files:
        raise FileNotFoundError(f"No JSON files found under {extract_dir}")
    return files[0].parent


# ─────────────────────────────────────────────────────────────────────────────
#  DATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def match_in_date_range(json_path: Path, start: str, end: str) -> bool:
    """Return True if the match's first date falls within [start, end]."""
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            info  = json.load(fh).get("info", {})
        dates = info.get("dates", [])
        if not dates:
            return False
        return start <= dates[0] <= end
    except Exception:
        return False


def copy_matching_files(src_dir: Path, dest_dir: Path, start: str, end: str,
                        logger) -> list:
    """
    Copy JSON files whose match start-date is within [start, end] from
    *src_dir* to *dest_dir*.  Returns list of copied Path objects.
    """
    all_files = list(src_dir.glob("*.json"))
    logger.info(f"Scanning {len(all_files):,} JSON files for date range {start} → {end}")
    copied = []
    for jf in all_files:
        if match_in_date_range(jf, start, end):
            dest = dest_dir / jf.name
            shutil.copy2(jf, dest)
            copied.append(dest)
    logger.info(f"Copied {len(copied):,} date-matching files → {dest_dir}")
    return copied


def copy_all_files(src_dir: Path, dest_dir: Path, logger) -> list:
    """Copy all JSON files from *src_dir* to *dest_dir*. Returns list of paths."""
    all_files = list(src_dir.glob("*.json"))
    logger.info(f"Copying {len(all_files):,} JSON files → {dest_dir}")
    copied = []
    for jf in all_files:
        dest = dest_dir / jf.name
        shutil.copy2(jf, dest)
        copied.append(dest)
    logger.info("Copy complete")
    return copied


# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE MANAGER
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- ── Matches ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS matches (
    match_id            TEXT    PRIMARY KEY,
    match_type          TEXT,
    gender              TEXT,
    start_date          TEXT,
    end_date            TEXT,
    dates_json          TEXT,       -- JSON array of all match dates
    venue               TEXT,
    city                TEXT,
    team1               TEXT,
    team2               TEXT,
    toss_winner         TEXT,
    toss_decision       TEXT,       -- 'bat' | 'field'
    outcome_winner      TEXT,
    outcome_result      TEXT,       -- 'win' | 'draw' | 'tie' | 'no result'
    outcome_by_type     TEXT,       -- 'runs' | 'wickets' | 'innings'
    outcome_by_value    INTEGER,
    balls_per_over      INTEGER     DEFAULT 6,
    event_name          TEXT,
    event_match_number  INTEGER,
    season              TEXT,
    umpires_json        TEXT,
    match_referees_json TEXT,
    json_file_path      TEXT,
    data_version        TEXT,
    imported_at         TEXT        NOT NULL
);

-- ── Innings ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS innings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT    NOT NULL,
    innings_number  INTEGER NOT NULL,
    team            TEXT,
    declared        INTEGER DEFAULT 0,
    forfeited       INTEGER DEFAULT 0,
    target_runs     INTEGER,
    target_overs    REAL,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

-- ── Overs ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS overs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    innings_id      INTEGER NOT NULL,
    match_id        TEXT    NOT NULL,
    over_number     INTEGER NOT NULL,
    FOREIGN KEY (innings_id) REFERENCES innings(id)
);

-- ── Deliveries (ball-by-ball) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    over_id             INTEGER NOT NULL,
    innings_id          INTEGER NOT NULL,
    match_id            TEXT    NOT NULL,
    ball_in_over        INTEGER,
    batter              TEXT,
    non_striker         TEXT,
    bowler              TEXT,
    runs_batter         INTEGER DEFAULT 0,
    runs_extras         INTEGER DEFAULT 0,
    runs_total          INTEGER DEFAULT 0,
    extras_wides        INTEGER DEFAULT 0,
    extras_noballs      INTEGER DEFAULT 0,
    extras_byes         INTEGER DEFAULT 0,
    extras_legbyes      INTEGER DEFAULT 0,
    extras_penalty      INTEGER DEFAULT 0,
    is_wicket           INTEGER DEFAULT 0,
    wicket_kind         TEXT,           -- first wicket kind (convenience)
    wicket_player_out   TEXT,           -- first wicket player (convenience)
    wickets_json        TEXT,           -- full wickets array as JSON
    review_json         TEXT,
    replacements_json   TEXT,
    FOREIGN KEY (over_id) REFERENCES overs(id)
);

-- ── Players registry ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS players (
    identifier  TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

-- ── Per-match squad ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS match_players (
    match_id    TEXT    NOT NULL,
    team        TEXT    NOT NULL,
    player_name TEXT    NOT NULL,
    player_id   TEXT,
    PRIMARY KEY (match_id, team, player_name),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

-- ── Download run audit log ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS download_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp       TEXT    NOT NULL,
    mode                TEXT    NOT NULL,
    filter_start_date   TEXT,
    filter_end_date     TEXT,
    zip_url             TEXT,
    matches_scanned     INTEGER DEFAULT 0,
    matches_inserted    INTEGER DEFAULT 0,
    matches_skipped     INTEGER DEFAULT 0,
    matches_filtered    INTEGER DEFAULT 0,
    errors              INTEGER DEFAULT 0,
    status              TEXT,
    error_message       TEXT,
    duration_seconds    REAL
);

-- ── Indexes ────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_matches_start_date   ON matches(start_date);
CREATE INDEX IF NOT EXISTS idx_matches_team1        ON matches(team1);
CREATE INDEX IF NOT EXISTS idx_matches_team2        ON matches(team2);
CREATE INDEX IF NOT EXISTS idx_innings_match        ON innings(match_id);
CREATE INDEX IF NOT EXISTS idx_overs_innings        ON overs(innings_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_over      ON deliveries(over_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_match     ON deliveries(match_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_batter    ON deliveries(batter);
CREATE INDEX IF NOT EXISTS idx_deliveries_bowler    ON deliveries(bowler);
CREATE INDEX IF NOT EXISTS idx_deliveries_wicket    ON deliveries(is_wicket) WHERE is_wicket = 1;
"""


class DatabaseManager:
    """Context-manager wrapper around an SQLite connection."""

    def __init__(self, db_path: Path, logger):
        self.db_path = db_path
        self.logger  = logger
        self.conn: sqlite3.Connection | None = None

    # ── context manager ───────────────────────────────────────────────────

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Performance pragmas safe for WAL mode
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous  = NORMAL")
        self.conn.execute("PRAGMA cache_size   = -32768")   # 32 MB cache
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA temp_store   = MEMORY")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()

    # ── schema ────────────────────────────────────────────────────────────

    def setup_schema(self) -> None:
        self.logger.info("Initialising database schema…")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        self.logger.info(f"Database ready : {self.db_path}")

    # ── helpers ───────────────────────────────────────────────────────────

    def match_exists(self, match_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
        )
        return cur.fetchone() is not None

    # ── insert a complete match ───────────────────────────────────────────

    def insert_match(self, raw: dict) -> None:
        """
        Parse a Cricsheet JSON document and insert all related rows:
        match → match_players → players → innings → overs → deliveries.
        """
        info     = raw.get("info", {})
        meta     = raw.get("meta", {})
        match_id = raw["_match_id"]

        # ── dates ─────────────────────────────────────────────────────────
        dates      = info.get("dates", [])
        start_date = dates[0]  if dates else None
        end_date   = dates[-1] if dates else None

        # ── teams ─────────────────────────────────────────────────────────
        teams = info.get("teams", [])
        team1 = teams[0] if len(teams) > 0 else None
        team2 = teams[1] if len(teams) > 1 else None

        # ── outcome ───────────────────────────────────────────────────────
        outcome          = info.get("outcome", {})
        outcome_winner   = outcome.get("winner")
        # result is used when there is no winner (draw, tie, no result)
        outcome_result   = outcome.get("result") or ("win" if outcome_winner else None)
        outcome_by       = outcome.get("by", {})
        outcome_by_type  = next(iter(outcome_by), None)
        outcome_by_value = outcome_by.get(outcome_by_type) if outcome_by_type else None

        # ── officials ─────────────────────────────────────────────────────
        officials     = info.get("officials", {})
        umpires       = officials.get("umpires", [])
        referees      = officials.get("match_referees", [])

        # ── toss / event ──────────────────────────────────────────────────
        toss  = info.get("toss",  {})
        event = info.get("event", {})

        # ── insert match row ──────────────────────────────────────────────
        self.conn.execute("""
            INSERT OR REPLACE INTO matches (
                match_id, match_type, gender,
                start_date, end_date, dates_json,
                venue, city,
                team1, team2,
                toss_winner, toss_decision,
                outcome_winner, outcome_result, outcome_by_type, outcome_by_value,
                balls_per_over,
                event_name, event_match_number, season,
                umpires_json, match_referees_json,
                json_file_path, data_version, imported_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            match_id,
            info.get("match_type"),
            info.get("gender"),
            start_date, end_date, json.dumps(dates),
            info.get("venue"), info.get("city"),
            team1, team2,
            toss.get("winner"), toss.get("decision"),
            outcome_winner, outcome_result, outcome_by_type, outcome_by_value,
            info.get("balls_per_over", 6),
            event.get("name"), event.get("match_number"),
            info.get("season"),
            json.dumps(umpires), json.dumps(referees),
            raw.get("_json_file_path", ""),
            meta.get("data_version"),
            datetime.now().isoformat(),
        ))

        # ── players registry ──────────────────────────────────────────────
        registry = info.get("registry", {}).get("people", {})
        for name, pid in registry.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO players (identifier, name) VALUES (?,?)",
                (pid, name)
            )

        # ── squad per team ────────────────────────────────────────────────
        for team, squad in info.get("players", {}).items():
            for player_name in squad:
                self.conn.execute("""
                    INSERT OR IGNORE INTO match_players
                        (match_id, team, player_name, player_id)
                    VALUES (?,?,?,?)
                """, (match_id, team, player_name, registry.get(player_name)))

        # ── innings ───────────────────────────────────────────────────────
        for inn_idx, inn in enumerate(raw.get("innings", []), 1):
            target = inn.get("target", {})
            cur = self.conn.execute("""
                INSERT INTO innings
                    (match_id, innings_number, team,
                     declared, forfeited, target_runs, target_overs)
                VALUES (?,?,?,?,?,?,?)
            """, (
                match_id, inn_idx, inn.get("team"),
                1 if inn.get("declared") else 0,
                1 if inn.get("forfeited") else 0,
                target.get("runs"), target.get("overs"),
            ))
            innings_id = cur.lastrowid

            # ── overs ─────────────────────────────────────────────────────
            for over_dict in inn.get("overs", []):
                cur = self.conn.execute("""
                    INSERT INTO overs (innings_id, match_id, over_number)
                    VALUES (?,?,?)
                """, (innings_id, match_id, over_dict.get("over", 0)))
                over_id = cur.lastrowid

                # ── deliveries ────────────────────────────────────────────
                for ball_idx, dlv in enumerate(over_dict.get("deliveries", []), 1):
                    runs    = dlv.get("runs",   {})
                    extras  = dlv.get("extras", {})
                    wickets = dlv.get("wickets", [])

                    # First wicket for convenience columns; all in JSON column
                    w1               = wickets[0] if wickets else {}
                    wicket_fielders  = [f.get("name") for f in w1.get("fielders", [])]

                    self.conn.execute("""
                        INSERT INTO deliveries (
                            over_id, innings_id, match_id, ball_in_over,
                            batter, non_striker, bowler,
                            runs_batter, runs_extras, runs_total,
                            extras_wides, extras_noballs, extras_byes,
                            extras_legbyes, extras_penalty,
                            is_wicket, wicket_kind, wicket_player_out,
                            wickets_json, review_json, replacements_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        over_id, innings_id, match_id, ball_idx,
                        dlv.get("batter"), dlv.get("non_striker"), dlv.get("bowler"),
                        runs.get("batter",   0),
                        runs.get("extras",   0),
                        runs.get("total",    0),
                        extras.get("wides",   0),
                        extras.get("noballs", 0),
                        extras.get("byes",    0),
                        extras.get("legbyes", 0),
                        extras.get("penalty", 0),
                        1 if wickets else 0,
                        w1.get("kind"),
                        w1.get("player_out"),
                        json.dumps(wickets)              if wickets          else None,
                        json.dumps(dlv["review"])        if "review"        in dlv else None,
                        json.dumps(dlv["replacements"])  if "replacements"  in dlv else None,
                    ))

    # ── audit log ─────────────────────────────────────────────────────────

    def log_run(self, run: dict) -> None:
        self.conn.execute("""
            INSERT INTO download_runs (
                run_timestamp, mode,
                filter_start_date, filter_end_date, zip_url,
                matches_scanned, matches_inserted, matches_skipped,
                matches_filtered, errors,
                status, error_message, duration_seconds
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            run.get("timestamp"),    run.get("mode"),
            run.get("start_date"),   run.get("end_date"),
            run.get("zip_url"),
            run.get("scanned",  0),  run.get("inserted", 0),
            run.get("skipped",  0),  run.get("filtered", 0),
            run.get("errors",   0),
            run.get("status"),       run.get("error_message"),
            run.get("duration_seconds"),
        ))
        self.conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
#  MATCH PROCESSING (JSON → SQLite)
# ─────────────────────────────────────────────────────────────────────────────

def process_json_files(
    json_files:  list,
    db_manager:  DatabaseManager,
    logger,
    start_date:  str | None = None,
    end_date:    str | None = None,
) -> dict:
    """
    Load *json_files* (list of Path) into the database.
    Skips already-imported matches (idempotent).
    If *start_date* / *end_date* are provided, also filters by match date.
    """
    total  = len(json_files)
    stats  = dict(scanned=0, inserted=0, skipped=0, filtered=0, errors=0)
    logger.info(f"Processing {total:,} JSON files…")

    for idx, jf in enumerate(json_files, 1):
        match_id = jf.stem   # filename without extension = Cricsheet match ID

        # ── optional date filter ──────────────────────────────────────────
        if start_date and end_date:
            if not match_in_date_range(jf, start_date, end_date):
                stats["filtered"] += 1
                continue

        # ── skip already imported ─────────────────────────────────────────
        if db_manager.match_exists(match_id):
            stats["skipped"] += 1
            logger.debug(f"  [skip] {match_id}")
            continue

        # ── parse & insert ────────────────────────────────────────────────
        stats["scanned"] += 1
        try:
            with open(jf, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            raw["_match_id"]        = match_id
            raw["_json_file_path"]  = str(jf)
            db_manager.insert_match(raw)
            stats["inserted"] += 1
        except json.JSONDecodeError as exc:
            logger.error(f"  JSON parse error [{jf.name}]: {exc}")
            stats["errors"] += 1
        except Exception as exc:
            logger.error(f"  Error [{jf.name}]: {exc}", exc_info=False)
            stats["errors"] += 1

        # ── periodic progress ─────────────────────────────────────────────
        if idx % 200 == 0 or idx == total:
            logger.info(
                f"  Progress {idx:>5}/{total}  "
                f"inserted={stats['inserted']}  "
                f"skipped={stats['skipped']}  "
                f"filtered={stats['filtered']}  "
                f"errors={stats['errors']}"
            )

    db_manager.conn.commit()
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  DOWNLOAD ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────────────

def _section_header(logger, title: str) -> None:
    logger.info("─" * 60)
    logger.info(f"  {title}")
    logger.info("─" * 60)


def run_full(db_manager: DatabaseManager, logger, cfg: configparser.ConfigParser) -> dict:
    """Download and import ALL Cricsheet test matches."""
    _section_header(logger, "MODE : FULL DOWNLOAD")

    t0    = datetime.now()
    ts    = t0.strftime("%Y%m%d_%H%M%S")
    url   = cfg["cricsheet"]["full_url"]
    run   = dict(timestamp=t0.isoformat(), mode="full", zip_url=url, status="running")

    zip_path     = DIRS["archives"] / f"tests_json_{ts}.zip"
    extract_dir  = DIRS["temp"]     / f"extract_{ts}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        if not download_file(url, zip_path, logger, cfg):
            raise RuntimeError("Download failed — check URL and connectivity")

        if not extract_zip(zip_path, extract_dir, logger):
            raise RuntimeError("ZIP extraction failed")

        json_root  = find_json_root(extract_dir)
        logger.info(f"JSON root   : {json_root}")

        copied     = copy_all_files(json_root, DIRS["json"], logger)
        stats      = process_json_files(copied, db_manager, logger)

        run.update(**stats, status="success",
                   duration_seconds=(datetime.now() - t0).total_seconds())

        _section_header(logger, "FULL DOWNLOAD COMPLETE")
        logger.info(f"  Inserted    : {stats['inserted']:,}")
        logger.info(f"  Skipped     : {stats['skipped']:,}  (already in DB)")
        logger.info(f"  Errors      : {stats['errors']}")
        logger.info(f"  Duration    : {run['duration_seconds']:.1f}s")

    except Exception as exc:
        run.update(status="error", error_message=str(exc),
                   duration_seconds=(datetime.now() - t0).total_seconds())
        logger.error(f"Full download failed : {exc}")
        raise
    finally:
        logger.info("Cleaning up temp directory…")
        shutil.rmtree(extract_dir, ignore_errors=True)
        db_manager.log_run(run)

    return run


def run_incremental(
    db_manager: DatabaseManager,
    logger,
    cfg:        configparser.ConfigParser,
    start_date: str,
    end_date:   str,
) -> dict:
    """Download and import test matches whose start-date is within [start_date, end_date]."""
    _section_header(logger, f"MODE : INCREMENTAL  {start_date}  →  {end_date}")

    t0  = datetime.now()
    ts  = t0.strftime("%Y%m%d_%H%M%S")
    run = dict(timestamp=t0.isoformat(), mode="incremental",
               start_date=start_date, end_date=end_date, status="running")

    # ── choose URL — try recently_added first if range is recent enough ───
    today           = date.today()
    d_start         = datetime.strptime(start_date, "%Y-%m-%d").date()
    days_back       = (today - d_start).days + RECENT_DAYS_BUFFER
    recent_limit    = int(cfg["cricsheet"]["recent_days_limit"])

    if days_back <= recent_limit:
        url = cfg["cricsheet"]["recent_url_tpl"].format(days=days_back)
        logger.info(f"Trying recently_added URL ({days_back} days) : {url}")
    else:
        url = cfg["cricsheet"]["full_url"]
        logger.info(f"Date range > {recent_limit} days — using full URL")

    zip_path    = DIRS["archives"] / f"tests_incremental_{start_date}_{end_date}_{ts}.zip"
    extract_dir = DIRS["temp"]     / f"extract_{ts}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    run["zip_url"] = url

    try:
        ok = download_file(url, zip_path, logger, cfg)
        if not ok and url != cfg["cricsheet"]["full_url"]:
            # Fall back to full ZIP
            logger.warning("recently_added download failed — falling back to full ZIP")
            url      = cfg["cricsheet"]["full_url"]
            zip_path = DIRS["archives"] / f"tests_full_fallback_{ts}.zip"
            run["zip_url"] = url
            if not download_file(url, zip_path, logger, cfg):
                raise RuntimeError("Both recently_added and full-zip downloads failed")

        if not extract_zip(zip_path, extract_dir, logger):
            raise RuntimeError("ZIP extraction failed")

        json_root = find_json_root(extract_dir)
        logger.info(f"JSON root   : {json_root}")

        # Copy only files matching date range to permanent JSON store
        copied = copy_matching_files(json_root, DIRS["json"], start_date, end_date, logger)

        if not copied:
            logger.warning("No matches found for the specified date range — nothing to import")
            run.update(inserted=0, skipped=0, filtered=0, scanned=0, errors=0,
                       status="success",
                       duration_seconds=(datetime.now() - t0).total_seconds())
            return run

        # Process (date filter already applied via copy_matching_files;
        # pass None here to avoid re-scanning — just check DB duplicates)
        stats = process_json_files(copied, db_manager, logger)

        run.update(**stats, status="success",
                   duration_seconds=(datetime.now() - t0).total_seconds())

        _section_header(logger, "INCREMENTAL DOWNLOAD COMPLETE")
        logger.info(f"  Date range  : {start_date}  →  {end_date}")
        logger.info(f"  Inserted    : {stats['inserted']:,}")
        logger.info(f"  Skipped     : {stats['skipped']:,}  (already in DB)")
        logger.info(f"  Errors      : {stats['errors']}")
        logger.info(f"  Duration    : {run['duration_seconds']:.1f}s")

    except Exception as exc:
        run.update(status="error", error_message=str(exc),
                   duration_seconds=(datetime.now() - t0).total_seconds())
        logger.error(f"Incremental download failed : {exc}")
        raise
    finally:
        logger.info("Cleaning up temp directory…")
        shutil.rmtree(extract_dir, ignore_errors=True)
        db_manager.log_run(run)

    return run


# ─────────────────────────────────────────────────────────────────────────────
#  CLI ARGUMENT PARSING
# ─────────────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cricket_downloader",
        description="Download Cricsheet Test match JSON data → SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
  Full download (all historical test matches):
      python cricket_downloader.py --mode full

  Today's matches only:
      python cricket_downloader.py --mode incremental --today

  Last 7 days:
      python cricket_downloader.py --mode incremental --last-days 7

  Custom date range:
      python cricket_downloader.py --mode incremental --start-date 2024-01-01 --end-date 2024-03-31

  From a date through today:
      python cricket_downloader.py --mode incremental --start-date 2025-01-01
        """,
    )

    parser.add_argument(
        "--mode", required=True, choices=["full", "incremental"],
        help="'full' downloads everything; 'incremental' uses a date range",
    )

    grp = parser.add_argument_group("Date range (incremental mode)")
    mx  = grp.add_mutually_exclusive_group()
    mx.add_argument("--today",      action="store_true",
                    help="Download matches played today")
    mx.add_argument("--last-days",  type=int, metavar="N",
                    help="Download matches from the last N days")
    mx.add_argument("--start-date", metavar="YYYY-MM-DD",
                    help="Range start date (inclusive)")
    grp.add_argument("--end-date",  metavar="YYYY-MM-DD",
                     help="Range end date (inclusive, default = today)")

    return parser


def resolve_dates(args) -> tuple[str | None, str | None]:
    """Return (start_date_str, end_date_str) or (None, None) for full mode."""
    if args.mode == "full":
        return None, None

    today_str = date.today().isoformat()

    if args.today:
        return today_str, today_str

    if args.last_days:
        start = (date.today() - timedelta(days=args.last_days)).isoformat()
        return start, today_str

    if args.start_date:
        # Validate
        for label, val in [("--start-date", args.start_date),
                           ("--end-date",   args.end_date or today_str)]:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                print(f"ERROR: Invalid date for {label}: '{val}' — use YYYY-MM-DD", file=sys.stderr)
                sys.exit(1)
        end = args.end_date or today_str
        if args.start_date > end:
            print("ERROR: --start-date must be ≤ --end-date", file=sys.stderr)
            sys.exit(1)
        return args.start_date, end

    print("ERROR: incremental mode requires --today, --last-days N, or --start-date YYYY-MM-DD",
          file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
#  BANNER
# ─────────────────────────────────────────────────────────────────────────────

BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║       CRICKET DATA DOWNLOADER  ·  Cricsheet  ·  Test Only    ║
║       JSON  →  SQLite                                        ║
╚══════════════════════════════════════════════════════════════╝
"""


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Clear screen
    os.system("cls" if os.name == "nt" else "clear")

    # 2. Ensure folder structure exists
    setup_directories()

    # 3. Load config
    cfg = load_config()

    # 4. Set up logging (file + console)
    logger, log_file = setup_logging(cfg)

    # 5. Print banner
    print(BANNER)
    logger.info("Cricket Data Downloader starting up")
    logger.info(f"Base dir : {BASE_DIR}")
    logger.info(f"Database : {DB_PATH}")
    logger.info(f"Log file : {log_file}")

    # 6. Parse args
    parser              = build_arg_parser()
    args                = parser.parse_args()
    start_date, end_date = resolve_dates(args)

    if start_date:
        logger.info(f"Date range : {start_date}  →  {end_date}")

    # 7. Connect to DB, run
    with DatabaseManager(DB_PATH, logger) as db:
        db.setup_schema()
        try:
            if args.mode == "full":
                run_full(db, logger, cfg)
            else:
                run_incremental(db, logger, cfg, start_date, end_date)

            logger.info("=" * 62)
            logger.info("  ALL DONE")
            logger.info(f"  Database : {DB_PATH}")
            logger.info(f"  Log file : {log_file}")
            logger.info("=" * 62)

        except KeyboardInterrupt:
            logger.warning("Interrupted by user (Ctrl+C)")
            sys.exit(130)
        except Exception as exc:
            logger.critical(f"Fatal error: {exc}", exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
