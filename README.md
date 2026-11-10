# Audio shows scraper

Collects shows from several audio platforms (metadata, thumbnails and playable episode audio URLs), stores them in **one set of Postgres tables**, and serves them through one REST API and web player.

| Platform | What is collected | Source |
|---|---|---|
| `dramafy` ([dramafy.com](https://www.dramafy.com)) | every free show and its free episodes | `https://v2.api.dramafy.com` |
| `parrotfm` ([parrotfm.app](https://parrotfm.app)) | every series and its episodes | `https://api.parrotfm.app` (no login needed) |
| `archiveorg` ([archive.org](https://archive.org)) | old-time radio series and LibriVox audiobooks, most downloaded first | `https://archive.org` search + metadata APIs |
| `audiodrama` ([audiodrama.com](https://www.audiodrama.com)) | every show in the directory and every episode in its podcast feed | WordPress REST API `https://www.audiodrama.com/wp-json/wp/v2/show` + each show's RSS feed |
| `learnoutloud` ([learnoutloud.com](https://www.learnoutloud.com)) | free audiobooks, podcasts and courses that have playable audio | the site's listing and title pages (no API) + each title's RSS feed |

## Run

```bash
cp .env.example .env      # optional: change ports / schedule
docker compose up -d --build
```

- Player: http://localhost:8000. Browse and search shows from every platform, filter by platform, category or genre. A show page has an **Audio** tab (play episodes), a **Read** tab (each episode's text, following the audio; see [Transcripts](#transcripts)) and a **Story** tab (the book's text with read/download links, for audiobooks that have one).
- API docs: http://localhost:8000/docs.
- Postgres: `localhost:5434`, user, password and database all `dramafy`. 5434 avoids clashing with other local Postgres containers.

On startup the API scrapes every platform at the same time. Dramafy and ParrotFM take about a minute, AudioDrama about five; archive.org and LearnOutLoud take longer (one request per item). It then re-scrapes every `SCRAPE_INTERVAL_HOURS` (default 24).

To pick up code changes: `docker compose up -d --build api`. The database is kept.

## Database

All platforms share the same tables (see [app/schema.sql](app/schema.sql)):

| Table | Holds |
|---|---|
| `shows` | one row per show. `platform` + `source_id` (the platform's own id) is unique. |
| `seasons` | seasons, for platforms that have them (Dramafy; AudioDrama feeds that tag `itunes:season`). |
| `episodes` | one row per playable episode. `season_id` is NULL for platforms without seasons (ParrotFM). |
| `scrape_runs` | one row per scrape, with `platform`, counts and errors. |
| `transcripts` | the text of an episode's audio, one row per requested episode. |

Every show has a `category`, a top-level grouping: `Audio Drama` for Dramafy, ParrotFM and AudioDrama; for archive.org, the category set for its collection in `ARCHIVE_COLLECTIONS` (`Old Time Radio`, `Audiobooks`); for LearnOutLoud, the one set for its section in `LEARNOUTLOUD_SECTIONS` (`Audiobooks`, `Podcasts`, `Courses`). `genres` are finer tags within it.

Fields every platform has are columns: `title`, `description`, `genres`, `thumbnail_url`, `cover_url`, `page_url`, `episode_count`, `audio_url`, `duration_seconds`, and so on. Fields only one platform has are in the `extra` JSON column. Dramafy's `extra` holds the rating, creator and credits. ParrotFM's holds the featured flag, view and play counts. archive.org's holds the source collection, all subject tags, license and download count. AudioDrama's holds the RSS feed URL, show tags, the AI-generated / human-created label and the creator's website. LearnOutLoud's holds the section, subcategories, the title-details table (author, narrator, publisher, running time, ...), rating, feed URL and every "Get this free title from" link. `shows.raw` keeps the full upstream JSON.

Rows are upserted, so a show or episode keeps the same `id` across scrapes.

```bash
docker compose exec db psql -U dramafy -c "select platform, title, episode_count from shows order by title limit 10"
docker compose exec db psql -U dramafy -c "select title, extra->>'total_views' from shows where platform = 'parrotfm'"
```

**Audio URLs:** Dramafy's, archive.org's, AudioDrama's and LearnOutLoud's are permanent. ParrotFM's are signed links that **expire after 7 days**. `episodes.audio_url_expires_at` says when. The daily re-scrape replaces them with fresh links, so keep `SCRAPE_INTERVAL_HOURS` under 168. The scraper stores URLs only. The audio files themselves stay on each platform's CDN.

Dramafy: an episode is stored when it is `Published` and not in its Dramafy+ early-access window. `has_locked_episodes` marks shows whose newest episodes are still Dramafy+ only.

## API

Every list endpoint takes an optional `?platform=` filter.

| Method | Path | Description |
|---|---|---|
| GET | `/` | Web player ([app/static/player.html](app/static/player.html)) |
| GET | `/health` | Health check (DB connectivity) |
| GET | `/platforms` | Each platform with show and episode counts and its last scrape |
| GET | `/stats?platform=` | Counts, total duration, the next audio URL expiry, last scrape |
| GET | `/categories?platform=` | Categories with show, episode and duration counts |
| GET | `/genres?platform=&category=` | Genres with show counts |
| GET | `/shows?platform=&category=&q=&genre=&has_story=&page=1&page_size=50` | Paginated show list, search by title or creator, filter by category, genre or whether it has a text version |
| GET | `/shows/{id}` | One show with its seasons and episodes |
| GET | `/shows/{platform}/{slug}` | Same, looked up by the platform's slug |
| GET | `/shows/{id}/story` | The show's text as plain text, fetched live from `story.txt_url` (Gutenberg header/footer removed; not stored) |
| GET | `/episodes/{id}` | One episode with its show and season info |
| GET | `/episodes/{id}/transcript?format=json` | The episode's text with timed segments and its `status` (`none`, `queued`, `running`, `done`, `failed`). `format=txt` returns the finished text |
| POST | `/episodes/{id}/transcript?force=false` | Queue the episode for a transcript (ahead of whole shows). Retries a failed one; `force` redoes a finished one |
| GET | `/shows/{id}/transcripts` | Transcript status of each episode of a show |
| POST | `/shows/{id}/transcripts` | Queue every episode of a show |
| POST | `/transcripts/batch` | Queue a batch of episodes. JSON body: `episode_ids`, `show_ids`, `platform`, `category` (combined with AND), `limit`, `only_new` (default true: skip episodes already requested), `force` |
| DELETE | `/transcripts/queue?platform=` | Take waiting transcripts out of the queue |
| GET | `/transcripts?platform=` | Transcript counts and audio hours by status |
| POST | `/scrape?platform=` | Start a scrape in the background, for one platform or all (409 if one is running) |
| GET | `/scrape/runs?platform=&limit=10` | History of scrape runs with counts and errors |

In `/shows/{id}`, a platform without seasons returns one untitled season that holds every episode, so clients can treat every show the same way.

## Transcripts

So listeners can read a story as well as hear it, the API makes a text version of any episode. Transcripts are requested through the API (below); the player only shows them. In a show's **Read** tab, pick an episode to read its text. While the episode plays, the line being spoken is highlighted (and followed, unless *Follow audio* is off); clicking a line plays from there. When an episode ends and the next starts, the Read tab moves on with it.

Transcripts are made only when requested, since all platforms together hold well over 100,000 hours of audio. Requests are queued in the `transcripts` table and background workers ([app/transcribe.py](app/transcribe.py)) take them one at a time:

1. **The creator's transcript.** Many podcast feeds link one per episode (`<podcast:transcript>`). The scraper keeps these links in `episodes.extra.transcripts` (AudioDrama and LearnOutLoud, from the next scrape on). The worker downloads one and parses it: VTT, SRT and Podcast Index JSON keep their timings and speaker names, HTML and plain text become text only.
2. **Speech recognition.** Otherwise the worker downloads the audio and transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), locally, with no API key or per-minute cost. The model is downloaded on first use into the `whisper-models` volume. The language is detected automatically. Segments are saved every few seconds, so the text can be read while it is being made.

`segments` is a list of `{start, end, text, speaker?}`, in seconds into the audio. `text` is the same text as paragraphs.

Queue order: a single episode someone asked for, then whole shows, then automatic ones, oldest request first. Transcripts survive re-scrapes (episode ids are stable) and a restart puts interrupted ones back in the queue.

Speed: on CPU, the default `small` model is a little faster than real time. A 6.7-minute episode took 4.9 minutes on 12 cores while a scrape was running. `base` is several times faster but less accurate, and a GPU is many times faster. Configure in `.env`:

- `WHISPER_MODEL`: `tiny`, `base`, `small` (default), `medium` or `large-v3`. Larger is more accurate and slower.
- `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE`: `cpu` / `int8` by default. With an NVIDIA GPU, `cuda` / `float16` (the container also needs GPU access in `docker-compose.yml`).
- `TRANSCRIBE_WORKERS`: episodes transcribed at the same time, default 1. Each uses several cores. `0` stops processing (requests still queue).
- `TRANSCRIBE_AUTO_PLATFORMS`: platforms whose every episode is queued after each scrape, e.g. `parrotfm,dramafy` (about 160 and 900 hours of audio). Empty by default.
- `TRANSCRIBE_MAX_AUDIO_MB`: larger audio files fail instead of being transcribed. Default 500.

Batches go through `POST /transcripts/batch`. Filters combine; `limit` takes episodes in show-title then episode order, and with the default `only_new` each call picks up where the last one stopped:

```bash
# the next 100 ParrotFM episodes without a transcript
curl -X POST localhost:8000/transcripts/batch -H 'Content-Type: application/json' -d '{"platform": "parrotfm", "limit": 100}'
# every episode of shows 42 and 43
curl -X POST localhost:8000/transcripts/batch -H 'Content-Type: application/json' -d '{"show_ids": [42, 43]}'
curl localhost:8000/transcripts                     # progress of the queue
curl -X DELETE localhost:8000/transcripts/queue     # stop: drop everything still waiting
```

## archive.org

One archive.org item is one show and each original audio file in it is one episode, played from its MP3 (or the MP3 derivative of a FLAC/OGG original). Genres are picked from the item's free-text `subject` tags using a fixed list in [app/platforms/archiveorg.py](app/platforms/archiveorg.py).

Configure in `.env`:

- `ARCHIVE_COLLECTIONS`: `collection:Category` pairs. Default `oldtimeradio:Old Time Radio,librivoxaudio:Audiobooks`. Any audio collection id from archive.org works, e.g. `podcasts:Podcasts`.
- `ARCHIVE_MAX_ITEMS_PER_COLLECTION`: default 2000 most-downloaded items per collection. `0` takes every item (about 9k radio sets and 22k audiobooks, several hundred thousand episodes, and a much longer scrape).

### Story (text) links

The `.pdf`/`.txt` files inside a LibriVox item are only its CD cover, so for audiobooks the scraper looks up the actual book and stores links to it in `shows.story` (JSON, NULL when none was found):

```json
{"source": "Project Gutenberg", "match": "title_search", "page_url": "...", "read_url": "...",
 "txt_url": "...", "epub_url": "...", "pdf_url": null}
```

It tries, in order: a Project Gutenberg link in the item's description, an archive.org text item linked from the description (`match: description_link`), then a search of archive.org's Gutenberg mirror and of Project Gutenberg's catalogue (`match: title_search`). Searches accept only an exact title match by an author with the same surname, so some books are missed rather than mismatched. A title match may be a different edition from the recording. Only the links are stored; the player loads the text through `/shows/{id}/story`.

## AudioDrama

AudioDrama.com is a directory: it hosts no audio, and each show page plays the creator's public podcast RSS feed. The scraper lists every show from the site's WordPress REST API (title, description, genres, tags, cover) and then reads the feed behind `https://www.audiodrama.com/feeds/<slug>`, which redirects to the creator's host (Acast, Megaphone, Buzzsprout, Anchor, Libsyn and others). One feed item with an audio enclosure is one episode, and its MP3 URL is the creator's own.

- `creator_name` and `language` come from the feed. The cover comes from the site, or from the feed when the site has none.
- Episodes are numbered oldest first, within their `itunes:season` when the feed has seasons. The feed's own season/episode numbers are kept in `episodes.extra`.
- Shows whose feed has no audio episodes are skipped. Shows whose feed is gone (e.g. `410 Gone`) or isn't RSS are skipped and listed in `scrape_runs.errors`.
- `shows.raw` holds the WordPress record only, not the feed, since some feeds are several MB.

## LearnOutLoud

LearnOutLoud.com has no API, so the scraper reads its pages. For each section it posts the listing form for 50 titles a page, sorted A-Z, then reads each title's page for its details, description, cover, categories and "Get this free title from" links. LearnOutLoud hosts almost no audio itself, so the episodes come from where the title is free:

- **A podcast RSS feed:** LibriVox for most audiobooks, the publisher's feed for podcasts and courses. The site links it as `/podcaststream/listen.php?url=<feed>`. Feed items are the episodes, in feed order when they have no dates (LibriVox chapters).
- **A direct MP3:** a few of LearnOutLoud's own recordings, one episode per file.
- **Anything else** (YouTube, Spotify, Amazon, ZIP downloads, web pages): nothing playable, so the title is skipped. This is why most Free-Courses titles are skipped: they are video.

`genres` are LearnOutLoud's top-level categories (`Literature`, `History`, ...); the subcategories are in `extra.subcategories`. Slugs are `<title>-<id>`, since titles repeat. Titles whose feed is dead (404/410, or now an HTML page) are listed in `scrape_runs.errors`. LibriVox titles can also appear under `archiveorg`, as separate shows.

Configure in `.env`:

- `LEARNOUTLOUD_SECTIONS`: `section:Category` pairs. Default `Free-Audiobooks:Audiobooks,Podcast-Directory:Podcasts,Free-Courses:Courses` (about 4.5k, 5.8k and 1.1k titles). A title in several sections is stored once, under the first.
- `LEARNOUTLOUD_MAX_ITEMS_PER_SECTION`: default `0`, every title. Set e.g. `200` for a quick test.

## Adding a platform

1. Add `app/platforms/<name>.py` with `PLATFORM`, `API`, `HEADERS`, `list_shows(client)` and `fetch_show(client, item)`. `fetch_show` returns the common shape (including `category`) described in [app/platforms/\_\_init\_\_.py](app/platforms/__init__.py).
   For a site that plays creators' podcast feeds, [app/platforms/rss.py](app/platforms/rss.py) fetches a feed and turns its items into episodes.
2. Register it in `PLATFORMS` in [app/scraper.py](app/scraper.py).
3. Rebuild. No new tables or endpoints are needed.
