"""Shared app defaults and exact legacy wrappers for transcript migration."""

LEGACY_BRIEF_REPLY = "Keep the final answer concise and focused on the result. Usually use 1-3 short sentences. Skip introductions, repeated points, and unnecessary explanation. Use more space only when the request needs it or the user asks for detail."

LEGACY_AUTO_WEB = "Decide whether this request needs current web evidence. Latest products, device compatibility, prices and software/API changes can require search even when the user does not say 'search'. Verify changing facts before answering; cite sources actually used and acknowledge unavailable evidence."

APP_REPLY_DEFAULTS = LEGACY_AUTO_WEB + "\n" + LEGACY_BRIEF_REPLY

LEGACY_APP_INSTRUCTIONS = frozenset(
    (
        "Search the web before answering this request. Use the sources you find, cite their URLs, and say when the results do not verify an answer.",
        "Keep the final answer concise and focused on the result. Usually use 1-3 short sentences. Skip introductions, repeated points, and unnecessary explanation. Use more space only when the request needs it or the user asks for detail.",
        "Research this request before answering. Use current, credible sources when external facts matter, prefer primary sources, and distinguish verified facts from uncertainty.",
        "Summarize the supplied material. Preserve the important facts, decisions, numbers, caveats, and action items; do not invent missing context.",
        "Explain this clearly and concretely. Define necessary jargon, show the reasoning structure, and use examples when they improve understanding.",
        "Rewrite the supplied material while preserving its intended meaning. Improve clarity, structure, and wording instead of adding new claims.",
        "Translate the supplied material accurately and naturally. Preserve names, numbers, code, links, and formatting where practical.",
        "Brainstorm useful, distinct ideas for this request. Prefer concrete options with tradeoffs over repetitive variants.",
        "Treat this as a coding/technical task. Inspect available context before assuming details, use tools when useful, and return a concrete implementation or debugging result.",
        "Do not use web_search or fetch_url for this request; work from the supplied conversation, attachments, and local context.",
        "Decide whether this request needs current web evidence. Latest products, device compatibility, prices and software/API changes can require search even when the user does not say 'search'. Verify changing facts before answering; cite sources actually used and acknowledge unavailable evidence.",
        "Give a thorough answer with the important reasoning, caveats, and implementation details.",
    )
)
