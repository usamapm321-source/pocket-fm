import asyncio
import logging
import re
from collections import OrderedDict
from contextlib import asynccontextmanager

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import transcribe
from .config import SCRAPE_INTERVAL_HOURS, SCRAPE_ON_STARTUP, TRANSCRIBE_AUTO_PLATFORMS, WHISPER_MODEL
from .db import init_db, pool
from .scraper import PLATFORMS, locks, run_scrape

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

SHOW_COLUMNS = """id, platform, source_id, slug, title, description, category, genres, language, status,
    creator_name, thumbnail_url, cover_url, page_url, episode_count, total_duration_seconds,
    has_locked_episodes, published_at, story, extra, scraped_at"""

EPISODE_COLUMNS = """id, platform, source_id, season_id, episode_number, title, description, audio_url,
    audio_url_expires_at, duration_seconds, thumbnail_url, published_at, extra"""

PlatformQuery = Query(None, description="Filter by platform: " + ", ".join(PLATFORMS))
CategoryQuery = Query(None, description="Filter by category (see /categories)")


def _check_platform(platform: str | None):
    if platform is not None and platform not in PLATFORMS:
        raise HTTPException(404, f"Unknown platform. Known: {', '.join(PLATFORMS)}")


async def _safe_scrape(platform: str):
    try:
        await run_scrape(platform)
        if platform in TRANSCRIBE_AUTO_PLATFORMS:
            log.info("%s: %d episodes queued for transcripts", platform, await transcribe.request_platform(platform))
    except Exception:
        log.exception("%s scrape failed", platform)


async def _scrape_all():
    await asyncio.gather(*(_safe_scrape(p) for p in PLATFORMS))


async def _scheduler():
    if SCRAPE_ON_STARTUP:
        await _scrape_all()
    while SCRAPE_INTERVAL_HOURS > 0:
        await asyncio.sleep(SCRAPE_INTERVAL_HOURS * 3600)
        await _scrape_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    tasks = [asyncio.create_task(_scheduler()), *await transcribe.start()]
    yield
    for task in tasks:
        task.cancel()
    await pool.close()


app = FastAPI(title="Audio shows API", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def player():
    return FileResponse(Path(__file__).parent / "static" / "player.html")


@app.get("/health")
async def health():
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/scrape", status_code=202)
async def trigger_scrape(platform: str | None = Query(None, description="Omit to scrape every platform")):
    _check_platform(platform)
    targets = [platform] if platform else list(PLATFORMS)
    if any(locks[p].locked() for p in targets):
        raise HTTPException(409, "A scrape is already running")
    for p in targets:
        asyncio.create_task(_safe_scrape(p))
    return {"status": "started", "platforms": targets}


@app.get("/scrape/runs")
async def scrape_runs(platform: str | None = PlatformQuery, limit: int = Query(10, ge=1, le=100)):
    where_sql, params = ("WHERE platform = %s", [platform]) if platform else ("", [])
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            f"SELECT * FROM scrape_runs {where_sql} ORDER BY id DESC LIMIT %s", [*params, limit]
        )).fetchall()
    return {"running": {p: lock.locked() for p, lock in locks.items()}, "runs": rows}


@app.get("/platforms")
async def list_platforms():
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            """
            SELECT platform, count(*) AS shows, sum(episode_count) AS episodes,
                   sum(total_duration_seconds) AS total_duration_seconds
            FROM shows GROUP BY platform
            """
        )).fetchall()
        last = await (await conn.execute(
            "SELECT platform, max(finished_at) AS last_scrape FROM scrape_runs "
            "WHERE status = 'success' GROUP BY platform"
        )).fetchall()
    counts = {r["platform"]: r for r in rows}
    last_scrape = {r["platform"]: r["last_scrape"] for r in last}
    return [
        {"platform": p, "shows": 0, "episodes": 0, "total_duration_seconds": 0,
         **counts.get(p, {}), "last_scrape": last_scrape.get(p)}
        for p in PLATFORMS
    ]


@app.get("/stats")
async def stats(platform: str | None = PlatformQuery):
    _check_platform(platform)
    show_where, params = ("WHERE platform = %s", [platform]) if platform else ("", [])
    async with pool.connection() as conn:
        return await (await conn.execute(
            f"""
            SELECT %s::text AS platform,
                   (SELECT count(*) FROM shows {show_where}) AS shows,
                   (SELECT count(*) FROM shows {show_where} {"AND" if platform else "WHERE"}
                       NOT has_locked_episodes) AS fully_free_shows,
                   (SELECT count(*) FROM episodes {show_where}) AS episodes,
                   (SELECT coalesce(sum(duration_seconds), 0) FROM episodes {show_where}) AS total_duration_seconds,
                   (SELECT min(audio_url_expires_at) FROM episodes {show_where}) AS next_audio_url_expiry,
                   (SELECT max(finished_at) FROM scrape_runs {show_where}
                       {"AND" if platform else "WHERE"} status = 'success') AS last_scrape
            """,
            [platform, *params * 6],
        )).fetchone()


def _filters(platform: str | None, category: str | None) -> tuple[list[str], list]:
    where, params = [], []
    if platform:
        where.append("platform = %s")
        params.append(platform)
    if category:
        where.append("category = %s")
        params.append(category)
    return where, params


@app.get("/categories")
async def list_categories(platform: str | None = PlatformQuery):
    where, params = _filters(platform, None)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    async with pool.connection() as conn:
        return await (await conn.execute(
            f"""
            SELECT category AS name, count(*) AS shows, sum(episode_count) AS episodes,
                   sum(total_duration_seconds) AS total_duration_seconds,
                   array_agg(DISTINCT platform) AS platforms
            FROM shows {where_sql} GROUP BY category ORDER BY category
            """,
            params,
        )).fetchall()


@app.get("/genres")
async def list_genres(platform: str | None = PlatformQuery, category: str | None = CategoryQuery):
    where, params = _filters(platform, category)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    async with pool.connection() as conn:
        return await (await conn.execute(
            f"SELECT g AS name, count(*) AS shows FROM shows, unnest(genres) g {where_sql} "
            "GROUP BY g ORDER BY g",
            params,
        )).fetchall()


@app.get("/shows")
async def list_shows(
    platform: str | None = PlatformQuery,
    category: str | None = CategoryQuery,
    q: str | None = Query(None, description="Search by show title or creator name"),
    genre: str | None = None,
    has_story: bool | None = Query(None, description="Only shows with (true) or without (false) a text version"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    where, params = _filters(platform, category)
    if q:
        where.append("(title ILIKE %s OR creator_name ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if genre:
        where.append("%s = ANY(genres)")
        params.append(genre)
    if has_story is not None:
        where.append(f"story IS {'NOT ' if has_story else ''}NULL")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    async with pool.connection() as conn:
        total = (await (await conn.execute(
            f"SELECT count(*) AS n FROM shows {where_sql}", params
        )).fetchone())["n"]
        rows = await (await conn.execute(
            f"SELECT {SHOW_COLUMNS} FROM shows {where_sql} ORDER BY title, platform LIMIT %s OFFSET %s",
            [*params, page_size, (page - 1) * page_size],
        )).fetchall()
    return {"total": total, "page": page, "page_size": page_size, "items": rows}


async def _show_detail(where_sql: str, params: list) -> dict:
    async with pool.connection() as conn:
        show = await (await conn.execute(
            f"SELECT {SHOW_COLUMNS} FROM shows WHERE {where_sql}", params
        )).fetchone()
        if not show:
            raise HTTPException(404, "Show not found")
        seasons = await (await conn.execute(
            "SELECT id, season_number, title, extra FROM seasons WHERE show_id = %s ORDER BY season_number",
            (show["id"],),
        )).fetchall()
        episodes = await (await conn.execute(
            f"SELECT {EPISODE_COLUMNS} FROM episodes WHERE show_id = %s ORDER BY episode_number, id",
            (show["id"],),
        )).fetchall()
    by_season: dict[int | None, list] = {}
    for e in episodes:
        by_season.setdefault(e.pop("season_id"), []).append(e)
    # Platforms without seasons get one untitled season holding every episode
    if None in by_season:
        seasons.append({"id": None, "season_number": len(seasons) + 1, "title": None, "extra": {}})
    show["seasons"] = [{**s, "episodes": by_season.get(s["id"], [])} for s in seasons]
    return show


@app.get("/shows/{show_id}")
async def get_show(show_id: int):
    return await _show_detail("id = %s", [show_id])


STORY_MAX_BYTES = 15_000_000
_story_cache: OrderedDict[int, str] = OrderedDict()  # recently read texts, by show id


def _strip_gutenberg_boilerplate(text: str) -> str:
    start = re.search(r"^\*\*\* ?START OF .*$", text, re.M)
    end = re.search(r"^\*\*\* ?END OF .*$", text, re.M)
    return text[start.end() if start else 0: end.start() if end else len(text)].strip()


@app.get("/shows/{show_id:int}/story", response_class=PlainTextResponse)
async def get_story_text(show_id: int):
    """The show's text as plain text, fetched from its stored `story.txt_url` (not stored in the DB)."""
    if show_id not in _story_cache:
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT story FROM shows WHERE id = %s", (show_id,))).fetchone()
        if not row:
            raise HTTPException(404, "Show not found")
        url = (row["story"] or {}).get("txt_url")
        if not url:
            raise HTTPException(404, "This show has no text version")
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60,
                                         headers={"User-Agent": "Mozilla/5.0 (audio-shows-scraper)"}) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Couldn't fetch the text: {exc}")
        if len(resp.content) > STORY_MAX_BYTES:
            raise HTTPException(502, "The text is too large to show here; use the download links")
        _story_cache[show_id] = _strip_gutenberg_boilerplate(resp.text)
        while len(_story_cache) > 20:
            _story_cache.popitem(last=False)
    _story_cache.move_to_end(show_id)
    return PlainTextResponse(_story_cache[show_id], headers={"Cache-Control": "public, max-age=86400"})


@app.get("/episodes/{episode_id}")
async def get_episode(episode_id: int):
    async with pool.connection() as conn:
        row = await (await conn.execute(
            """
            SELECT e.*, s.title AS season_title, s.season_number, sh.slug AS show_slug,
                   sh.title AS show_title, sh.thumbnail_url AS show_thumbnail_url
            FROM episodes e JOIN shows sh ON sh.id = e.show_id LEFT JOIN seasons s ON s.id = e.season_id
            WHERE e.id = %s
            """,
            (episode_id,),
        )).fetchone()
    if not row:
        raise HTTPException(404, "Episode not found")
    return row


# ---------- transcripts ----------

TRANSCRIPT_COLUMNS = """episode_id, status, source, model, language, progress, segments, error,
    requested_at, started_at, finished_at"""


async def _episode_or_404(conn, episode_id: int) -> dict:
    episode = await (await conn.execute(
        "SELECT id, show_id, extra FROM episodes WHERE id = %s", (episode_id,)
    )).fetchone()
    if not episode:
        raise HTTPException(404, "Episode not found")
    return episode


@app.get("/episodes/{episode_id}/transcript")
async def get_transcript(episode_id: int, format: str = Query("json", pattern="^(json|txt)$")):
    """The episode's text with timed segments. `status` is none | queued | running | done | failed.

    While running, `segments` holds what is transcribed so far. `format=txt` returns the finished text.
    """
    async with pool.connection() as conn:
        episode = await _episode_or_404(conn, episode_id)
        row = await (await conn.execute(
            f"SELECT {TRANSCRIPT_COLUMNS}, text FROM transcripts WHERE episode_id = %s", (episode_id,)
        )).fetchone()
        if row and row["status"] == "queued":
            row["queue_position"] = (await (await conn.execute(
                """
                SELECT count(*) AS n FROM transcripts q, transcripts me WHERE me.episode_id = %s
                  AND q.status = 'queued' AND (q.priority, -extract(epoch FROM q.requested_at), -q.episode_id)
                      > (me.priority, -extract(epoch FROM me.requested_at), -me.episode_id)
                """,
                (episode_id,),
            )).fetchone())["n"]
    if format == "txt":
        if not row or row["status"] != "done":
            raise HTTPException(404, "This episode has no finished transcript")
        return PlainTextResponse(row["text"] or "")
    return row or {"episode_id": episode_id, "status": "none", "segments": [],
                   "feed_transcript": transcribe.feed_has_transcript(episode["extra"])}


@app.post("/episodes/{episode_id}/transcript", status_code=202)
async def request_transcript(episode_id: int, force: bool = Query(False, description="Redo a finished transcript")):
    """Queue the episode for a transcript (ahead of whole-show requests). Failed ones are retried."""
    async with pool.connection() as conn:
        await _episode_or_404(conn, episode_id)
    await transcribe.request([episode_id], transcribe.PRIORITY_EPISODE, force)
    return await get_transcript(episode_id, "json")


@app.get("/shows/{show_id:int}/transcripts")
async def show_transcripts(show_id: int):
    """Transcript status of each episode of a show that has one (without the text)."""
    async with pool.connection() as conn:
        return await (await conn.execute(
            """
            SELECT t.episode_id, t.status, t.progress, t.source FROM transcripts t
            JOIN episodes e ON e.id = t.episode_id WHERE e.show_id = %s ORDER BY e.episode_number, e.id
            """,
            (show_id,),
        )).fetchall()


@app.post("/shows/{show_id:int}/transcripts", status_code=202)
async def request_show_transcripts(show_id: int):
    """Queue every episode of a show for a transcript, in episode order."""
    async with pool.connection() as conn:
        ids = [r["id"] for r in await (await conn.execute(
            "SELECT id FROM episodes WHERE show_id = %s ORDER BY episode_number, id", (show_id,)
        )).fetchall()]
    if not ids:
        raise HTTPException(404, "Show not found or has no episodes")
    return {"episodes": len(ids), "queued": await transcribe.request(ids, transcribe.PRIORITY_SHOW)}


class TranscriptBatch(BaseModel):
    """Which episodes to transcribe. The filters combine (AND); at least one is needed."""
    episode_ids: list[int] | None = Field(None, description="These episodes")
    show_ids: list[int] | None = Field(None, description="Every episode of these shows")
    platform: str | None = Field(None, description="Episodes of this platform")
    category: str | None = Field(None, description="Episodes of shows in this category")
    only_new: bool = Field(True, description="Skip episodes that already have a transcript (any status)")
    force: bool = Field(False, description="Redo finished transcripts (ignored when only_new is true)")
    limit: int | None = Field(None, ge=1, description="At most this many episodes, by show then episode order")


@app.post("/transcripts/batch", status_code=202)
async def request_transcript_batch(batch: TranscriptBatch):
    """Queue a batch of episodes for transcripts. Returns how many matched and how many were queued."""
    _check_platform(batch.platform)
    where, params = [], []
    for column, value in (("e.id", batch.episode_ids), ("e.show_id", batch.show_ids)):
        if value is not None:
            where.append(f"{column} = ANY(%s)")
            params.append(value)
    if batch.platform:
        where.append("e.platform = %s")
        params.append(batch.platform)
    if batch.category:
        where.append("s.category = %s")
        params.append(batch.category)
    if not where:
        raise HTTPException(422, "Give episode_ids, show_ids, platform or category")
    if batch.only_new:
        where.append("NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.episode_id = e.id)")
    async with pool.connection() as conn:
        ids = [r["id"] for r in await (await conn.execute(
            f"""
            SELECT e.id FROM episodes e JOIN shows s ON s.id = e.show_id WHERE {" AND ".join(where)}
            ORDER BY s.title, e.show_id, e.episode_number, e.id {"LIMIT %s" if batch.limit else ""}
            """,
            [*params, *([batch.limit] if batch.limit else [])],
        )).fetchall()]
    queued = await transcribe.request(ids, transcribe.PRIORITY_SHOW, batch.force) if ids else 0
    return {"matched": len(ids), "queued": queued}


@app.delete("/transcripts/queue")
async def cancel_queued_transcripts(platform: str | None = PlatformQuery):
    """Take waiting transcripts out of the queue. A queued redo goes back to its finished text."""
    _check_platform(platform)
    where_sql = "e.id = t.episode_id AND t.status = 'queued'" + (" AND e.platform = %s" if platform else "")
    params = [platform] if platform else []
    async with pool.connection() as conn, conn.transaction():
        restored = await conn.execute(
            f"UPDATE transcripts t SET status = 'done' FROM episodes e WHERE {where_sql} AND t.text IS NOT NULL",
            params,
        )
        deleted = await conn.execute(f"DELETE FROM transcripts t USING episodes e WHERE {where_sql}", params)
    return {"cancelled": restored.rowcount + deleted.rowcount}


@app.get("/transcripts")
async def transcripts_summary(platform: str | None = PlatformQuery):
    """Transcript counts by status, and audio hours done, for all platforms or one."""
    _check_platform(platform)
    where_sql, params = ("WHERE e.platform = %s", [platform]) if platform else ("", [])
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            f"""
            SELECT t.status, count(*) AS episodes, round(coalesce(sum(e.duration_seconds), 0) / 3600.0, 1) AS hours
            FROM transcripts t JOIN episodes e ON e.id = t.episode_id {where_sql} GROUP BY t.status
            """,
            params,
        )).fetchall()
    return {"whisper_model": WHISPER_MODEL, "by_status": {r["status"]: r for r in rows}}


# Last, so /shows/{id}/<something> routes above match first
@app.get("/shows/{platform}/{slug}")
async def get_show_by_slug(platform: str, slug: str):
    return await _show_detail("platform = %s AND slug = %s", [platform, slug])
