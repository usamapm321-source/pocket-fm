"""AudioDrama (audiodrama.com): every show in its directory.

The site is WordPress. Show metadata comes from its public REST API:
  /wp-json/wp/v2/show          every show (title, description, genre / show-tag / ai_content term ids, cover)
  /wp-json/wp/v2/genre etc.    term id -> name
AudioDrama hosts no audio itself: a show page plays the creator's public podcast RSS feed.
  /feeds/{slug}                redirects to that feed (Acast, RedCircle, Spotify for Creators, ...)
Episodes are read from the feed, so their audio URLs are the creator's own (permanent) enclosure links.
"""

import asyncio
import html
from datetime import datetime, timezone

import httpx

from ..config import AUDIODRAMA_SITE
from . import get_json, rss

PLATFORM = "audiodrama"
API = AUDIODRAMA_SITE
HEADERS = {"User-Agent": "Mozilla/5.0 (audiodrama-scraper)"}

PAGE_SIZE = 100
SHOW_FIELDS = "id,slug,date_gmt,modified_gmt,link,title,content,genre,show-tag,ai_content,_links,_embedded"


async def _terms(client: httpx.AsyncClient, taxonomy: str) -> dict[int, str]:
    terms: dict[int, str] = {}
    page = 1
    while True:
        batch = await get_json(client, f"/wp-json/wp/v2/{taxonomy}?per_page=100&page={page}&_fields=id,name")
        terms.update({t["id"]: html.unescape(t["name"]) for t in batch})
        if len(batch) < 100:
            return terms
        page += 1


async def list_shows(client: httpx.AsyncClient) -> list[tuple[dict, dict]]:
    names = dict(zip(("genre", "show-tag", "ai_content"), await asyncio.gather(
        _terms(client, "genre"), _terms(client, "show-tag"), _terms(client, "ai_content"),
    )))
    path = f"/wp-json/wp/v2/show?per_page={PAGE_SIZE}&_embed=wp:featuredmedia&_fields={SHOW_FIELDS}"
    resp = await client.get(f"{path}&page=1")
    resp.raise_for_status()
    pages = int(resp.headers.get("X-WP-TotalPages", "1"))
    shows = resp.json()
    for batch in await asyncio.gather(*(get_json(client, f"{path}&page={p}") for p in range(2, pages + 1))):
        shows.extend(batch)
    return [(s, names) for s in shows]


def _gmt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None


def _covers(show: dict) -> tuple[str | None, str | None]:
    """(thumbnail, full size) from the embedded featured image."""
    media = (show.get("_embedded") or {}).get("wp:featuredmedia") or [{}]
    sizes = (media[0].get("media_details") or {}).get("sizes") or {}
    full = media[0].get("source_url")
    return (sizes.get("medium") or {}).get("source_url") or full, full


async def fetch_show(client: httpx.AsyncClient, item: tuple[dict, dict]) -> dict | None:
    show, names = item
    channel, feed_url = await rss.fetch(client, f"/feeds/{show['slug']}/")
    seasons, episodes = rss.episodes(channel)
    if not episodes:
        return None

    thumbnail, cover = _covers(show)
    feed_image = rss.image(channel)
    ai_content = [names["ai_content"].get(t) for t in show.get("ai_content") or []]
    return {
        "source_id": str(show["id"]),
        "slug": show["slug"],
        "title": html.unescape(show["title"]["rendered"]),
        "description": rss.text(show["content"]["rendered"]) or rss.text(
            channel.findtext("description") or rss.channel_field(channel, "itunes:summary")),
        "category": "Audio Drama",
        "genres": [names["genre"][g] for g in show.get("genre") or [] if g in names["genre"]],
        "language": rss.channel_field(channel, "language"),
        "status": None,
        "creator_name": rss.channel_field(channel, "itunes:author"),
        "thumbnail_url": thumbnail or feed_image,
        "cover_url": cover or feed_image,
        "page_url": show["link"],
        "has_locked_episodes": False,
        "published_at": _gmt(show.get("date_gmt")),
        "extra": {
            "feed_url": feed_url,
            "vanity_feed_url": f"{AUDIODRAMA_SITE}/feeds/{show['slug']}",
            "website_url": channel.findtext("link"),
            "tags": [names["show-tag"][t] for t in show.get("show-tag") or [] if t in names["show-tag"]],
            "ai_content": [a for a in ai_content if a],
            "feed_image_url": feed_image,
            "copyright": channel.findtext("copyright"),
            "feed_item_count": len(channel.findall("item")),
            "updated_at": show.get("modified_gmt"),
        },
        # The feed itself can be several MB, so only the WordPress record is kept
        "raw": {k: v for k, v in show.items() if k not in ("_links", "_embedded")},
        "seasons": seasons,
        "episodes": episodes,
    }
