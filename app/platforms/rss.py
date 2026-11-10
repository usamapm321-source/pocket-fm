"""Podcast RSS feeds: fetching and turning items into episodes in the common shape.

Used by platforms that list shows but play the creator's own feed (AudioDrama, LearnOutLoud).
"""

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "podcast": "https://podcastindex.org/namespace/1.0"}


def text(value: str | None) -> str | None:
    """HTML to plain text."""
    if not value:
        return None
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", value)
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value)).strip() or None


def _int(value: str | None) -> int | None:
    try:
        return int(value.strip())
    except (AttributeError, ValueError):
        return None


def seconds(value: str | None) -> int | None:
    """itunes:duration is seconds ("2270", "2270.5") or [[h:]m:]s ("37:50", "1:02:03")."""
    if not value or not value.strip():
        return None
    try:
        total = 0.0
        for part in value.strip().split(":"):
            total = total * 60 + float(part)
        return round(total)
    except ValueError:
        return None


def _date(value: str | None) -> datetime | None:
    try:
        return parsedate_to_datetime(value.strip())
    except (AttributeError, TypeError, ValueError):
        return None


def image(el: ET.Element) -> str | None:
    """itunes:image (or RSS <image>) of a channel or item."""
    found = el.find("itunes:image", NS)
    return found.get("href") if found is not None else el.findtext("image/url")


def channel_field(channel: ET.Element, name: str) -> str | None:
    """A channel text field, e.g. "language" or "itunes:author"."""
    return (channel.findtext(name, namespaces=NS) or "").strip() or None


def episodes(channel: ET.Element) -> tuple[list[dict], list[dict]]:
    """Feed items with audio, oldest first, as (seasons, episodes).

    Items without a pubDate (e.g. LibriVox chapters) keep their feed order. Feeds that tag episodes
    with itunes:season are split into those seasons. episode_number is the 1-based position within
    the season (or the whole feed), since itunes:episode is often missing or restarts; the feed's
    own numbers are kept in `extra`.
    """
    items = []
    for item in channel.iter("item"):
        enclosure = item.find("enclosure")
        url = enclosure.get("url") if enclosure is not None else None
        if not url or not (enclosure.get("type") or "audio").startswith("audio"):
            continue
        items.append((item, enclosure, url.strip()))
    items.sort(key=lambda i: _date(i[0].findtext("pubDate")) or datetime.min.replace(tzinfo=timezone.utc))

    seasons: dict[int, dict] = {}
    numbers: dict[int | None, int] = {}
    result, seen = [], set()
    for item, enclosure, url in items:
        source_id = (item.findtext("guid") or "").strip() or url
        if source_id in seen:  # some feeds repeat an item
            continue
        seen.add(source_id)
        season = _int(item.findtext("itunes:season", namespaces=NS))
        if season is not None and season not in seasons:
            seasons[season] = {"source_id": str(season), "season_number": season,
                               "title": f"Season {season}", "extra": {}}
        numbers[season] = numbers.get(season, 0) + 1
        result.append({
            "source_id": source_id,
            "season_source_id": str(season) if season is not None else None,
            "episode_number": numbers[season],
            "title": (item.findtext("title") or item.findtext("itunes:title", namespaces=NS) or "").strip() or None,
            "description": text(item.findtext("description") or item.findtext("itunes:summary", namespaces=NS)),
            "audio_url": url,
            "audio_url_expires_at": None,
            "duration_seconds": seconds(item.findtext("itunes:duration", namespaces=NS)),
            "thumbnail_url": image(item),
            "published_at": _date(item.findtext("pubDate")),
            "extra": {
                "itunes_season": season,
                "itunes_episode": _int(item.findtext("itunes:episode", namespaces=NS)),
                "episode_type": item.findtext("itunes:episodeType", namespaces=NS),
                "explicit": item.findtext("itunes:explicit", namespaces=NS),
                "file_size": _int(enclosure.get("length")),
                "mime_type": enclosure.get("type"),
                "link": item.findtext("link"),
                # Transcripts the creator publishes (podcast:transcript), used instead of speech recognition
                "transcripts": [
                    {"url": t.get("url").strip(), "type": t.get("type"), "language": t.get("language")}
                    for t in item.findall("podcast:transcript", NS) if t.get("url")
                ],
            },
        })
    return sorted(seasons.values(), key=lambda s: s["season_number"]), result


async def fetch(client: httpx.AsyncClient, url: str, attempts: int = 3) -> tuple[ET.Element, str]:
    """GET a feed (following redirects) and return (its <channel>, the final URL).

    Retries network errors and 5xx; a 4xx (feed deleted) fails straight away.
    """
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.get(url, follow_redirects=True, timeout=60)
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 or attempt == attempts:
                raise
        except httpx.HTTPError:
            if attempt == attempts:
                raise
        await asyncio.sleep(2 ** attempt)
    try:
        channel = ET.fromstring(resp.content).find("channel")
    except ET.ParseError:  # e.g. a dead feed that now redirects to an HTML page
        channel = None
    if channel is None:
        raise ValueError(f"not an RSS feed: {resp.url}")
    return channel, str(resp.url)
