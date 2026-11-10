"""Runs a platform's scraper (see app/platforms) and stores its shows in the shared tables.

Rows are upserted by (platform, source_id), so a show, season or episode keeps the same
internal id across scrapes. Seasons and episodes that disappear upstream are deleted.
"""

import asyncio
import logging

import httpx
from psycopg.types.json import Jsonb

from .config import SCRAPE_CONCURRENCY
from .db import pool
from .platforms import archiveorg, audiodrama, dramafy, learnoutloud, parrotfm

log = logging.getLogger("scraper")

PLATFORMS = {m.PLATFORM: m for m in (dramafy, parrotfm, archiveorg, audiodrama, learnoutloud)}

# Only one scrape per platform at a time
locks = {name: asyncio.Lock() for name in PLATFORMS}

SHOW_FIELDS = ["source_id", "slug", "title", "description", "category", "genres", "language", "status",
               "creator_name", "thumbnail_url", "cover_url", "page_url", "has_locked_episodes",
               "published_at", "story", "extra", "raw"]
EPISODE_FIELDS = ["source_id", "episode_number", "title", "description", "audio_url",
                  "audio_url_expires_at", "duration_seconds", "thumbnail_url", "published_at", "extra"]
JSON_FIELDS = {"story", "extra", "raw"}


def _values(row: dict, fields: list[str]) -> list:
    return [Jsonb(row[f]) if f in JSON_FIELDS and row.get(f) is not None else row.get(f) for f in fields]


def _updates(fields: list[str]) -> str:
    return ", ".join(f"{f} = EXCLUDED.{f}" for f in fields)


async def save_show(platform: str, show: dict) -> int:
    """Upsert one show with its seasons and episodes. Returns the number of episodes saved."""
    episodes = show["episodes"]
    async with pool.connection() as conn, conn.transaction():
        show_id = (await (await conn.execute(
            f"""
            INSERT INTO shows (platform, {", ".join(SHOW_FIELDS)}, episode_count, total_duration_seconds)
            VALUES (%s, {", ".join(["%s"] * len(SHOW_FIELDS))}, %s, %s)
            ON CONFLICT (platform, source_id) DO UPDATE SET {_updates(SHOW_FIELDS)},
                episode_count = EXCLUDED.episode_count,
                total_duration_seconds = EXCLUDED.total_duration_seconds, scraped_at = now()
            RETURNING id
            """,
            [platform, *_values(show, SHOW_FIELDS), len(episodes),
             sum(e["duration_seconds"] or 0 for e in episodes)],
        )).fetchone())["id"]

        season_ids: dict[str, int] = {}
        for s in show["seasons"]:
            season_ids[s["source_id"]] = (await (await conn.execute(
                """
                INSERT INTO seasons (show_id, source_id, season_number, title, extra)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (show_id, source_id) DO UPDATE SET season_number = EXCLUDED.season_number,
                    title = EXCLUDED.title, extra = EXCLUDED.extra
                RETURNING id
                """,
                (show_id, s["source_id"], s["season_number"], s["title"], Jsonb(s["extra"])),
            )).fetchone())["id"]

        async with conn.cursor() as cur:
            await cur.executemany(
                f"""
                INSERT INTO episodes (platform, show_id, season_id, {", ".join(EPISODE_FIELDS)})
                VALUES (%s, %s, %s, {", ".join(["%s"] * len(EPISODE_FIELDS))})
                ON CONFLICT (platform, source_id) DO UPDATE SET show_id = EXCLUDED.show_id,
                    season_id = EXCLUDED.season_id, {_updates(EPISODE_FIELDS)}, scraped_at = now()
                """,
                [
                    [platform, show_id, season_ids.get(e["season_source_id"]), *_values(e, EPISODE_FIELDS)]
                    for e in episodes
                ],
            )
        await conn.execute(
            "DELETE FROM episodes WHERE show_id = %s AND source_id <> ALL(%s)",
            (show_id, [e["source_id"] for e in episodes]),
        )
        await conn.execute(
            "DELETE FROM seasons WHERE show_id = %s AND source_id <> ALL(%s)",
            (show_id, list(season_ids)),
        )
    return len(episodes)


async def run_scrape(platform: str) -> dict:
    module = PLATFORMS[platform]
    async with locks[platform]:
        async with pool.connection() as conn:
            run_id = (await (await conn.execute(
                "INSERT INTO scrape_runs (platform) VALUES (%s) RETURNING id", (platform,)
            )).fetchone())["id"]

        errors: list[dict] = []
        shows_saved = episodes_saved = 0
        items: list = []
        try:
            async with httpx.AsyncClient(base_url=module.API, headers=module.HEADERS, timeout=30) as client:
                items = await module.list_shows(client)
                log.info("%s scrape %s: found %d shows", platform, run_id, len(items))
                sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)

                async def process(item):
                    nonlocal shows_saved, episodes_saved
                    async with sem:
                        try:
                            show = await module.fetch_show(client, item)
                            saved = await save_show(platform, show) if show else 0
                        except Exception as exc:  # keep going; record per-show failures
                            log.warning("%s show %s failed: %s", platform, item, exc)
                            errors.append({"show": str(item)[:200], "error": str(exc)})
                            return
                    if saved:
                        shows_saved += 1
                        episodes_saved += saved

                await asyncio.gather(*(process(i) for i in items))
            status = "success"
        except Exception as exc:
            log.exception("%s scrape %s failed", platform, run_id)
            errors.append({"error": str(exc)})
            status = "failed"

        async with pool.connection() as conn:
            row = await (await conn.execute(
                """
                UPDATE scrape_runs SET finished_at = now(), status = %s, shows_found = %s,
                       shows_saved = %s, episodes_saved = %s, errors = %s
                WHERE id = %s RETURNING *
                """,
                (status, len(items), shows_saved, episodes_saved, Jsonb(errors), run_id),
            )).fetchone()
        log.info("%s scrape %s %s: %d shows, %d episodes saved",
                 platform, run_id, status, shows_saved, episodes_saved)
        return row
