"""The one judgement the refresh needs a model for: does this mail need a reply.

Everything else about a refresh is deterministic once the source is read directly
(`google.py`): times, titles, roles and attendees come off the API. What does not
is "is a specific person waiting on the owner", which is the question the old
refresh prompt put to a whole Claude Code session. Here it is one Messages API call
with a JSON schema on the output, on the cheapest current model, with the input
capped by count and snippet length rather than by dollars. The rules below are the
old prompt's mail-triage rules, kept verbatim, because they were tuned against real
inboxes and the failure they guard against ("reply" meaning "maybe reply") is the
one that makes the panel useless.

`call_model` is the seam: the tests replace it, and nothing else in the module
touches the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel

from .. import config

MAX_MESSAGES = 25        # the old prompt capped the list at 10; the model sees a few more
MAX_SNIPPET = 300        # characters of Gmail's own snippet per message
MAX_OUTPUT_TOKENS = 2048

# Per million tokens, for the models this is meant to run on. The daemon otherwise
# never prices tokens (Claude Code reports cost itself), but a direct API call
# reports only usage, and a run without a cost column is a run the spend view cannot
# compare. Unknown model: tokens are recorded, cost stays None.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}

SYSTEM = """You are Otto's mail triage for %(owner)s. You are given a list of recent
email threads (sender, recipients, subject, date, and Gmail's snippet) and you decide,
for each one, whether it needs a reply or is only for awareness. Output ONLY the JSON
the schema asks for.

Rules:
- Ignore newsletters, notifications, automated alerts, and marketing: mark them
  "awareness" with no `why`. Automated password resets, order confirmations,
  delivery notices and "someone sent you a message" prompts are not reply-worthy.
- Drop nothing and invent nothing: every id you are given appears exactly once in
  the output, with a verdict. A spam or phishing message (a display name unrelated to
  the sending address, a `Re:` on a thread that never existed, a send date weeks before
  the subject it replied to) is "awareness" with why "looks like phishing".

Mail triage (this is the part that matters most):
- `needs` is "reply" ONLY when a specific person is waiting on a response or an
  action from %(owner)s. A question addressed to them, a request, an approval, a
  document someone asked for.
- `needs` is "awareness" for everything else worth knowing: an FYI, a decision
  announced, a vendor touching base, a thread they are cc'd on that is proceeding fine
  without them.
- When you cannot tell, use "awareness". Never default to "reply". A list where
  "reply" means "maybe reply" is a list %(owner)s has to re-read from scratch every
  time, which defeats the entire point of classifying it.
- `why` is one short clause of evidence for a "reply", naming who is waiting and
  what for ("Tim asked for the kit spec, no answer yet"). Omit it for awareness
  items; the subject line is already the whole story there.
- `summary` is one short line about the whole list: how many need a reply, or that
  nothing does.
"""


class MailVerdict(BaseModel):
    id: str
    needs: Literal["reply", "awareness"]
    why: str | None = None


class MailTriage(BaseModel):
    summary: str
    items: list[MailVerdict]


@dataclass
class Classified:
    summary: str
    verdicts: dict[str, MailVerdict]
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def api_key() -> str | None:
    """The key for the classification call: the OTTO_ANTHROPIC_API_KEY setting, then
    ANTHROPIC_API_KEY in the environment, then the env-loader cache the Anthropic
    probe reads (its ANTHROPIC_API_KEY line; the admin key there cannot call the
    Messages API)."""
    key = (config.ANTHROPIC_API_KEY or "").strip()
    if key:
        return key
    import os
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    cache = Path(config.CLAUDE_DIR) / ".anthropic-env.cache"
    try:
        m = re.search(r'export ANTHROPIC_API_KEY="([^"]*)"', cache.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1).strip() if m and m.group(1).strip() else None


def _sdk_call(system: str, user: str, model: str) -> dict[str, Any]:
    """The real call: one Messages API request with the triage schema as the output
    format. Returns {"data": <dict matching MailTriage>, "usage": {...}}."""
    import anthropic

    key = api_key()
    if not key:
        raise RuntimeError("no Anthropic API key: set OTTO_ANTHROPIC_API_KEY in otto.env")
    client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=60.0)
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=MailTriage,
        )
    except anthropic.AuthenticationError as e:
        raise RuntimeError(f"Anthropic rejected the API key: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"Anthropic rate limit: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"could not reach the Anthropic API: {e}") from e
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to classify this batch")
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError("the model returned no parseable triage")
    usage = response.usage
    return {
        "data": parsed.model_dump(),
        "usage": {"input_tokens": getattr(usage, "input_tokens", None),
                  "output_tokens": getattr(usage, "output_tokens", None)},
    }


# The seam. Tests assign a function with the same signature; nothing else here
# reaches the network.
call_model: Callable[[str, str, str], dict[str, Any]] = _sdk_call


def _render(messages: list[dict[str, Any]]) -> str:
    rows = []
    for m in messages[:MAX_MESSAGES]:
        rows.append({
            "id": m["id"],
            "from": str(m.get("from") or "")[:120],
            "to": str(m.get("to") or "")[:200],
            "subject": str(m.get("subject") or "")[:200],
            "date": str(m.get("date") or m.get("at") or "")[:60],
            "unread": bool(m.get("unread")),
            "snippet": str(m.get("snippet") or "")[:MAX_SNIPPET],
        })
    return "Threads, newest first:\n" + json.dumps(rows, ensure_ascii=False, indent=1)


def cost_for(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)


def classify(messages: list[dict[str, Any]], *, owner: str | None = None,
             model: str | None = None) -> Classified:
    """Verdicts keyed by message id. An empty list costs nothing: no call is made."""
    model = (model or config.CLASSIFY_MODEL or "claude-haiku-5-5").strip()
    owner = owner or config.OWNER_NAME
    if not messages:
        return Classified(summary="no mail in the window", verdicts={}, model=model)
    system = SYSTEM % {"owner": owner}
    out = call_model(system, _render(messages), model)
    triage = MailTriage.model_validate(out.get("data") or {})
    usage = out.get("usage") or {}
    given = {m["id"] for m in messages[:MAX_MESSAGES]}
    verdicts = {v.id: v for v in triage.items if v.id in given}
    # A message the model skipped is "awareness", never dropped: the panel must show
    # what arrived, and silence from the model is not evidence it was fine.
    for m in messages[:MAX_MESSAGES]:
        verdicts.setdefault(m["id"], MailVerdict(id=m["id"], needs="awareness"))
    tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
    return Classified(
        summary=triage.summary.strip()[:200] or "classified",
        verdicts=verdicts, model=model,
        input_tokens=tin, output_tokens=tout, cost_usd=cost_for(model, tin, tout), raw=out,
    )
