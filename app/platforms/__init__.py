"""One module per platform. Each module exposes:

  PLATFORM, API, HEADERS
  async list_shows(client) -> list          everything needed to fetch each show
  async fetch_show(client, item) -> dict    one show in the common shape below, or None to skip it

Common show shape (keys match the `shows` columns):
  source_id, slug, title, description, category, genres, language, status, creator_name, thumbnail_url,
  cover_url, page_url, has_locked_episodes, published_at, story, extra, raw,
  seasons:  [{source_id, season_number, title, extra}]
  episodes: [{source_id, season_source_id, episode_number, title, description, audio_url,
              audio_url_expires_at, duration_seconds, thumbnail_url, published_at, extra}]
story: links to the show's text, {source, match, page_url, read_url, txt_url, epub_url, pdf_url}, or None.
Fields a platform doesn't have are None (and may be left out); platform-only fields go in `extra`.
"""

import asyncio
from datetime import datetime

import httpx


async def get_json(client: httpx.AsyncClient, path: str, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            if attempt == attempts:
                raise
            await asyncio.sleep(2 ** attempt)


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
