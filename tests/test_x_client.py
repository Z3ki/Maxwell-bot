"""X (Twitter): normalization, backend fallback, the post budget, mentions.

The parts worth pinning are the ones that fail quietly. A shape change turns
posts into empty strings, a broken fallback chain turns a rotated cookie into
"X is down", and a reset post budget turns a crash loop into forty tweets.
"""

import asyncio
import json

import pytest

import x_client
from inbox import InboxStore
from x_client import (
    PostBudget,
    Tweet,
    XClient,
    XError,
    XMentionPoller,
    _clean_id,
    _missing_features,
    collect_tweets,
    mention_item_id,
    normalize_tweet,
    parse_rss,
    relative_age,
    render_tweets,
    syndication_token,
)


# ── normalization ─────────────────────────────────────────────────────────


def _graphql_tweet(tid="1", text="hello", handle="z3ki", quoted=None):
    node = {
        "__typename": "Tweet",
        "rest_id": tid,
        "core": {
            "user_results": {
                "result": {"legacy": {"screen_name": handle, "name": "Z3ki"}}
            }
        },
        "views": {"count": "4200"},
        "legacy": {
            "full_text": text,
            "created_at": "Wed Aug 20 12:00:00 +0000 2026",
            "favorite_count": 12,
            "retweet_count": 3,
            "reply_count": 1,
        },
    }
    if quoted:
        node["quoted_status_result"] = {"result": quoted}
    return node


def test_normalize_graphql_shape():
    tweet = normalize_tweet(_graphql_tweet())
    assert tweet.id == "1"
    assert tweet.text == "hello"
    assert tweet.author == "z3ki"
    assert (tweet.likes, tweet.reposts, tweet.replies, tweet.views) == (12, 3, 1, 4200)
    assert tweet.created_at.startswith("2026-08-20T12:00:00")
    assert tweet.url == "https://x.com/z3ki/status/1"


def test_normalize_syndication_shape():
    tweet = normalize_tweet(
        {
            "id_str": "77",
            "text": "from the embed API &amp; nothing else",
            "created_at": "2026-08-20T12:00:00.000Z",
            "favorite_count": 5,
            "conversation_count": 2,
            "user": {"screen_name": "nasa", "name": "NASA"},
            "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/media/a.jpg"}],
            "entities": {"media": [{"url": "https://t.co/abc"}]},
        }
    )
    assert tweet.author == "nasa"
    assert tweet.likes == 5 and tweet.replies == 2
    # The t.co stub is a link to the post's own page, not to a picture.
    assert tweet.media == ["https://pbs.twimg.com/media/a.jpg"]
    assert "&" in tweet.text and "&amp;" not in tweet.text


def test_normalize_generic_gateway_shape():
    tweet = normalize_tweet({"id": 99, "text": "hi", "username": "@someone"})
    assert (tweet.id, tweet.author, tweet.text) == ("99", "someone", "hi")


def test_normalize_rejects_non_posts():
    assert normalize_tweet({"cursor": "abc"}) is None
    assert normalize_tweet("not a dict") is None


def test_long_form_body_beats_truncated_legacy_text():
    node = _graphql_tweet(text="the first 280 chars…")
    node["note_tweet"] = {
        "note_tweet_results": {"result": {"text": "the whole essay, untruncated"}}
    }
    assert normalize_tweet(node).text == "the whole essay, untruncated"


def test_quoted_post_rides_on_its_parent_not_beside_it():
    payload = {
        "data": {
            "entries": [
                {"content": {"tweet": _graphql_tweet("2", "look at this", quoted=_graphql_tweet("1", "original", handle="nasa"))}}
            ]
        }
    }
    tweets = collect_tweets(payload)
    assert [t.id for t in tweets] == ["2"]
    assert "nasa" in tweets[0].quoted and "original" in tweets[0].quoted


def test_collect_walks_a_timeline_and_dedupes():
    payload = {
        "instructions": [
            {"entries": [{"item": _graphql_tweet("3")}, {"item": _graphql_tweet("4")}]},
            # The same post can appear twice in one timeline payload.
            {"entries": [{"item": _graphql_tweet("3")}]},
        ]
    }
    assert [t.id for t in collect_tweets(payload)] == ["3", "4"]


def test_collect_respects_the_limit():
    payload = [{"item": _graphql_tweet(str(i))} for i in range(30)]
    assert len(collect_tweets(payload, limit=5)) == 5


# ── rendering ─────────────────────────────────────────────────────────────


def test_render_omits_counts_nobody_supplied():
    """An unauthenticated read has no like count. Printing 0 would be a lie."""
    text = render_tweets([Tweet(id="1", text="hi", author="z3ki")])
    assert "likes" not in text
    assert "id=1" in text and "https://x.com/z3ki/status/1" in text


def test_render_includes_stats_when_present():
    text = render_tweets([Tweet(id="1", text="hi", author="z", likes=0, reposts=4)])
    assert "0 likes" in text and "4 reposts" in text


def test_render_empty_is_explicit():
    assert "nothing found" in render_tweets([])


def test_relative_age_reads_as_english():
    assert relative_age("") == ""
    assert relative_age("2026-08-20T12:00:00+00:00").endswith(("s", "m", "h", "d"))


# ── ids, tokens, feeds ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://x.com/nasa/status/1349129669258448897", "1349129669258448897"),
        ("https://twitter.com/i/statuses/42", "42"),
        ("  1234  ", "1234"),
        ("id=99", "99"),
        ("", ""),
    ],
)
def test_clean_id(raw, expected):
    assert _clean_id(raw) == expected


def test_syndication_token_matches_the_javascript():
    """Values taken from Node: ((id/1e15)*Math.PI).toString(36) minus 0s and dots.

    X validates this, so a token that differs by one digit is a 404 on every
    single-post read.
    """
    assert syndication_token("1234567890123456789") == "2zqic77uqyk"
    assert syndication_token("1349129669258448897") == syndication_token(
        "https://x.com/elonmusk/status/1349129669258448897"
    )


def test_parse_rss_reads_a_nitter_feed():
    body = """<?xml version="1.0"?><rss><channel>
      <item>
        <title>hello world</title>
        <description>hello &lt;b&gt;world&lt;/b&gt; &lt;img src="https://pic/1.jpg"/&gt;</description>
        <link>https://nitter.net/z3ki/status/123#m</link>
        <pubDate>Wed, 20 Aug 2026 12:00:00 GMT</pubDate>
      </item></channel></rss>"""
    tweets = parse_rss(body)
    assert len(tweets) == 1
    assert tweets[0].id == "123"
    assert tweets[0].author == "z3ki"
    assert "hello world" in tweets[0].text
    assert tweets[0].media == ["https://pic/1.jpg"]


def test_parse_rss_rejects_garbage():
    with pytest.raises(XError):
        parse_rss("<html>not a feed")


def test_missing_features_are_parsed_out_of_the_error():
    body = json.dumps(
        {"errors": [{"message": "The following features cannot be null: foo_enabled, bar_enabled"}]}
    )
    assert _missing_features(json.loads(body), body) == ["foo_enabled", "bar_enabled"]


# ── the post budget ───────────────────────────────────────────────────────


def test_budget_blocks_past_the_hourly_ceiling(tmp_path):
    async def run():
        budget = PostBudget(tmp_path, per_hour=2)
        assert await budget.check() == ""
        await budget.record(now=1000.0)
        await budget.record(now=1001.0)
        assert "budget spent" in await budget.check(now=1002.0)
        # An hour later there is room again.
        assert await budget.check(now=1002.0 + 3600) == ""

    asyncio.run(run())


def test_budget_survives_a_restart(tmp_path):
    """A crash loop with a fresh counter is how you send forty posts."""

    async def run():
        first = PostBudget(tmp_path, per_hour=1)
        await first.record(now=5000.0)
        second = PostBudget(tmp_path, per_hour=1)
        assert "budget spent" in await second.check(now=5001.0)

    asyncio.run(run())


def test_zero_budget_means_never(tmp_path):
    async def run():
        assert "disabled" in await PostBudget(tmp_path, per_hour=0).check()

    asyncio.run(run())


# ── backend selection and fallback ────────────────────────────────────────


class _FakeBackend(x_client._Backend):
    def __init__(self, client, name, *, reads, can_write=False, answer=None, error=None):
        super().__init__(client)
        self.name = name
        self.reads = frozenset(reads)
        self.can_write = can_write
        self._answer = answer
        self._error = error
        self.calls = 0

    def configured(self):
        return True

    async def read(self, action, **params):
        self.calls += 1
        if self._error:
            raise self._error
        return list(self._answer or [])

    async def write(self, action, **params):
        self.calls += 1
        if self._error:
            raise self._error
        return {"id": "1", "url": "https://x.com/z/status/1", "text": params.get("text", "")}


def _client(tmp_path, backends, **cfg):
    client = XClient({"cache_seconds": 0, **cfg}, data_dir=tmp_path)
    client.backends = [b(client) for b in backends]
    return client


def test_read_falls_through_to_the_next_backend(tmp_path):
    async def run():
        dead = None
        client = XClient({"cache_seconds": 0}, data_dir=tmp_path)
        dead = _FakeBackend(client, "cookies", reads={"user"}, error=XError("stale id"))
        alive = _FakeBackend(
            client, "syndication", reads={"user"}, answer=[Tweet(id="1", text="hi")]
        )
        client.backends = [dead, alive]
        tweets = await client.read("user", handle="nasa")
        assert [t.id for t in tweets] == ["1"]
        assert dead.calls == 1 and alive.calls == 1

    asyncio.run(run())


def test_read_reports_every_backend_that_failed(tmp_path):
    async def run():
        client = XClient({"cache_seconds": 0}, data_dir=tmp_path)
        client.backends = [
            _FakeBackend(client, "cookies", reads={"user"}, error=XError("stale id")),
            _FakeBackend(client, "rss", reads={"user"}, error=XError("instance down")),
        ]
        with pytest.raises(XError) as excinfo:
            await client.read("user", handle="nasa")
        assert "stale id" in str(excinfo.value) and "instance down" in str(excinfo.value)

    asyncio.run(run())


def test_home_and_mentions_say_what_is_missing(tmp_path):
    async def run():
        client = XClient({}, data_dir=tmp_path)
        client.backends = [
            _FakeBackend(client, "syndication", reads={"user", "tweet"})
        ]
        with pytest.raises(XError) as excinfo:
            await client.read("home")
        assert "X_AUTH_TOKEN" in str(excinfo.value)

    asyncio.run(run())


def test_identical_reads_are_cached(tmp_path):
    async def run():
        client = XClient({"cache_seconds": 300}, data_dir=tmp_path)
        backend = _FakeBackend(
            client, "rss", reads={"search"}, answer=[Tweet(id="1", text="hi")]
        )
        client.backends = [backend]
        await client.read("search", query="ai")
        await client.read("search", query="ai")
        assert backend.calls == 1
        await client.read("search", query="something else")
        assert backend.calls == 2

    asyncio.run(run())


def test_a_rate_limited_backend_is_skipped_until_it_cools_off(tmp_path):
    async def run():
        client = XClient({"cache_seconds": 0}, data_dir=tmp_path)
        limited = _FakeBackend(client, "cookies", reads={"user"}, error=XError("429"))
        fallback = _FakeBackend(
            client, "rss", reads={"user"}, answer=[Tweet(id="2", text="hi")]
        )
        client.backends = [limited, fallback]
        client.note_rate_limit("cookies")
        await client.read("user", handle="nasa")
        assert limited.calls == 0

    asyncio.run(run())


def test_backend_can_be_pinned(tmp_path):
    client = XClient({"backend": "rss"}, data_dir=tmp_path)
    client.backends = [
        _FakeBackend(client, "cookies", reads={"user"}),
        _FakeBackend(client, "rss", reads={"user"}),
    ]
    assert [b.name for b in client.selected_backends()] == ["rss"]


def test_unknown_read_action_is_refused(tmp_path):
    async def run():
        with pytest.raises(XError):
            await XClient({}, data_dir=tmp_path).read("dms")

    asyncio.run(run())


# ── writing ───────────────────────────────────────────────────────────────


def test_post_refuses_when_writing_is_off(tmp_path):
    async def run():
        client = XClient({"post_enabled": False}, data_dir=tmp_path)
        with pytest.raises(XError) as excinfo:
            await client.post("hello")
        assert "turned off" in str(excinfo.value)

    asyncio.run(run())


def test_post_refuses_an_over_long_post(tmp_path):
    async def run():
        client = XClient({"max_chars": 10}, data_dir=tmp_path)
        client.backends = [_FakeBackend(client, "cookies", reads=set(), can_write=True)]
        with pytest.raises(XError) as excinfo:
            await client.post("x" * 40)
        assert "too long" in str(excinfo.value)

    asyncio.run(run())


def test_post_without_a_write_backend_says_what_to_set(tmp_path):
    async def run():
        client = XClient({}, data_dir=tmp_path)
        client.backends = [_FakeBackend(client, "syndication", reads={"user"})]
        with pytest.raises(XError) as excinfo:
            await client.post("hello")
        assert "X_AUTH_TOKEN" in str(excinfo.value)

    asyncio.run(run())


def test_a_successful_post_spends_budget_and_a_refused_one_does_not(tmp_path):
    async def run():
        client = XClient({"posts_per_hour": 5}, data_dir=tmp_path)
        client.backends = [_FakeBackend(client, "cookies", reads=set(), can_write=True)]
        await client.post("hello")
        assert len(client.budget._stamps) == 1
        with pytest.raises(XError):
            await client.post("")  # empty: rejected before it reaches a backend
        assert len(client.budget._stamps) == 1

    asyncio.run(run())


def test_a_failed_post_hands_its_budget_slot_back(tmp_path):
    """An expired cookie must not eat the hour's budget on top of failing."""

    async def run():
        client = XClient({"posts_per_hour": 1}, data_dir=tmp_path)
        client.backends = [
            _FakeBackend(
                client, "cookies", reads=set(), can_write=True, error=XError("boom")
            )
        ]
        with pytest.raises(XError):
            await client.post("hello")
        assert client.budget._stamps == []
        assert await client.budget.check() == ""

    asyncio.run(run())


def test_two_posts_at_once_cannot_both_take_the_last_slot(tmp_path):
    async def run():
        client = XClient({"posts_per_hour": 1}, data_dir=tmp_path)
        client.backends = [_FakeBackend(client, "cookies", reads=set(), can_write=True)]
        results = await asyncio.gather(
            client.post("one"), client.post("two"), return_exceptions=True
        )
        failures = [r for r in results if isinstance(r, XError)]
        assert len(failures) == 1 and "budget spent" in str(failures[0])

    asyncio.run(run())


def test_writes_do_not_fall_through_silently(tmp_path):
    """Two write backends must not mean a post lands from a surprise account."""

    async def run():
        client = XClient({}, data_dir=tmp_path)
        broken = _FakeBackend(
            client, "cookies", reads=set(), can_write=True, error=XError("session expired")
        )
        client.backends = [broken]
        with pytest.raises(XError) as excinfo:
            await client.post("hello")
        assert "session expired" in str(excinfo.value)

    asyncio.run(run())


# ── mentions → inbox ──────────────────────────────────────────────────────


def _poller(tmp_path, tweets, *, handle="maxwell"):
    client = XClient({"handle": handle, "cache_seconds": 0}, data_dir=tmp_path)
    client.backends = [
        _FakeBackend(client, "cookies", reads={"mentions"}, answer=tweets)
    ]
    store = InboxStore(str(tmp_path))
    return store, XMentionPoller(store, client, data_dir=tmp_path, interval=60)


def test_a_mention_becomes_one_inbox_notice(tmp_path):
    async def run():
        store, poller = _poller(
            tmp_path, [Tweet(id="10", text="yo max", author="z3ki")]
        )
        assert await poller.poll_once() == 1
        # The same mention on the next tick is not new mail.
        assert await poller.poll_once() == 0
        items = await store.load_items()
        assert [i["id"] for i in items] == [mention_item_id("10")]
        assert items[0]["kind"] == "x_mention"
        assert "read" in items[0]["actions"]

    asyncio.run(run())


def test_his_own_posts_are_not_mentions(tmp_path):
    async def run():
        store, poller = _poller(
            tmp_path,
            [Tweet(id="11", text="@maxwell talking to himself", author="Maxwell")],
        )
        assert await poller.poll_once() == 0
        assert await store.load_items() == []
        # ...and the mark still moved past it, so it is not reconsidered.
        assert poller.last_id == 11

    asyncio.run(run())


def test_a_dismissed_mention_stays_dismissed(tmp_path):
    async def run():
        store, poller = _poller(tmp_path, [Tweet(id="12", text="yo", author="z3ki")])
        await poller.poll_once()
        await store.mark(mention_item_id("12"), "dismissed")
        poller.last_id = 0  # as if the state file were lost
        assert await poller.poll_once() == 0
        items = await store.load_items()
        assert [i["state"] for i in items] == ["dismissed"]

    asyncio.run(run())


def test_a_backlog_drains_over_several_ticks(tmp_path):
    async def run():
        tweets = [Tweet(id=str(100 + i), text=f"hi {i}", author="z3ki") for i in range(10)]
        store, poller = _poller(tmp_path, tweets)
        first = await poller.poll_once()
        assert first == poller.MAX_NEW_PER_POLL
        # Nothing was skipped: the rest arrive next tick.
        second = await poller.poll_once()
        assert first + second == 10

    asyncio.run(run())


def test_poll_failure_backs_off_and_recovers(tmp_path):
    async def run():
        store, poller = _poller(tmp_path, [])
        poller.client.backends[0]._error = XError("session expired")
        assert await poller.poll_once() == 0
        assert poller.backoff_seconds() > poller.interval
        poller.client.backends[0]._error = None
        poller.client.backends[0]._answer = [Tweet(id="20", text="hi", author="z3ki")]
        assert await poller.poll_once() == 1
        assert poller.backoff_seconds() == poller.interval

    asyncio.run(run())


def test_poller_stays_off_without_a_handle(tmp_path):
    client = XClient({"cache_seconds": 0}, data_dir=tmp_path)
    client.backends = [_FakeBackend(client, "cookies", reads={"mentions"})]
    poller = XMentionPoller(InboxStore(str(tmp_path)), client, data_dir=tmp_path)
    assert poller.configured() is False


# ── the HTTP backends, against a real local server ────────────────────────


def _serve(routes):
    """Run a tiny aiohttp app on an ephemeral port; yields the base URL.

    The gateway and RSS backends are the two paths a user actually points at
    their own infrastructure, so they are worth exercising over real HTTP
    rather than with a stubbed backend object.
    """
    from aiohttp import web

    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    return app


async def _run_server(routes):
    from aiohttp import web

    runner = web.AppRunner(_serve(routes))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}"


def test_gateway_backend_reads_and_posts_over_http(tmp_path):
    from aiohttp import web

    seen = {}

    async def search(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["query"] = request.query.get("query")
        return web.json_response(
            {"data": [{"id": "5", "text": "gateway says hi", "username": "nasa"}]}
        )

    async def post(request):
        seen["body"] = await request.json()
        return web.json_response({"id": "6", "text": seen["body"]["text"]})

    async def run():
        runner, base = await _run_server(
            [("GET", "/search", search), ("POST", "/tweet", post)]
        )
        try:
            client = XClient(
                {
                    "backend": "api",
                    "api_base_url": base,
                    "api_key": "secret",
                    "handle": "maxwell",
                    "cache_seconds": 0,
                },
                data_dir=tmp_path,
            )
            tweets = await client.read("search", query="space stuff")
            assert [t.text for t in tweets] == ["gateway says hi"]
            assert seen["auth"] == "Bearer secret"
            assert seen["query"] == "space stuff"

            result = await client.post("hello from maxwell")
            assert result["id"] == "6"
            assert result["url"] == "https://x.com/maxwell/status/6"
            assert seen["body"]["text"] == "hello from maxwell"
            await client.aclose()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_gateway_paths_can_be_remapped(tmp_path):
    from aiohttp import web

    async def custom(request):
        assert request.query.get("q") == "ai"
        return web.json_response([{"id_str": "9", "full_text": "remapped"}])

    async def run():
        runner, base = await _run_server([("GET", "/v1/find", custom)])
        try:
            client = XClient(
                {
                    "backend": "api",
                    "api_base_url": base,
                    "api_paths": {"search": "/v1/find?q={query}&n={limit}"},
                    "cache_seconds": 0,
                },
                data_dir=tmp_path,
            )
            tweets = await client.read("search", query="ai")
            assert [t.text for t in tweets] == ["remapped"]
            await client.aclose()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_a_failing_gateway_falls_through_to_the_next_backend(tmp_path):
    from aiohttp import web

    async def broken(request):
        return web.Response(status=502, text="upstream died")

    async def run():
        runner, base = await _run_server([("GET", "/tweets", broken)])
        try:
            client = XClient(
                {"api_base_url": base, "cache_seconds": 0}, data_dir=tmp_path
            )
            # Keep the gateway, replace the public sources with one that answers.
            client.backends = [
                b for b in client.backends if b.name == "api"
            ] + [
                _FakeBackend(
                    client, "rss", reads={"user"}, answer=[Tweet(id="1", text="ok")]
                )
            ]
            tweets = await client.read("user", handle="nasa")
            assert [t.id for t in tweets] == ["1"]
            await client.aclose()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_rss_backend_reads_a_live_feed(tmp_path):
    from aiohttp import web

    feed = """<?xml version="1.0"?><rss><channel>
      <item><title>hello</title><description>hello there</description>
      <link>https://nitter.example/nasa/status/7</link>
      <pubDate>Wed, 20 Aug 2026 12:00:00 GMT</pubDate></item></channel></rss>"""

    async def rss(request):
        return web.Response(text=feed, content_type="application/rss+xml")

    async def run():
        runner, base = await _run_server([("GET", "/nasa/rss", rss)])
        try:
            client = XClient(
                {"backend": "rss", "rss_base_url": base, "cache_seconds": 0},
                data_dir=tmp_path,
            )
            tweets = await client.read("user", handle="@nasa")
            assert [(t.id, t.author) for t in tweets] == [("7", "nasa")]
            await client.aclose()
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_a_dead_host_is_an_error_not_a_traceback(tmp_path):
    async def run():
        client = XClient(
            {
                "backend": "api",
                "api_base_url": "http://127.0.0.1:1",
                "cache_seconds": 0,
                "timeout": 5,
            },
            data_dir=tmp_path,
        )
        with pytest.raises(XError):
            await client.read("search", query="anything")
        await client.aclose()

    asyncio.run(run())
