-- =============================================================================
--  player_profiles  —  T20 Cricket Player Intelligence
--  Created: 2026-04-18
--
--  This table stores per-player metadata scraped from ESPN Cricinfo.
--  It is the authoritative source for:
--    • batting_style     (as-scraped, e.g. "Right hand Bat")
--    • bowling_style     (as-scraped, e.g. "Right arm Fast")
--    • bowling_type      (normalised category for analytics queries)
--    • playing_role      (as-scraped, e.g. "Bowler", "Allrounder")
--
--  bowling_type normalisation:
--    The raw bowling_style strings from ESPN Cricinfo vary enormously.
--    We map them to one of the following standard categories:
--
--    Pace:
--      Right arm Fast
--      Right arm Fast-Medium
--      Right arm Medium-Fast
--      Right arm Medium
--      Left arm Fast
--      Left arm Fast-Medium
--      Left arm Medium-Fast
--      Left arm Medium
--
--    Spin:
--      Right arm Off-break         (Off Spin)
--      Right arm Leg-break         (Leg Spin)
--      Left arm Orthodox           (Slow Left Arm)
--      Left arm Unorthodox         (Chinaman)
--      Right arm Off-break Googly  (Doosra specialist)
--
--    Non-bowler:
--      NA
-- =============================================================================

-- ── Postgres / Supabase version ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS player_profiles (
    player_name             TEXT        NOT NULL,           -- matches name in deliveries / match_players
    espncricinfo_id         INTEGER,                        -- numeric ID from ESPN Cricinfo URL
    full_name               TEXT,                           -- full legal name (e.g. Jasprit Jasbirsingh Bumrah)
    date_of_birth           TEXT,                           -- ISO date string YYYY-MM-DD
    birth_place             TEXT,                           -- city / country of birth
    batting_style           TEXT,                           -- raw string from ESPN Cricinfo
    bowling_style           TEXT,                           -- raw string from ESPN Cricinfo
    bowling_type            TEXT        NOT NULL DEFAULT 'NA',
                                                            -- normalised category (see header)
                                                            -- values: 'Right arm Fast' | 'Right arm Fast-Medium' |
                                                            --         'Right arm Medium' | 'Left arm Fast' |
                                                            --         'Left arm Fast-Medium' | 'Left arm Medium' |
                                                            --         'Right arm Off-break' | 'Right arm Leg-break' |
                                                            --         'Left arm Orthodox' | 'Left arm Unorthodox' |
                                                            --         'NA'
    bowling_subtype         TEXT,                           -- further detail, e.g. 'Googly', 'Carrom Ball'
    batting_hand            TEXT,                           -- 'Right' | 'Left' (derived from batting_style)
    bowling_arm             TEXT,                           -- 'Right' | 'Left' (derived from bowling_style)
    playing_role            TEXT,                           -- 'Bowler' | 'Batter' | 'Allrounder' | 'Wicketkeeper'
    is_bowler               BOOLEAN     NOT NULL DEFAULT FALSE,
    is_batter               BOOLEAN     NOT NULL DEFAULT TRUE,
    is_wicketkeeper         BOOLEAN     NOT NULL DEFAULT FALSE,
    is_allrounder           BOOLEAN     NOT NULL DEFAULT FALSE,
    nationality             TEXT,
    teams_json              TEXT,                           -- JSON array of teams represented
    espncricinfo_url        TEXT,
    scraped_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (player_name)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_pp_bowling_type   ON player_profiles (bowling_type);
CREATE INDEX IF NOT EXISTS idx_pp_batting_hand   ON player_profiles (batting_hand);
CREATE INDEX IF NOT EXISTS idx_pp_bowling_arm    ON player_profiles (bowling_arm);
CREATE INDEX IF NOT EXISTS idx_pp_playing_role   ON player_profiles (playing_role);
CREATE INDEX IF NOT EXISTS idx_pp_espn_id        ON player_profiles (espncricinfo_id);
CREATE INDEX IF NOT EXISTS idx_pp_nationality    ON player_profiles (nationality);

-- ── Comments (Postgres) ───────────────────────────────────────────────────────
COMMENT ON TABLE  player_profiles                   IS 'ESPN Cricinfo player metadata for T20 analytics — scraper-managed';
COMMENT ON COLUMN player_profiles.bowling_type      IS 'Normalised bowling category for analytics. NA = non-bowler or unknown.';
COMMENT ON COLUMN player_profiles.bowling_subtype   IS 'Optional sub-specialisation, e.g. Googly, Carrom Ball, Seam.';
COMMENT ON COLUMN player_profiles.batting_hand      IS 'Right or Left, derived from batting_style.';
COMMENT ON COLUMN player_profiles.bowling_arm       IS 'Right or Left, derived from bowling_style. NULL for non-bowlers.';


-- =============================================================================
--  SQLite version (for local caching — used by scraper before Supabase upload)
-- =============================================================================
-- Uncomment this block when running against the local SQLite DB:
--
-- CREATE TABLE IF NOT EXISTS player_profiles (
--     player_name             TEXT    NOT NULL PRIMARY KEY,
--     espncricinfo_id         INTEGER,
--     full_name               TEXT,
--     date_of_birth           TEXT,
--     birth_place             TEXT,
--     batting_style           TEXT,
--     bowling_style           TEXT,
--     bowling_type            TEXT    NOT NULL DEFAULT 'NA',
--     bowling_subtype         TEXT,
--     batting_hand            TEXT,
--     bowling_arm             TEXT,
--     playing_role            TEXT,
--     is_bowler               INTEGER NOT NULL DEFAULT 0,
--     is_batter               INTEGER NOT NULL DEFAULT 1,
--     is_wicketkeeper         INTEGER NOT NULL DEFAULT 0,
--     is_allrounder           INTEGER NOT NULL DEFAULT 0,
--     nationality             TEXT,
--     teams_json              TEXT,
--     espncricinfo_url        TEXT,
--     scraped_at              TEXT    NOT NULL DEFAULT (datetime('now')),
--     updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
-- );
