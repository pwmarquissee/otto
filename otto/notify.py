"""Otto reaching its owner.

Two channels, deliberately layered:

  NOTICE  the durable one. Stored, survives a restart, has read state, and is what
          the dashboard renders. This is the message.
  TOAST   the transient nudge. Fires once per notice and may fail freely. A toast
          that cannot render must never lose the message, which is why the notice is
          written first and the toast is best-effort afterwards.

Getting that order wrong is the whole risk here: a notification system that drops
the content when the delivery fails is worse than no notification system, because it
teaches you the absence of a toast means the absence of news.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .models import Notice, iso, utcnow
from .store import Store

TOAST_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "Show-OttoToast.ps1"

# Only these raise a toast unprompted. `info` still lands as a notice and shows in the
# dashboard; it just does not interrupt. The owner's attention is the scarce resource here,
# and a system that toasts everything gets muted, which costs you the crit ones too.
TOAST_LEVELS = {"warn", "crit"}

# Sources allowed only N notices per day, enforced HERE rather than in the prompt of
# whatever is posting. `observe` is the model-composed nudge: it gets one interruption a
# day, and a budget written into a command file is a budget the command can reason its
# way around. This is the chokepoint every poster goes through, so this is where the
# ceiling belongs.
#
# A source over its cap is DROPPED, not raised as an error: the caller is a scheduled
# agent that cannot do anything useful with an exception, and its second thought of the
# day is not important enough to be worth failing a run over.
SOURCE_DAILY_CAP = {"observe": 1}


def over_cap(store: Store, source: str, today: str | None = None) -> bool:
    cap = SOURCE_DAILY_CAP.get(source)
    if cap is None:
        return False
    today = today or iso(utcnow())[:10]
    posted = sum(1 for n in store.notices()
                 if n.source == source and (n.at or "")[:10] == today)
    return posted >= cap


# A reference to a board card is the most reliable thing a notice about work carries,
# and it survives the sentence being rewritten around it. Matches "card 136aca",
# "Board card 136aca", "(card 136aca)".
_CARD_REF = re.compile(r"\bcards?\s+([0-9a-f]{6,32})\b", re.I)
_PARENS = re.compile(r"\([^)]*\)")
# Apostrophes are DELETED, not turned into a space, so "Dana's" collapses to
# "danas" rather than "dana s". Everything else becomes a space, which is what
# makes "game-audio" and "game audio" the same subject.
_APOS = re.compile(r"['‘’]")
_NOISE = re.compile(r"[^a-z0-9 ]+")


def fingerprint(title: str, body: str | None = None, source: str = "otto") -> str:
    """What makes two notices the same thing.

    Prefers a card id, because that is stable while the prose around it is not. The
    night this was written for produced FOUR phrasings of one errand in seven hours
    ("are due today", "needs the ... today", with and without a parenthetical), and
    only the card id was constant across all of them.

    Falls back to the title with the volatile parts removed: parentheticals (which is
    where the rewritten "asked 8/6 19:14 PDT, owner agreed/committed" detail lived) and
    all punctuation, so "game-audio" and "game audio" land in the same place. That
    fallback catches exact and near-exact repeats and will not catch a full rewrite,
    which is why a producer that can supply a key should.
    """
    hay = f"{title} {body or ''}"
    card = _CARD_REF.search(hay)
    if card:
        return f"{source}:card:{card.group(1).lower()}"
    core = _PARENS.sub(" ", (title or "").lower())
    core = _NOISE.sub(" ", _APOS.sub("", core))
    return f"{source}:title:{' '.join(core.split())}"


def recent_same(store: Store, key: str, hours: int | None = None,
                now: datetime | None = None) -> Notice | None:
    """An existing notice for the same subject inside the dedupe window."""
    hours = config.NOTICE_DEDUPE_HOURS if hours is None else hours
    if hours <= 0:
        return None
    now = now or utcnow()
    floor = now - timedelta(hours=hours)
    for n in store.notices():          # newest first
        if n.key != key:
            continue
        try:
            when = datetime.fromisoformat((n.at or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.astimezone(timezone.utc) >= floor:
            return n
    return None


def post(store: Store, title: str, *, body: str | None = None,
         level: str = "info", domain: str = "work", source: str = "otto",
         command: str | None = None, notify: bool | None = None,
         key: str | None = None, task_id: str | None = None) -> Notice:
    """Record a message from Otto. Never raises on a delivery problem.

    A repeat of something already said inside NOTICE_DEDUPE_HOURS bumps that notice's
    `seen_count` and returns it, rather than adding another card and another toast.
    """
    if over_cap(store, source):
        store.log(f"notice from '{source}' DROPPED, over its daily cap "
                  f"({SOURCE_DAILY_CAP[source]}): {title[:60]}",
                  level="info", source="notify")
        # A Notice is returned rather than None so no caller has to branch on it, but
        # it is never stored. Nothing reads a notice by identity right after posting.
        return Notice(title=title.strip(), body=body, level=level,  # type: ignore[arg-type]
                      domain=domain, source=source, command=command,  # type: ignore[arg-type]
                      notify=False, read_at=iso(utcnow()))
    if notify is None:
        notify = level in TOAST_LEVELS

    fp = key or fingerprint(title, body, source)
    prior = recent_same(store, fp)
    if prior is not None:
        # Bump, do not repeat. `at` is left alone so the age keeps meaning "since when",
        # and `notified_at` is left alone so this does not buy a second toast.
        prior.seen_count += 1
        prior.last_seen = iso(utcnow())
        store.put_notice(prior)
        store.log(f"notice from '{source}' is a repeat of {prior.id[:6]} "
                  f"(seen {prior.seen_count}x), not re-posted: {title[:60]}",
                  level="info", source="notify")
        return prior

    n = Notice(title=title.strip(), body=(body or None), level=level,  # type: ignore[arg-type]
               domain=domain, source=source, command=command, notify=notify,  # type: ignore[arg-type]
               key=fp, task_id=task_id)
    store.put_notice(n)
    store.log(f"notice [{level}] {title}", level="info", source="notify")
    return n


def unread(store: Store, domain: str | None = None) -> list[Notice]:
    return [n for n in store.notices()
            if n.read_at is None and (domain is None or n.domain == domain)]


def mark_read(store: Store, notice_id: str) -> Notice | None:
    n = store.get_notice(notice_id)
    if n is None:
        return None
    if n.read_at is None:
        n.read_at = iso(utcnow())
        store.put_notice(n)
    return n


_quiet_logged_hour: str | None = None


def deliver_pending(store: Store, limit: int = 3) -> list[str]:
    """Fire toasts for notices that are owed one. Called from the tick loop.

    Capped per tick on purpose. If the daemon was down for a day, the backlog of
    notices is real but a burst of twenty toasts is not information, it is noise, and
    Windows collapses them into the Action Center anyway. The notices themselves are
    all still there to read.
    """
    notes: list[str] = []
    owed = [n for n in store.notices()
            if n.notify and n.notified_at is None and n.read_at is None]
    # Weekend: work does not interrupt. The notice is still stored and still shows in
    # the dashboard, so nothing is lost -- it just does not buzz. `crit` is exempt:
    # a fleet-wide outage on a Saturday is exactly when the owner does want to be told,
    # and silencing that would make the whole channel untrustworthy.
    if config.WEEKEND_PAUSES_WORK and config.is_weekend():
        held = [n for n in owed if n.domain == "work" and n.level != "crit"]
        if held:
            notes.append(f"weekend: holding {len(held)} work notice(s), no toast")
        owed = [n for n in owed if n not in held]
    # Overnight: HELD, not dropped. `notified_at` stays unset, so everything held here
    # toasts once quiet hours end rather than being lost. Same shape as the weekend
    # rule above and `crit` is exempt for the same reason.
    #
    # The night that motivated this delivered six warn toasts between 00:15 and 06:37
    # local. Dedupe (see `post`) turns those six into one; this decides whether that
    # one is worth waking him for, and for a warn about an errand due tomorrow it is
    # not.
    if config.in_quiet_hours():
        held = [n for n in owed if n.level != "crit"]
        # Say it once an hour, not once a tick: the same sentence 300 times filled
        # the whole event window one night and buried the two lines that mattered.
        global _quiet_logged_hour
        hour = iso(utcnow())[:13]
        if held and _quiet_logged_hour != hour:
            _quiet_logged_hour = hour
            notes.append(f"quiet hours ({config.QUIET_FROM:02d}:00-{config.QUIET_TO:02d}:00): "
                         f"holding {len(held)} notice(s) until morning")
        owed = [n for n in owed if n not in held]
    owed.sort(key=lambda n: n.at)          # oldest first, so nothing starves
    if len(owed) > limit:
        notes.append(f"{len(owed)} notices owed a toast, showing {limit} this tick")
    for n in owed[:limit]:
        ok = _toast(n.title, n.body or "", n.level, reply_url=reply_url(n))
        # Marked notified either way. A toast that failed to render is not going to
        # succeed on retry for the same reason, and the notice is already durable.
        n.notified_at = iso(utcnow())
        store.put_notice(n)
        notes.append(f"toast {'shown' if ok else 'FAILED'}: {n.title[:48]}")
    return notes


def desktop_registered() -> bool:
    """Whether the Otto desktop shell owns the `otto://` scheme on this machine.

    The shell registers it in HKCU on every start (desktop/src-tauri/src/main.rs),
    so the key's presence is the honest test: it means an exe is there to answer.
    Read fresh each time rather than cached, because a first launch of the shell
    between two toasts should flip the very next link."""
    if config.REPLY_SCHEME in ("otto", "http"):
        return config.REPLY_SCHEME == "otto"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\otto\shell\open\command"):
            return True
    except (ImportError, OSError):
        return False


def reply_url(n: Notice) -> str | None:
    """Where the toast's Reply button lands: this card's reply sheet.

    Only a notice about one card has one. `otto://reply/<id>` when the desktop shell
    is registered, so the click lands in Otto's own window (owner report, 2026-09-03: the http
    form went to Chrome). Otherwise the dashboard URL with `#reply=<id>`, which the
    same JavaScript handles, in whatever browser owns http."""
    if not n.task_id:
        return None
    if desktop_registered():
        return f"otto://reply/{n.task_id}"
    return f"{config.BASE_URL}/#reply={n.task_id}"


def _toast(title: str, body: str, level: str, reply_url: str | None = None) -> bool:
    # Checked before anything else. A notification is the one side effect in Otto that
    # escapes the state directory entirely: point OTTO_HOME at a temp dir and the store
    # is sandboxed while the desktop is not. See config.TOASTS_ENABLED for the night
    # that earned this line.
    if not config.TOASTS_ENABLED:
        return False
    if not TOAST_SCRIPT.is_file():
        return False
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(TOAST_SCRIPT),
             "-Title", f"{config.PERSONA_NAME}: {title}"[:120],
             "-Body", (body or "")[:280], "-Level", level]
            + (["-ReplyUrl", reply_url] if reply_url else []),
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0 and "toast: none" not in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False
