"""Dramafy (dramafy.com): free shows only.

The site renders in the browser from https://v2.api.dramafy.com:
  /search/v2?page=N&q=   lists every show (Shows group, paginated)
  /shows/{slug}          full show details incl. seasons and episodes
An episode's `url` is exactly what the site's player puts in its <audio src>.
An episode is free when it is Published and not in its Dramafy+ early-access window.
"""

import httpx

from ..config import DRAMAFY_API, DRAMAFY_SITE
from . import get_json, parse_ts

PLATFORM = "dramafy"
API = DRAMAFY_API
HEADERS = {"User-Agent": "Mozilla/5.0 (dramafy-scraper)", "Origin": DRAMAFY_SITE}


async def list_shows(client: httpx.AsyncClient) -> list[str]:
    slugs: dict[str, None] = {}
    page = 0
    while True:
        data = await get_json(client, f"/search/v2?page={page}&q=")
        shows_group = next((g for g in data.get("groups", []) if g["title"] == "Shows"), None)
        if shows_group is None:
            break
        for item in shows_group["items"]:
            slugs[item["slug"]] = None
        if not shows_group["hasMore"]:
            break
        page += 1
    return list(slugs)


def is_free(episode: dict) -> bool:
    return (
        episode.get("status") == "Published"
        and not episode.get("inExclusivityPeriod")
        and bool(episode.get("url"))
    )


async def fetch_show(client: httpx.AsyncClient, slug: str) -> dict | None:
    show = await get_json(client, f"/shows/{slug}")
    seasons = show.get("seasons") or []
    all_episodes = [e for s in seasons for e in s["episodes"]]
    if not any(is_free(e) for e in all_episodes):
        return None

    creator = show.get("creator") or {}
    return {
        "source_id": str(show["id"]),
        "slug": show["slug"],
        "title": show["name"],
        "description": show.get("description"),
        "category": "Audio Drama",
        "genres": show.get("genres") or [],
        "language": None,
        "status": None,
        "creator_name": creator.get("name"),
        "thumbnail_url": show.get("imageUrl"),
        "cover_url": show.get("imageUrl"),
        "page_url": f"{DRAMAFY_SITE}/shows/{show['slug']}",
        "has_locked_episodes": any(e.get("inExclusivityPeriod") for e in all_episodes),
        "published_at": parse_ts(show.get("publishedDate")),
        "extra": {
            "rating": show.get("rating"),
            "colors": show.get("colors") or [],
            "creator_id": creator.get("id"),
            "creator_slug": creator.get("urlSlug"),
            "credits": show.get("credits") or [],
            "social_links": show.get("socialLinks") or [],
            "copyright_holder": show.get("copyrightHolder"),
        },
        "raw": show,
        "seasons": [
            {
                "source_id": str(s["id"]),
                "season_number": s["index"] + 1,
                "title": s.get("name"),
                "extra": {"more_episodes_coming": s.get("moreEpisodesComing"),
                          "next_episode_date": s.get("nextEpisodeDate")},
            }
            for s in seasons
        ],
        "episodes": [
            {
                "source_id": str(e["id"]),
                "season_source_id": str(s["id"]),
                "episode_number": e["index"] + 1,
                "title": e.get("name"),
                "description": None,
                "audio_url": e["url"],
                "audio_url_expires_at": None,
                "duration_seconds": e.get("duration"),
                "thumbnail_url": None,
                "published_at": parse_ts(e.get("publishDate")),
                "extra": {"file_extension": e.get("fileExtension")},
            }
            for s in seasons
            for e in s["episodes"]
            if is_free(e)
        ],
    }
