"""LearnOutLoud (learnoutloud.com): free audiobooks, podcasts and courses from its directory.

The site is server-rendered PHP with no API. For each section in LEARNOUTLOUD_SECTIONS:
  POST /{section}/Page{n}           50 titles per page (selCount=3), A-Z (selType=1) so paging is stable
  POST /Podcast-Directory/{n}       the podcast directory pages without the "Page" prefix
Each title page (/Free-Audio-Video/.../{id} or /Podcast-Directory/.../{id}) has the details (author or host,
narrator, running time, rating, description, categories) and its "Get this free title from" links.
LearnOutLoud hosts almost no audio itself; it links to where a title is free:
  - a podcast RSS feed (LibriVox for most audiobooks, the publisher's feed for podcasts), linked as
    /podcaststream/listen.php?url={feed}&title={id}; its items are the episodes
  - a direct MP3 (a few of LearnOutLoud's own recordings); one episode per file
  - Spotify, YouTube, Amazon, ZIP downloads or web pages: nothing playable, so the title is skipped
"""

import asyncio
import html
import math
import re
from urllib.parse import urljoin, urlparse

import httpx

from ..config import LEARNOUTLOUD_MAX_ITEMS_PER_SECTION, LEARNOUTLOUD_SECTIONS, LEARNOUTLOUD_SITE
from . import rss

PLATFORM = "learnoutloud"
API = LEARNOUTLOUD_SITE
HEADERS = {"User-Agent": "Mozilla/5.0 (learnoutloud-scraper)"}

PAGE_SIZE = 50
LISTING_FORM = {"selCount": "3", "selType": "1"}  # 50 per page, A-Z
LISTING_CONCURRENCY = 4
CREATOR_LABELS = ("Author", "Host", "Speaker", "Lecturer", "Instructor", "Professor", "Narrator", "Publisher")
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".m4b", ".ogg", ".wav", ".aac")
FEED_URL = re.compile(r"(\.xml|\.rss|/rss|/feed|feeds?\.|podcast\.php)", re.I)


async def _page(client: httpx.AsyncClient, path: str, data: dict | None = None, attempts: int = 3) -> str:
    """One page as text. The HTML says windows-1251 but is UTF-8 apart from a few stray bytes."""
    for attempt in range(1, attempts + 1):
        try:
            resp = await (client.post(path, data=data) if data else client.get(path))
            resp.raise_for_status()
            return resp.content.decode("utf-8", "replace")
        except httpx.HTTPError:
            if attempt == attempts:
                raise
            await asyncio.sleep(2 ** attempt)


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _feed_link(page: str, title_id: str) -> str | None:
    m = re.search(rf"listen\.php\?url=([^\"']+?)&(?:amp;)?(?:all=\d+&(?:amp;)?)?title={title_id}\b", page)
    return html.unescape(m.group(1)) if m else None


def _listing_rows(page: str, section: str, category: str) -> list[dict]:
    rows = []
    for url, title_id, title in re.findall(
        r'<a href="(https?://[^"]+/(\d+))"\s+class="categorytitle1"\s*>(.*?)</a>', page, re.S
    ):
        thumb = re.search(rf'<a href="{re.escape(url)}"\s*>\s*<img src="([^"]+)"', page)
        rows.append({
            "id": title_id,
            "url": url,
            "title": _clean(title),
            "section": section,
            "category": category,
            "thumbnail_url": urljoin(LEARNOUTLOUD_SITE, thumb.group(1)) if thumb else None,
            "feed_url": _feed_link(page, title_id),
        })
    return rows


def _page_path(section: str, n: int) -> str:
    return f"/{section}/{n}" if section == "Podcast-Directory" else f"/{section}/Page{n}"


async def _list_section(client: httpx.AsyncClient, section: str, category: str) -> list[dict]:
    first = await _page(client, _page_path(section, 1), {**LISTING_FORM, "pageno": "1"})
    m = re.search(r"of\s+([\d,]+)\s+titles", first, re.I)
    total = int(m.group(1).replace(",", "")) if m else 0
    if LEARNOUTLOUD_MAX_ITEMS_PER_SECTION:
        total = min(total, LEARNOUTLOUD_MAX_ITEMS_PER_SECTION)
    sem = asyncio.Semaphore(LISTING_CONCURRENCY)

    async def page(n: int) -> str:
        async with sem:
            return await _page(client, _page_path(section, n), {**LISTING_FORM, "pageno": str(n)})

    pages = [first, *await asyncio.gather(*(page(n) for n in range(2, math.ceil(total / PAGE_SIZE) + 1)))]
    return [row for p in pages for row in _listing_rows(p, section, category)][:total or None]


async def list_shows(client: httpx.AsyncClient) -> list[dict]:
    # A title listed in several sections is kept once, under the first section
    titles: dict[str, dict] = {}
    for section, category in LEARNOUTLOUD_SECTIONS.items():
        for row in await _list_section(client, section, category):
            titles.setdefault(row["id"], row)
    return list(titles.values())


def _details(page: str) -> dict[str, str]:
    """The "Title Details" rows, label -> text. An unlabelled row (e.g. "Unabridged Edition") is kept as "Edition"."""
    rows = {}
    for label, value in re.findall(
        r'main_wrapper_data_sub_left">(.*?)</div>\s*<div[^>]*main_wrapper_data_sub_right[^>]*>(.*?)</div>', page, re.S
    ):
        label, value = _clean(label), _clean(value)
        if value:
            rows.setdefault(label or "Edition", value)
    return rows


def _running_time(value: str | None) -> int | None:
    if not value:
        return None
    hours = re.search(r"(\d+)\s*Hr", value)
    minutes = re.search(r"(\d+)\s*Min", value)
    if not hours and not minutes:
        return None
    return (int(hours.group(1)) if hours else 0) * 3600 + (int(minutes.group(1)) if minutes else 0) * 60


def _categories(page: str) -> list[tuple[str, str | None]]:
    """(category, subcategory) pairs from the "... in These Categories" box."""
    start = page.find("These Categories")
    if start < 0:
        return []
    block = page[start:page.find("Find More Titles", start)]
    pairs = []
    for line in re.findall(r'<div class="pntr_line">(.*?)</div>', block, re.S):
        names = [n for n in (_clean(a) for a in re.findall(r"<a [^>]*>(.*?)</a>", line, re.S)) if n]
        if names:
            pairs.append((names[0], names[1] if len(names) > 1 else None))
    return pairs


def _sources(page: str) -> list[dict]:
    """The "Get this free title from" links, e.g. {"type": "audio_dwld", "url": "https://librivox.org/..."}."""
    start = page.find("Get this free title from")
    if start < 0:
        return []
    block = page[start:page.find('class="bg_bot"', start)]
    return [{"type": kind, "url": html.unescape(url).strip()}
            for kind, url in re.findall(r'<a class="ic ic_(\w+)" href="([^"]+)"', block)]


def _is_audio_file(url: str) -> bool:
    return urlparse(url).path.lower().endswith(AUDIO_EXTENSIONS)


def _file_episodes(title: str, urls: list[str], running_time: int | None) -> list[dict]:
    return [
        {
            "source_id": url,
            "season_source_id": None,
            "episode_number": n,
            "title": title if len(urls) == 1 else f"{title} - Part {n}",
            "description": None,
            "audio_url": url,
            "audio_url_expires_at": None,
            "duration_seconds": running_time if len(urls) == 1 else None,
            "thumbnail_url": None,
            "published_at": None,
            "extra": {},
        }
        for n, url in enumerate(urls, 1)
    ]


def _first(pattern: str, page: str) -> str | None:
    m = re.search(pattern, page, re.S)
    return m.group(1) if m else None


async def fetch_show(client: httpx.AsyncClient, item: dict) -> dict | None:
    page = re.sub(r"<!--.*?-->", "", await _page(client, item["url"]), flags=re.S)
    details = _details(page)
    sources = _sources(page)
    title = _clean(_first(r'<h2 itemprop="name"[^>]*>(.*?)</h2>', page) or "") or item["title"]
    running_time = _running_time(details.get("Running Time"))

    feed_url = _feed_link(page, item["id"]) or item["feed_url"] or next(
        (s["url"] for s in sources if FEED_URL.search(s["url"]) and not _is_audio_file(s["url"])), None)
    files = list(dict.fromkeys(s["url"] for s in sources if _is_audio_file(s["url"])))
    seasons, episodes, channel = [], [], None
    if feed_url:
        try:
            channel, feed_url = await rss.fetch(client, feed_url)
            seasons, episodes = rss.episodes(channel)
        except Exception:
            if not files:
                raise
            feed_url = None
    if not episodes and files:
        episodes = _file_episodes(title, files, running_time)
    if not episodes:
        return None

    categories = _categories(page)
    rating = _first(r'itemprop="ratingValue">.*?([\d.]+)\s*</span>', page)
    rating_count = _first(r'itemprop="ratingCount"><strong>(\d+)', page)
    cover = _first(r'id="pimage"[^>]*value="([^"]+)"', page)
    description = _first(r'<div class="description">(.*?)</div>', page) or _first(
        r'itemprop="reviewBody"[^>]*>(.*?)</font>', page)
    name = item["url"].rstrip("/").split("/")[-2]
    return {
        "source_id": item["id"],
        "slug": f"{name.lower()}-{item['id']}",
        "title": title,
        "description": rss.text(description) or (rss.text(channel.findtext("description")) if channel is not None else None),
        "category": item["category"],
        "genres": list(dict.fromkeys(c for c, _ in categories)),
        "language": rss.channel_field(channel, "language") if channel is not None else None,
        "status": None,
        "creator_name": next((details[label] for label in CREATOR_LABELS if details.get(label)), None) or (
            rss.channel_field(channel, "itunes:author") if channel is not None else None),
        "thumbnail_url": item["thumbnail_url"] or cover,
        "cover_url": cover or item["thumbnail_url"],
        "page_url": item["url"],
        "has_locked_episodes": False,
        "published_at": None,
        "extra": {
            "section": item["section"],
            "subcategories": list(dict.fromkeys(s for _, s in categories if s)),
            "details": details,
            "running_time_seconds": running_time,
            "rating": float(rating) if rating else None,
            "rating_count": int(rating_count) if rating_count else None,
            "feed_url": feed_url,
            "sources": sources,
            "feed_image_url": rss.image(channel) if channel is not None else None,
        },
        "raw": {"listing": item, "details": details, "categories": categories, "sources": sources},
        "seasons": seasons,
        "episodes": episodes,
    }
