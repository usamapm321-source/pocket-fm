"""ParrotFM (parrotfm.app): every series.

The site renders in the browser from https://api.parrotfm.app:
  /genres          genre list
  /series          every series with its metadata
  /series/{slug}   one series with all its episodes, each with a signed `audio_url`
These endpoints need no login. `audio_url` is a presigned Cloudflare R2 link that
expires (X-Amz-Date + X-Amz-Expires, currently 7 days), so the periodic re-scrape keeps them fresh.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import PARROTFM_API, PARROTFM_SITE
from . import get_json, parse_ts

PLATFORM = "parrotfm"
API = PARROTFM_API
HEADERS = {"User-Agent": "Mozilla/5.0 (parrotfm-scraper)", "Origin": PARROTFM_SITE}


async def list_shows(client: httpx.AsyncClient) -> list[tuple[dict, str | None]]:
    genre_names = {g["slug"]: g["name"] for g in await get_json(client, "/genres")}
    return [(s, genre_names.get(s.get("genre_slug"))) for s in await get_json(client, "/series")]


def _thumbnail(cover: str | None) -> str | None:
    # The site shows `<name>_500.webp` as the small cover next to the full-size `<name>.webp`
    if cover and cover.endswith(".webp"):
        return cover[: -len(".webp")] + "_500.webp"
    return cover


def _url_expiry(url: str) -> datetime | None:
    qs = parse_qs(urlparse(url).query)
    try:
        signed = datetime.strptime(qs["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return signed + timedelta(seconds=int(qs["X-Amz-Expires"][0]))
    except (KeyError, ValueError):
        return None


async def fetch_show(client: httpx.AsyncClient, item: tuple[dict, str | None]) -> dict | None:
    summary, genre_name = item
    detail = await get_json(client, f"/series/{summary['slug']}")
    series = {**summary, **(detail.get("series") or {})}
    episodes = [e for e in detail.get("episodes") or [] if e.get("is_active", True) and e.get("audio_url")]
    if not episodes:
        return None

    cover = series.get("cover_art_url")
    return {
        "source_id": series["id"],
        "slug": series["slug"],
        "title": series["title"],
        "description": series.get("series_description"),
        "category": "Audio Drama",
        "genres": [genre_name or series["genre_slug"]] if series.get("genre_slug") else [],
        "language": series.get("series_language"),
        "status": series.get("status"),
        "creator_name": None,
        "thumbnail_url": _thumbnail(cover),
        "cover_url": cover,
        "page_url": f"{PARROTFM_SITE}/series/{series['slug']}",
        "has_locked_episodes": False,
        "published_at": parse_ts(series.get("created_at")),
        "extra": {
            "genre_slug": series.get("genre_slug"),
            "creator_id": series.get("creator_id") or None,
            "is_featured": series.get("is_featured"),
            "total_episodes": series.get("total_episodes"),
            "total_views": series.get("total_views"),
            "updated_at": series.get("updated_at"),
        },
        "raw": {"series": series, "episodes": detail.get("episodes") or []},
        "seasons": [],
        "episodes": [
            {
                "source_id": e["id"],
                "season_source_id": None,
                "episode_number": e.get("episode_number"),
                "title": e.get("title"),
                "description": e.get("episode_description"),
                "audio_url": e["audio_url"],
                "audio_url_expires_at": _url_expiry(e["audio_url"]),
                "duration_seconds": e.get("duration_seconds"),
                "thumbnail_url": e.get("cover_art_url"),
                "published_at": parse_ts(e.get("created_at")),
                "extra": {"plays_count": e.get("plays_count")},
            }
            for e in episodes
        ],
    }
