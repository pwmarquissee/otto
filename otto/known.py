"""Known failures, and the moment they stop being true.

WHAT THIS IS. The owner can annotate a schedule or an integration as a KNOWN failure
with a reason: "heartbeat fails until the EDR key is rotated", "Notion is
mcp-only until the token lands in the credential store". While the annotation stands, the
thing's alert is demoted to info and carries the reason, so the morning read says
"known: waiting on X" instead of screaming about something the owner already decided
to live with.

WHY IT EARNS A MODULE, when `ack` and `reviewed_at` already exist. Those are for
runs, which are history: a failed run never un-fails, so acknowledging it is the
whole story. A schedule or an integration is a CONDITION, and conditions clear.
Otto had no way to say "this is broken and I know" about a condition, so the owner's
choices were a permanent alert or `toggle --off`, and a disabled schedule stays
disabled after the credential is rotated because nothing re-enables it. That is
the pattern retire.py documents: every detector adds, none removes, and state only
moves one way.

THE STALE NOTICE is the part worth stealing from the Dev Console's scenario tool,
where an expectation annotated "known failure" flips to STALE the moment it starts
passing, with a banner saying the annotation is now lying. Here: `sweep()` runs on
the tick, and when a known-broken schedule stamps ok, or a known-down integration
probes ok, Otto posts ONE warn notice telling the owner to clear the annotation and
close the ticket it pointed at. It is the cheapest positive signal Otto can give,
it is genuinely rare, and it is the only notice in Otto whose action is to DELETE
a piece of state rather than add one.

WHAT IT DELIBERATELY DOES NOT DO. It never clears the annotation itself. Whether
the underlying thing is really fixed, or succeeded once by luck, is a judgment,
and a detector that retires the owner's annotations on a timer is deciding what he
knows. Same rule as retire.py: it reports, he decides. `stale_noticed_at` exists
so the report happens once, not every tick until he gets round to it.

STORAGE. A separate `known.json` rather than fields on Schedule and Integration,
because Integration rows are REBUILT wholesale by every probe
(`store.save_integrations(external.probe_all())`), so an annotation on the row
would last five minutes. Keying by kind and name survives that, and keeps the
list of things the owner has decided to tolerate in one file he can read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import config, notify
from .models import iso, utcnow
from .store import Store

KINDS = ("schedule", "integration")


def key(kind: str, name: str) -> str:
    return f"{kind}/{name}"


def _target_exists(store: Store, kind: str, name: str) -> tuple[bool, str | None]:
    """Whether the thing being annotated is real, and its domain if it is.

    Annotating a schedule that does not exist would record a decision about
    nothing, and then never go stale because nothing could ever succeed under that
    name. The check is the difference between a ledger and a scratchpad.
    """
    if kind == "schedule":
        sched = store.get_schedule(name)
        return (sched is not None, sched.domain if sched else None)
    if kind == "integration":
        return (any(i.name == name for i in store.integrations()), config.WORK)
    return (False, None)


def mark(store: Store, kind: str, name: str, reason: str, by: str = "owner") -> dict[str, Any]:
    """Record that `kind/name` is broken and the owner knows why. Replaces any prior mark."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a known failure needs a reason: it is what the alert will say "
                         "in place of the alarm, and what you will read in a month")
    exists, _domain = _target_exists(store, kind, name)
    if not exists:
        raise ValueError(f"no {kind} named {name!r}")
    item = {
        "kind": kind,
        "name": name,
        "reason": reason,
        "since": iso(utcnow()),
        "by": by,
        # Reset on every mark, so re-marking a thing that went stale re-arms the
        # detector rather than inheriting the old "already told you".
        "stale_noticed_at": None,
    }
    store.put_known(item)
    store.log(f"known failure marked: {kind} {name} ({reason[:60]})", source="known")
    return item


def clear(store: Store, kind: str, name: str) -> bool:
    removed = store.delete_known(kind, name)
    if removed:
        store.log(f"known failure cleared: {kind} {name}", source="known")
    return removed


def lookup(store: Store) -> dict[str, dict[str, Any]]:
    return {key(i["kind"], i["name"]): i for i in store.known()}


def healthy(store: Store, item: dict[str, Any]) -> bool:
    """Has the annotated thing succeeded SINCE it was marked?

    "Since" matters. A schedule whose last stamp predates the mark is not evidence
    of anything: the owner marked it knowing that. Both timestamps are Z-suffixed ISO
    strings from `iso()`, so a plain string comparison orders them correctly.
    """
    since = item.get("since") or ""
    if item["kind"] == "schedule":
        sched = store.get_schedule(item["name"])
        if sched is None:
            return False
        return bool(sched.last_status == "ok" and sched.last_run and sched.last_run > since)
    if item["kind"] == "integration":
        for i in store.integrations():
            if i.name == item["name"]:
                return bool(i.ok and i.checked_at and i.checked_at > since)
    return False


def sweep(store: Store, now: datetime | None = None) -> list[str]:
    """The tick hook: notice, once, when a known failure heals."""
    notes: list[str] = []
    for item in store.known():
        if item.get("stale_noticed_at"):
            continue
        if not healthy(store, item):
            continue
        kind, name = item["kind"], item["name"]
        _exists, domain = _target_exists(store, kind, name)
        notify.post(
            store,
            f"Known failure is now stale: {name}",
            body=(f"You marked {kind} {name} as a known failure "
                  f"({item['reason']}, since {str(item['since'])[:10]}). It just "
                  "succeeded on its own. The annotation is now lying about it: clear "
                  "it, and close whatever ticket it pointed at."),
            level="warn",
            domain=domain or config.WORK,
            source="known-stale",
            command=f"otto known rm {kind} {name}",
            key=f"known-stale:{key(kind, name)}",
        )
        item["stale_noticed_at"] = iso(now or utcnow())
        store.put_known(item)
        notes.append(f"known failure {key(kind, name)} is now STALE, told the owner")
    return notes


def render(store: Store) -> str:
    return render_items(store.known())


def render_items(items: list[dict[str, Any]]) -> str:
    """CLI rendering, from the API list. Shared so the daemon-side and client-side
    callers print the same thing."""
    if not items:
        return ("  nothing marked. otto known add <schedule|integration> <name> "
                "--reason \"...\" demotes its alert until it heals.\n")
    lines = [f"  {'KIND':<12}{'NAME':<22}{'SINCE':<12}REASON"]
    for i in sorted(items, key=lambda x: (x["kind"], x["name"])):
        flag = "  STALE, clear it" if i.get("stale_noticed_at") else ""
        lines.append(f"  {i['kind']:<12}{i['name'][:20]:<22}{str(i['since'])[:10]:<12}"
                     f"{i['reason'][:60]}{flag}")
    return "\n".join(lines) + "\n"
