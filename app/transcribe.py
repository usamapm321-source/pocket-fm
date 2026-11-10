"""Episode transcripts: the text of an episode's audio, so listeners can read along.

Transcripts are requested per episode or per show and queued in the `transcripts` table.
Each worker takes the highest-priority queued episode and:
  1. uses the transcript the creator publishes in the podcast feed (`podcast:transcript`, kept in
     `episodes.extra.transcripts`), parsed from VTT, SRT, JSON, HTML or plain text, when there is one;
  2. otherwise downloads the audio and transcribes it locally with faster-whisper.
Segments are saved while Whisper runs, so a transcript can be read before it is finished.
"""

import asyncio
import html
import logging
import os
import re
import tempfile
import threading
import time

import httpx
from psycopg.types.json import Jsonb

from .config import (TRANSCRIBE_MAX_AUDIO_MB, TRANSCRIBE_WORKERS, WHISPER_COMPUTE_TYPE, WHISPER_CPU_THREADS,
                     WHISPER_DEVICE, WHISPER_MODEL)
from .db import pool
from .platforms.rss import text as html_to_text

log = logging.getLogger("transcribe")

HEADERS = {"User-Agent": "Mozilla/5.0 (audio-shows-scraper)"}
SAVE_EVERY_SECONDS = 10  # how often partial Whisper output is written while running
# Priorities: a single episode someone is waiting for runs before a whole show, which runs before automatic ones
PRIORITY_EPISODE, PRIORITY_SHOW, PRIORITY_AUTO = 2, 1, 0

_wake = asyncio.Event()
_model = None
_model_lock = threading.Lock()


# ---------- queue ----------

async def request(episode_ids: list[int], priority: int, force: bool = False) -> int:
    """Queue transcripts. Failed ones are retried; done ones are redone only with `force`.

    An already queued episode moves up when requested with a higher priority. Returns how many were queued.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO transcripts (episode_id, priority) SELECT unnest(%s::int[]), %s
            ON CONFLICT (episode_id) DO UPDATE SET status = 'queued', priority = EXCLUDED.priority,
                requested_at = now(), error = NULL, progress = 0
            WHERE transcripts.status = 'failed' OR (%s AND transcripts.status = 'done')
               OR (transcripts.status = 'queued' AND transcripts.priority < EXCLUDED.priority)
            """,
            (episode_ids, priority, force),
        )
    _wake.set()
    return cur.rowcount


async def request_platform(platform: str) -> int:
    """Queue every episode of a platform that has no transcript yet."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO transcripts (episode_id, priority) SELECT id, %s FROM episodes WHERE platform = %s "
            "ON CONFLICT (episode_id) DO NOTHING",
            (PRIORITY_AUTO, platform),
        )
    _wake.set()
    return cur.rowcount


async def _claim() -> dict | None:
    async with pool.connection() as conn:
        return await (await conn.execute(
            """
            UPDATE transcripts t SET status = 'running', started_at = now(), finished_at = NULL, progress = 0,
                error = NULL, source = NULL, model = NULL, language = NULL, text = NULL, segments = '[]'
            FROM episodes e
            WHERE t.episode_id = (SELECT episode_id FROM transcripts WHERE status = 'queued'
                                  ORDER BY priority DESC, requested_at, episode_id LIMIT 1 FOR UPDATE SKIP LOCKED)
              AND e.id = t.episode_id
            RETURNING e.id, e.audio_url, e.duration_seconds, e.extra
            """
        )).fetchone()


async def _save(episode_id: int, **fields) -> None:
    if "segments" in fields:
        fields["segments"] = Jsonb(fields["segments"])
    sets = ", ".join(f"{k} = %s" for k in fields)
    if fields.get("status") in ("done", "failed"):
        sets += ", finished_at = now()"
    async with pool.connection() as conn:
        await conn.execute(f"UPDATE transcripts SET {sets} WHERE episode_id = %s", [*fields.values(), episode_id])


async def _worker(n: int) -> None:
    while True:
        try:
            job = await _claim()
        except Exception:
            log.exception("transcript worker %d: couldn't read the queue", n)
            job = None
        if not job:
            _wake.clear()
            try:
                await asyncio.wait_for(_wake.wait(), timeout=30)
            except TimeoutError:
                pass
            continue
        started = time.monotonic()
        try:
            await _transcribe(job)
            log.info("episode %s (%ss of audio) transcribed in %.0fs",
                     job["id"], job["duration_seconds"], time.monotonic() - started)
        except Exception as exc:
            log.warning("episode %s transcript failed: %s", job["id"], exc)
            await _save(job["id"], status="failed", error=str(exc)[:1000] or type(exc).__name__)


async def start() -> list[asyncio.Task]:
    """Requeue transcripts interrupted by a restart and start the workers."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE transcripts SET status = 'queued' WHERE status = 'running'")
    return [asyncio.create_task(_worker(n)) for n in range(TRANSCRIBE_WORKERS)]


# ---------- one transcript ----------

async def _transcribe(job: dict) -> None:
    feed = _pick_feed_transcript((job["extra"] or {}).get("transcripts") or [])
    if feed:
        try:
            segments, text = await _from_feed(feed)
            if text:
                await _save(job["id"], status="done", progress=1, source="feed", model=feed["url"],
                            language=feed.get("language"), segments=segments, text=text)
                return
        except (httpx.HTTPError, ValueError) as exc:  # fall back to speech recognition
            log.info("episode %s feed transcript unusable (%s), using whisper", job["id"], exc)
    await _from_audio(job)


async def _from_audio(job: dict) -> None:
    fd, path = tempfile.mkstemp(suffix=".audio")
    os.close(fd)
    try:
        await _download(job["audio_url"], path)
        await _save(job["id"], source="whisper", model=WHISPER_MODEL)
        loop = asyncio.get_running_loop()
        pending = None

        def progress(segments: list[dict], done: float, language: str | None):
            # Don't make Whisper wait for the database; skip this save if the previous one is still going
            nonlocal pending
            if pending is None or pending.done():
                pending = asyncio.run_coroutine_threadsafe(
                    _save(job["id"], segments=list(segments), progress=done, language=language), loop
                )

        segments, language = await asyncio.to_thread(_whisper, path, job["duration_seconds"], progress)
        if pending:  # so a late partial save can't overwrite the finished transcript
            await asyncio.wrap_future(pending)
        await _save(job["id"], status="done", progress=1, language=language, segments=segments,
                    text=_join(segments))
    finally:
        os.unlink(path)


async def _download(url: str, path: str) -> None:
    limit = TRANSCRIBE_MAX_AUDIO_MB * 1_000_000
    async with httpx.AsyncClient(follow_redirects=True, timeout=120, headers=HEADERS) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            size = 0
            with open(path, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"audio is larger than {TRANSCRIBE_MAX_AUDIO_MB} MB")
                    f.write(chunk)


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # heavy import, only when first needed
            log.info("loading whisper model %s (%s, %s)", WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
            _model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE,
                                  cpu_threads=WHISPER_CPU_THREADS, num_workers=max(1, TRANSCRIBE_WORKERS))
        return _model


def _whisper(path: str, duration: int | None, progress) -> tuple[list[dict], str | None]:
    """Runs in a thread. Calls progress(segments so far, fraction done, language) every few seconds."""
    # The VAD filter skips music and silence, which also stops Whisper inventing text over them
    raw, info = _get_model().transcribe(path, vad_filter=True, beam_size=5)
    total = info.duration or duration or 0
    segments, last = [], time.monotonic()
    for s in raw:
        if s.text.strip():
            segments.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
        if time.monotonic() - last > SAVE_EVERY_SECONDS:
            last = time.monotonic()
            progress(segments, min(0.99, s.end / total) if total else 0, info.language)
    return segments, info.language


def _join(segments: list[dict]) -> str:
    """Plain text: segments joined into paragraphs at pauses and speaker changes."""
    out, prev = [], None
    for s in segments:
        new_para = prev is not None and (s["start"] - prev["end"] > 2 or s.get("speaker") != prev.get("speaker"))
        label = f"{s['speaker']}: " if s.get("speaker") and (prev is None or new_para) else ""
        out.append(("\n\n" if new_para else " " if prev else "") + label + s["text"])
        prev = s
    return "".join(out).strip()


# ---------- transcripts published in podcast feeds ----------

FEED_TYPES = ["text/vtt", "application/x-subrip", "application/srt", "application/json", "text/html", "text/plain"]


def _pick_feed_transcript(transcripts: list[dict]) -> dict | None:
    """The most useful one: timed formats (which can follow the audio) before plain text."""
    known = [t for t in transcripts if (t.get("type") or "").lower() in FEED_TYPES]
    return min(known, key=lambda t: FEED_TYPES.index(t["type"].lower()), default=None)


async def _from_feed(feed: dict) -> tuple[list[dict], str]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers=HEADERS) as client:
        resp = await client.get(feed["url"])
        resp.raise_for_status()
    kind = feed["type"].lower()
    if kind == "application/json":
        segments = _merge(_parse_json(resp.json()))
    elif kind in ("text/vtt", "application/x-subrip", "application/srt"):
        segments = _merge(_parse_cues(resp.text))
    else:
        text = html_to_text(resp.text) if kind == "text/html" else resp.text.strip()
        return [], text or ""
    return segments, _join(segments)


def _seconds(value: str) -> float:
    """VTT / SRT timestamps: 01:02:03.500, 02:03.500, 01:02:03,500."""
    total = 0.0
    for part in value.strip().replace(",", ".").split(":"):
        total = total * 60 + float(part)
    return total


def _parse_cues(body: str) -> list[dict]:
    """WebVTT or SRT cues. VTT voice tags (<v Name>) become the speaker."""
    segments = []
    for block in re.split(r"\n\s*\n", body.replace("\r", "")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            if "-->" not in line:
                continue
            start, end = (_seconds(p.split()[0]) for p in line.split("-->", 1))
            cue = " ".join(lines[i + 1:])
            voice = re.match(r"<v(?:\.[^ >]*)? ([^>]+)>", cue)
            cue = html.unescape(re.sub(r"<[^>]+>", "", cue)).strip()
            if cue:
                segments.append({"start": start, "end": end, "text": cue,
                                 **({"speaker": voice.group(1).strip()} if voice else {})})
            break
    return segments


def _parse_json(data: dict) -> list[dict]:
    """Podcast Index JSON transcript: {"segments": [{"startTime", "endTime", "body", "speaker"}]}."""
    segments = []
    for s in data.get("segments") or []:
        body = (s.get("body") or "").strip()
        if body:
            segments.append({"start": float(s.get("startTime") or 0), "end": float(s.get("endTime") or 0),
                             "text": body, **({"speaker": s["speaker"]} if s.get("speaker") else {})})
    return segments


def _merge(segments: list[dict]) -> list[dict]:
    """Join word-by-word or caption-sized pieces into sentences, so the text reads naturally."""
    out = []
    for s in segments:
        prev = out[-1] if out else None
        if (prev and prev.get("speaker") == s.get("speaker") and s["start"] - prev["end"] < 1
                and not re.search(r"[.!?…]['\"”’)]?$", prev["text"]) and len(prev["text"]) < 300):
            prev["text"] += " " + s["text"]
            prev["end"] = s["end"]
        else:
            out.append(dict(s))
    return out


def feed_has_transcript(extra: dict | None) -> bool:
    return _pick_feed_transcript((extra or {}).get("transcripts") or []) is not None
