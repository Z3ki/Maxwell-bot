"""Bounded retrieval infrastructure, used only after the model calls web_search.

No prompt classification or pre-inference lookups live here. HTTP providers
share a connection pool; blocking ddgs work has its own small executor so a
stuck engine cannot occupy the threads used by memory and Discord preparation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_MAX_RESPONSE_BYTES = 512 * 1024
_CACHE_SECONDS = {"live": 0.0, "recent": 120.0, "stable": 1800.0}
_TIME_RANGES = {"day", "week", "month", "year"}


def normalize_hit(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("href") or raw.get("url") or raw.get("link") or "").strip()
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or len(url) > 1500
            or any(ord(c) < 33 for c in url)
        ):
            return None
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal", ".lan")
        ):
            return None
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None:
            if isinstance(address, ipaddress.IPv6Address) and address in _NAT64:
                return None
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if (
                not address.is_global
                or address.is_multicast
                or address.is_reserved
                or getattr(address, "is_site_local", False)
            ):
                return None
    except ValueError:
        return None
    body = str(
        raw.get("body")
        or raw.get("excerpt")
        or raw.get("content")
        or raw.get("snippet")
        or ""
    ).strip()
    if not body:
        return None
    hit = {
        "title": str(raw.get("title") or "No title").strip()[:240],
        "href": url,
        "body": body[:2000],
    }
    published = raw.get("publishedDate") or raw.get("published_date") or raw.get("date")
    if published:
        hit["published_at"] = str(published)[:80]
    return hit


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[dict[str, str], ...] = ()
    errors: tuple[str, ...] = ()
    provider: str = ""
    retrieved_at: str = ""
    cached: bool = False

    def copy(self, *, cached: bool = False) -> SearchResult:
        # Callers may persist/enrich hits; never let that mutate the cache or
        # the evidence returned to a different conversation.
        return replace(self, hits=tuple(dict(row) for row in self.hits), cached=cached)


class SearchService:
    def __init__(
        self,
        *,
        searxng_url: str = "",
        tavily_key: str = "",
        budget: float = 12.0,
        concurrency: int = 4,
        max_pending: int = 32,
        cache_size: int = 256,
    ):
        self.searxng_url = searxng_url.strip().rstrip("/")
        self.tavily_key = tavily_key.strip()
        self.budget = budget
        self.max_pending = max_pending
        self.cache_size = cache_size
        self._slots = asyncio.Semaphore(concurrency)
        self._worker_slots = asyncio.Semaphore(concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="maxwell-search"
        )
        self._session: aiohttp.ClientSession | None = None
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._cache: OrderedDict[tuple, tuple[float, SearchResult]] = OrderedDict()
        self._cooldowns: OrderedDict[str, float] = OrderedDict()
        self._closed = False

    async def search(
        self,
        query: str,
        limit: int,
        backends: list[str],
        ddgs_cls: Any = None,
        *,
        freshness: str = "recent",
        time_range: str | None = None,
    ) -> SearchResult:
        if self._closed:
            return SearchResult(errors=("search service is closed",))
        ttl = _CACHE_SECONDS.get(freshness, _CACHE_SECONDS["recent"])
        time_range = time_range if time_range in _TIME_RANGES else None
        # The model chooses freshness, including live (no completed cache).
        key = (query, limit, tuple(backends), time_range, freshness, ddgs_cls)
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is not None:
            created, result = entry
            if ttl and now - created < ttl:
                self._cache.move_to_end(key)
                return result.copy(cached=True)
            self._cache.pop(key, None)
        task = self._inflight.get(key)
        if task is None:
            if len(self._inflight) >= self.max_pending:
                return SearchResult(
                    errors=("search capacity is busy; try again shortly",)
                )
            task = asyncio.create_task(
                self._search(query, limit, backends, ddgs_cls, time_range)
            )
            self._inflight[key] = task

            def completed(done: asyncio.Task) -> None:
                if self._inflight.get(key) is done:
                    self._inflight.pop(key, None)
                if done.cancelled():
                    return
                # Consume errors even if all waiting requests were cancelled.
                if done.exception() is not None:
                    return
                result = done.result()
                if result.hits and ttl and not self._closed:
                    self._cache[key] = (time.monotonic(), result)
                    self._cache.move_to_end(key)
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)

            task.add_done_callback(completed)
        # One user's cancellation must not cancel another user's identical
        # search. The shared task still has its own deadline and capacity cap.
        result = await asyncio.shield(task)
        return result.copy()

    async def _search(
        self, query, limit, backends, ddgs_cls, time_range
    ) -> SearchResult:
        errors: list[str] = []
        try:
            # Includes admission time, HTTP providers and every fallback.
            async with asyncio.timeout(self.budget):
                async with self._slots:
                    deadline = time.monotonic() + self.budget
                    providers = []
                    if self.searxng_url:
                        providers.append("searxng")
                    if self.tavily_key:
                        providers.append("tavily")
                    if ddgs_cls is not None:
                        providers.extend("ddgs:" + b for b in backends[:8])
                    for provider in providers:
                        if self._cooldowns.get(provider, 0) > time.monotonic():
                            errors.append(provider + ": temporarily cooling down")
                            continue
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        timeout = min(4.0, remaining)
                        try:
                            if provider.startswith("ddgs:"):
                                rows = await self._ddgs(
                                    ddgs_cls,
                                    query,
                                    limit,
                                    provider[5:],
                                    timeout,
                                    time_range,
                                )
                            else:
                                rows = await self._http(
                                    provider, query, limit, timeout, time_range
                                )
                        except Exception as exc:
                            if re.search(r"no results", str(exc), re.I):
                                continue
                            # No response bodies, credentials or private user
                            # queries in logs or tool error messages.
                            detail = (
                                str(exc)
                                if isinstance(exc, SearchHTTPError)
                                else type(exc).__name__
                            )
                            errors.append(provider + ": " + detail)
                            logger.info(
                                "search provider %s failed (%s)", provider, detail
                            )
                            self._cooldowns[provider] = time.monotonic() + 20.0
                            while len(self._cooldowns) > 64:
                                self._cooldowns.popitem(last=False)
                            continue
                        hits, seen = [], set()
                        for raw in rows[:50]:
                            hit = normalize_hit(raw)
                            if hit is not None and hit["href"] not in seen:
                                seen.add(hit["href"])
                                hits.append(hit)
                            if len(hits) >= limit:
                                break
                        if hits:
                            # A useful partial response is enough. Do not
                            # query six more engines just to fill five slots.
                            return SearchResult(
                                hits=tuple(hits),
                                errors=tuple(errors),
                                provider=provider,
                                retrieved_at=datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                            )
                    if not providers:
                        errors.append("no search provider is configured or installed")
        except TimeoutError:
            errors.append("search deadline exceeded")
        return SearchResult(errors=tuple(errors))

    async def _http(self, provider, query, limit, timeout, time_range):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=8, limit_per_host=4),
                trust_env=False,
            )
        kwargs = {
            "timeout": aiohttp.ClientTimeout(total=timeout),
            "allow_redirects": False,
        }
        if provider == "searxng":
            # Only this operator-configured endpoint may be internal. Search
            # hits and fetch_url still use the public-URL/SSRF checks.
            parsed = urlsplit(self.searxng_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise SearchHTTPError("invalid SearXNG endpoint")
            params = {"q": query, "format": "json", "categories": "general"}
            if time_range:
                params["time_range"] = time_range
            request = self._session.get(
                self.searxng_url + "/search", params=params, **kwargs
            )
        else:
            payload = {
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
                "auto_parameters": False,
            }
            if time_range:
                payload["time_range"] = time_range
            request = self._session.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={"Authorization": "Bearer " + self.tavily_key},
                **kwargs,
            )
        async with request as response:
            if response.status != 200:
                raise SearchHTTPError(f"HTTP {response.status}")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(8192):
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise SearchHTTPError("response too large")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
            rows = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise SearchHTTPError("invalid search response")
            return rows

    async def _ddgs(self, cls, query, limit, backend, timeout, time_range):
        async with asyncio.timeout(timeout):
            await self._worker_slots.acquire()
            loop = asyncio.get_running_loop()

            def run():
                params = {"max_results": limit, "backend": backend}
                if time_range:
                    params["timelimit"] = {
                        "day": "d",
                        "week": "w",
                        "month": "m",
                        "year": "y",
                    }[time_range]
                return list(cls(timeout=max(1, int(timeout))).text(query, **params))

            try:
                future = loop.run_in_executor(self._executor, run)
            except BaseException:
                self._worker_slots.release()
                raise

            def worker_finished(done: asyncio.Future):
                # A timeout cannot stop Python threads. Retain this permit
                # until the actual worker exits, preventing an unbounded
                # executor queue during repeated timeouts/cancellations.
                self._worker_slots.release()
                if not done.cancelled():
                    done.exception()

            future.add_done_callback(worker_finished)
            return await asyncio.shield(future)

    async def close(self):
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        if self._session is not None:
            await self._session.close()
        self._executor.shutdown(wait=False, cancel_futures=True)


class SearchHTTPError(RuntimeError):
    """Safe, deliberately short provider diagnostics."""
