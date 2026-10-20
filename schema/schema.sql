-- SPX — community edition schema (SQLite 3.31+)
--
-- Derived from shared/spx_pilot.sql, with the pilot-only columns removed and
-- the operating rules from shared/spx-resolutions.md applied.
--
-- Money does not pass through the platform (R19). A member may pledge an
-- amount toward the date; the venue charges it on its own terminal or payment
-- link, and the platform only records the reference. There is no wallet, no
-- balance, no custody, and no user-to-user transfer.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- members
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS members (
    member_id       TEXT PRIMARY KEY,
    district        TEXT NOT NULL,                 -- venue neighbourhood, not a home address
    gender          TEXT NOT NULL CHECK (gender IN ('M','F','NB')),
    target_gender   TEXT NOT NULL,                 -- 'M' | 'F' | 'NB' | 'M,F'
    age             INTEGER NOT NULL CHECK (age BETWEEN 18 AND 99),
    target_age_min  INTEGER NOT NULL,
    target_age_max  INTEGER NOT NULL,
    intent          TEXT NOT NULL CHECK (intent IN ('long_term','marriage','open')),
    kids            TEXT NOT NULL CHECK (kids IN ('want','dont_want','have_open')),
    -- 12 availability half-hours, Thu/Fri/Sat/Sun 18:30-21:30
    slot_bitmask    INTEGER NOT NULL CHECK (slot_bitmask > 0),
    -- sha256(salt + phone). The number itself is never stored. This is the
    -- ban key: it survives a purge and blocks the same phone from returning.
    phone_hash      TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','paused','graduated','banned')),
    created_at      TEXT NOT NULL,
    CHECK (target_age_min <= target_age_max)
);

-- ---------------------------------------------------------------------------
-- pairs — one row per unordered pair, five raw feasibility flags
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pairs (
    pair_id      TEXT PRIMARY KEY,
    a            TEXT NOT NULL,
    b            TEXT NOT NULL,
    gender_ok    INTEGER NOT NULL CHECK (gender_ok IN (0,1)),
    age_ok       INTEGER NOT NULL CHECK (age_ok IN (0,1)),
    intent_ok    INTEGER NOT NULL CHECK (intent_ok IN (0,1)),
    commute_ok   INTEGER NOT NULL CHECK (commute_ok IN (0,1)),
    slot_ok      INTEGER NOT NULL CHECK (slot_ok IN (0,1)),
    feasible     INTEGER GENERATED ALWAYS AS
                 (gender_ok * age_ok * intent_ok * commute_ok * slot_ok) STORED,
    evaluated_at TEXT NOT NULL,
    CHECK (a < b),
    UNIQUE (a, b),
    FOREIGN KEY (a) REFERENCES members(member_id) ON DELETE CASCADE,
    FOREIGN KEY (b) REFERENCES members(member_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- proposals — a served pair, with each side's decision
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id  TEXT PRIMARY KEY,
    pair_id      TEXT NOT NULL,
    served_at    TEXT NOT NULL,
    deadline_at  TEXT NOT NULL,
    decision_a   TEXT CHECK (decision_a IN ('ACCEPT','DECLINE','TIMEOUT')),
    decision_b   TEXT CHECK (decision_b IN ('ACCEPT','DECLINE','TIMEOUT')),
    decided_at_a TEXT,
    decided_at_b TEXT,
    FOREIGN KEY (pair_id) REFERENCES pairs(pair_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- venues — named, public, staffed. No capacity contract, no booking API.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venues (
    venue_id  TEXT PRIMARY KEY,
    district  TEXT NOT NULL,
    name      TEXT NOT NULL,
    address   TEXT NOT NULL,
    priority  INTEGER NOT NULL DEFAULT 100,        -- lower wins a tie
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
);

-- ---------------------------------------------------------------------------
-- dates — one row per mutual accept that reaches the venue stage
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dates (
    date_id        TEXT PRIMARY KEY,
    proposal_id    TEXT NOT NULL UNIQUE,
    venue_id       TEXT,                           -- NULL while negotiating or lapsed
    window_start   TEXT,                           -- NULL while negotiating or lapsed
    window_minutes INTEGER NOT NULL DEFAULT 45,
    outcome        TEXT NOT NULL DEFAULT 'NEGOTIATING'
                   CHECK (outcome IN ('NEGOTIATING','CONFIRMED','LAPSED')),
    lapse_cause    TEXT CHECK (lapse_cause IN ('NO_COMMON_VENUE','ROUND_TIMEOUT','ORGANIZER_CANCELLED')),
    checkin_a      INTEGER NOT NULL DEFAULT 0 CHECK (checkin_a IN (0,1)),
    checkin_b      INTEGER NOT NULL DEFAULT 0 CHECK (checkin_b IN (0,1)),
    meeting_status TEXT CHECK (meeting_status IN
                   ('COMPLETED','NO_SHOW_A','NO_SHOW_B','MUTUAL_NO_SHOW')),
    req_a          INTEGER NOT NULL DEFAULT 0 CHECK (req_a IN (0,1)),
    req_b          INTEGER NOT NULL DEFAULT 0 CHECK (req_b IN (0,1)),
    FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id) ON DELETE CASCADE,
    FOREIGN KEY (venue_id) REFERENCES venues(venue_id)
);

-- The one hard scheduling invariant: a venue hosts at most one confirmed
-- date per slot. This is enforced by the database, not by application logic.
CREATE UNIQUE INDEX IF NOT EXISTS one_date_per_venue_slot
    ON dates (venue_id, window_start)
    WHERE outcome = 'CONFIRMED';

-- ---------------------------------------------------------------------------
-- date_pledges — an honor pledge toward the date. No money moves through SPX.
--
--   * no card, no wallet, no provider reference, no settlement
--   * `amount_cents` is what the member says they will spend at the venue
--   * a broken pledge bans the member, enforced in domain.py
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS date_pledges (
    pledge_id    TEXT PRIMARY KEY,
    date_id      TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('A','B')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents BETWEEN 500 AND 20000),
    currency     TEXT NOT NULL DEFAULT 'USD' CHECK (length(currency) = 3),
    status       TEXT NOT NULL DEFAULT 'PLEDGED'
                 CHECK (status IN ('PLEDGED','HONORED','BROKEN','VOID')),
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    UNIQUE (date_id, side),                        -- one pledge per side
    FOREIGN KEY (date_id) REFERENCES dates(date_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- venue_rounds — what each side accepted, so per-round consensus is measurable
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venue_rounds (
    round_id        TEXT PRIMARY KEY,
    date_id         TEXT NOT NULL,
    round_no        INTEGER NOT NULL CHECK (round_no IN (1,2,3)),
    offered         TEXT NOT NULL,                 -- JSON array of venue_id
    accept_a        TEXT NOT NULL,                 -- JSON array
    accept_b        TEXT NOT NULL,                 -- JSON array
    intersection    INTEGER NOT NULL,
    outcome         TEXT NOT NULL
                    CHECK (outcome IN ('CONFIRMED','NO_COMMON_VENUE','ROUND_TIMEOUT')),
    agreed_venue_id TEXT,
    FOREIGN KEY (date_id) REFERENCES dates(date_id) ON DELETE CASCADE,
    UNIQUE (date_id, round_no)
);

-- ---------------------------------------------------------------------------
-- retained markers — the only thing that survives a purge (R1)
-- No link to specs, dates, or people; salted hash only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retained_markers (
    marker_hash TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('verified','banned')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- settings — install-local values. The hash salt is generated once per
-- database and never exported; without it, a phone hash cannot be reversed
-- or correlated across installs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- audit — operational events, no free text, no contact data
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL,                        -- member_id or 'system'
    action   TEXT NOT NULL,
    subject  TEXT,
    detail   TEXT
);

CREATE INDEX IF NOT EXISTS idx_pairs_a ON pairs(a);
CREATE INDEX IF NOT EXISTS idx_pairs_b ON pairs(b);
CREATE INDEX IF NOT EXISTS idx_proposals_pair ON proposals(pair_id);
CREATE INDEX IF NOT EXISTS idx_dates_venue ON dates(venue_id, window_start);
CREATE INDEX IF NOT EXISTS idx_pledges_date ON date_pledges(date_id);
