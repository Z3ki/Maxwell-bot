"""Test the live SSE streaming path against OpenRouter, using the production
providers.py code. This is intentionally NOT a unit test of _read_sse_response
in isolation — it imports the real module so any bug in the consumer is caught
here, not in a mock-only test that wouldn't have caught the request_start * 1000
unit mismatch from the first edit.

Run: cd /root/maxwell && python3 tests/test_streaming.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, "/root/maxwell")

import providers  # noqa: E402  -- production module under test


async def main() -> int:
    base_url = os.environ.get(
        "OLLAMA_FALLBACK_BASE_URL", "https://openrouter.ai/api/v1"
    )
    api_key = os.environ.get("OLLAMA_FALLBACK_API_KEY", "")
    model = os.environ.get(
        "OLLAMA_FALLBACK_MODEL",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    )
    if not api_key:
        print("FAIL: OLLAMA_FALLBACK_API_KEY is empty; cannot test", file=sys.stderr)
        return 2

    print(f"Testing stream=True against {base_url} model={model}")
    p = providers.OllamaProvider(
        base_url=base_url,
        model=model,
        max_tokens=120,
        temperature=0.7,
        api_key=api_key,
    )
    ok = await p.initialize()
    print(f"  initialize: {ok} (available={p.available})")
    if not p.available:
        print("FAIL: provider not available; cannot test")
        return 1

    start = time.perf_counter()
    try:
        result = await p.generate_chat_completion(
            messages=[
                {"role": "system", "content": "You are a concise assistant."},
                {
                    "role": "user",
                    "content": "Reply with one short sentence about the moon.",
                },
            ],
            max_tokens=120,
            timeout=60,
        )
    except Exception as e:
        elapsed = time.perf_counter() - start
        print(
            f"FAIL: generate_chat_completion raised {type(e).__name__}: {e} (after {elapsed:.1f}s)"
        )
        return 1
    finally:
        await p.close()

    elapsed = time.perf_counter() - start
    print(f"OK in {elapsed:.2f}s")
    print(
        f"  result_keys: {list(result.keys()) if isinstance(result, dict) else type(result).__name__}"
    )
    print(f"  last_usage: {p._last_usage!r}")
    print(f"  result preview: {str(result)[:400]!r}")

    if not isinstance(result, dict):
        print(f"FAIL: result is not a dict, got {type(result).__name__}")
        return 1

    # Stream contract: result is the *message* (or wrapper around it)?
    # Look at how generate_chat_completion returns it; the bot's request handler
    # returns the message dict. The streaming path should match.
    content = result.get("content") if isinstance(result, dict) else None
    tool_calls = result.get("tool_calls") if isinstance(result, dict) else None
    if content is None and isinstance(result, dict) and "message" in result:
        # Some wrappers return the OpenAI-style envelope
        envelope = result["message"]
        content = envelope.get("content") if isinstance(envelope, dict) else None
        tool_calls = envelope.get("tool_calls") if isinstance(envelope, dict) else None

    print(f"  content_chars: {len(content or '')}")
    print(f"  content: {content!r}")
    print(f"  tool_calls: {tool_calls!r}")

    if not content:
        print("FAIL: empty content (stream merge produced no content)")
        return 1
    if tool_calls:
        print("UNEXPECTED: got tool_calls for a non-tool prompt")
        return 1
    if not p._last_usage or p._last_usage.get("total_tokens", 0) <= 0:
        print("WARN: usage not populated (some providers omit it on free tier)")

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
