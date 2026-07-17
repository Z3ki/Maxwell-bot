"""Modular tool registry for Maxwell Bot.

This is the single sane place where tools get declared, schemas get built, and
tool calls get dispatched. The old code had tool schemas hardcoded in one file,
descriptions living on tool instances, two parallel dispatch paths (native vs
XML) that both reinvented reasoning recording, and a *separate* `reasoning_log`
tool the model had to call before doing anything. It was a goddamn mess.

New contract (read this before you touch anything):

- Every tool gets an OPTIONAL `reasoning` string parameter, auto-injected into
  its OpenAI/JSON schema. The model fills it with real reasoning right before it
  asks for the tool's action. No more standalone reasoning tool. Reasoning now
  LIVES INSIDE the tool call the model actually wants to use. That's the whole
  point of this refactor.
- Plain chat turns (no helper tool needed) still go through `send_message`,
  which itself carries a `reasoning` field. So there's exactly one reasoning
  location: the tool the model is calling. Predictable. Inspectable. Done.
- `record_reasoning()` is the ONE function that persists a reasoning trace to
  the dashboard JSON. Both dispatch paths funnel through it. Stop adding new
  places to write `llm_traces.json` — there is one. (If you need a second, you
  don't.)

The registry is intentionally dumb: it holds ToolSpec objects, builds schemas
on demand, and runs a normalized call against a live tool instance. No magic,
no metaclasses, no decorators that half the codebase doesn't know exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# The reasoning parameter we stamp onto EVERY tool schema. Same shape, same
# description, same key name everywhere. The model picks ONE tool per step and
# writes its real reasoning here. We never want this to diverge per-tool.
REASONING_PARAM_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Your real, plain-English reasoning BEFORE you take this action. Why "
        "you are calling this tool, what you expect to happen, and any "
        "assumptions or risks. Plain text only — no XML, no JSON, no tags. "
        "Fill this in for EVERY tool call including send_message."
    ),
}

# Max chars we keep from a reasoning string. The trace file is shown on a
# dashboard; a novel here helps nobody and bloats the context budget.
REASONING_MAX_CHARS = 1000


def _sanitize_reasoning(raw: Any) -> str:
    """Coerce reasoning to a bounded, plain-text string.

    The model occasionally wraps thoughts in <thoughts>...</thoughts> like a
    smartass, or dumps JSON. Strip the tags, cap the length, move on. We do NOT
    try to be clever and parse nested payloads — reasoning is one string. If
    the model can't follow that, it gets clamped, not interpreted.
    """
    text = str(raw or "").strip()
    if "<" in text and ">" in text:
        # Cheap, ruthless tag strip: drop anything that looks like <...>.
        import re

        text = re.sub(r"<[^>]+>", " ", text).strip()
    if len(text) > REASONING_MAX_CHARS:
        text = text[: REASONING_MAX_CHARS - 1] + "…"
    return text


@dataclass
class ToolSpec:
    """Static description of a tool, decoupled from its live instance.

    Why a dataclass and not just the Tool class? Because schemas and the
    reasoning param want to be declared ONCE, in data, without instantiating a
    Discord-coupled Tool (which needs a `bot`) just to know what arguments it
    takes. Specs are registry-side metadata; instances are runtime.

    Attributes:
        name: canonical tool name as referenced by the model / provider.
        description: human + model-facing description text.
        parameters: JSON Schema ``parameters`` object (the inner dict, NOT the
            OpenAI ``function`` wrapper). Never include `reasoning` — we inject
            that automatically so it's impossible to forget on a new tool.
        required: list of required parameter names (minus `reasoning`).
        terminal: True for tools that end a turn (send_message / no_response).
            Dispatch runs all non-terminal tools first, then the single first
            terminal tool. Duplicate terminals are skipped. This matches the
            old behavior but now it's an explicit, visible flag instead of a
            hardcoded set buried in a dispatch function.
        is_destructive: mirror of Tool.is_destructive — passed through so the
            prompt-injection confirm gate can read it from the spec too.
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": True})
    required: list[str] = field(default_factory=list)
    terminal: bool = False
    is_destructive: bool = False
    compatible_platforms: set[str] | None = None  # None = all platforms

    def openai_function(self) -> dict[str, Any]:
        """Build the OpenAI ``{type:function, function:{...}}`` entry.

        `reasoning` is injected on top of whatever parameters the tool declared
        so EVERY tool — including the no-arg ones — gets the reasoning field.
        No special cases. No forgetting one.
        """
        props = dict(self.parameters.get("properties") or {})
        props = dict(props)  # don't mutate the spec's dict, Jesus
        props.setdefault("reasoning", REASONING_PARAM_SCHEMA)
        # reasoning is never required — let the model be terse on a trivial
        # call instead of forcing a paragraph. The AUTO BACKFILL records
        # "no reasoning provided" if it's empty.
        required = [r for r in (self.required or []) if r != "reasoning"]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (self.description or self.name).strip() or self.name,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "additionalProperties": bool(
                        self.parameters.get("additionalProperties", True)
                    ),
                    "required": required,
                },
            },
        }


class ToolRegistry:
    """A tiny, predictable registry of ToolSpecs.

    One instance per bot. Register specs at startup, then ask it for the
    OpenAI tools payload or look a spec up by name. That's it. If you find
    yourself adding behavior here, ask whether it belongs on ToolSpec or in
    the dispatcher instead. Keep this dumb.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("ToolSpec.name is required, you absolute legend")
        if spec.name in self._specs:
            logger.warning(
                "Tool %r registered twice; overwriting the old spec. "
                "Sort your registration order out.",
                spec.name,
            )
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def all_specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def build_openai_tools(
        self,
        *,
        allowed_names: set[str] | None = None,
        disabled_names: set[str] | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        """OpenAI ``tools`` payload filtered by platform + allow/disable lists.

        Platform filtering uses spec.compatible_platforms (None = available
        everywhere). This replaces the scattered `TELEGRAM_COMPATIBLE_*`
        intersection logic with a per-spec declaration. For a transition we
        still accept an explicit allowed_names override (see bot.py wiring).
        """
        disabled = disabled_names or set()
        out: list[dict[str, Any]] = []
        for spec in self._specs.values():
            if spec.name in disabled:
                continue
            if allowed_names is not None and spec.name not in allowed_names:
                continue
            if (
                platform is not None
                and spec.compatible_platforms is not None
                and platform not in spec.compatible_platforms
                and spec.name not in (allowed_names or set())
            ):
                continue
            out.append(spec.openai_function())
        return out

    def descriptions_block(self, *, allowed_names: set[str], disabled_names: set[str] | None = None) -> list[str]:
        """``name: description`` lines for the system prompt, filtered."""
        disabled = disabled_names or set()
        lines: list[str] = []
        for spec in self._specs.values():
            if spec.name in disabled or spec.name not in allowed_names:
                continue
            lines.append(f"{spec.name}: {spec.description}")
        return lines


async def record_reasoning(
    bot: Any,
    message: Any,
    *,
    tool_name: str,
    reasoning: str,
    params: dict[str, Any],
    result: str,
) -> None:
    """ONE reasoning recorder. Both dispatch paths call this.

    Writes a trace payload keyed by the tool that actually ran (not a phantom
    `reasoning_log` tool), so the dashboard shows reasoning attached to the
    real action. If `reasoning` is empty we still record a stub so every tool
    call is auditable — that's the whole reason this exists.

    Swallows errors: a trace write failure must NEVER abort the tool result
    that already happened. The user already saw the action; losing a trace
    line is fine, losing the reply is not.
    """
    cleaned = _sanitize_reasoning(reasoning)
    payload: dict[str, Any] = {
        "tool": tool_name,
        "thoughts": cleaned or "(no reasoning provided by the model)",
        "params_preview": _summarize_params(params),
    }
    try:
        await bot._record_llm_trace(message, payload)
    except Exception as e:  # noqa: BLE001 — intentional, see docstring
        logger.warning("Failed to record reasoning trace for %s: %s", tool_name, e)


def _summarize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Throw away the giant blobs (HTML bodies, file contents) for the trace.

    The trace is for humans eyeballing reasoning, not a byte-exact replay.
    Keeping a 2MB create_site body in llm_traces.json would be insane.
    """
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if k == "reasoning":
            continue
        if isinstance(v, str) and len(v) > 200:
            out[k] = f"[{len(v)} chars]"
        elif isinstance(v, (list, tuple)) and len(v) > 20:
            out[k] = f"[{len(v)} items]"
        else:
            out[k] = v
    return out


def extract_reasoning(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pop `reasoning` out of a tool's params so it's not passed to execute().

    Returns (reasoning, params_without_reasoning). Tools don't know about
    `reasoning`; it's a registry-level concern. This keeps Tool.execute()
    signatures clean and stops a tool from accidentally `**kwargs`-ing it into
    a real API call somewhere.
    """
    params = dict(params or {})
    reasoning = str(params.pop("reasoning", "") or "")
    return reasoning, params
