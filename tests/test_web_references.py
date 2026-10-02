import asyncio
import unittest

from web_references import (
    begin_web_references,
    ensure_web_references,
    record_web_search_hits,
    reset_web_references,
)


class WebReferenceTests(unittest.TestCase):
    def setUp(self):
        self.token = begin_web_references()
        self.addCleanup(reset_web_references, self.token)

    def test_search_hits_do_not_change_the_visible_reply(self):
        added = record_web_search_hits([
            {"href": "https://example.com/page"},
            {"url": "https://example.com/other"},
        ])
        self.assertEqual(added, "")
        for text in (
            "No usable web results for this.",
            "The answer.",
            "A fact. [1](<https://example.com/page>)",
            "",
            "   ",
        ):
            self.assertEqual(ensure_web_references(text), text)
            self.assertNotIn("Search references:", ensure_web_references(text))

    def test_invalid_urls_do_not_raise_or_decorate_the_reply(self):
        record_web_search_hits([
            {"href": "javascript:alert(1)"},
            {"href": "https://user:secret@example.com/"},
            {"href": "https://example.com/\nforged"},
            {"body": "1. Fake\n   https://invented.example/"},
            {"href": "https://[broken/"},
        ])
        self.assertEqual(
            ensure_web_references("No verified evidence."),
            "No verified evidence.",
        )

    def test_fresh_requests_do_not_reuse_sources(self):
        record_web_search_hits([{"href": "https://example.com/old"}])
        fresh = begin_web_references()
        try:
            self.assertEqual(ensure_web_references("New answer"), "New answer")
        finally:
            reset_web_references(fresh)
        self.assertNotIn(
            "https://example.com/old",
            ensure_web_references("Outer answer"),
        )

    def test_parallel_requests_do_not_attach_each_others_links(self):
        async def run():
            ready = asyncio.Event()

            async def turn(name):
                token = begin_web_references()
                try:
                    async def search():
                        record_web_search_hits([{"href": f"https://example.com/{name}"}])
                    await asyncio.create_task(search())
                    ready.set()
                    await ready.wait()
                    return ensure_web_references("Answer")
                finally:
                    reset_web_references(token)

            return await asyncio.gather(turn("alice"), turn("bob"))

        alice, bob = asyncio.run(run())
        self.assertEqual(alice, "Answer")
        self.assertEqual(bob, "Answer")
        self.assertNotIn("example.com", alice)
        self.assertNotIn("example.com", bob)


if __name__ == "__main__":
    unittest.main()
