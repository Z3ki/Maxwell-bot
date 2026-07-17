"""IntelEngine — Maxwell's background tech/AI/news knowledge gatherer.

Runs automatically on a schedule (default every hour). It **receives** news
directly from general AI/tech outlet feeds / APIs (RSS from Hugging Face, MarkTechPost,
TLDR AI, arXiv, Simon Willison, VentureBeat, TechCrunch, The Verge, etc.)
instead of actively searching the web. Focuses on broad model releases and tech news.

Then uses the *exact same* provider + model as autonomy/REM/context_cleanup
(via bot._get_autonomy_provider()) to curate clean, dated facts.

Facts are injected into long_term_memory (and thus visible to the main bot
in every reply + to autonomy). This stops the "I don't know about new models"
problem.

No Discord posts by default — pure memory enrichment. Can be triggered with
`,intel now`.

The main bot + API run under PM2; this loop lives in the main bot process
and keeps running as long as the PM2 bot process is up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiohttp

from control_defaults import DEFAULT_CONTROL  # noqa: E402
from utils import _atomic_json_write_sync  # noqa: E402

# Reuse the synthetic message object autonomy uses for tool execution
from autonomy import SyntheticMessage  # noqa: E402

logger = logging.getLogger(__name__)

LOG_RING_SIZE = 50
MAX_FACTS_PER_RUN = 8

# Curated high-signal general AI/tech news outlets (not company-specific).
# Focus on broad coverage of model releases, papers, benchmarks, and tech news.
# These are "receive from the source" instead of web search.
# User can override via control "intel_feed_urls" or intel_control.json.
DEFAULT_AI_FEEDS: list[str] = [
    "https://huggingface.co/blog/feed.xml",  # HF: models, tools, papers, releases
    "https://www.marktechpost.com/feed/",  # Excellent for new model releases, papers, benchmarks
    "https://tldr.tech/api/rss/ai",  # Daily curated AI digest covering many model launches
    "https://arxiv.org/rss/cs.AI",  # arXiv AI research papers
    "https://arxiv.org/rss/cs.LG",  # Machine learning papers
    "https://simonwillison.net/atom/everything/",  # Broad practical AI coverage, models, tools, analysis
    "https://venturebeat.com/category/ai/feed/",  # Enterprise AI, new models, tech developments
    "https://techcrunch.com/feed/",  # General tech + AI model releases and news
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",  # Consumer tech/AI news and releases
    # Add/override more via bot control for full customization.
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_safe(path: Path, default):
    try:
        if not path.exists():
            return default() if callable(default) else default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default() if callable(default) else default
        data = json.loads(raw)
        return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Fail closed: do NOT overwrite the on-disk file with {} on transient
        # read errors — that wiped feed_urls / state in production.
        logger.warning(
            f"Corrupt/unreadable {path.name}, using defaults (file left intact): {e}"
        )
        return default() if callable(default) else default


def _truncate(text: str, budget: int) -> str:
    budget = max(0, int(budget or 0))
    if len(text) <= budget:
        return text
    suffix = "\n... [truncated]"
    if budget <= len(suffix):
        return text[:budget]
    return text[: budget - len(suffix)] + suffix


def _text_similarity(a: str, b: str) -> float:
    """Lightweight Jaccard for quick dedup of intel facts vs LTM."""
    if not a or not b:
        return 0.0
    ta = set(str(a).lower().split())
    tb = set(str(b).lower().split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union


class IntelStore:
    """JSON-backed persistence for intel runs + state (atomic)."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "intel_state.json"
        self.log_file = self.data_dir / "intel_log.json"
        self.control_file = self.data_dir / "intel_control.json"
        self._lock = asyncio.Lock()

    async def load_state(self) -> dict:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            return data if isinstance(data, dict) else {}

    async def save_state(self, state: dict):
        async with self._lock:
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)

    async def patch_state(self, updates: dict) -> dict:
        async with self._lock:
            state = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            if not isinstance(state, dict):
                state = {}
            state.update(updates)
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
            return state

    async def update_state(self, fn) -> dict:
        async with self._lock:
            state = await asyncio.to_thread(_load_json_safe, self.state_file, dict)
            if not isinstance(state, dict):
                state = {}
            fn(state)
            await asyncio.to_thread(_atomic_json_write_sync, self.state_file, state)
            return state

    async def load_control(self) -> dict:
        async with self._lock:
            control = await asyncio.to_thread(_load_json_safe, self.control_file, dict)
            return control if isinstance(control, dict) else {}

    async def save_control(self, control: dict):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.control_file, dict(control or {})
            )

    async def load_log(self) -> list[dict]:
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.log_file, dict)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            return entries if isinstance(entries, list) else []

    async def append_log_entry(self, entry: dict):
        async with self._lock:
            data = await asyncio.to_thread(_load_json_safe, self.log_file, dict)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            entries = entries[-LOG_RING_SIZE:]
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": entries}
            )

    async def clear_log(self):
        async with self._lock:
            await asyncio.to_thread(
                _atomic_json_write_sync, self.log_file, {"entries": []}
            )

    async def record_error(self, error: str):
        await self.patch_state({"last_error": str(error)[:2000]})


class IntelEngine:
    """Background engine that keeps Maxwell's long-term memory up-to-date with
    real-world tech/AI developments so the main bot isn't clueless about new models.
    """

    def __init__(self, bot: Any):
        self.bot = bot
        self.store = IntelStore(getattr(bot.config, "DATA_DIR", "data"))
        self.enabled = self._default_enabled()
        self.interval_seconds = self._default_interval()
        self._running_flag = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_audit = ""

    def _default_enabled(self) -> bool:
        return bool(
            (getattr(self.bot, "_control", None) or {}).get(
                "intel_enabled", DEFAULT_CONTROL.get("intel_enabled", True)
            )
        )

    def _default_interval(self) -> int:
        try:
            return max(
                300,
                int(
                    (getattr(self.bot, "_control", None) or {}).get(
                        "intel_interval_seconds",
                        DEFAULT_CONTROL.get("intel_interval_seconds", 3600),
                    )
                    or 3600,
                ),
            )
        except (TypeError, ValueError):
            return 3600

    # -- lifecycle --

    async def start(self):
        # Guard FIRST, before any await. Two concurrent start() calls both used
        # to pass the (post-await) done-check and each create a _loop, leaking a
        # second loop that stop() couldn't cancel.
        if self._task is not None and not self._task.done():
            return
        await self.load_control()
        # Clear a stale on-disk "running" flag left by a previous process that
        # died mid-pass. Without this, status() reports running=True forever
        # after a crash (the in-memory flag is False, but state.running sticks).
        try:
            state = await self.store.load_state()
            if state.get("running"):
                await self.store.patch_state({"running": False, "running_since": ""})
        except Exception as e:
            logger.debug(f"Intel stale-running clear failed: {e}")
        self._task = asyncio.create_task(self._loop())
        logger.info("IntelEngine started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("IntelEngine stopped")

    async def load_control(self):
        try:
            control = await self.store.load_control()
            self.enabled = bool(control.get("enabled", self._default_enabled()))
            self.interval_seconds = max(
                300,
                int(control.get("interval_seconds", self._default_interval()) or 3600),
            )
        except Exception as e:
            logger.warning(f"Intel load_control failed: {e}")

    async def save_control(self):
        # Preserve feed_urls / other custom keys when toggling enable/interval.
        try:
            existing = await self.store.load_control()
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
        existing["enabled"] = self.enabled
        existing["interval_seconds"] = self.interval_seconds
        await self.store.save_control(existing)

    # -- loop --

    async def _loop(self):
        MAX_INTERVAL = 86400
        consecutive_failures = 0
        while True:
            try:
                await self.load_control()
                if self.enabled:
                    result = await self.run_once()
                    if result.get("error"):
                        consecutive_failures += 1
                    elif not result.get("skipped"):
                        consecutive_failures = 0
                else:
                    # Reset backoff while disabled so re-enabling after a run
                    # of failures doesn't delay the first run by up to 6x.
                    consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"IntelEngine loop error: {e}", exc_info=True)
                with contextlib.suppress(Exception):
                    await self.store.record_error(str(e))
            interval = max(300, min(self.interval_seconds, MAX_INTERVAL))
            # Cap the exponent first so we don't compute a huge 2**N int every
            # iteration on a long-dead endpoint before min() clamps it. Max 6x.
            backoff = min(1 << min(consecutive_failures, 3), 6) if consecutive_failures > 0 else 1
            await asyncio.sleep(max(300, int(interval * backoff)))

    # -- main run --

    async def run_once(self) -> dict:
        """One intel gathering + memory injection pass."""
        if self._lock.locked():
            logger.debug("Intel pass skipped — previous still running")
            return {"skipped": True}
        acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=600)
            acquired = True
        except asyncio.TimeoutError:
            logger.error("Intel lock timed out — previous pass hung, forcing release")
            return {"skipped": False, "error": "lock timeout"}
        try:
            self._running_flag = True
            await self.store.patch_state(
                {"running": True, "running_since": _utcnow_iso()}
            )
            started = _utcnow_iso()
            start = time.time()
            try:
                feed_items = await self._collect_from_feeds()
                # Optional legacy search (off by default)
                search_results = await self._perform_searches()
                facts = await self._curate_with_model(feed_items, search_results)
                added = await self._ingest_into_memory(facts)
                duration = time.time() - start
                src = "feeds" if feed_items else "search"
                audit = (
                    f"added {added} new facts from {src}"
                    if added
                    else f"no new unique facts this cycle ({src})"
                )
                await self._finish_pass(started, duration, audit, added, 0, None)
                return {
                    "skipped": False,
                    "facts_added": added,
                    "audit": audit,
                    "duration": duration,
                }
            except Exception as e:
                logger.error(f"Intel pass failed: {e}")
                duration = time.time() - start
                await self._finish_pass(started, duration, f"ERROR: {e}", 0, 0, str(e))
                return {"skipped": False, "error": str(e), "duration": duration}
            finally:
                self._running_flag = False
        finally:
            if acquired:
                self._lock.release()

    async def _finish_pass(
        self,
        started_iso: str,
        duration: float,
        audit: str,
        added: int,
        skipped: int,
        error: str | None,
    ):
        self._last_audit = str(audit)[:4000]

        def _update(s):
            s["last_run"] = started_iso
            s["last_duration"] = round(duration, 2)
            s["last_audit"] = str(audit)[:4000]
            s["facts_added_total"] = int(s.get("facts_added_total", 0)) + added
            s["passes_total"] = int(s.get("passes_total", 0)) + 1
            s["last_error"] = error

        await self.store.update_state(_update)
        await self.store.append_log_entry(
            {
                "id": f"intel_{uuid.uuid4().hex[:8]}",
                "timestamp": _utcnow_iso(),
                "duration": round(duration, 2),
                "facts_added": added,
                "audit": str(audit)[:2000],
                "error": error,
            }
        )
        with contextlib.suppress(Exception):
            await self.store.patch_state({"running": False, "running_since": ""})

    # -- gathering (feed-first "receive from outlet", no broad web search) --

    def _get_feed_urls(self) -> list[str]:
        """Get feed list. Prefers control override so user can customize outlets."""
        control = getattr(self.bot, "_control", None) or {}
        # Allow override in main bot_control
        custom = control.get("intel_feed_urls")
        if isinstance(custom, (list, tuple)) and custom:
            urls = [str(u).strip() for u in custom if str(u).strip()]
            if urls:
                return urls

        # Try to peek at intel_control.json without full async (simple read)
        try:
            p = self.store.control_file
            if p.exists():
                raw = p.read_text(encoding="utf-8")
                ec = json.loads(raw) if raw.strip() else {}
                custom2 = ec.get("feed_urls") or ec.get("intel_feed_urls")
                if isinstance(custom2, (list, tuple)) and custom2:
                    urls = [str(u).strip() for u in custom2 if str(u).strip()]
                    if urls:
                        return urls
        except Exception:
            pass

        return list(DEFAULT_AI_FEEDS)

    def _make_synthetic(self) -> SyntheticMessage:
        """Minimal synthetic for any tool fallback (kept for compatibility)."""
        author = SimpleNamespace(
            id="intel",
            display_name="Intel",
            name="intel",
            bot=True,
        )
        return SyntheticMessage(
            channel=None,
            author=author,
            guild=None,
            content="background intel gather",
        )

    async def _fetch_xml(self, url: str) -> str:
        """Fetch raw XML for a feed."""
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {
            "User-Agent": "Maxwell-Intel/1.0 (+https://github.com/maxwell-bot)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        }
        # Hard cap on bytes read from a feed. RSS is usually tens of KB; some
        # pathological feeds balloon to MB and used to OOM the bot. 10 MB is
        # far more than any sane feed and still cheap to read.
        MAX_FEED_BYTES = 10 * 1024 * 1024
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url, timeout=timeout, headers=headers, allow_redirects=True
                ) as resp,
            ):
                if resp.status != 200:
                    logger.debug(f"Intel feed HTTP {resp.status} for {url}")
                    return ""
                # Pre-check the advertised length when present...
                content_length = resp.headers.get("Content-Length")
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > MAX_FEED_BYTES
                ):
                    logger.debug(
                        f"Intel feed too large for {url}: {content_length} bytes"
                    )
                    return ""
                # ...and ALSO bound the actual read, because chunked feeds
                # (Transfer-Encoding: chunked) have no Content-Length and would
                # otherwise load an unbounded body into memory via resp.text().
                # Read one byte over the cap; if we get it, the feed is too big.
                raw = await resp.content.read(MAX_FEED_BYTES + 1)
                if len(raw) > MAX_FEED_BYTES:
                    logger.debug(
                        f"Intel feed exceeded byte cap during stream for {url}"
                    )
                    return ""
                return raw.decode(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug(f"Intel feed fetch error for {url}: {e}")
            return ""

    def _parse_feed(self, xml_text: str, feed_url: str) -> list[dict]:
        """Parse RSS 2.0 or Atom into normalized items. Very tolerant."""
        if not xml_text or len(xml_text) < 50:
            return []
        items: list[dict] = []
        source_name = feed_url.split("/")[2] if "//" in feed_url else feed_url

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            # Try to strip BOM or weird stuff
            try:
                root = ET.fromstring(xml_text.lstrip("\ufeff").strip())
            except Exception:
                return []

        # RSS: rss > channel > item
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            summary = (
                item.findtext("description")
                or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or ""
            ).strip()
            pub = (
                item.findtext("pubDate")
                # ElementTree requires the full namespaced name for dc:date;
                # the bare "dc:date" lookup always returned None, so feeds that
                # publish dates only via Dublin Core (e.g. arXiv) bypassed the
                # 7-day recency filter and crowded the 100-item cap with old items.
                or item.findtext("{http://purl.org/dc/elements/1.1/}date")
                or ""
            )
            if title or link:
                items.append(
                    {
                        "source": source_name,
                        "title": title[:300],
                        "link": link[:500],
                        "summary": summary[:800],
                        "published": pub,
                    }
                )

        # Atom: feed > entry
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href", "") if link_el is not None else "") or ""
            summary = (
                entry.findtext("{http://www.w3.org/2005/Atom}summary")
                or entry.findtext("{http://www.w3.org/2005/Atom}content")
                or ""
            ).strip()
            pub = (
                entry.findtext("{http://www.w3.org/2005/Atom}published")
                or entry.findtext("{http://www.w3.org/2005/Atom}updated")
                or ""
            )
            if title or link:
                items.append(
                    {
                        "source": source_name,
                        "title": title[:300],
                        "link": link[:500],
                        "summary": summary[:800],
                        "published": pub,
                    }
                )

        # Fallback: any <item> or <entry> without namespace
        if not items:
            for tag in ("item", "entry"):
                for el in root.findall(f".//{tag}"):
                    title = (el.findtext("title") or "").strip()
                    link = (el.findtext("link") or "").strip()
                    summary = (
                        el.findtext("description") or el.findtext("summary") or ""
                    ).strip()
                    pub = (
                        el.findtext("pubDate")
                        or el.findtext("published")
                        or el.findtext("updated")
                        or ""
                    )
                    if title or link:
                        items.append(
                            {
                                "source": source_name,
                                "title": title[:300],
                                "link": link[:500],
                                "summary": summary[:800],
                                "published": pub,
                            }
                        )

        return items

    def _parse_published(self, pub_str: str) -> datetime | None:
        if not pub_str:
            return None
        s = pub_str.strip()
        try:
            # email.utils handles most RSS dates like "Wed, 08 Jul 2026 13:30:00 GMT"
            # parsedate_to_datetime returns a NAIVE datetime for tz-less / -0000
            # dates (common in real feeds). Normalize to UTC so downstream
            # (now_aware - dt) subtraction doesn't raise TypeError and nuke the
            # whole intel pass on a single malformed date.
            dt = parsedate_to_datetime(s)
            if dt is not None and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
        # Try ISO
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    async def _collect_from_feeds(self) -> list[dict]:
        """Receive items directly from the configured news outlet feeds.
        Fetches are done in parallel for speed so commands don't feel frozen.
        """
        feed_urls = self._get_feed_urls()
        if not feed_urls:
            return []

        # Parallel fetches (much faster than sequential)
        tasks = [self._fetch_and_parse_one(url) for url in feed_urls[:12]]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: list[dict] = []
        for res in results:
            if isinstance(res, list):
                all_items.extend(res)
            elif isinstance(res, Exception):
                logger.debug(f"Intel feed task error: {res}")

        # Dedup by link or title
        seen = set()
        unique = []
        for it in all_items:
            key = (it.get("link") or it.get("title", "")).lower()[:100]
            if key and key not in seen:
                seen.add(key)
                unique.append(it)

        # Keep recent items (last ~7 days) + any without dates, to gather general intel without skipping too much
        now = datetime.now(timezone.utc)
        recent = []
        for it in unique:
            dt = self._parse_published(it.get("published", ""))
            if dt is None:
                recent.append(it)
                continue
            # Defense in depth: even with tz normalization, a pathological date
            # could still slip through and raise here. One bad item must not
            # abort the entire intel gather for the cycle.
            try:
                age = (now - dt).total_seconds()
            except Exception:
                recent.append(it)
                continue
            if age < 0:
                age = 0
            if age <= 7 * 24 * 3600:  # 7 days for more general coverage
                recent.append(it)

        # Limit volume per run so the curator LLM stays focused
        return recent[:100]

    async def _fetch_and_parse_one(self, url: str) -> list[dict]:
        xml = await self._fetch_xml(url)
        if not xml:
            return []
        return self._parse_feed(xml, url)

    # Legacy search methods kept as optional fallback (disabled by default)
    async def _perform_searches(self) -> list[dict]:
        # Intentionally lightweight now. Feeds are the primary "receive" path.
        # If you really want search fallback, set intel_use_search_fallback=true in control.
        control = getattr(self.bot, "_control", None) or {}
        if not control.get("intel_use_search_fallback"):
            return []
        # ... (original search code could be restored here if needed)
        return []

    async def _curate_with_model(
        self, feed_items: list[dict], search_fallback: list[dict] | None = None
    ) -> list[str]:
        """Use the exact same model/provider as autonomy to compile facts.
        Primary input is now direct feed items received from news outlets.
        """
        items = feed_items or []
        if not items and search_fallback:
            # legacy fallback shape
            items = [
                {
                    "source": "search",
                    "title": s.get("query", ""),
                    "summary": s.get("result", "")[:600],
                    "link": "",
                }
                for s in search_fallback[:10]
            ]

        if not items:
            return []

        # Build compact context for the model — "received" feed style
        # Limit input to keep total intel memory under control
        now_str = datetime.now().astimezone().strftime("%Y-%m-%d %A")
        ctx_parts = [f"Current date context: {now_str}"]
        ctx_parts.append(
            "The following items were received directly from general AI/tech news outlet feeds (HF, MarkTechPost, TLDR, arXiv, Simon Willison, VentureBeat, TechCrunch, The Verge, etc.)."
        )

        total_ctx_words = 0
        max_ctx_words = 1500  # leave room for prompt + output <2k
        for it in items:
            src = it.get("source", "feed")
            title = it.get("title", "").strip()
            summ = (it.get("summary") or "")[:450].replace("\n", " ")
            link = it.get("link", "")[:120]
            line = f"[{src}] {title}"
            if summ:
                line += f" — {summ}"
            if link:
                line += f" ({link})"
            w = len(line.split())
            if total_ctx_words + w > max_ctx_words:
                break
            ctx_parts.append(line)
            total_ctx_words += w

        context_blob = "\n\n".join(ctx_parts)
        context_blob = _truncate(context_blob, 16000)

        system_prompt = (
            "You are Maxwell's background intel curator (not a chat responder).\n"
            "Your job: turn items **received directly from general news outlet feeds** (Hugging Face, MarkTechPost, TLDR AI, arXiv, Simon Willison, VentureBeat, TechCrunch, The Verge, etc.) "
            "into a short list of *new, specific, high-value* facts about AI models, LLM releases, capabilities, benchmarks, papers, org announcements, or major tech news.\n\n"
            "STRICT RULES:\n"
            "- Only output facts that look genuinely recent/new based on the received items.\n"
            "- Be concrete: model names + org + key capability, date, or notable detail if available.\n"
            "- Keep each fact to one memorable sentence (max ~220 chars).\n"
            "- Do NOT invent details or repeat old knowledge.\n"
            "- Prefer unique signals (new model drops, papers, releases) over generic hype.\n"
            "- At most 8 facts total. Fewer is better if nothing solid.\n"
            "- Total combined facts text must stay under 2000 words.\n\n"
            "Return ONLY this JSON (no other text):\n"
            '{\n  "facts": ["fact one here", "fact two here"]\n}\n'
        )

        user_prompt = (
            f"Feed items received today ~ {now_str}:\n\n{context_blob}\n\n"
            "Extract the facts JSON now. Focus on model releases and concrete developments."
        )

        try:
            provider = await self.bot._get_autonomy_provider()
            if provider is None or not callable(
                getattr(provider, "generate_response", None)
            ):
                provider = getattr(self.bot, "ai_provider", None)
            if provider is None:
                logger.warning(
                    "Intel: no provider available for curation, using smart extraction from feeds"
                )
                # Strong fallback: extract specific model/release facts from titles + summaries
                facts = []
                seen = set()
                for it in (feed_items or [])[: MAX_FACTS_PER_RUN * 2]:
                    title = (it.get("title") or "").strip()
                    summary = (it.get("summary") or "").strip()[:200]
                    src = it.get("source", "feed")
                    link = it.get("link", "")
                    if not title or title.lower() in seen:
                        continue
                    seen.add(title.lower())
                    # Heuristically make good memory facts for AI models/news
                    fact = title
                    if any(
                        k in title.lower()
                        for k in [
                            "gpt",
                            "model",
                            "live",
                            "release",
                            "introducing",
                            "llm",
                            "claude",
                            "gemini",
                            "llama",
                        ]
                    ):
                        fact = f"AI release from {src}: {title}"
                        if summary and len(summary) > 20:
                            fact += f". {summary}"
                    else:
                        fact = f"Recent from {src}: {title}"
                    if link:
                        fact += f" ({link})"
                    facts.append(fact[:280])
                    if len(facts) >= MAX_FACTS_PER_RUN:
                        break

                # Enforce total Intel memory under ~2000 words max
                total_words = 0
                capped = []
                for f in facts:
                    w = len(f.split())
                    if total_words + w > 2000:
                        break
                    capped.append(f)
                    total_words += w
                return capped
            if getattr(provider, "available", None) is False:
                logger.info("Intel: provider not available, soft skip curation")
                return []

            control = getattr(self.bot, "_control", None) or {}
            model = str(control.get("autonomy_model", "") or "") or None
            timeout = max(
                30,
                min(int(control.get("ai_timeout_seconds", 120) or 120), 300),
            )

            await self.bot._acquire_ai_slot(timeout=timeout)
            try:
                raw = await provider.generate_response(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    timeout=timeout,
                    model=model,
                    max_tokens=2048,
                    disable_reasoning=bool(
                        control.get("autonomy_disable_reasoning", True)
                    ),
                )
            finally:
                await self.bot._release_ai_slot()
        except Exception as e:
            logger.error(f"Intel LLM curation call failed: {e}")
            return []

        facts = self._parse_facts(raw)

        # Enforce total Intel memory/facts under ~2000 words max
        total_words = 0
        capped = []
        for f in facts:
            w = len(f.split())
            if total_words + w > 2000:
                break
            capped.append(f)
            total_words += w
        return capped

    def _parse_facts(self, raw: str) -> list[str]:
        if not raw:
            return []
        text = str(raw).strip()
        json_str = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                json_str = text
        except Exception:
            pass
        if json_str is None:
            m = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*```", text, re.DOTALL)
            if m:
                json_str = m.group(1)
        if json_str is None:
            # last resort: first balanced {}
            candidates = []
            depth = 0
            start = -1
            for i, ch in enumerate(text):
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start != -1:
                        candidates.append(text[start : i + 1])
                        start = -1
            for c in candidates:
                try:
                    obj = json.loads(c)
                    if isinstance(obj, dict) and "facts" in obj:
                        json_str = c
                        break
                except Exception:
                    continue
            if json_str is None and candidates:
                json_str = candidates[0]

        if not json_str:
            return []

        try:
            obj = json.loads(json_str)
            facts = obj.get("facts", []) if isinstance(obj, dict) else []
            if not isinstance(facts, list):
                return []
            clean = []
            for f in facts:
                s = str(f).strip()
                if s and len(s) > 8:
                    clean.append(s[:280])
            return clean[:MAX_FACTS_PER_RUN]
        except Exception:
            return []

    async def _ingest_into_memory(self, facts: list[str]) -> int:
        memory = cast(Any, getattr(self.bot, "memory", None))
        if memory is None or not hasattr(memory, "add_long_term_memory"):
            return 0
        if not hasattr(memory, "get_long_term_memory"):
            return 0

        try:
            existing = memory.get_long_term_memory() or []
        except Exception:
            existing = []

        recent = [str(e.get("content", "")) for e in existing[-80:]]
        added = 0
        for fact in facts:
            dup = False
            for ex in recent:
                if _text_similarity(fact, ex) > 0.72:
                    dup = True
                    break
            if dup:
                continue
            dated = f"[Intel {datetime.now().strftime('%Y-%m-%d')}] {fact}"
            try:
                await memory.add_long_term_memory(dated)
                added += 1
                # keep recent list fresh for this run
                recent.append(dated)
            except Exception as e:
                logger.warning(f"Intel failed to add LTM fact: {e}")
        if added:
            logger.info(f"Intel added {added} new facts to long-term memory")
        return added

    # -- status + trigger helpers --

    async def status(self) -> dict:
        state = await self.store.load_state()
        log = await self.store.load_log()
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "running": self._running_flag or bool(state.get("running")),
            "last_run": state.get("last_run", ""),
            "last_duration": state.get("last_duration"),
            "last_audit": str(state.get("last_audit") or self._last_audit or "")[:4000],
            "last_error": state.get("last_error"),
            "facts_added_total": state.get("facts_added_total", 0),
            "passes_total": state.get("passes_total", 0),
            "log": log[-15:],
        }

    async def trigger_now(self) -> dict:
        """Force an immediate run (used by ,intel now and command queue)."""
        return await self.run_once()
