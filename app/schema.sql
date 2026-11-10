-- One-time upgrade from the old per-platform layout (Dramafy-only shows table + parrotfm_* tables).
-- The data is re-scraped on startup, so the old tables are simply dropped.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'shows' AND column_name = 'free_episode_count') THEN
        DROP TABLE IF EXISTS episodes, seasons, shows CASCADE;
    END IF;
END $$;
DROP TABLE IF EXISTS parrotfm_episodes, parrotfm_series, parrotfm_genres;

-- Every platform shares these tables. Common fields are columns; platform-only fields are in `extra`.
CREATE TABLE IF NOT EXISTS shows (
    id                     SERIAL PRIMARY KEY,
    platform               TEXT NOT NULL,            -- dramafy | parrotfm | archiveorg | ...
    source_id              TEXT NOT NULL,            -- the platform's own show id
    slug                   TEXT NOT NULL,
    title                  TEXT NOT NULL,
    description            TEXT,
    category               TEXT,                     -- top-level category, e.g. Audio Drama | Audiobooks | Old Time Radio
    genres                 TEXT[] NOT NULL DEFAULT '{}',
    language               TEXT,
    status                 TEXT,                     -- e.g. Ongoing
    creator_name           TEXT,
    thumbnail_url          TEXT,                     -- small cover
    cover_url              TEXT,                     -- full-size cover
    page_url               TEXT NOT NULL,
    episode_count          INTEGER NOT NULL DEFAULT 0,  -- playable episodes stored
    total_duration_seconds INTEGER NOT NULL DEFAULT 0,
    has_locked_episodes    BOOLEAN NOT NULL DEFAULT FALSE,  -- some episodes are paywalled / early access
    published_at           TIMESTAMPTZ,
    extra                  JSONB NOT NULL DEFAULT '{}',
    raw                    JSONB NOT NULL,           -- full upstream JSON
    scraped_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, source_id),
    UNIQUE (platform, slug)
);

CREATE TABLE IF NOT EXISTS seasons (
    id            SERIAL PRIMARY KEY,
    show_id       INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,
    season_number INTEGER NOT NULL,                  -- 1-based
    title         TEXT,
    extra         JSONB NOT NULL DEFAULT '{}',
    UNIQUE (show_id, source_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    id                   SERIAL PRIMARY KEY,
    platform             TEXT NOT NULL,
    source_id            TEXT NOT NULL,              -- the platform's own episode id
    show_id              INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season_id            INTEGER REFERENCES seasons(id) ON DELETE CASCADE,  -- NULL when the platform has no seasons
    episode_number       INTEGER,                    -- 1-based
    title                TEXT,
    description          TEXT,
    audio_url            TEXT NOT NULL,              -- the src the site's player uses
    audio_url_expires_at TIMESTAMPTZ,                -- NULL = permanent link
    duration_seconds     INTEGER,
    thumbnail_url        TEXT,
    published_at         TIMESTAMPTZ,
    extra                JSONB NOT NULL DEFAULT '{}',
    scraped_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, source_id)
);

ALTER TABLE shows ADD COLUMN IF NOT EXISTS category TEXT;
-- Links to the show's text (audiobooks): {source, match, page_url, read_url, txt_url, epub_url, pdf_url}
ALTER TABLE shows ADD COLUMN IF NOT EXISTS story JSONB;

CREATE INDEX IF NOT EXISTS idx_shows_platform ON shows(platform);
CREATE INDEX IF NOT EXISTS idx_shows_category ON shows(category);
CREATE INDEX IF NOT EXISTS idx_shows_genres ON shows USING GIN (genres);
CREATE INDEX IF NOT EXISTS idx_seasons_show ON seasons(show_id);
CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id);
CREATE INDEX IF NOT EXISTS idx_episodes_season ON episodes(season_id);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id             SERIAL PRIMARY KEY,
    platform       TEXT NOT NULL DEFAULT 'dramafy',
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'running',  -- running | success | failed
    shows_found    INTEGER,
    shows_saved    INTEGER,
    episodes_saved INTEGER,
    errors         JSONB NOT NULL DEFAULT '[]'
);
ALTER TABLE scrape_runs ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'dramafy';

-- The text of an episode's audio (see app/transcribe.py). One row per requested episode.
CREATE TABLE IF NOT EXISTS transcripts (
    episode_id   INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued',     -- queued | running | done | failed
    priority     INTEGER NOT NULL DEFAULT 0,         -- higher runs first: 2 episode, 1 show, 0 automatic
    source       TEXT,                               -- feed (published by the creator) | whisper (speech recognition)
    model        TEXT,                               -- whisper model, or the feed transcript's URL
    language     TEXT,
    progress     REAL NOT NULL DEFAULT 0,            -- 0..1 while running
    text         TEXT,
    segments     JSONB NOT NULL DEFAULT '[]',        -- [{start, end, text, speaker?}], seconds into the audio
    error        TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_transcripts_queue ON transcripts(priority DESC, requested_at) WHERE status = 'queued';
