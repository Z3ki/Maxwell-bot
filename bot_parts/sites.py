"""Site-request detection, HTML routing and the knowledge-graph prompt block.

Split out of bot.py; methods keep their original bodies.
"""

import bot_common as _common

# Mechanical split: the methods below were written against bot.py's
# module globals. Bind every shared name so the bodies run unchanged.
for _name in dir(_common):
    if _name.startswith("__"):
        continue
    globals().setdefault(_name, getattr(_common, _name))
del _name

class SitesMixin:


    def _graph_prompt_block(
        self, query: str, user_id: str, budget: int,
        requester: MemoryRequester | None = None,
    ) -> str:
        # Chat triples predate provenance scopes. Only an authorized admin can
        # inspect the legacy unscoped graph until edges carry validated scope.
        if not isinstance(requester, MemoryRequester) or not requester.is_admin:
            return ""
        if not parse_bool(
            (getattr(self, "_control", None) or {}).get(
                "knowledge_graph_enabled", True
            ),
            True,
        ):
            return ""
        graph = getattr(getattr(self, "memory", None), "graph", None)
        if graph is None:
            return ""
        try:
            return graph.prompt_block(
                query=query, user_id=user_id, budget=budget, requester=requester
            )
        except Exception as e:
            logger.debug("graph prompt skipped: %s", e)
            return ""

    async def _backfill_site_graph(self) -> None:
        """Index published sites into the graph once at boot (ast scan, no LLM)."""
        if not parse_bool(
            (getattr(self, "_control", None) or {}).get(
                "knowledge_graph_enabled", True
            ),
            True,
        ):
            return
        try:
            from knowledge_graph import refresh_site

            slugs = list((getattr(self, "_sites", None) or {}) or [])
            n = 0
            for slug in slugs:
                with contextlib.suppress(Exception):
                    note = await asyncio.to_thread(refresh_site, self, slug)
                    if note:
                        n += 1
            if n:
                logger.info("Knowledge graph: indexed %s/%s sites", n, len(slugs))
        except Exception as e:
            logger.debug("site graph backfill skipped: %s", e)

    _SITE_REQUEST_RE = re.compile(
        r"\b(make|build|create|code|design|generate|spin\s*up|throw\s*together|cobble|craft|put\s*together)\b"
        r"[^\.!?\n]{0,40}\b(site|website|web\s*page|page|landing\s*page|landing|portfolio|webapp|web\s*app|dashboard|storefront|homepage|home\s*page|webview)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _looks_like_site_request(cls, content: str) -> bool:
        if not content:
            return False
        if cls._SITE_REQUEST_RE.search(content):
            return True
        # Common shorthand the model might still treat as "make a site"
        low = content.lower().strip()
        if low in {"site", "website", "webpage", "page", "landing"}:
            return True
        return bool(
            re.match(
                r"^(make|build|create|code|design)\s+me\s+a\s+(site|website|page|landing)",
                low,
            )
        )

    _HTML_DOC_HINTS = (
        "<!doctype html",
        "<html",
        "<head",
        "<body",
        "<style",
        "<script",
        "<canvas",
    )

    @classmethod
    def _looks_like_html_document(cls, text: str) -> bool:
        if not text or len(text) < 200:
            return False
        low = text.lower()
        # Real HTML document markers
        if (
            "<!doctype html" in low
            or "<html" in low
            or "<head" in low
            or "<body" in low
        ):
            return True
        # Common landing-page fingerprint: :root{} CSS vars + body{} selector
        # (model's go-to opener for any "build a site" task). Require length
        # to avoid false positives on a normal chat reply that pastes a
        # one-line CSS snippet.
        if ":root{" in low and "body{" in low and len(text) >= 1500:
            return True
        # Long block with a CSS root and a script body — generated page.
        if ":root{" in low and "<script" in low and len(text) >= 2000:
            return True
        # Generic fallback: 3+ distinct HTML/CSS/JS markers and 2K+ chars.
        hits = sum(1 for h in cls._HTML_DOC_HINTS if h in low)
        return hits >= 3 and len(text) >= 2000

    async def _auto_route_html_to_site(
        self, message, html: str, original_content: str
    ) -> str | None:
        """If the model replied with raw HTML instead of calling create_site,
        salvage the response by calling create_site ourselves. The user gets
        a working URL either way; the bot just stops spamming markup into
        chat. Returns the user-facing success message or None on no-op.
        """
        tool = self.tools.get("create_site")
        if tool is None:
            return None
        # Pick a slug from the user's message (short alphanumeric/hyphen),
        # fall back to "site-<timestamp>".
        slug_seed = re.sub(r"[^a-z0-9]+", "-", (original_content or "").lower())[:24]
        slug_seed = re.sub(r"-+", "-", slug_seed).strip("-") or "site"
        slug = f"{slug_seed[:20]}-{int(time.time()) % 100000}"
        title = (original_content or "").strip().splitlines()[0][
            :80
        ].strip() or "untitled site"
        result = await MaxwellBot._invoke_request_tool(
            self,
            message,
            "create_site",
            tool,
            name=slug,
            title=title,
            body=html,
            encoding="text",
        )
        if isinstance(result, str) and result.startswith("Error"):
            logger.warning(f"Auto-route create_site returned: {result}")
            return None
        return f"⚠️ I dropped the HTML straight into chat by mistake — saving it as a site instead.\n{result}"
