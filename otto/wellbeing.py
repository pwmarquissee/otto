"""What actually creates the owner's energy, measured instead of guessed.

THE ASK (owner, 2026-08-06, operating profile). "Track what actually creates energy
rather than relying on broad mood judgments." The profile lists roughly fifteen
signals to watch and a set of questions to ask. `otto checkin` stored a free-text
note and energy 1-5, so the profile described tracking that did not exist.

THE CONSTRAINT THAT SHAPES ALL OF THIS, and it predates the ask. `CheckinRequest`
says: "he asked for two or three lines, not a form, and a form he will not fill in is
worth less than a sentence he will." That is still true, and a fifteen-field
questionnaire every evening is the single most likely way to end up with three days of
data and then silence. So:

  * THE NOTE REMAINS SUFFICIENT. `otto checkin "..."` is unchanged and complete. Every
    signal here is optional, and a day with only prose is a valid day.
  * THREE-STATE, NEVER TWO. Unset means "not asked", not "no". A day where he did not
    say whether he got outside must not be counted as a day he stayed in, or every
    correlation below is quietly poisoned by silence.
  * ONE FLAG, NOT FIFTEEN. `--did kayak,outside --not doom` beats a prompt per field.

WHY THESE SIGNALS AND NOT OTHERS. They are his own causal hypotheses, taken from the
profile's "what gives me energy" and "what drains me" lists, so the data can test the
beliefs he already holds rather than a generic mood inventory. `kayak` is separate from
`moved` because he named it specifically as his current recovery, and the open question
he raised himself is whether it is a hyperfixation or a real input. That is now
answerable.

WHAT THIS REFUSES TO DO. It does not score him, produce a wellness index, or tell him
how he is doing. It reports how often a thing happened and what his energy averaged on
the days it did, with the sample size attached and an explicit refusal to compare when
either side is too small. He is a systems thinker; fake precision from nine days of
data would discredit the whole thing on first read, and rightly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from .store import Store


class Signal(NamedTuple):
    key: str
    prompt: str          # how it is asked
    kind: str            # "bool" | "scale"
    good: bool           # True if more of it is hypothesised to help
    label: str           # how it is reported


# The outcome, then his hypothesised inputs, then the drains, then affect. Order is
# the order `--ask` walks them, so it runs outcome-first: energy and sleep are the two
# he can answer without thinking, which is the right way to start a 20-second ritual.
SIGNALS: tuple[Signal, ...] = (
    Signal("sleep", "Sleep quality", "scale", True, "slept well"),
    Signal("energy", "Energy at end of day", "scale", True, "energy"),
    Signal("waking", "Energy on waking", "scale", True, "woke rested"),
    Signal("outside", "Time outside?", "bool", True, "got outside"),
    Signal("moved", "Moved your body?", "bool", True, "exercised"),
    Signal("kayak", "Kayaked?", "bool", True, "kayaked"),
    Signal("connected", "Real conversation with an adult?", "bool", True,
           "talked to someone properly"),
    Signal("known", "Did you feel known by anyone?", "bool", True, "felt known"),
    Signal("made", "Did you create anything?", "bool", True, "made something"),
    Signal("valued", "Did anyone use or value something you made?", "bool", True,
           "someone valued it"),
    Signal("quiet", "Any time where nobody needed anything from you?", "bool", True,
           "had time to yourself"),
    Signal("doom", "Doomscrolling or arguing online?", "bool", False, "doomscrolled"),
    Signal("obligation", "Shallow social obligation?", "bool", False,
           "shallow obligation"),
    Signal("irritable", "Irritable?", "bool", False, "irritable"),
    Signal("resentful", "Resentful?", "bool", False, "resentful"),
    Signal("self", "Did you feel like yourself?", "bool", True, "felt like yourself"),
)

BY_KEY: dict[str, Signal] = {s.key: s for s in SIGNALS}
BOOLS: tuple[str, ...] = tuple(s.key for s in SIGNALS if s.kind == "bool")
SCALES: tuple[str, ...] = tuple(s.key for s in SIGNALS if s.kind == "scale")

# Below this on EITHER side, a difference is not reported as a difference. Nine days of
# check-ins can easily produce a two-point "effect" that is one bad Tuesday. The number
# is arbitrary and stated rather than hidden, which is the point.
MIN_PER_SIDE = 4

# The outcome every input is measured against.
OUTCOME = "energy"


class Refused(RuntimeError):
    """The check-in was not recorded."""


def normalize(did: str | None, didnt: str | None,
              scales: dict[str, int | None] | None = None) -> dict[str, Any]:
    """Turn `--did a,b --not c` plus the numbers into a signal dict.

    Raises on an unknown key rather than dropping it. A typo that silently records
    nothing would show up weeks later as a signal that mysteriously has no data, and
    the whole value here is in the series being trustworthy.
    """
    out: dict[str, Any] = {}

    def mark(raw: str | None, value: bool) -> None:
        for name in (raw or "").replace(" ", ",").split(","):
            name = name.strip().lower()
            if not name:
                continue
            if name not in BY_KEY:
                raise Refused(
                    f"unknown signal {name!r}. Known: {', '.join(BOOLS)}")
            if BY_KEY[name].kind != "bool":
                raise Refused(f"{name} is a 1-5 scale, use --{name} N")
            if name in out and out[name] is not value:
                raise Refused(f"{name} given as both did and not-did")
            out[name] = value

    mark(did, True)
    mark(didnt, False)

    for key, value in (scales or {}).items():
        if value is None:
            continue
        if key not in BY_KEY or BY_KEY[key].kind != "scale":
            raise Refused(f"unknown scale {key!r}")
        if not 1 <= int(value) <= 5:
            raise Refused(f"{key} must be 1-5, got {value}")
        out[key] = int(value)
    return out


def series(store: Store, days: int = 90,
           today: date | None = None) -> list[dict[str, Any]]:
    """Check-ins in the window, oldest first, each flattened to {date, **signals}."""
    today = today or datetime.now().astimezone().date()
    floor = today - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for key, rec in (store.days() or {}).items():
        try:
            when = date.fromisoformat(key)
        except (ValueError, TypeError):
            continue
        if not (floor <= when <= today):
            continue
        ci = (rec or {}).get("checkin") or {}
        if not ci:
            continue
        row: dict[str, Any] = {"date": key, "note": ci.get("note")}
        for s in SIGNALS:
            if s.key in ci:
                row[s.key] = ci[s.key]
        # `energy` predates the signal set and lives at the top of the check-in, so it
        # is already picked up by the loop above. Nothing to migrate.
        out.append(row)
    out.sort(key=lambda r: r["date"])
    return out


def coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How many days actually carry each signal. The honest denominator."""
    return {s.key: sum(1 for r in rows if r.get(s.key) is not None) for s in SIGNALS}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def patterns(store: Store, days: int = 90,
             today: date | None = None) -> dict[str, Any]:
    """For each boolean signal, energy on the days it was true vs false.

    Deliberately not a model call and deliberately not a correlation coefficient. It
    is two means and two counts, which is the most that nine days of data can honestly
    support, and it is the shape that answers his actual question: does this thing
    move the number.
    """
    rows = series(store, days, today)
    outcome_rows = [r for r in rows if isinstance(r.get(OUTCOME), int)]

    found: list[dict[str, Any]] = []
    for key in BOOLS:
        sig = BY_KEY[key]
        yes = [float(r[OUTCOME]) for r in outcome_rows if r.get(key) is True]
        no = [float(r[OUTCOME]) for r in outcome_rows if r.get(key) is False]
        entry = {
            "key": key, "label": sig.label, "good": sig.good,
            "n_yes": len(yes), "n_no": len(no),
            "energy_yes": _mean(yes), "energy_no": _mean(no),
            "delta": None, "enough": len(yes) >= MIN_PER_SIDE and len(no) >= MIN_PER_SIDE,
        }
        if entry["enough"]:
            entry["delta"] = round(entry["energy_yes"] - entry["energy_no"], 2)
        found.append(entry)

    # Ranked by strength of effect, but ONLY among the ones with enough data. An
    # unranked list of mostly-unknowns is what makes a dashboard like this ignorable.
    ranked = sorted([f for f in found if f["enough"]],
                    key=lambda f: -abs(f["delta"] or 0))

    return {
        "days": len(rows),
        "with_energy": len(outcome_rows),
        "min_per_side": MIN_PER_SIDE,
        "coverage": coverage(rows),
        "signals": found,
        "ranked": ranked,
        "recent": recent_counts(rows, today),
        "suggestion": suggestion(ranked, rows, today),
    }


def recent_counts(rows: list[dict[str, Any]], today: date | None = None,
                  window: int = 7) -> dict[str, dict[str, Any]]:
    """How often each thing happened lately, and how long since the last time.

    "14 days since you last kayaked" is more actionable than any average, and it is
    the number that survives having almost no data.
    """
    today = today or datetime.now().astimezone().date()
    floor = today - timedelta(days=window)
    out: dict[str, dict[str, Any]] = {}
    for key in BOOLS:
        hits = [r for r in rows if r.get(key) is True]
        last = hits[-1]["date"] if hits else None
        since = None
        if last:
            since = (today - date.fromisoformat(last)).days
        out[key] = {
            "label": BY_KEY[key].label,
            "good": BY_KEY[key].good,
            "in_window": sum(1 for r in hits
                             if floor <= date.fromisoformat(r["date"]) <= today),
            "last": last,
            "days_since": since,
        }
    return out


def suggestion(ranked: list[dict[str, Any]], rows: list[dict[str, Any]],
               today: date | None = None) -> dict[str, Any] | None:
    """The smallest action with evidence behind it, or None.

    His profile asks for "the smallest action that would improve tomorrow by 10
    percent". This answers it only when the data supports an answer: the
    highest-delta good thing that has NOT happened in the last few days. When nothing
    has enough data it returns None rather than inventing advice, because a suggestion
    with no evidence is the generic encouragement the profile explicitly rejects.
    """
    today = today or datetime.now().astimezone().date()
    counts = recent_counts(rows, today)
    for entry in ranked:
        if not entry["good"] or (entry["delta"] or 0) <= 0:
            continue
        rec = counts.get(entry["key"]) or {}
        since = rec.get("days_since")
        if since is None or since >= 3:
            return {
                "key": entry["key"], "label": entry["label"],
                "delta": entry["delta"], "days_since": since,
                "n_yes": entry["n_yes"], "n_no": entry["n_no"],
            }
    return None
