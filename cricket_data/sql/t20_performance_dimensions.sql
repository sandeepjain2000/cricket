-- =============================================================================
--  T20 PERFORMANCE DIMENSIONS — Migration
--  Created: 2026-04-18
--
--  OVERVIEW
--  --------
--  This migration adds three new tables that enable full slice-and-dice of
--  T20 batting (and bowling) performance across every analytics dimension:
--
--    match_meta_t20   — per-match enriched metadata
--      • captain / vice-captain per team
--      • competition type (bilateral vs tournament)
--      • home/away/neutral venue classification
--
--    delivery_dimensions  — per-delivery computed labels
--      • over phase (powerplay / middle / death)
--      • bowling type faced (from player_profiles)
--      • innings number (batting first / second)
--
--  Together with player_profiles these tables support queries like:
--    "Virat Kohli's T20I strike rate in the powerplay vs leg-spin
--     when batting second in ICC tournaments, as captain"
-- =============================================================================


-- =============================================================================
--  1.  match_meta_t20
--  Enriches each match with captain / VC, competition type, and venue context
-- =============================================================================

CREATE TABLE IF NOT EXISTS match_meta_t20 (
    match_id                TEXT        NOT NULL,
    team                    TEXT        NOT NULL,   -- team this row describes

    -- ── Leadership ──────────────────────────────────────────────────────────
    captain                 TEXT,                   -- player name of captain for this team
    vice_captain            TEXT,                   -- player name of VC (if known)

    -- ── Competition type ────────────────────────────────────────────────────
    competition_type        TEXT,
        -- 'Bilateral'   — two-nation bilateral series
        -- 'Tournament'  — multi-team tournament (ICC or regional)
        -- 'Tri-series'  — three-team series
        -- 'Domestic'    — domestic T20 league / franchise cricket
        -- 'Unknown'
    competition_name        TEXT,                   -- e.g. "ICC T20 World Cup 2024"
    series_name             TEXT,                   -- e.g. "India vs Australia T20I 2024"

    -- ── Venue / home-away ───────────────────────────────────────────────────
    venue_context           TEXT,
        -- 'Home'    — team playing on their home soil
        -- 'Away'    — team playing on opponent's home soil
        -- 'Neutral' — neither team's home ground (e.g. World Cup venue)
    home_country            TEXT,                   -- country that owns the venue
    venue                   TEXT,                   -- copy from matches for convenience
    city                    TEXT,

    PRIMARY KEY (match_id, team),
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mmt_captain        ON match_meta_t20 (captain);
CREATE INDEX IF NOT EXISTS idx_mmt_comp_type      ON match_meta_t20 (competition_type);
CREATE INDEX IF NOT EXISTS idx_mmt_venue_context  ON match_meta_t20 (venue_context);
CREATE INDEX IF NOT EXISTS idx_mmt_team           ON match_meta_t20 (team);


-- =============================================================================
--  2.  player_match_roles
--  Per player per match — what role they had: captain, VC, or regular player.
--  Also tracks career state: "after_captaincy" flag for ex-captains.
-- =============================================================================

CREATE TABLE IF NOT EXISTS player_match_roles (
    match_id                TEXT        NOT NULL,
    player_name             TEXT        NOT NULL,
    team                    TEXT        NOT NULL,

    -- ── Role flags ──────────────────────────────────────────────────────────
    is_captain              BOOLEAN     NOT NULL DEFAULT FALSE,
    is_vice_captain         BOOLEAN     NOT NULL DEFAULT FALSE,
    -- TRUE if this player was previously captain of this team but has stepped down
    is_post_captaincy       BOOLEAN     NOT NULL DEFAULT FALSE,

    -- ── Under which captain ─────────────────────────────────────────────────
    -- Allows "performance under captain X" queries
    team_captain            TEXT,                   -- the captain of this player's team in this match

    PRIMARY KEY (match_id, player_name),
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pmr_player          ON player_match_roles (player_name);
CREATE INDEX IF NOT EXISTS idx_pmr_captain         ON player_match_roles (team_captain);
CREATE INDEX IF NOT EXISTS idx_pmr_is_captain      ON player_match_roles (is_captain);
CREATE INDEX IF NOT EXISTS idx_pmr_post_captaincy  ON player_match_roles (is_post_captaincy);


-- =============================================================================
--  3.  delivery_dimensions
--  Pre-computed per-delivery analytics labels for fast slice-and-dice.
--  Points at the deliveries table via (match_id, inning_number, over_number,
--  delivery_number) — same grain.
-- =============================================================================

CREATE TABLE IF NOT EXISTS delivery_dimensions (
    -- ── Key ─────────────────────────────────────────────────────────────────
    -- Matches the primary key of your deliveries table
    delivery_id             INTEGER     NOT NULL,   -- FK → deliveries.delivery_id (or deliveries.id)
    match_id                TEXT        NOT NULL,

    -- ── Phase of play ───────────────────────────────────────────────────────
    over_phase              TEXT        NOT NULL,
        -- 'Powerplay'   — overs 1–6
        -- 'Middle'      — overs 7–15
        -- 'Death'       — overs 16–20

    -- ── Innings dimension ───────────────────────────────────────────────────
    innings_role            TEXT        NOT NULL,
        -- 'Batting First'  — team batting in innings 1
        -- 'Batting Second' — team batting in innings 2 (chasing)

    -- ── Bowler type faced ───────────────────────────────────────────────────
    bowler_bowling_type     TEXT,
        -- from player_profiles.bowling_type for this delivery's bowler
        -- e.g. 'Right arm Fast', 'Left arm Orthodox', 'Right arm Leg-break', 'NA'
    bowler_bowling_subtype  TEXT,                   -- e.g. 'Googly', 'Seam'
    bowler_bowling_arm      TEXT,                   -- 'Right' | 'Left'
    bowler_pace_or_spin     TEXT,
        -- 'Pace' | 'Spin' | 'NA'  (derived from bowler_bowling_type)

    -- ── Batter role in match ─────────────────────────────────────────────────
    batter_is_captain       BOOLEAN     NOT NULL DEFAULT FALSE,
    batter_is_vice_captain  BOOLEAN     NOT NULL DEFAULT FALSE,
    batter_is_post_captaincy BOOLEAN    NOT NULL DEFAULT FALSE,
    batter_team_captain     TEXT,                   -- who was captaining batter's team

    -- ── Competition / venue context (denormalised for query performance) ─────
    competition_type        TEXT,                   -- 'Bilateral' | 'Tournament' | ...
    venue_context           TEXT,                   -- 'Home' | 'Away' | 'Neutral'

    PRIMARY KEY (delivery_id),
    FOREIGN KEY (match_id) REFERENCES matches (match_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dd_match          ON delivery_dimensions (match_id);
CREATE INDEX IF NOT EXISTS idx_dd_phase          ON delivery_dimensions (over_phase);
CREATE INDEX IF NOT EXISTS idx_dd_innings_role   ON delivery_dimensions (innings_role);
CREATE INDEX IF NOT EXISTS idx_dd_bowl_type      ON delivery_dimensions (bowler_bowling_type);
CREATE INDEX IF NOT EXISTS idx_dd_pace_spin      ON delivery_dimensions (bowler_pace_or_spin);
CREATE INDEX IF NOT EXISTS idx_dd_venue          ON delivery_dimensions (venue_context);
CREATE INDEX IF NOT EXISTS idx_dd_comp_type      ON delivery_dimensions (competition_type);
CREATE INDEX IF NOT EXISTS idx_dd_is_captain     ON delivery_dimensions (batter_is_captain);


-- =============================================================================
--  4.  REFERENCE: Slice-and-Dice Dimension Map
--      How to join the tables to answer each analytical question
-- =============================================================================

/*
  DIMENSION                       SOURCE TABLE / COLUMN
  ─────────────────────────────── ────────────────────────────────────────────
  As captain                      player_match_roles.is_captain = TRUE
  As vice-captain                 player_match_roles.is_vice_captain = TRUE
  After giving up captaincy       player_match_roles.is_post_captaincy = TRUE
  As non-captain                  player_match_roles.is_captain = FALSE
                                   AND is_vice_captain = FALSE
  Under captain X                 player_match_roles.team_captain = 'X'

  Powerplay                       delivery_dimensions.over_phase = 'Powerplay'
  Middle overs                    delivery_dimensions.over_phase = 'Middle'
  Death overs                     delivery_dimensions.over_phase = 'Death'

  Home ground                     delivery_dimensions.venue_context = 'Home'
  Away                            delivery_dimensions.venue_context = 'Away'
  Neutral                         delivery_dimensions.venue_context = 'Neutral'

  Bilateral series                delivery_dimensions.competition_type = 'Bilateral'
  Tournament (ICC/regional)       delivery_dimensions.competition_type = 'Tournament'

  vs Fast bowlers                 delivery_dimensions.bowler_pace_or_spin = 'Pace'
  vs Spin bowlers                 delivery_dimensions.bowler_pace_or_spin = 'Spin'
  vs Right arm Fast               delivery_dimensions.bowler_bowling_type = 'Right arm Fast'
  vs Left arm Fast-Medium         delivery_dimensions.bowler_bowling_type = 'Left arm Fast-Medium'
  vs Leg spin                     delivery_dimensions.bowler_bowling_type = 'Right arm Leg-break'
  vs Off spin                     delivery_dimensions.bowler_bowling_type = 'Right arm Off-break'
  vs Left arm Orthodox            delivery_dimensions.bowler_bowling_type = 'Left arm Orthodox'
  vs Left arm Unorthodox          delivery_dimensions.bowler_bowling_type = 'Left arm Unorthodox'

  Batting first                   delivery_dimensions.innings_role = 'Batting First'
  Batting second / chasing        delivery_dimensions.innings_role = 'Batting Second'
*/


-- =============================================================================
--  5.  EXAMPLE QUERIES
-- =============================================================================

/*
── Virat Kohli: powerplay strike rate as captain vs leg-spin in ICC tournaments ──

SELECT
    d.batter,
    COUNT(*)                                        AS balls_faced,
    SUM(d.runs_batter)                              AS runs,
    ROUND(SUM(d.runs_batter) * 100.0 / COUNT(*), 2) AS strike_rate
FROM deliveries d
JOIN delivery_dimensions dd ON dd.delivery_id = d.delivery_id
WHERE d.batter               = 'V Kohli'
  AND dd.batter_is_captain   = TRUE
  AND dd.over_phase          = 'Powerplay'
  AND dd.bowler_bowling_type = 'Right arm Leg-break'
  AND dd.competition_type    = 'Tournament';


── Any player: stats after giving up captaincy vs when captain ──

SELECT
    pmr.player_name,
    CASE
        WHEN pmr.is_captain         THEN 'As Captain'
        WHEN pmr.is_post_captaincy  THEN 'Post Captaincy'
        ELSE                             'Non-Captain'
    END                                             AS leadership_context,
    COUNT(d.delivery_id)                            AS balls,
    SUM(d.runs_batter)                              AS runs,
    SUM(CASE WHEN d.wicket_player_out = d.batter THEN 1 ELSE 0 END) AS dismissals,
    ROUND(SUM(d.runs_batter) * 100.0 / NULLIF(COUNT(*),0), 2)       AS strike_rate,
    ROUND(SUM(d.runs_batter) * 1.0   / NULLIF(SUM(CASE WHEN d.wicket_player_out = d.batter THEN 1 ELSE 0 END),0), 2) AS average
FROM deliveries d
JOIN player_match_roles pmr ON pmr.match_id = d.match_id AND pmr.player_name = d.batter
WHERE d.batter = 'MS Dhoni'
GROUP BY leadership_context;


── Home vs Away batting performance breakdown ──

SELECT
    d.batter,
    dd.venue_context,
    COUNT(*)                                                          AS balls,
    SUM(d.runs_batter)                                                AS runs,
    ROUND(SUM(d.runs_batter) * 100.0 / NULLIF(COUNT(*), 0), 2)       AS strike_rate
FROM deliveries d
JOIN delivery_dimensions dd ON dd.delivery_id = d.delivery_id
WHERE d.batter = 'Rohit Sharma'
GROUP BY d.batter, dd.venue_context
ORDER BY dd.venue_context;


── Bowler type breakdown — who does a batter struggle against ──

SELECT
    d.batter,
    dd.bowler_bowling_type,
    COUNT(*)                                                          AS balls,
    SUM(d.runs_batter)                                                AS runs,
    SUM(CASE WHEN d.wicket_player_out = d.batter THEN 1 ELSE 0 END)  AS dismissals,
    ROUND(SUM(d.runs_batter) * 100.0 / NULLIF(COUNT(*), 0), 2)       AS strike_rate
FROM deliveries d
JOIN delivery_dimensions dd ON dd.delivery_id = d.delivery_id
WHERE d.batter = 'KL Rahul'
GROUP BY d.batter, dd.bowler_bowling_type
ORDER BY dismissals DESC, balls DESC;
*/
