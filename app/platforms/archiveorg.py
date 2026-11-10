"""Internet Archive (archive.org): audio series and audiobooks from selected collections.

Public JSON endpoints, no login needed:
  /services/search/v1/scrape?q=collection:X   every item in a collection (cursor-paginated)
  /metadata/{identifier}                       one item's metadata and its file list
One item is one show; each original audio file in it is one episode. When the original
isn't an MP3 (e.g. FLAC), its MP3 derivative is used. Audio URLs
(https://archive.org/download/{identifier}/{file}) are permanent.

Collections and their category come from ARCHIVE_COLLECTIONS. Items are taken in order of
most downloads, up to ARCHIVE_MAX_ITEMS_PER_COLLECTION each (0 = every item).

Audiobooks (STORY_COLLECTIONS) also get `story`: links to the book's text. The text files inside a
LibriVox item are only its CD cover, so the book is found, in order, from: a Project Gutenberg link
in the description, an archive.org text item linked from the description, a title + author search
of archive.org's `gutenberg` collection, or (as that mirror is incomplete) of Project Gutenberg's
own OPDS catalogue. Searches need the exact title and the author's surname. Only links are stored.
"""

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..config import ARCHIVE_API, ARCHIVE_COLLECTIONS, ARCHIVE_MAX_ITEMS_PER_COLLECTION
from . import get_json

PLATFORM = "archiveorg"
API = ARCHIVE_API
HEADERS = {"User-Agent": "Mozilla/5.0 (audio-shows-scraper)"}

PAGE_SIZE = 1000
AUDIO_ORIGINAL_FORMATS = ("MP3", "Flac", "Ogg Vorbis", "WAVE", "AIFF", "Apple Lossless Audio", "M4A")

# archive.org `subject` tags are free text (actors, years, series names...). Only these are kept
# as genres, matched case-insensitively against each tag; the full list stays in `extra.subjects`.
GENRES = {
    "Action": ["action"], "Adventure": ["adventure"], "Biography": ["biography", "autobiography"],
    "Children": ["children", "juvenile", "kids"], "Comedy": ["comedy", "humor", "humour", "sitcom"],
    "Crime": ["crime", "police", "gangster"], "Detective": ["detective", "private eye"],
    "Drama": ["drama", "anthology"], "Fantasy": ["fantasy", "fairy tale", "fairy tales"],
    "Fiction": ["fiction", "novel", "short stories", "short story"], "History": ["history", "historical"],
    "Horror": ["horror", "supernatural", "ghost", "ghost stories"], "Literature": ["literature", "classic"],
    "Music": ["music", "musical", "variety"], "Mystery": ["mystery", "mysteries"],
    "Philosophy": ["philosophy"], "Poetry": ["poetry", "poems", "poem"],
    "Religion": ["religion", "religious", "bible", "christianity"],
    "Romance": ["romance", "love story", "love stories"], "Science": ["science", "nature"],
    "Science Fiction": ["science fiction", "sci-fi", "scifi", "sci fi"],
    "Suspense": ["suspense"], "Thriller": ["thriller", "espionage", "spy"],
    "War": ["war", "military", "wwii", "world war ii"], "Western": ["western", "westerns", "cowboy"],
}
_GENRE_LOOKUP = {alias: name for name, aliases in GENRES.items() for alias in aliases}

STORY_COLLECTIONS = {"librivoxaudio"}
GUTENBERG = "https://www.gutenberg.org"
_GUTENBERG_LINK = re.compile(r"gutenberg\.org/(?:ebooks|etext|files|cache/epub)/(\d+)", re.I)
_ARCHIVE_LINK = re.compile(r"archive\.org/(?:details|stream)/([A-Za-z0-9._-]+)", re.I)
_GUTENBERG_ID = re.compile(r"(\d+)gut$")  # archive.org's copies of Gutenberg books, e.g. barchestertowers03409gut


async def list_shows(client: httpx.AsyncClient) -> list[dict]:
    items: dict[str, dict] = {}
    for collection, category in ARCHIVE_COLLECTIONS.items():
        cursor, taken = None, 0
        while ARCHIVE_MAX_ITEMS_PER_COLLECTION <= 0 or taken < ARCHIVE_MAX_ITEMS_PER_COLLECTION:
            params = {"q": f"collection:{collection} AND mediatype:audio", "fields": "identifier,downloads",
                      "sorts": "downloads desc", "count": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            data = await get_json(client, f"/services/search/v1/scrape?{httpx.QueryParams(params)}")
            for hit in data.get("items", []):
                if ARCHIVE_MAX_ITEMS_PER_COLLECTION > 0 and taken >= ARCHIVE_MAX_ITEMS_PER_COLLECTION:
                    break
                taken += 1
                # An item in several configured collections keeps the first one's category
                items.setdefault(hit["identifier"], {"identifier": hit["identifier"], "downloads": hit.get("downloads"),
                                                     "collection": collection, "category": category})
            cursor = data.get("cursor")
            if not cursor:
                break
    return list(items.values())


def _first(value):
    return value[0] if isinstance(value, list) else value


def _as_list(value) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [v.strip() for s in values for v in str(s).split(";") if v.strip()]


def _text(value) -> str | None:
    """Item descriptions are HTML; keep plain text with paragraph breaks."""
    value = "\n".join(value) if isinstance(value, list) else value
    if not value:
        return None
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", value)
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", value)).strip() or None


def _seconds(length) -> int | None:
    """`length` is either seconds ("507.1") or a clock ("08:26", "1:02:03")."""
    if not length:
        return None
    try:
        parts = [float(p) for p in str(length).split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return round(total)


def _track(value) -> int | None:
    m = re.match(r"\s*(\d+)", str(value or ""))
    return int(m.group(1)) if m else None


def _date(value) -> datetime | None:
    value = _first(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _genres(subjects: list[str]) -> list[str]:
    found = {_GENRE_LOOKUP[s.lower()] for s in subjects if s.lower() in _GENRE_LOOKUP}
    return sorted(found)


def _episode_files(files: list[dict]) -> list[tuple[dict, dict]]:
    """(original, playable mp3) pairs: every original audio file with an MP3 to play it from."""
    def is_mp3(f: dict) -> bool:
        # Excludes "VBR MP3 ZIP" / M3U bundles, whose `original` is a list of files
        return f["name"].lower().endswith(".mp3") and "MP3" in (f.get("format") or "")

    mp3_of: dict[str, dict] = {}
    for f in files:
        if f.get("source") == "derivative" and is_mp3(f) and isinstance(f.get("original"), str):
            # Prefer the higher-quality VBR derivative over the 64Kbps one
            current = mp3_of.get(f["original"])
            if current is None or f.get("format") == "VBR MP3":
                mp3_of[f["original"]] = f
    pairs = []
    for f in files:
        fmt = f.get("format") or ""
        if f.get("source") != "original" or not any(a in fmt for a in AUDIO_ORIGINAL_FORMATS) or "ZIP" in fmt:
            continue
        playable = f if is_mp3(f) else mp3_of.get(f["name"])
        if playable:
            pairs.append((f, playable))
    pairs.sort(key=lambda p: (_track(p[0].get("track")) is None, _track(p[0].get("track")) or 0, p[0]["name"]))
    return pairs


def _gutenberg_story(number: int, match: str) -> dict:
    return {
        "source": "Project Gutenberg",
        "match": match,
        "page_url": f"{GUTENBERG}/ebooks/{number}",
        "read_url": f"{GUTENBERG}/ebooks/{number}.html.images",
        "txt_url": f"{GUTENBERG}/ebooks/{number}.txt.utf-8",
        "epub_url": f"{GUTENBERG}/ebooks/{number}.epub3.images",
        "pdf_url": None,
    }


async def _archive_story(client: httpx.AsyncClient, identifier: str, match: str) -> dict | None:
    """Text links for an archive.org `texts` item, or None if it isn't a readable text."""
    if m := _GUTENBERG_ID.search(identifier):
        return _gutenberg_story(int(m.group(1)), match)
    data = await get_json(client, f"/metadata/{quote(identifier)}")
    meta = data.get("metadata") or {}
    if meta.get("mediatype") != "texts" or meta.get("access-restricted-item") == "true":
        return None

    def file_url(*formats: str) -> str | None:
        f = next((f for fmt in formats for f in data.get("files") or [] if f.get("format") == fmt), None)
        return f"{ARCHIVE_API}/download/{quote(identifier)}/{quote(f['name'])}" if f else None

    txt_url = file_url("DjVuTXT", "Text")
    if not txt_url:
        return None
    return {
        "source": "archive.org",
        "match": match,
        "page_url": f"{ARCHIVE_API}/details/{quote(identifier)}",
        "read_url": f"{ARCHIVE_API}/details/{quote(identifier)}?view=theater",
        "txt_url": txt_url,
        "epub_url": file_url("EPUB"),
        "pdf_url": file_url("Text PDF"),
    }


def _norm(title: str) -> str:
    title = re.sub(r"[^a-z0-9]+", " ", re.sub(r"['’]", "", title.lower())).strip()
    return re.sub(r"^(the|a|an) ", "", title)


def _book_title(title: str) -> str:
    """LibriVox titles carry extras: "Alice's Adventures in Wonderland, by Lewis Carroll (version 2)"."""
    title = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title)
    title = re.sub(r",\s*by\s+.+$", "", title, flags=re.I)
    return re.sub(r"\s+(version|vol\.?|volume)\s+\d+$", "", title, flags=re.I).strip()


def _same_book(wanted: str, surname: str, title: str, creators: str) -> bool:
    title = re.split(r"[:;]|\s+—\s+", title)[0]
    title = re.sub(r",?\s*complete$", "", title.strip(), flags=re.I)
    return _norm(title) == wanted and surname in _norm(creators).split()


async def _search_gutenberg(client: httpx.AsyncClient, words: list[str], wanted: str, surname: str) -> dict | None:
    resp = await client.get(f"{GUTENBERG}/ebooks/search.opds/",
                            params={"query": " ".join(words + [surname])})
    resp.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    numbers = []
    for entry in ET.fromstring(resp.content).findall("a:entry", ns):
        number = re.search(r"/ebooks/(\d+)\.opds$", entry.findtext("a:id", "", ns))
        if number and _same_book(wanted, surname, entry.findtext("a:title", "", ns),
                                 entry.findtext("a:content", "", ns)):
            numbers.append(int(number.group(1)))
    return _gutenberg_story(min(numbers), "title_search") if numbers else None


async def _search_story(client: httpx.AsyncClient, title: str, author: str | None) -> dict | None:
    """Same book on archive.org's Gutenberg mirror, else on Gutenberg: exact title and the author's surname."""
    wanted = _norm(_book_title(title))
    surname = _norm(author.split(",")[0]).split(" ")[-1] if author else ""
    if not wanted or not surname or surname in ("various", "anonymous", "unknown"):
        return None
    # Search words split at apostrophes ("Tom's" -> "Tom"), as the search index does
    words = [w for w in re.findall(r"[a-z0-9]+", _book_title(title).lower()) if len(w) > 2] or wanted.split()
    params = httpx.QueryParams({
        "q": f"collection:gutenberg AND title:({' AND '.join(words)})",
        "fl[]": ["identifier", "title", "creator"],
        "rows": 50, "output": "json",
    })
    docs = (await get_json(client, f"/advancedsearch.php?{params}")).get("response", {}).get("docs", [])
    candidates = []
    for doc in docs:
        if _same_book(wanted, surname, _first(doc.get("title")) or "", " ".join(_as_list(doc.get("creator")))):
            number = _GUTENBERG_ID.search(doc["identifier"])
            # Prefer items with a Gutenberg number, then the lowest (the original release)
            candidates.append((number is None, int(number.group(1)) if number else 0, doc["identifier"]))
    for *_, identifier in sorted(candidates):
        if story := await _archive_story(client, identifier, "title_search"):
            return story
    return await _search_gutenberg(client, words, wanted, surname)


async def _find_story(client: httpx.AsyncClient, identifier: str, meta: dict) -> dict | None:
    description = "\n".join(meta["description"]) if isinstance(meta.get("description"), list) \
        else meta.get("description") or ""
    if m := _GUTENBERG_LINK.search(description):
        return _gutenberg_story(int(m.group(1)), "description_link")
    for linked in dict.fromkeys(_ARCHIVE_LINK.findall(description)):
        if linked != identifier and (story := await _archive_story(client, linked, "description_link")):
            return story
    return await _search_story(client, _first(meta.get("title")) or "", _first(meta.get("creator")))


async def fetch_show(client: httpx.AsyncClient, item: dict) -> dict | None:
    identifier = item["identifier"]
    data = await get_json(client, f"/metadata/{quote(identifier)}")
    meta = data.get("metadata") or {}
    if not meta or data.get("is_dark"):
        return None
    pairs = _episode_files(data.get("files") or [])
    if not pairs:
        return None

    story = None
    if item["collection"] in STORY_COLLECTIONS:
        try:
            story = await _find_story(client, identifier, meta)
        except (httpx.HTTPError, ValueError):
            pass  # the show is still saved; the next scrape tries again

    subjects = _as_list(meta.get("subject"))
    thumbnail = f"{ARCHIVE_API}/services/img/{quote(identifier)}"
    return {
        "source_id": identifier,
        "slug": identifier,
        "title": _first(meta.get("title")) or identifier,
        "description": _text(meta.get("description")),
        "category": item["category"],
        "genres": _genres(subjects),
        "language": _first(meta.get("language")),
        "status": None,
        "creator_name": _first(meta.get("creator")),
        "thumbnail_url": thumbnail,
        "cover_url": thumbnail,
        "page_url": f"{ARCHIVE_API}/details/{quote(identifier)}",
        "has_locked_episodes": False,
        "published_at": _date(meta.get("publicdate")),
        "story": story,
        "extra": {
            "collection": item["collection"],
            "collections": _as_list(meta.get("collection")),
            "subjects": subjects,
            "license_url": meta.get("licenseurl"),
            "date": _first(meta.get("date")),
            "runtime": meta.get("runtime"),
            "downloads": item.get("downloads"),
        },
        # The file list is large and fully represented by the episodes, so only metadata is kept
        "raw": {"metadata": meta},
        "seasons": [],
        "episodes": [
            {
                "source_id": f"{identifier}/{original['name']}",
                "season_source_id": None,
                "episode_number": number,
                "title": original.get("title") or playable.get("title")
                         or original["name"].rsplit(".", 1)[0].replace("_", " "),
                "description": None,
                "audio_url": f"{ARCHIVE_API}/download/{quote(identifier)}/{quote(playable['name'])}",
                "audio_url_expires_at": None,
                "duration_seconds": _seconds(original.get("length") or playable.get("length")),
                "thumbnail_url": None,
                "published_at": None,
                "extra": {"file": original["name"], "format": playable.get("format"),
                          "size_bytes": int(playable["size"]) if playable.get("size") else None,
                          "artist": original.get("artist"), "album": original.get("album")},
            }
            for number, (original, playable) in enumerate(pairs, start=1)
        ],
    }
