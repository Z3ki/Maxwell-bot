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

    def test_missing_citations_get_clickable_search_references(self):
        result = record_web_search_hits([{"href": "https://example.com/page"}])
        self.assertIn("final visible answer must include", result)
        self.assertEqual(
            ensure_web_references("The answer."),
            "The answer.\n\nSearch references: [1](<https://example.com/page>)",
        )

    def test_existing_inline_citations_are_preserved(self):
        record_web_search_hits([{"href": "https://example.com/page"}])
        for text in (
            "A fact. [1](<https://example.com/page>)",
            "A fact. [1](https://example.com/page)",
        ):
            self.assertEqual(ensure_web_references(text), text)

    def test_duplicate_urls_keep_stable_numbers_across_searches(self):
        record_web_search_hits([{"href": "https://example.com/a"}])
        prompt = record_web_search_hits([
            {"href": "https://example.com/a"}, {"url": "https://example.com/b"},
        ])
        self.assertIn("[2](<https://example.com/b>)", prompt)
        reply = ensure_web_references("Answer")
        self.assertEqual(reply.count("https://example.com/a"), 1)
        self.assertEqual(ensure_web_references(reply), reply)

    def test_uncited_urls_wrong_numbers_and_code_do_not_satisfy_contract(self):
        record_web_search_hits([{"href": "https://example.com/a"}])
        for text in (
            "https://example.com/a",
            "[1](<https://invented.example/>)",
            "[9](<https://example.com/a>)",
            "`[1](<https://example.com/a>)`",
            "```\n[1](<https://example.com/a>)\n```",
        ):
            self.assertIn("Search references:", ensure_web_references(text))

    def test_snippet_links_and_invalid_urls_are_not_registered(self):
        record_web_search_hits([
            {"href": "javascript:alert(1)"},
            {"href": "https://user:secret@example.com/"},
            {"href": "https://example.com/\nforged"},
            {"body": "1. Fake\n   https://invented.example/"},
            {"href": "https://[broken/"},
        ])
        self.assertEqual(ensure_web_references("No verified evidence."), "No verified evidence.")

    def test_markdown_delimiters_are_encoded_and_parentheses_survive(self):
        record_web_search_hits([{"href": "https://example.com/a_(b)?q=<x>"}])
        reply = ensure_web_references("Answer")
        self.assertIn("[1](<https://example.com/a_(b)?q=%3Cx%3E>)", reply)
        self.assertEqual(ensure_web_references(reply), reply)

    def test_no_search_and_empty_responses_do_not_gain_citations(self):
        self.assertEqual(ensure_web_references("Hello"), "Hello")
        record_web_search_hits([{"href": "https://example.com/"}])
        self.assertEqual(ensure_web_references(""), "")
        self.assertEqual(ensure_web_references("   "), "   ")

    def test_unclosed_code_fence_cannot_hide_fallback_references(self):
        record_web_search_hits([{"href": "https://example.com/"}])
        reply = ensure_web_references("Example:\n```python\nprint(1)")
        self.assertIn("print(1)\n```\n\nSearch references:", reply)

    def test_collection_and_link_lengths_are_bounded(self):
        record_web_search_hits([
            {"href": f"https://example.com/{i}"} for i in range(100)
        ])
        reply = ensure_web_references("Answer")
        self.assertIn("[20]", reply)
        self.assertNotIn("[21]", reply)
        fresh = begin_web_references()
        try:
            record_web_search_hits([{"href": "https://example.com/" + "é" * 1000}])
            self.assertEqual(ensure_web_references("Answer"), "Answer")
        finally:
            reset_web_references(fresh)

    def test_fresh_requests_and_failure_cleanup_do_not_reuse_sources(self):
        record_web_search_hits([{"href": "https://example.com/old"}])
        fresh = begin_web_references()
        try:
            self.assertEqual(ensure_web_references("New answer"), "New answer")
            raise RuntimeError("cancelled or failed turn")
        except RuntimeError:
            pass
        finally:
            reset_web_references(fresh)
        self.assertIn("https://example.com/old", ensure_web_references("Outer answer"))

    def test_parallel_requests_are_isolated_and_child_tools_share_their_turn(self):
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
        self.assertIn("/alice", alice)
        self.assertNotIn("/bob", alice)
        self.assertIn("/bob", bob)
        self.assertNotIn("/alice", bob)


if __name__ == "__main__":
    unittest.main()
