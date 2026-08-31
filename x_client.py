"""Maxwell on X (Twitter).

He can read a Discord room, a mailbox and a web page. X was the one feed he
got quoted at all day and could never look at himself — and the official API
wants money for the privilege. This module is the free way in, and it keeps
the two halves apart on purpose:

* **Reading costs nothing.** ``syndication`` (X's own embed backend — no
  account, no key, works for any public profile or tweet) and ``rss`` (any
  Nitter or RSSHub instance you point it at) answer "what has @someone
  posted" and "what is happening with X" with no credentials at all.
* **Writing needs an account**, and there is exactly one free way to have
  one: the session cookies of a browser already logged in as him
  (``X_AUTH_TOKEN`` + ``X_CT0``). Same posture as the Discord side of this
  project — it is his account, driven by his software, and X's ToS has
  opinions about that. Or point ``X_API_BASE_URL`` at whatever gateway you
  already run and the ``api`` backend speaks to that instead.

Nothing here talks to the paid developer API, and nothing here needs a
developer account.

Backends are tried in order for reads, so a stale cookie or a dead Nitter
instance degrades to the next one instead of turning the feature off::

    cookies  read+write  home timeline, mentions, search, user, tweet
    api      read+write  your own gateway (X_API_BASE_URL)
    rss      read        Nitter/RSSHub: user + search
    syndication read     X's embed backend: user + tweet, zero config

The fragile part is deliberately isolated: X's internal GraphQL query ids
rot every few weeks. They live in ``data/x_graphql.json`` (or
``X_GRAPHQL_FILE``) and every call that fails on a stale id says exactly
how to refresh it. A stale id costs the cookie backend, not the feature —
reads fall through to syndication, and only posting actually stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

import aiohttp

from utils import _atomic_json_write_sync, _load_json_safe

logger = logging.getLogger(__name__)

# The bearer token every logged-out x.com browser tab sends. It is not a
# secret and never has been — it is baked into the web app's JS bundle — but
# the internal API refuses requests without it.
WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

WEB_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

# Query ids for the internal GraphQL API. THESE ROT — X redeploys and the
# hash changes, at which point the endpoint 404s. Override them without
# touching the code by writing data/x_graphql.json:
#
#   {"ids": {"CreateTweet": "abc123..."}, "features": {"some_flag": false}}
#
# To find a fresh one: log into x.com, open devtools → Network, filter on
# "graphql", do the thing you want (post a tweet, open a profile) and copy
# the id out of the request URL — /i/api/graphql/<ID>/CreateTweet.
DEFAULT_GRAPHQL_IDS: dict[str, str] = {
    "CreateTweet": "SoVnbfCycZ7fERGCwpZkYA",
    "DeleteTweet": "VaenaVgh5q5ih7kvyVjgtg",
    "FavoriteTweet": "lI07N6Otwv1PhnEgXILM7A",
    "UnfavoriteTweet": "ZYKSe-w7KEslx3JhSIk5LA",
    "CreateRetweet": "ojPdsZsimiJrUGLR1sjUtA",
    "UserByScreenName": "G3KGOASz96M-Qu0nwmGXNg",
    "UserTweets": "HuTx74BxAnezK1gWvYY7zg",
    "SearchTimeline": "gkjsKepM6gl_HmFWoWKfgg",
    "HomeLatestTimeline": "zhX91JE87mWvfprhYE97xA",
    "TweetDetail": "VWFGPVAGkZMGRKGe3GFFnA",
}

# The internal API rejects a GraphQL call whose `features` blob is missing a
# flag it expects, and tells you which one in the error body. We ship a
# broad set, and _graphql() parses "The following features cannot be null"
# out of a 400 and retries once with the missing flags added — so a new flag
# costs one wasted request, not an outage.
DEFAULT_GRAPHQL_FEATURES: dict[str, bool] = {
    "articles_preview_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
}

READ_ACTIONS = frozenset({"home", "user", "search", "mentions", "tweet"})
WRITE_ACTIONS = frozenset({"post", "reply", "quote", "delete", "like", "repost"})

MAX_LIMIT = 50
TWEET_TEXT_CHARS = 600


class XError(Exception):
    """Anything that stopped a read or a write. Carries a usable message."""


# ---------------------------------------------------------------------------
# The one shape everything downstream sees
# ---------------------------------------------------------------------------


@dataclass
class Tweet:
    """One post, normalized out of whichever backend produced it.

    Every backend returns these, so the tools, the renderer and the mention
    poller never learn which API the data came from. A field nobody could
    fill stays empty rather than guessed — an absent like count renders as
    nothing, not as zero.
    """

    id: str = ""
    text: str = ""
    author: str = ""  # screen name, no @
    author_name: str = ""
    created_at: str = ""  # ISO 8601 UTC when we could parse it
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    views: int | None = None
    media: list[str] = field(default_factory=list)
    is_repost: bool = False
    reply_to_id: str = ""
    quoted: str = ""  # "@who: text" of a quoted post, when present
    source: str = ""  # which backend produced it

    @property
    def url(self) -> str:
        if self.id and self.author:
            return f"https://x.com/{self.author}/status/{self.id}"
        if self.id:
            return f"https://x.com/i/status/{self.id}"
        return ""


def _text_of(raw: Any) -> str:
    return html.unescape(str(raw or "")).strip()


def _int_or_none(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


_TWITTER_TIME = "%a %b %d %H:%M:%S %z %Y"


def parse_time(raw: Any) -> datetime | None:
    """Both time formats X uses, plus RSS, or None.

    The internal API says ``Wed Aug 20 12:00:00 +0000 2026`` (which is not
    RFC 2822 — the year is at the end, so email.utils chokes on it), the
    syndication backend says ISO 8601 with a Z, and RSS says RFC 2822. All
    three arrive here.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return datetime.strptime(text, _TWITTER_TIME)
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    with contextlib.suppress(Exception):
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(text)
    return None


def _iso(raw: Any) -> str:
    parsed = parse_time(raw)
    if parsed is None:
        return str(raw or "")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def relative_age(iso_text: str, *, now: datetime | None = None) -> str:
    """"2h" / "3d". A timestamp is noise; how old it is, is the news."""
    parsed = parse_time(iso_text)
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    seconds = (current - parsed).total_seconds()
    if seconds < 0:
        return "now"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _view_count(raw: dict) -> Any:
    """GraphQL nests the view count in a dict; a gateway may send a number."""
    views = raw.get("views")
    if isinstance(views, dict):
        return views.get("count")
    return views


def _media_from_legacy(body: dict) -> list[str]:
    """Real media URLs out of whichever media block a shape carries.

    Never the ``url`` field: that is the t.co stub, which resolves to the
    post's own web page rather than to a picture. Handing that to the vision
    tools gets an HTML page back, so a media list is either pbs/video URLs or
    empty.
    """
    out: list[str] = []
    entities = (
        body.get("extended_entities")
        or body.get("entities")
        or {}
    )
    items = list(entities.get("media") or []) if isinstance(entities, dict) else []
    # The syndication shape keeps the usable URLs here instead.
    items = list(body.get("mediaDetails") or []) + items
    for item in items[:6]:
        if not isinstance(item, dict):
            continue
        info = item.get("video_info")
        if isinstance(info, dict):
            variants = [
                v
                for v in (info.get("variants") or [])
                if isinstance(v, dict) and str(v.get("content_type") or "").endswith("mp4")
            ]
            if variants:
                best = max(variants, key=lambda v: _int_or_none(v.get("bitrate")) or 0)
                out.append(str(best.get("url") or ""))
                continue
        url = item.get("media_url_https") or item.get("media_url")
        if url:
            out.append(str(url))
    # Same picture can arrive through mediaDetails and entities both.
    return list(dict.fromkeys(u for u in out if u))[:4]


def _strip_trailing_media_link(text: str, media: list[str]) -> str:
    """Drop the t.co stub X appends for its own attached media.

    It is not a link anyone can follow usefully and it is noise in every
    rendered line. A t.co in the middle of a sentence is somebody's actual
    link and is left alone.
    """
    if not media:
        return text
    return re.sub(r"\s*https://t\.co/\w+\s*$", "", text).strip()


def normalize_tweet(raw: Any, *, source: str = "") -> Tweet | None:
    """Any backend's post object → :class:`Tweet`, or None if it isn't one.

    Four shapes reach this: the internal GraphQL result (``legacy`` +
    ``core``), the syndication embed shape, a v2-ish ``{id, text, author}``,
    and whatever a custom gateway invented. Rather than four parsers with
    four sets of bugs, this reads the union of the key names they use and
    takes the first that is present.
    """
    if not isinstance(raw, dict):
        return None

    # TweetWithVisibilityResults wraps the real thing one level down.
    if isinstance(raw.get("tweet"), dict) and "legacy" in raw.get("tweet", {}):
        raw = raw["tweet"]

    legacy = raw.get("legacy") if isinstance(raw.get("legacy"), dict) else {}
    body = legacy or raw

    text = _text_of(
        body.get("full_text")
        or body.get("text")
        or body.get("note_tweet_text")
        or raw.get("text")
        or raw.get("content")
    )
    tid = str(
        raw.get("rest_id")
        or body.get("id_str")
        or body.get("id")
        or raw.get("id_str")
        or raw.get("id")
        or raw.get("tweet_id")
        or ""
    ).strip()
    # A "post" with neither an id nor text is some other object in the tree.
    if not tid and not text:
        return None

    # Long-form posts carry the full body out of band; legacy.full_text is
    # the truncated 280-char version with an ellipsis.
    note = raw.get("note_tweet") or {}
    if isinstance(note, dict):
        results = ((note.get("note_tweet_results") or {}).get("result") or {})
        if isinstance(results, dict) and results.get("text"):
            text = _text_of(results["text"])

    user = {}
    for candidate in (
        ((raw.get("core") or {}).get("user_results") or {}).get("result"),
        raw.get("user"),
        raw.get("author"),
        body.get("user"),
    ):
        if isinstance(candidate, dict):
            user = candidate
            break
    user_legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else user
    core_user = user.get("core") if isinstance(user.get("core"), dict) else {}

    author = str(
        user_legacy.get("screen_name")
        or core_user.get("screen_name")
        or user.get("screen_name")
        or user.get("username")
        or raw.get("username")
        or raw.get("screen_name")
        or ""
    ).lstrip("@")
    author_name = str(
        user_legacy.get("name") or core_user.get("name") or user.get("name") or ""
    )

    media = _media_from_legacy(body if legacy else raw)
    if not media:
        for photo in (raw.get("photos") or [])[:4]:
            if isinstance(photo, dict) and photo.get("url"):
                media.append(str(photo["url"]))
        video = raw.get("video")
        if isinstance(video, dict):
            variants = [
                v
                for v in (video.get("variants") or [])
                if isinstance(v, dict) and str(v.get("type") or "").endswith("mp4")
            ]
            if variants:
                media.append(str(variants[-1].get("src") or ""))
    media = [m for m in media if m]

    quoted = ""
    quoted_raw = (
        ((raw.get("quoted_status_result") or {}).get("result"))
        or raw.get("quoted_tweet")
        or raw.get("quoted_status")
    )
    quoted_tweet = normalize_tweet(quoted_raw, source=source) if quoted_raw else None
    if quoted_tweet is not None:
        who = f"@{quoted_tweet.author}" if quoted_tweet.author else "quoted"
        quoted = f"{who}: {quoted_tweet.text}"[:280]

    return Tweet(
        id=tid,
        text=_strip_trailing_media_link(text, media),
        author=author,
        author_name=author_name,
        created_at=_iso(body.get("created_at") or raw.get("created_at")),
        likes=_int_or_none(body.get("favorite_count") or raw.get("favorite_count")),
        reposts=_int_or_none(body.get("retweet_count") or raw.get("retweet_count")),
        replies=_int_or_none(
            body.get("reply_count")
            or raw.get("reply_count")
            or raw.get("conversation_count")
        ),
        views=_int_or_none(_view_count(raw)),
        media=media,
        is_repost=bool(
            body.get("retweeted_status_result")
            or body.get("retweeted_status")
            or text.startswith("RT @")
        ),
        reply_to_id=str(body.get("in_reply_to_status_id_str") or ""),
        quoted=quoted,
        source=source,
    )


def collect_tweets(node: Any, *, source: str = "", limit: int = MAX_LIMIT) -> list[Tweet]:
    """Pull every post out of a timeline payload, in document order.

    A deep walk rather than the documented instruction/entry path, because
    that path is exactly the part of the internal API that changes shape
    between deploys and this one only needs the objects. A tweet node is not
    descended into once matched, so a quote-tweet's inner post is attached to
    its parent rather than surfacing as a separate item.
    """
    out: list[Tweet] = []
    seen: set[str] = set()

    def walk(item: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if not isinstance(item, dict):
            return
        typename = str(item.get("__typename") or "")
        looks_like_tweet = typename in {"Tweet", "TweetWithVisibilityResults"} or (
            "rest_id" in item and isinstance(item.get("legacy"), dict)
        )
        if looks_like_tweet:
            tweet = normalize_tweet(item, source=source)
            if tweet is not None and tweet.id and tweet.id not in seen:
                seen.add(tweet.id)
                out.append(tweet)
                return
        for value in item.values():
            walk(value)

    walk(node)
    return out[:limit]


def render_tweets(tweets: list[Tweet], *, header: str = "", max_text: int = TWEET_TEXT_CHARS) -> str:
    """The block the model reads. Dense, one post per stanza, links last.

    Counts are only rendered when a backend actually supplied them: an
    unauthenticated read has no like count, and printing "0 likes" would be
    a lie the model then repeats out loud.
    """
    if not tweets:
        return (header + "\n" if header else "") + "(nothing found)"
    lines = [header] if header else []
    for index, tweet in enumerate(tweets, 1):
        who = f"@{tweet.author}" if tweet.author else "(unknown)"
        if tweet.author_name and tweet.author_name.lower() != tweet.author.lower():
            who += f" ({tweet.author_name})"
        age = relative_age(tweet.created_at)
        head = f"[{index}] {who}" + (f" · {age} ago" if age else "")
        if tweet.is_repost:
            head += " · repost"
        lines.append(head)
        text = tweet.text[:max_text] + ("…" if len(tweet.text) > max_text else "")
        lines.append(f"    {text}" if text else "    (no text)")
        if tweet.quoted:
            lines.append(f"    ↳ quoting {tweet.quoted[:200]}")
        stats = [
            f"{value} {label}"
            for value, label in (
                (tweet.likes, "likes"),
                (tweet.reposts, "reposts"),
                (tweet.replies, "replies"),
                (tweet.views, "views"),
            )
            if value is not None
        ]
        if stats:
            lines.append("    " + " · ".join(stats))
        lines.extend(f"    media: {url}" for url in tweet.media[:4])
        if tweet.url:
            lines.append(f"    {tweet.url}  (id={tweet.id})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _Backend:
    name = "base"
    can_write = False
    reads: frozenset[str] = frozenset()

    def __init__(self, client: "XClient"):
        self.client = client
        self.cfg = client.cfg

    def configured(self) -> bool:
        return False

    async def read(self, action: str, **params: Any) -> list[Tweet]:
        raise XError(f"{self.name} cannot read {action}")

    async def write(self, action: str, **params: Any) -> dict:
        raise XError(f"{self.name} cannot {action}")


class CookieBackend(_Backend):
    """x.com's own internal API, driven with a logged-in session cookie.

    This is the only free way to *write*, and the only way to read the home
    timeline and mentions — both are per-account views that no
    unauthenticated endpoint exposes. It needs two cookies out of a browser
    already logged in as him: ``auth_token`` (the session) and ``ct0`` (the
    CSRF token, which must also be echoed in a header — that pairing is the
    whole of X's CSRF scheme).
    """

    name = "cookies"
    can_write = True
    reads = frozenset({"home", "user", "search", "mentions", "tweet"})

    def configured(self) -> bool:
        return bool(self.cfg.get("auth_token") and self.cfg.get("ct0"))

    def _headers(self) -> dict[str, str]:
        ct0 = str(self.cfg.get("ct0") or "")
        return {
            "authorization": f"Bearer {WEB_BEARER}",
            "cookie": f"auth_token={self.cfg.get('auth_token')}; ct0={ct0}",
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": str(self.cfg.get("lang") or "en"),
            "content-type": "application/json",
            "user-agent": WEB_UA,
            "referer": "https://x.com/",
            "origin": "https://x.com",
        }

    async def _graphql(
        self, operation: str, variables: dict, *, method: str = "GET"
    ) -> dict:
        """One GraphQL call, with the two failures that actually happen handled.

        A missing feature flag comes back as a 400 naming the flags; we add
        them and retry once. A rotated query id comes back as a 404, which no
        retry can fix — that one is turned into the message that tells the
        operator where to get a fresh id, because otherwise it reads as "X is
        down" and someone spends an evening on it.
        """
        qid = self.client.graphql_ids.get(operation)
        if not qid:
            raise XError(
                f"no GraphQL query id for {operation}. Add one to "
                f"{self.client.graphql_path} under \"ids\"."
            )
        url = f"https://x.com/i/api/graphql/{qid}/{operation}"
        features = dict(self.client.graphql_features)
        for attempt in (1, 2):
            # POST bodies carry the query id as well as the path; the web
            # app sends it on every POST operation, not just the writes.
            payload = {"variables": variables, "features": features}
            if method == "POST":
                payload["queryId"] = qid
            if method == "POST":
                status, body = await self.client.request(
                    "POST", url, headers=self._headers(), json_body=payload
                )
            else:
                params = {
                    "variables": json.dumps(variables, separators=(",", ":")),
                    "features": json.dumps(features, separators=(",", ":")),
                }
                status, body = await self.client.request(
                    "GET", url, headers=self._headers(), params=params
                )
            if status == 404:
                raise XError(
                    f"GraphQL {operation} returned 404 — the query id "
                    f"({qid}) has rotated. Grab a fresh one from x.com "
                    f"devtools → Network → filter 'graphql', and put it in "
                    f"{self.client.graphql_path} under \"ids\"."
                )
            if status in {401, 403}:
                raise XError(
                    f"X rejected the session on {operation} (HTTP {status}). "
                    "X_AUTH_TOKEN / X_CT0 are expired or from a different "
                    "account — copy both cookies again from a logged-in tab."
                )
            if status == 429:
                self.client.note_rate_limit(self.name)
                raise XError(f"{operation}: rate limited by X (429). Backing off.")
            data = self.client.decode_json(body)
            missing = _missing_features(data, body)
            if status >= 400 and missing and attempt == 1:
                # One wasted request per new flag, then it works. Cheaper
                # than an outage every time X ships a feature gate.
                for flag in missing:
                    features.setdefault(flag, False)
                logger.info("X GraphQL %s: added missing features %s", operation, missing)
                continue
            if status >= 400:
                raise XError(f"{operation} failed (HTTP {status}): {_first_error(data, body)}")
            errors = data.get("errors") if isinstance(data, dict) else None
            if errors and not data.get("data"):
                raise XError(f"{operation}: {_first_error(data, body)}")
            return data if isinstance(data, dict) else {}
        raise XError(f"{operation}: gave up after a feature retry")

    async def _user_id(self, handle: str) -> str:
        clean = handle.lstrip("@").strip()
        cached = self.client.user_ids.get(clean.lower())
        if cached:
            return cached
        data = await self._graphql(
            "UserByScreenName",
            {"screen_name": clean, "withSafetyModeUserFields": True},
        )
        node = ((data.get("data") or {}).get("user") or {}).get("result") or {}
        uid = str(node.get("rest_id") or "")
        if not uid:
            raise XError(f"no such account: @{clean}")
        self.client.user_ids[clean.lower()] = uid
        return uid

    async def read(self, action: str, **params: Any) -> list[Tweet]:
        limit = int(params.get("limit") or 20)
        if action == "user":
            handle = str(params.get("handle") or "").lstrip("@")
            if not handle:
                raise XError("handle is required for a user timeline")
            uid = await self._user_id(handle)
            data = await self._graphql(
                "UserTweets",
                {
                    "userId": uid,
                    "count": min(limit, MAX_LIMIT),
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetFields": False,
                    "withVoice": False,
                    "withV2Timeline": True,
                },
            )
        elif action == "search":
            query = str(params.get("query") or "").strip()
            if not query:
                raise XError("query is required for a search")
            data = await self._graphql(
                "SearchTimeline",
                {
                    "rawQuery": query,
                    "count": min(limit, MAX_LIMIT),
                    "querySource": "typed_query",
                    "product": str(params.get("product") or "Latest"),
                },
            )
        elif action == "mentions":
            handle = str(self.cfg.get("handle") or "").lstrip("@")
            if not handle:
                raise XError(
                    "X_HANDLE is not set, so there is no account to read "
                    "mentions of"
                )
            data = await self._graphql(
                "SearchTimeline",
                {
                    "rawQuery": f"(@{handle}) -from:{handle}",
                    "count": min(limit, MAX_LIMIT),
                    "querySource": "typed_query",
                    "product": "Latest",
                },
            )
        elif action == "home":
            data = await self._graphql(
                "HomeLatestTimeline",
                {
                    "count": min(limit, MAX_LIMIT),
                    "includePromotedContent": False,
                    "latestControlAvailable": True,
                    "withCommunity": True,
                },
                method="POST",
            )
        elif action == "tweet":
            tid = _clean_id(params.get("tweet_id"))
            if not tid:
                raise XError("tweet_id is required")
            data = await self._graphql(
                "TweetDetail",
                {
                    "focalTweetId": tid,
                    "with_rux_injections": False,
                    "includePromotedContent": False,
                    "withCommunity": True,
                    "withQuickPromoteEligibilityTweetFields": False,
                    "withBirdwatchNotes": False,
                    "withVoice": False,
                    "withV2Timeline": True,
                },
            )
        else:
            raise XError(f"cookies backend cannot read {action}")
        return collect_tweets(data, source=self.name, limit=limit)

    async def write(self, action: str, **params: Any) -> dict:
        if action in {"post", "reply", "quote"}:
            text = str(params.get("text") or "")
            variables: dict[str, Any] = {
                "tweet_text": text,
                "dark_request": False,
                "media": {"media_entities": [], "possibly_sensitive": False},
                "semantic_annotation_ids": [],
            }
            reply_to = _clean_id(params.get("reply_to"))
            if reply_to:
                variables["reply"] = {
                    "in_reply_to_tweet_id": reply_to,
                    "exclude_reply_user_ids": [],
                }
            quote_id = _clean_id(params.get("quote"))
            if quote_id:
                variables["attachment_url"] = f"https://x.com/i/status/{quote_id}"
            data = await self._graphql("CreateTweet", variables, method="POST")
            result = (
                ((data.get("data") or {}).get("create_tweet") or {}).get("tweet_results")
                or {}
            ).get("result") or {}
            tweet = normalize_tweet(result, source=self.name)
            if tweet is None or not tweet.id:
                raise XError("X accepted the call but returned no post id")
            if not tweet.author:
                tweet.author = str(self.cfg.get("handle") or "").lstrip("@")
            return {"id": tweet.id, "url": tweet.url, "text": tweet.text or text}
        tid = _clean_id(params.get("tweet_id"))
        if not tid:
            raise XError("tweet_id is required")
        if action == "delete":
            await self._graphql(
                "DeleteTweet", {"tweet_id": tid, "dark_request": False}, method="POST"
            )
            return {"id": tid, "url": "", "text": ""}
        if action == "like":
            await self._graphql("FavoriteTweet", {"tweet_id": tid}, method="POST")
            return {"id": tid, "url": f"https://x.com/i/status/{tid}", "text": ""}
        if action == "repost":
            await self._graphql(
                "CreateRetweet", {"tweet_id": tid, "dark_request": False}, method="POST"
            )
            return {"id": tid, "url": f"https://x.com/i/status/{tid}", "text": ""}
        raise XError(f"cookies backend cannot {action}")


class ApiBackend(_Backend):
    """Whatever gateway the operator already runs.

    Free X APIs are a moving target — a self-hosted scraper, a friend's
    proxy, a free tier somewhere. Rather than pick one and be wrong, this
    talks to a base URL with conventional paths, every one of them
    overridable via ``X_API_PATHS``, and normalizes whatever JSON comes
    back by finding the post-shaped objects in it. If your gateway answers
    ``GET /search?query=…`` with a list of objects that have an id and some
    text, it already works.
    """

    name = "api"
    can_write = True
    reads = frozenset({"home", "user", "search", "mentions", "tweet"})

    DEFAULT_PATHS: ClassVar[dict[str, str]] = {
        "home": "/timeline?limit={limit}",
        "user": "/tweets?username={handle}&limit={limit}",
        "search": "/search?query={query}&limit={limit}",
        "mentions": "/mentions?limit={limit}",
        "tweet": "/tweet?id={tweet_id}",
        "post": "/tweet",
        "delete": "/tweet/{tweet_id}",
        "like": "/like",
        "repost": "/repost",
    }

    def configured(self) -> bool:
        return bool(self.cfg.get("api_base_url"))

    def _paths(self) -> dict[str, str]:
        paths = dict(self.DEFAULT_PATHS)
        override = self.cfg.get("api_paths")
        if isinstance(override, dict):
            paths.update({str(k): str(v) for k, v in override.items() if v})
        return paths

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "user-agent": "maxwell-x/1.0"}
        key = str(self.cfg.get("api_key") or "")
        if key:
            header = str(self.cfg.get("api_key_header") or "Authorization")
            headers[header.lower()] = (
                f"Bearer {key}" if header.lower() == "authorization" else key
            )
        return headers

    def _url(self, template: str, **fields: Any) -> str:
        base = str(self.cfg.get("api_base_url") or "").rstrip("/")
        filled = template
        for key, value in fields.items():
            filled = filled.replace(
                "{" + key + "}", quote(str(value if value is not None else ""), safe="")
            )
        return base + filled

    async def read(self, action: str, **params: Any) -> list[Tweet]:
        template = self._paths().get(action)
        if not template:
            raise XError(f"no api path configured for {action}")
        limit = int(params.get("limit") or 20)
        url = self._url(
            template,
            limit=min(limit, MAX_LIMIT),
            handle=str(params.get("handle") or "").lstrip("@"),
            query=str(params.get("query") or ""),
            tweet_id=_clean_id(params.get("tweet_id")),
        )
        status, body = await self.client.request("GET", url, headers=self._headers())
        if status == 429:
            self.client.note_rate_limit(self.name)
            raise XError("gateway rate limited (429)")
        if status >= 400:
            raise XError(f"gateway {action} failed (HTTP {status})")
        data = self.client.decode_json(body)
        tweets = _collect_generic(data, source=self.name, limit=limit)
        if not tweets:
            raise XError(f"gateway returned nothing usable for {action}")
        return tweets

    async def write(self, action: str, **params: Any) -> dict:
        paths = self._paths()
        if action in {"post", "reply", "quote"}:
            payload = {"text": str(params.get("text") or "")}
            if _clean_id(params.get("reply_to")):
                payload["reply_to"] = _clean_id(params.get("reply_to"))
            if _clean_id(params.get("quote")):
                payload["quote"] = _clean_id(params.get("quote"))
            url = self._url(paths["post"])
            method = "POST"
        elif action == "delete":
            url = self._url(paths["delete"], tweet_id=_clean_id(params.get("tweet_id")))
            payload = {}
            method = "DELETE"
        elif action in {"like", "repost"}:
            url = self._url(paths[action])
            payload = {"id": _clean_id(params.get("tweet_id"))}
            method = "POST"
        else:
            raise XError(f"api backend cannot {action}")
        status, body = await self.client.request(
            method, url, headers=self._headers(), json_body=payload or None
        )
        if status >= 400:
            raise XError(f"gateway {action} failed (HTTP {status}): {body[:200]}")
        data = self.client.decode_json(body)
        tweet = normalize_tweet(data if isinstance(data, dict) else {}, source=self.name)
        if tweet is None or not tweet.id:
            found = _collect_generic(data, source=self.name, limit=1)
            tweet = found[0] if found else None
        if tweet is None:
            return {"id": "", "url": "", "text": str(params.get("text") or "")}
        if not tweet.author:
            tweet.author = str(self.cfg.get("handle") or "").lstrip("@")
        return {"id": tweet.id, "url": tweet.url, "text": tweet.text}


class RssBackend(_Backend):
    """Nitter, RSSHub, or any feed that carries posts.

    Read-only and credential-free. The instance is the operator's choice
    because public Nitter instances come and go; ``X_RSS_BASE_URL`` is
    whichever one is alive this month, and the two path templates are
    overridable for feeds that shape their URLs differently.
    """

    name = "rss"
    reads = frozenset({"user", "search"})

    def configured(self) -> bool:
        return bool(self.cfg.get("rss_base_url"))

    def _url(self, action: str, **fields: Any) -> str:
        base = str(self.cfg.get("rss_base_url") or "").rstrip("/")
        template = str(
            (self.cfg.get("rss_paths") or {}).get(action)
            or ("/{handle}/rss" if action == "user" else "/search/rss?f=tweets&q={query}")
        )
        for key, value in fields.items():
            template = template.replace("{" + key + "}", quote(str(value or ""), safe=""))
        return base + template

    async def read(self, action: str, **params: Any) -> list[Tweet]:
        if action not in self.reads:
            raise XError(f"rss backend cannot read {action}")
        limit = int(params.get("limit") or 20)
        url = self._url(
            action,
            handle=str(params.get("handle") or "").lstrip("@"),
            query=str(params.get("query") or ""),
        )
        status, body = await self.client.request(
            "GET", url, headers={"user-agent": WEB_UA, "accept": "application/rss+xml"}
        )
        if status >= 400:
            raise XError(f"feed returned HTTP {status} ({url})")
        tweets = parse_rss(body, source=self.name, limit=limit)
        if not tweets:
            raise XError("feed had no items")
        return tweets


class SyndicationBackend(_Backend):
    """X's own embed backend — the one that powers quoted tweets on blogs.

    No account, no key, no configuration: this is what makes reading work on
    a fresh install. It only serves public profiles and single posts, which
    is exactly the half of reading that does not need to know who is asking.
    """

    name = "syndication"
    reads = frozenset({"user", "tweet"})

    def configured(self) -> bool:
        return bool(self.cfg.get("syndication_enabled", True))

    async def read(self, action: str, **params: Any) -> list[Tweet]:
        limit = int(params.get("limit") or 20)
        if action == "tweet":
            tid = _clean_id(params.get("tweet_id"))
            if not tid:
                raise XError("tweet_id is required")
            url = (
                "https://cdn.syndication.twimg.com/tweet-result"
                f"?id={tid}&token={syndication_token(tid)}&lang=en"
            )
            status, body = await self.client.request(
                "GET", url, headers={"user-agent": WEB_UA, "accept": "application/json"}
            )
            if status >= 400:
                raise XError(f"syndication returned HTTP {status} for {tid}")
            tweet = normalize_tweet(self.client.decode_json(body), source=self.name)
            if tweet is None or not tweet.id:
                raise XError(f"no public post {tid} (deleted, private, or age-gated)")
            return [tweet]
        if action == "user":
            handle = str(params.get("handle") or "").lstrip("@")
            if not handle:
                raise XError("handle is required for a user timeline")
            url = (
                "https://syndication.twitter.com/srv/timeline-profile/screen-name/"
                f"{quote(handle, safe='')}"
            )
            status, body = await self.client.request(
                "GET", url, headers={"user-agent": WEB_UA, "accept": "text/html"}
            )
            if status >= 400:
                raise XError(f"syndication returned HTTP {status} for @{handle}")
            data = _next_data(body)
            if data is None:
                raise XError(f"@{handle} has no public embeddable timeline")
            tweets = collect_tweets(data, source=self.name, limit=limit)
            if not tweets:
                tweets = _collect_generic(data, source=self.name, limit=limit)
            if not tweets:
                raise XError(f"@{handle}: nothing public to read")
            return tweets
        raise XError(f"syndication backend cannot read {action}")


# ---------------------------------------------------------------------------
# Parsing helpers the backends share
# ---------------------------------------------------------------------------


def _clean_id(raw: Any) -> str:
    """Digits out of anything: an id, a URL, "id=123", an @-prefixed mess."""
    text = str(raw or "").strip()
    if not text:
        return ""
    match = re.search(r"status(?:es)?/(\d+)", text)
    if match:
        return match.group(1)
    digits = re.sub(r"[^0-9]", "", text)
    return digits[:25]


def _first_error(data: Any, body: str) -> str:
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                return str(first["message"])[:300]
        for key in ("error", "detail", "message"):
            if data.get(key):
                return str(data[key])[:300]
    return str(body or "")[:300]


def _missing_features(data: Any, body: str) -> list[str]:
    """Feature flags X says the call should have sent.

    The error text is "The following features cannot be null: foo, bar",
    which is machine-readable enough to fix ourselves.
    """
    text = _first_error(data, body)
    match = re.search(r"features cannot be null:?\s*([A-Za-z0-9_,\s]+)", text)
    if not match:
        return []
    return [f.strip() for f in match.group(1).split(",") if f.strip()]


def _collect_generic(node: Any, *, source: str, limit: int) -> list[Tweet]:
    """Find post-shaped dicts anywhere in an unknown JSON body.

    For gateways whose response shape nobody documented. A dict counts if it
    has some text and either an id or an author; the walk stops descending
    once it accepts one, so a nested quote does not become its own row.
    """
    out: list[Tweet] = []
    seen: set[str] = set()

    def looks_like_post(item: dict) -> bool:
        has_text = any(
            isinstance(item.get(k), str) and item.get(k).strip()
            for k in ("text", "full_text", "content")
        )
        has_id = any(item.get(k) for k in ("id", "id_str", "rest_id", "tweet_id"))
        has_author = any(
            item.get(k) for k in ("user", "author", "username", "screen_name")
        )
        return has_text and (has_id or has_author)

    def walk(item: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if not isinstance(item, dict):
            return
        if looks_like_post(item):
            tweet = normalize_tweet(item, source=source)
            if tweet is not None and (tweet.text or tweet.id):
                key = tweet.id or tweet.text[:80]
                if key not in seen:
                    seen.add(key)
                    out.append(tweet)
                    return
        for value in item.values():
            walk(value)

    walk(node)
    return out[:limit]


def parse_rss(body: str, *, source: str = "rss", limit: int = 20) -> list[Tweet]:
    """RSS/Atom items → tweets. Handles Nitter's and RSSHub's shapes."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise XError(f"feed is not valid XML: {exc}") from exc
    atom = "{http://www.w3.org/2005/Atom}"
    dc = "{http://purl.org/dc/elements/1.1/}"
    items = root.findall(".//item") or root.findall(f".//{atom}entry")
    out: list[Tweet] = []
    def field(item: Any, *names: str) -> str:
        """First of ``names`` that carries text (or an href, for Atom links)."""
        for name in names:
            node = item.find(name)
            if node is not None:
                if node.text:
                    return node.text
                href = node.get("href")
                if href:
                    return href
        return ""

    for item in items[:limit]:
        def text_of(*names: str, _item=item) -> str:
            return field(_item, *names)

        link = text_of("link", f"{atom}link")
        raw_text = text_of("description", "title", f"{atom}summary", f"{atom}title")
        clean = html.unescape(re.sub(r"<[^>]+>", " ", raw_text))
        media = re.findall(r'<img[^>]+src="([^"]+)"', raw_text)
        creator = text_of(f"{dc}creator", "author", f"{atom}author")
        handle = ""
        # Any host: the same item shape arrives from x.com, a Nitter mirror
        # and an RSSHub route, and only the path is common to all three.
        match = re.search(r"//[^/]+/([^/?#]+)/status(?:es)?/\d+", link or "")
        if match:
            handle = match.group(1)
        elif creator:
            handle = creator.lstrip("@").strip()
        out.append(
            Tweet(
                id=_clean_id(link),
                text=" ".join(clean.split()),
                author=handle,
                created_at=_iso(text_of("pubDate", f"{atom}published", f"{atom}updated")),
                media=[m for m in media[:4] if not m.endswith(".svg")],
                is_repost=clean.startswith(("RT by", "RT @")),
                source=source,
            )
        )
    return out


def _next_data(body: str) -> Any:
    """The JSON blob a syndication page ships its timeline in."""
    match = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.DOTALL
    )
    if not match:
        return None
    with contextlib.suppress(ValueError):
        return json.loads(match.group(1))
    return None


def _to_radix_string(value: float, radix: int = 36) -> str:
    """JavaScript's ``Number.prototype.toString(radix)``, ported.

    The syndication endpoint validates a token derived from the post id in
    JS, so the digits have to match V8's output exactly — including how many
    fractional digits it decides to emit, which is "until the remaining
    error is below half a ULP". Anything simpler produces a token X rejects.
    """
    if value != value or value in (float("inf"), float("-inf")):
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    negative = value < 0
    value = abs(value)
    integer = math.floor(value)
    fraction = value - integer

    delta = max(0.5 * (math.nextafter(value, math.inf) - value), math.ulp(0.0))
    out_fraction = ""
    if fraction >= delta:
        chars: list[str] = []
        while True:
            fraction *= radix
            delta *= radix
            digit = int(fraction)
            chars.append(digits[digit])
            fraction -= digit
            if fraction > 0.5 or (fraction == 0.5 and digit & 1):
                if fraction + delta > 1:
                    # Round up, carrying through the digits we already emitted.
                    index = len(chars) - 1
                    while True:
                        if index < 0:
                            integer += 1
                            break
                        position = digits.index(chars[index])
                        if position + 1 < radix:
                            chars[index] = digits[position + 1]
                            break
                        chars.pop()
                        index -= 1
                    break
            if fraction < delta:
                break
        out_fraction = "".join(chars)

    out_integer = ""
    if integer == 0:
        out_integer = "0"
    while integer > 0:
        out_integer = digits[int(integer % radix)] + out_integer
        integer = math.floor(integer / radix)

    text = out_integer + ("." + out_fraction if out_fraction else "")
    return "-" + text if negative else text


def syndication_token(tweet_id: str) -> str:
    """The `token` query param cdn.syndication.twimg.com wants.

    Their web code is ``((id / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, "")``
    — an obfuscation, not a secret, but it is checked, so we reproduce it.
    """
    try:
        numeric = int(_clean_id(tweet_id) or "0")
    except ValueError:
        return "0"
    return re.sub(r"(0+|\.)", "", _to_radix_string((numeric / 1e15) * math.pi))


# ---------------------------------------------------------------------------
# Post budget
# ---------------------------------------------------------------------------


class PostBudget:
    """A hard ceiling on posts per hour, persisted across restarts.

    Autonomy runs on a timer with nobody watching, and the failure mode of a
    model with a public megaphone is not one bad post, it is forty. The
    budget is enforced here rather than in the prompt because a prompt is a
    suggestion and this is not. It survives a restart on purpose: crash-loop
    plus a fresh counter is how you send forty posts.
    """

    def __init__(self, data_dir: str | Path, *, per_hour: int = 8):
        self.path = Path(data_dir) / "x_post_log.json"
        self.per_hour = max(0, int(per_hour))
        self._stamps: list[float] = []
        self._loaded = False
        self._lock = asyncio.Lock()

    async def _load(self) -> None:
        if self._loaded:
            return
        data = await asyncio.to_thread(_load_json_safe, self.path, dict)
        stamps = data.get("posts") if isinstance(data, dict) else []
        self._stamps = [
            float(s) for s in (stamps or []) if isinstance(s, (int, float))
        ]
        self._loaded = True

    def _recent(self, now: float) -> list[float]:
        return [s for s in self._stamps if now - s < 3600]

    def _blocked(self, now: float) -> str:
        if self.per_hour <= 0:
            return "posting to X is disabled (x_posts_per_hour is 0)"
        recent = self._recent(now)
        if len(recent) < self.per_hour:
            return ""
        wait = int((3600 - (now - min(recent))) // 60) + 1
        return (
            f"X post budget spent: {len(recent)}/{self.per_hour} in the last "
            f"hour. Try again in ~{wait}m."
        )

    async def check(self, *, now: float | None = None) -> str:
        """Empty string when there is room, else the reason there is not.

        Read-only — for `,x status`. A post takes its slot with `reserve`,
        which is the same question asked while holding the lock.
        """
        await self._load()
        return self._blocked(now if now is not None else time.time())

    async def reserve(self, *, now: float | None = None) -> tuple[str, float]:
        """Take a slot before posting. Returns (problem, stamp).

        Check-then-record leaves a window where two posts started at once
        both see room and both go out — a small overshoot, but the whole
        point of this class is that the ceiling is exact. The slot is taken
        under the lock and handed back by `release` if the post then fails.
        """
        async with self._lock:
            await self._load()
            current = now if now is not None else time.time()
            problem = self._blocked(current)
            if problem:
                return problem, 0.0
            self._stamps = [*self._recent(current), current]
            await self._flush()
            return "", current

    async def release(self, stamp: float) -> None:
        """Hand a reserved slot back — the post never landed."""
        if not stamp:
            return
        async with self._lock:
            if stamp in self._stamps:
                self._stamps.remove(stamp)
                await self._flush()

    async def record(self, *, now: float | None = None) -> None:
        """Spend a slot outright. Kept for callers that already posted."""
        async with self._lock:
            await self._load()
            current = now if now is not None else time.time()
            self._stamps = [*self._recent(current), current]
            await self._flush()

    async def _flush(self) -> None:
        await asyncio.to_thread(
            _atomic_json_write_sync, self.path, {"posts": self._stamps}
        )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class XClient:
    """Backend selection, caching, and the two verbs the tools actually use.

    Reads walk the backend chain and take the first that answers, so a
    rotated GraphQL id or a dead Nitter instance is a slower read rather
    than a broken feature. Writes do not fall through: there is one account
    doing the posting and silently posting through a different path would be
    a surprise, so a failed write is reported as a failure.
    """

    def __init__(self, cfg: dict, *, data_dir: str | Path = "data"):
        self.cfg = dict(cfg or {})
        self.data_dir = Path(data_dir)
        self.timeout = max(5.0, float(self.cfg.get("timeout") or 20.0))
        self.cache_seconds = max(0.0, float(self.cfg.get("cache_seconds") or 60.0))
        self.max_chars = max(1, int(self.cfg.get("max_chars") or 280))
        self.post_enabled = bool(self.cfg.get("post_enabled", True))
        self.budget = PostBudget(
            self.data_dir, per_hour=int(self.cfg.get("posts_per_hour") or 8)
        )
        self.user_ids: dict[str, str] = {}
        self._cache: dict[str, tuple[float, list[Tweet]]] = {}
        self._cooldowns: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self.graphql_path = Path(
            self.cfg.get("graphql_file") or (self.data_dir / "x_graphql.json")
        )
        self.graphql_ids, self.graphql_features = self._load_graphql()
        self.backends: list[_Backend] = [
            CookieBackend(self),
            ApiBackend(self),
            RssBackend(self),
            SyndicationBackend(self),
        ]

    # -- configuration -----------------------------------------------------

    def _load_graphql(self) -> tuple[dict[str, str], dict[str, bool]]:
        ids = dict(DEFAULT_GRAPHQL_IDS)
        features = dict(DEFAULT_GRAPHQL_FEATURES)
        data = _load_json_safe(self.graphql_path, dict)
        if isinstance(data, dict):
            if isinstance(data.get("ids"), dict):
                ids.update({str(k): str(v) for k, v in data["ids"].items() if v})
            if isinstance(data.get("features"), dict):
                features.update(
                    {str(k): bool(v) for k, v in data["features"].items()}
                )
        return ids, features

    def _selected(self) -> list[_Backend]:
        """Backends to try, in order, honouring an explicit X_BACKEND."""
        choice = str(self.cfg.get("backend") or "auto").strip().lower()
        available = [b for b in self.backends if b.configured()]
        if choice in {"", "auto"}:
            return available
        wanted = {c.strip() for c in choice.split(",") if c.strip()}
        return [b for b in available if b.name in wanted]

    def selected_backends(self) -> list[_Backend]:
        """The backends this configuration will actually try, in order."""
        return self._selected()

    def configured(self) -> bool:
        """True when at least one backend can do something.

        Syndication needs nothing, so this is normally true — which is the
        point: reading X works out of the box and only posting asks for
        credentials.
        """
        return bool(self._selected())

    def can_write(self) -> bool:
        return self.post_enabled and any(b.can_write for b in self._selected())

    def status(self) -> str:
        ready = [b.name for b in self._selected()]
        writers = [b.name for b in self._selected() if b.can_write]
        handle = str(self.cfg.get("handle") or "").lstrip("@")
        parts = [f"backends: {', '.join(ready) or 'none'}"]
        if not self.post_enabled:
            parts.append("posting: off (x_post_enabled=false)")
        elif writers:
            parts.append(f"posting: {', '.join(writers)}")
        else:
            parts.append("posting: no write backend — set X_AUTH_TOKEN + X_CT0")
        if handle:
            parts.append(f"account: @{handle}")
        parts.append(f"budget: {self.budget.per_hour}/h")
        return " · ".join(parts)

    # -- http --------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                )
            return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json_body: Any = None,
    ) -> tuple[int, str]:
        """One HTTP round trip. Returns (status, body) and never raises for status."""
        if not str(url).lower().startswith(("http://", "https://")):
            raise XError(f"refusing to fetch a non-http URL: {url[:80]}")
        session = await self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                allow_redirects=True,
            ) as response:
                return response.status, await response.text()
        except asyncio.TimeoutError as exc:
            raise XError(f"{urlparse(url).netloc} timed out after {self.timeout:.0f}s") from exc
        except aiohttp.ClientError as exc:
            raise XError(f"{urlparse(url).netloc}: {exc}") from exc

    @staticmethod
    def decode_json(body: str) -> Any:
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            return {}

    def note_rate_limit(self, backend: str, *, seconds: float = 900.0) -> None:
        """Park a backend that just 429'd, so the next read skips it."""
        self._cooldowns[backend] = time.time() + seconds

    def _cooling(self, backend: str) -> bool:
        until = self._cooldowns.get(backend, 0.0)
        if until <= time.time():
            self._cooldowns.pop(backend, None)
            return False
        return True

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- reads -------------------------------------------------------------

    def _cache_key(self, action: str, params: dict) -> str:
        return json.dumps([action, params], sort_keys=True, default=str)

    async def read(
        self,
        action: str,
        *,
        handle: str | None = None,
        query: str | None = None,
        tweet_id: str | None = None,
        limit: int = 20,
        use_cache: bool = True,
    ) -> list[Tweet]:
        """Read a timeline, a search, or a single post.

        Cached for ``x_cache_seconds`` because the autonomy tick and a chat
        turn ask the same question minutes apart, and every uncached repeat
        spends the same rate-limit budget as a new one.
        """
        if action not in READ_ACTIONS:
            raise XError(f"unknown read action {action!r} (try: {', '.join(sorted(READ_ACTIONS))})")
        params = {
            "handle": str(handle or "").lstrip("@").strip(),
            "query": str(query or "").strip(),
            "tweet_id": _clean_id(tweet_id),
            "limit": max(1, min(int(limit or 20), MAX_LIMIT)),
        }
        key = self._cache_key(action, params)
        now = time.time()
        if use_cache and self.cache_seconds > 0:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

        candidates = [
            b for b in self._selected() if action in b.reads and not self._cooling(b.name)
        ]
        if not candidates:
            raise XError(self._no_backend_message(action))
        problems: list[str] = []
        for backend in candidates:
            try:
                tweets = await backend.read(action, **params)
            except XError as exc:
                problems.append(f"{backend.name}: {exc}")
                continue
            except Exception as exc:  # pragma: no cover - defensive
                problems.append(f"{backend.name}: {type(exc).__name__}: {exc}")
                continue
            if tweets:
                if self.cache_seconds > 0:
                    self._cache[key] = (now + self.cache_seconds, tweets)
                    self._trim_cache()
                return tweets
            problems.append(f"{backend.name}: no results")
        raise XError("; ".join(problems) or "no backend could answer")

    def _no_backend_message(self, action: str) -> str:
        if action in {"home", "mentions"}:
            return (
                f"reading {action} needs a logged-in session — set X_AUTH_TOKEN "
                "and X_CT0 in .env (or point X_API_BASE_URL at a gateway). "
                "Public reads (user, search, tweet) work without either."
            )
        if all(self._cooling(b.name) for b in self._selected()):
            return "every X backend is in rate-limit cooldown; try again later"
        return f"no configured backend can read {action}"

    def _trim_cache(self, *, keep: int = 64) -> None:
        if len(self._cache) <= keep:
            return
        for key in sorted(self._cache, key=lambda k: self._cache[k][0])[: len(self._cache) - keep]:
            self._cache.pop(key, None)

    # -- writes ------------------------------------------------------------

    def check_text(self, text: str) -> str:
        """Empty string if this text can be posted, else why not."""
        body = str(text or "").strip()
        if not body:
            return "nothing to post: text is empty"
        if len(body) > self.max_chars:
            return (
                f"too long: {len(body)} chars, limit is {self.max_chars}. "
                "Shorten it or split it into a thread."
            )
        return ""

    async def post(
        self,
        text: str,
        *,
        reply_to: str | None = None,
        quote: str | None = None,
    ) -> dict:
        if not self.post_enabled:
            raise XError("posting to X is turned off (x_post_enabled=false)")
        problem = self.check_text(text)
        if problem:
            raise XError(problem)
        blocked, slot = await self.budget.reserve()
        if blocked:
            raise XError(blocked)
        action = "reply" if reply_to else ("quote" if quote else "post")
        try:
            result = await self._write(
                action, text=str(text).strip(), reply_to=reply_to, quote=quote
            )
        except Exception:
            # A rejected post is not a spent post: an expired cookie must not
            # eat the hour's budget on top of failing.
            await self.budget.release(slot)
            raise
        return result

    async def act(self, action: str, tweet_id: str) -> dict:
        if action not in {"delete", "like", "repost"}:
            raise XError(f"unknown action {action!r}")
        if not self.post_enabled:
            raise XError("X writes are turned off (x_post_enabled=false)")
        tid = _clean_id(tweet_id)
        if not tid:
            raise XError("tweet_id is required")
        return await self._write(action, tweet_id=tid)

    async def _write(self, action: str, **params: Any) -> dict:
        writers = [b for b in self._selected() if b.can_write]
        if not writers:
            raise XError(
                "no way to write to X: set X_AUTH_TOKEN + X_CT0 (cookies from "
                "a logged-in x.com tab) or X_API_BASE_URL for your own gateway"
            )
        last: Exception | None = None
        for backend in writers:
            try:
                return await backend.write(action, **params)
            except XError as exc:
                last = exc
                continue
        raise XError(str(last or f"{action} failed"))


# ---------------------------------------------------------------------------
# Mentions → inbox notices
# ---------------------------------------------------------------------------


def mention_item_id(tweet_id: str) -> str:
    return f"x_{_clean_id(tweet_id)}"


class XMentionPoller:
    """Files new @-mentions as inbox notices.

    Same three rules as the mail poller, for the same reasons: one notice per
    post ever (``insert_if_absent`` plus a persisted high-water id, so a
    dismissed mention stays dismissed), a bounded number per tick so a
    backlog drains instead of evicting the rest of the inbox ring, and his
    own posts are never news. Failure backs off exponentially — a rotated
    cookie should cost one log line every few minutes, not one per tick.
    """

    MAX_NEW_PER_POLL = 6

    def __init__(
        self,
        store: Any,
        client: XClient,
        *,
        data_dir: str | Path,
        interval: float = 300.0,
        max_backoff: float = 3600.0,
    ):
        self.store = store
        self.client = client
        self.path = Path(data_dir) / "x_poll_state.json"
        self.interval = max(60.0, float(interval))
        self.max_backoff = max(self.interval, float(max_backoff))
        self.last_id = 0
        self._loaded = False
        self._failures = 0

    def configured(self) -> bool:
        """Only when a backend can actually see mentions of a known account."""
        if not str(self.client.cfg.get("handle") or "").strip():
            return False
        return any(
            "mentions" in b.reads for b in self.client.selected_backends()
        )

    def backoff_seconds(self) -> float:
        if self._failures <= 0:
            return self.interval
        return min(self.interval * (2 ** min(self._failures, 6)), self.max_backoff)

    async def _load(self) -> None:
        if self._loaded:
            return
        data = await asyncio.to_thread(_load_json_safe, self.path, dict)
        if isinstance(data, dict):
            with contextlib.suppress(TypeError, ValueError):
                self.last_id = int(data.get("last_id") or 0)
        self._loaded = True

    async def _save(self, last_id: int) -> None:
        if last_id <= self.last_id:
            return
        self.last_id = int(last_id)
        self._loaded = True
        await asyncio.to_thread(
            _atomic_json_write_sync, self.path, {"last_id": self.last_id}
        )

    def _build_item(self, tweet: Tweet) -> dict:
        who = f"@{tweet.author}" if tweet.author else "someone"
        text = " ".join(tweet.text.split())[:220]
        return {
            "id": mention_item_id(tweet.id),
            "kind": "x_mention",
            "state": "unread",
            "actor_id": tweet.author,
            "actor_name": tweet.author_name or tweet.author,
            "summary": f"{who} mentioned you on X: {text}",
            "actions": ["read", "dismiss"],
            "payload": {
                "tweet_id": tweet.id,
                "author": tweet.author,
                "text": text,
                "url": tweet.url,
                "created_at": tweet.created_at,
                "hint": f"x_post action=reply reply_to={tweet.id} to answer it",
            },
        }

    async def poll_once(self) -> int:
        if not self.configured():
            return 0
        await self._load()
        try:
            tweets = await self.client.read(
                "mentions", limit=self.MAX_NEW_PER_POLL * 2, use_cache=False
            )
        except XError as exc:
            self._failures += 1
            logger.warning(
                "X mention poll failed (%s consecutive, next try in %.0fs): %s",
                self._failures,
                self.backoff_seconds(),
                exc,
            )
            return 0
        self._failures = 0
        own = str(self.client.cfg.get("handle") or "").lstrip("@").lower()
        filed = 0
        highest = self.last_id
        for tweet in sorted(tweets, key=lambda t: _as_int(t.id)):
            tid = _as_int(tweet.id)
            if not tid or tid <= self.last_id:
                continue
            if filed >= self.MAX_NEW_PER_POLL:
                # Stop before touching the mark: a backlog drains over the
                # next few ticks instead of being skipped outright.
                break
            if own and tweet.author.lower() == own:
                # His own post, matched by the search. Move past it and file
                # nothing — the mail poller learned this lesson already.
                highest = max(highest, tid)
                continue
            row = await self.store.insert_if_absent(self._build_item(tweet))
            if row is not None:
                filed += 1
            highest = max(highest, tid)
        if highest > self.last_id:
            await self._save(highest)
        if filed:
            logger.info("X mention poll: %s new mention(s) in the inbox", filed)
        return filed

    async def run(self) -> None:
        """Loop until cancelled. Never raises out of a tick."""
        if not self.configured():
            logger.info("X mention poll disabled: no readable mentions source")
            return
        await asyncio.sleep(min(45.0, self.interval))
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self._failures += 1
                logger.error("X mention poll loop error: %s", exc)
            await asyncio.sleep(self.backoff_seconds())


def _as_int(raw: Any) -> int:
    try:
        return int(str(raw or "0"))
    except (TypeError, ValueError):
        return 0
