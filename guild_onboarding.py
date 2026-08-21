"""Member-side guild onboarding — picking roles and channels in a server.

Most COMMUNITY servers gate their content behind Discord's onboarding
prompts ("What kind of roles do you want?", "Which channels interest
you?"). Until the account answers them it holds none of the opt-in roles
and sees only the server's default channels, which is why Maxwell can
land in a server and still not be able to do anything in it.

There is no ``/members/@me/onboarding`` route — that path 404s as an
unknown route, so the old auto-onboard never actually submitted
anything. The real client flow is:

    GET  /guilds/{guild_id}/onboarding            -> prompts + this
                                                     member's answers
    POST /guilds/{guild_id}/onboarding-responses  -> submit answers

The picking itself is Maxwell's call: the prompt titles/descriptions go
to the model, which answers with option ids. Everything here is pure and
side-effect free except :func:`fetch_onboarding` and
:func:`submit_responses`, both of which take an injected ``request``
callable so the flow can run from the bot, from tests, or from a
one-shot script.
"""

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# async (method, path_template, payload|None, **route_params) -> dict
# The template stays unformatted so a discord.py Route keeps its major
# parameter (guild_id) and its own rate-limit bucket.
RequestFn = Callable[..., Awaitable[Any]]

ONBOARDING_PATH = "/guilds/{guild_id}/onboarding"
RESPONSES_PATH = "/guilds/{guild_id}/onboarding-responses"

# Prompts flagged in_onboarding=False are the "Channels & Roles" extras a
# member can still opt into after the join flow, so they're answerable too.
# Kept as a constant because the tool exposes it as a knob.
INCLUDE_POST_JOIN_PROMPTS = True


def normalize_prompts(
    data: dict | None, *, include_post_join: bool = True
) -> list[dict]:
    """Flatten the onboarding payload into the fields the picker needs.

    Unknown/None entries are dropped rather than raised on — this parses a
    live API response, and a prompt with no options is useless anyway.
    """
    prompts = []
    for raw in (data or {}).get("prompts") or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "")
        if not pid:
            continue
        if not raw.get("in_onboarding", True) and not include_post_join:
            continue
        options = []
        for opt in raw.get("options") or []:
            if not isinstance(opt, dict):
                continue
            oid = str(opt.get("id") or "")
            if not oid:
                continue
            options.append(
                {
                    "id": oid,
                    "title": str(opt.get("title") or "").strip() or "(untitled)",
                    "description": str(opt.get("description") or "").strip(),
                    "role_ids": [str(r) for r in (opt.get("role_ids") or [])],
                    "channel_ids": [str(c) for c in (opt.get("channel_ids") or [])],
                }
            )
        if not options:
            continue
        prompts.append(
            {
                "id": pid,
                "title": str(raw.get("title") or "").strip() or "(untitled prompt)",
                "single_select": bool(raw.get("single_select")),
                "required": bool(raw.get("required")),
                "in_onboarding": bool(raw.get("in_onboarding", True)),
                "options": options,
            }
        )
    return prompts


def answered_option_ids(data: dict | None) -> set[str]:
    """Option ids this member has already picked, per the API payload.

    ``responses`` has been seen both as bare option objects and as
    ``{prompt_id, option_ids}`` pairs, so accept either shape.
    """
    picked: set[str] = set()
    for entry in (data or {}).get("responses") or []:
        if isinstance(entry, str):
            picked.add(entry)
        elif isinstance(entry, dict):
            if entry.get("option_ids"):
                picked.update(str(o) for o in entry["option_ids"])
            elif entry.get("id"):
                picked.add(str(entry["id"]))
    return picked


def describe_prompts(prompts: list[dict], answered: set[str] | None = None) -> str:
    """Render prompts as the text block the model picks from."""
    answered = answered or set()
    lines = []
    for prompt in prompts:
        rule = "pick exactly one" if prompt["single_select"] else "pick any number"
        need = "required" if prompt["required"] else "optional"
        where = "join flow" if prompt["in_onboarding"] else "channels & roles"
        lines.append(
            f'PROMPT {prompt["id"]} — "{prompt["title"]}" ({rule}, {need}, {where})'
        )
        for opt in prompt["options"]:
            desc = f" — {opt['description']}" if opt["description"] else ""
            grants = []
            if opt["role_ids"]:
                grants.append(f"{len(opt['role_ids'])} role(s)")
            if opt["channel_ids"]:
                grants.append(f"{len(opt['channel_ids'])} channel(s)")
            grant = f" [grants {', '.join(grants)}]" if grants else ""
            mark = " [already selected]" if opt["id"] in answered else ""
            lines.append(f'  {opt["id"]} — "{opt["title"]}"{desc}{grant}{mark}')
    return "\n".join(lines)


def clamp_choice(
    choice: dict[str, list[str]], prompts: list[dict]
) -> dict[str, list[str]]:
    """Drop ids the server never offered and enforce each prompt's rules.

    The model is perfectly capable of inventing an option id or picking
    three answers to a single-select prompt; Discord answers both with a
    400 that loses the whole submission, so filter before sending.
    """
    by_prompt = {p["id"]: p for p in prompts}
    cleaned: dict[str, list[str]] = {}
    for pid, oids in (choice or {}).items():
        prompt = by_prompt.get(str(pid))
        if prompt is None:
            continue
        valid_ids = [o["id"] for o in prompt["options"]]
        kept = []
        for oid in oids or []:
            oid = str(oid)
            if oid in valid_ids and oid not in kept:
                kept.append(oid)
        if prompt["single_select"]:
            kept = kept[:1]
        if not kept and prompt["required"]:
            kept = [valid_ids[0]]
        if kept:
            cleaned[prompt["id"]] = kept
    # A required prompt the model skipped entirely still has to be answered.
    for prompt in prompts:
        if prompt["required"] and prompt["id"] not in cleaned:
            cleaned[prompt["id"]] = [prompt["options"][0]["id"]]
    return cleaned


def fallback_choice(prompts: list[dict]) -> dict[str, list[str]]:
    """First option of every prompt — used when the model can't be reached.

    Not clever, but it leaves the account with roles instead of stranded,
    which is the behaviour this whole path exists for.
    """
    return {p["id"]: [p["options"][0]["id"]] for p in prompts if p["options"]}


def parse_choice_json(text: str, prompts: list[dict]) -> dict[str, list[str]]:
    """Pull ``{"picks": [{"prompt_id", "option_ids"}]}`` out of a model reply.

    Tolerates prose around the JSON and a bare list instead of the
    wrapper object. Returns {} when nothing parses, so callers can fall
    back rather than submit garbage.
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    # Strip ```json fences before hunting for the object.
    raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    payload: Any = None
    for candidate in (raw, _first_json_blob(raw)):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    if payload is None:
        return {}
    if isinstance(payload, dict):
        picks = payload.get("picks") or payload.get("responses") or []
    elif isinstance(payload, list):
        picks = payload
    else:
        return {}
    choice: dict[str, list[str]] = {}
    for entry in picks:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("prompt_id") or entry.get("id") or "")
        oids = entry.get("option_ids") or entry.get("options") or []
        if isinstance(oids, (str, int)):
            oids = [oids]
        if pid:
            choice[pid] = [str(o) for o in oids]
    return clamp_choice(choice, prompts)


def _first_json_blob(text: str) -> str:
    """Longest brace/bracket-balanced blob in the text, or ''."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            return text[start : end + 1]
    return ""


def build_payload(
    choice: dict[str, list[str]], prompts: list[dict], now_ms: int | None = None
) -> dict:
    """Body for POST /guilds/{id}/onboarding-responses.

    ``onboarding_responses`` is a flat list of the chosen *option* ids —
    the prompt id is implied by the option and sending the
    ``{prompt_id, option_ids}`` pairs the guild config uses gets a 400
    ("Value {...} is not snowflake"). ``*_seen`` marks the prompts and
    options the client rendered; Discord uses them for the "new options
    since you joined" badge and expects them alongside the answers.
    """
    stamp = int(now_ms if now_ms is not None else time.time() * 1000)
    responses: list[str] = []
    for oids in choice.values():
        for oid in oids:
            if oid not in responses:
                responses.append(oid)
    return {
        "onboarding_responses": responses,
        "onboarding_prompts_seen": {p["id"]: stamp for p in prompts},
        "onboarding_responses_seen": {
            opt["id"]: stamp for p in prompts for opt in p["options"]
        },
    }


def summarize_choice(choice: dict[str, list[str]], prompts: list[dict]) -> str:
    """Human/model-readable 'prompt -> options' recap."""
    by_prompt = {p["id"]: p for p in prompts}
    parts = []
    for pid, oids in choice.items():
        prompt = by_prompt.get(pid)
        if prompt is None:
            continue
        titles = [o["title"] for o in prompt["options"] if o["id"] in oids]
        parts.append(f"{prompt['title'][:60]} -> {', '.join(titles)[:120]}")
    return "; ".join(parts)


def granted_ids(
    choice: dict[str, list[str]], prompts: list[dict]
) -> tuple[list[str], list[str]]:
    """(role_ids, channel_ids) the selected options unlock."""
    by_prompt = {p["id"]: p for p in prompts}
    roles: list[str] = []
    channels: list[str] = []
    for pid, oids in choice.items():
        prompt = by_prompt.get(pid)
        if prompt is None:
            continue
        for opt in prompt["options"]:
            if opt["id"] not in oids:
                continue
            roles.extend(r for r in opt["role_ids"] if r not in roles)
            channels.extend(c for c in opt["channel_ids"] if c not in channels)
    return roles, channels


def build_picker_messages(
    guild_name: str,
    prompts: list[dict],
    answered: set[str] | None = None,
    personality: str = "",
    preferences: str = "",
) -> list[dict]:
    """Chat messages asking Maxwell which roles/channels he wants."""
    persona = (personality or "").strip()
    if persona:
        persona = f"Your personality:\n{persona[:1200]}\n\n"
    guidance = (
        f"\nThe user asked you to bias the picks like this: {preferences.strip()[:400]}\n"
        if (preferences or "").strip()
        else ""
    )
    system = (
        "You are Maxwell, choosing your own roles and channels in a Discord "
        "server you just joined. Pick what genuinely fits you — the topics "
        "you'd actually read, the pings you'd actually want. Skip options "
        "that don't interest you; you do not have to pick from every prompt. "
        "Respect each prompt's rule (single-select prompts take exactly one "
        "option).\n\n"
        f"{persona}"
        "Reply with ONLY JSON in this shape, no prose:\n"
        '{"picks": [{"prompt_id": "<id>", "option_ids": ["<id>", ...]}]}'
    )
    user = (
        f'Server: "{guild_name}"\n'
        f"{guidance}"
        "Here are the prompts and their options (ids first):\n\n"
        f"{describe_prompts(prompts, answered)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def fetch_onboarding(request: RequestFn, guild_id: int | str) -> dict:
    """GET the guild's onboarding config plus this member's answers."""
    return await request("GET", ONBOARDING_PATH, guild_id=guild_id) or {}


async def submit_responses(
    request: RequestFn, guild_id: int | str, payload: dict
) -> Any:
    """POST the member's onboarding answers (grants the roles/channels)."""
    return await request("POST", RESPONSES_PATH, payload, guild_id=guild_id)


async def run_onboarding(
    request: RequestFn,
    guild_id: int | str,
    guild_name: str,
    *,
    ask_llm: Callable[[list[dict]], Awaitable[str]] | None = None,
    personality: str = "",
    preferences: str = "",
    include_post_join: bool = INCLUDE_POST_JOIN_PROMPTS,
    dry_run: bool = False,
) -> dict:
    """Fetch prompts, let Maxwell choose, submit, and report what happened.

    Returns a dict with ``summary`` (one line for logs/tool output) and the
    detail fields a caller may want to render. Never raises: transport and
    parse failures come back as ``ok=False`` with the reason in ``summary``.
    """
    result: dict[str, Any] = {
        "ok": False,
        "guild_id": str(guild_id),
        "guild_name": guild_name,
        "summary": "",
        "prompts": [],
        "choice": {},
        "role_ids": [],
        "channel_ids": [],
        "picked_by": "none",
        "dry_run": dry_run,
    }
    try:
        data = await fetch_onboarding(request, guild_id)
    except Exception as e:
        result["summary"] = f"onboarding unavailable: {type(e).__name__}: {e}"
        return result
    if not (data or {}).get("enabled", True):
        result["summary"] = "no onboarding (disabled for this server)"
        return result
    prompts = normalize_prompts(data, include_post_join=include_post_join)
    result["prompts"] = prompts
    if not prompts:
        result["summary"] = "no onboarding prompts to answer"
        return result
    answered = answered_option_ids(data)
    result["already_selected"] = sorted(answered)

    choice: dict[str, list[str]] = {}
    if ask_llm is not None:
        try:
            reply = await ask_llm(
                build_picker_messages(
                    guild_name, prompts, answered, personality, preferences
                )
            )
            choice = parse_choice_json(reply, prompts)
            if choice:
                result["picked_by"] = "model"
            else:
                logger.warning(
                    "onboarding picker returned unusable reply for %s: %r",
                    guild_name,
                    (reply or "")[:200],
                )
        except Exception as e:
            logger.warning("onboarding picker failed for %s: %s", guild_name, e)
    if not choice:
        choice = fallback_choice(prompts)
        result["picked_by"] = "fallback"
    result["choice"] = choice
    roles, channels = granted_ids(choice, prompts)
    result["role_ids"] = roles
    result["channel_ids"] = channels
    picked_text = summarize_choice(choice, prompts)

    if dry_run:
        result["ok"] = True
        result["summary"] = f"would pick ({result['picked_by']}): {picked_text}"
        return result
    try:
        await submit_responses(request, guild_id, build_payload(choice, prompts))
    except Exception as e:
        detail = getattr(e, "text", "") or str(e)
        result["summary"] = (
            f"onboarding submit failed: {type(e).__name__}: {detail[:200]}"
        )
        return result
    result["ok"] = True
    result["summary"] = f"onboarding completed ({result['picked_by']}): {picked_text}"
    return result
