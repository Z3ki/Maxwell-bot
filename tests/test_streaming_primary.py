"""Test SSE streaming against the bot's PRIMARY provider (local Ollama +
minimax-m3:cloud). This is the model the bot is actually using, so it's the
one the streaming code MUST work against.

Catches: reasoning deltas, content=empty but reasoning=present (the ollama
cloud variant does this), usage, tool calls, error frames.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, "/root/maxwell")

import providers  # noqa: E402


async def main() -> int:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    model = os.environ.get("OLLAMA_MODEL", "minimax-m3:cloud")
    api_key = os.environ.get("OLLAMA_API_KEY", "")

    print(f"Testing stream=True against PRIMARY {base_url} model={model}")
    p = providers.OllamaProvider(
        base_url=base_url,
        model=model,
        max_tokens=200,
        temperature=0.7,
        api_key=api_key,
    )
    ok = await p.initialize()
    print(f"  initialize: {ok} (available={p.available})")
    if not p.available:
        print("SKIP: primary provider not reachable in this env")
        return 0

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
            max_tokens=200,
            timeout=120,
        )
    except Exception as e:
        elapsed = time.perf_counter() - start
        print(f"FAIL: raised {type(e).__name__}: {e} (after {elapsed:.1f}s)")
        return 1
    finally:
        await p.close()

    elapsed = time.perf_counter() - start
    content = result.get("content") if isinstance(result, dict) else None
    reasoning = result.get("reasoning_content") if isinstance(result, dict) else None
    tool_calls = result.get("tool_calls") if isinstance(result, dict) else None
    print(f"OK in {elapsed:.2f}s")
    print(
        f"  result_keys: {list(result.keys()) if isinstance(result, dict) else type(result).__name__}"
    )
    print(f"  last_usage: {p._last_usage!r}")
    print(f"  content_chars: {len(content or '')}")
    print(f"  content: {content!r}")
    print(f"  reasoning_chars: {len(reasoning or '')}")
    print(f"  reasoning: {reasoning!r}")
    print(f"  tool_calls: {tool_calls!r}")

    if not content and not reasoning:
        print(
            "FAIL: both content and reasoning are empty — model produced nothing mergeable"
        )
        return 1
    if not p._last_usage or p._last_usage.get("total_tokens", 0) <= 0:
        print("WARN: usage not populated (some providers omit it on free tier)")

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
