"""Short public posts, drawn from the work that already happened.

The gap this closes is a career one. A senior role expects mentorship, internal or
external, and there is no energy left in the week for the in-person kind. Writing about the work is the form of external mentorship that a
depleted week can still hold, provided two things are true: the ideas arrive
without being hunted for, and the draft is most of the way there before he sits
down. So this module mines the week for post ideas, drafts the one he picks in his
own voice, and puts a confidentiality check between the draft and the world.

WHERE THE IDEAS COME FROM. Only from what he already produces: the day rollups
(`journal.py`, his own transcripts reduced to titles and stated intents), the
decision log (the `why` fields are the best raw material in Otto), the notices
Otto raised, and the notes he wrote. Never from a form. The morning check-in was
removed for being write-only; a "what did you learn this week" box would be the
same nag with a different label.

THE GATE IS DETERMINISTIC AND OTTO NEVER CLEARS IT. Every draft is run through
`scan()`: codenames, colleagues on the roster, hostnames, emails, account ids,
dollar figures and counts, vendors, family. The result is shown on the draft and
in the CLI, and the only thing that moves a post to `posted` is the owner saying so.
The org rule is that unreleased product detail does not leave the company unless
a person clears it, and this is how it stays a person's call rather than a
model's. Numbers are flagged too, every time: a price quoted from memory went out
under his name once and was ten times low.

WHAT THE MODEL IS FOR. Two calls. `ideas` reads the material and proposes a
handful of hooks with the angle and the evidence line each rests on. `draft`
writes one post from one idea, in the voice recorded in `writing-voice.md`, with
his notes on the previous attempt if there was one. Both run as headless sessions
with no tools and no MCP servers: the material is in the prompt, so there is
nothing to fetch, and an empty MCP config is what makes the session cheap.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psutil

from . import config, launcher, notify
from .models import Post, Run, iso, utcnow
from .refresh import _extract as extract_json
from .runners import detached
from .store import Store

THEMES = ("gamedev", "ai", "security", "enterprise")

# Every built-in tool. The session has nothing to fetch and nothing to write: the
# material is in the prompt and the answer is JSON on stdout. Denying the lot is
# what lets this run unattended without --dangerously-skip-permissions.
DENIED_TOOLS = [
    "Bash", "Edit", "Write", "NotebookEdit", "Task", "Agent", "WebFetch", "WebSearch",
    "Read", "Glob", "Grep", "KillShell", "BashOutput", "TodoWrite", "ToolSearch",
    "Skill", "PowerShell",
]

ID_LEN = 6
MATERIAL_MAX_CHARS = 14000
SESSIONS_PER_DAY = 8
NOTE_EXCERPT_CHARS = 500
EXEMPLARS = 3

# Sessions Otto started look, in a transcript, like sessions the owner typed in: the
# `-p` prompt is a user message. Their stated intent is a system prompt, and it
# starts one of these ways. Anything Otto spawned is also excluded by session id.
_OTTO_INTENT_PREFIXES = ("You are Otto", "You are ", "/", f"Sweep {config.OWNER_NAME}'s",
                         "Work the ")
MIN_SESSION_MINUTES = 3

VOICE_SEED = """# How I write (edit me; Otto reads this before every draft)

The rules below matter less than writing-samples.md, which is things I actually
wrote. If a draft follows every rule here and still does not sound like the
samples, the samples win.

Who I am on the page: I run the whole IT and security function at a small game
studio, alone, and I am writing for people doing that kind of job, or about to.
I am a practitioner and an executive at the same time, and the writing should
read like both: someone who built the thing and someone who decided to.

What I write about: the intersection of game development, AI, security, and
enterprise technology, and the human side of it. What the work is actually like,
what it costs, what surprised me, what I got wrong.

Register
- Report, not confession. The situation, the decision, the reasoning. A person
  is behind it, but "I" is not the subject of every sentence.
- Specific at the mechanism level. What component, what it does, what fails.
- Name the trade. "X in exchange for Y", and which side I land on, and why.
- The position is in the substance, not the phrasing. I do not write hot takes.
  I write the decision and let the reader disagree with the reasoning.
- Say what is not known and what still needs confirming.
- Dry humor inside plain sentences is fine. A joke is never the closer.
- Short technical analogies, one line, then move on.
- No em dashes. Commas, periods, parentheses. American English.
- Words I do not use: delve, leverage, game-changer, journey, unlock, superpower,
  "in today's landscape", "it's important to note".

AI tells I will not post (Otto: these are hard bans, not preferences)
- "The interesting part wasn't X. It was Y." and every "not X, but Y" reveal.
- "That's the actual variable", "here's the thing", "the real question is".
- A one-sentence paragraph used as a punchline.
- Three parallel items for rhythm where two or four would be true.
- Explaining what I am about to say, or that I am being honest or humble.
- Ending on "What's the one thing you'd..." or any question aimed at the
  audience for engagement. A question is fine if I actually want the answer.
- Sentences that restate the previous one with more emphasis.
- Confessional openers ("I told my own AI agent...", "I said no this week...").

Form (LinkedIn)
- 90 to 200 words. The first line says what happened or what I decided, plainly.
- One idea per post. Short paragraphs. A list is fine when the items are parallel.
- End on the reasoning, the open question I actually have, or what I would
  check next. Not a moral, not a call to action.
- Hashtags: none, or at most three at the very end. No emojis.

What never goes in (Otto flags these; I decide)
- Codenames, unreleased features, milestone names, playtest details.
- Colleagues by name. "A teammate", "our lead", "someone on the art team".
- Numbers I have not verified against a source that day.
- Anything about my family, unless I chose to put it there.
"""


def ensure_voice() -> Path:
    """Seed the voice file once. Never overwrites: it is his, like profile.md."""
    p = config.WRITING_VOICE_PATH
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(VOICE_SEED, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

_NOISE = re.compile(r"[^a-z0-9 ]+")


def fingerprint(hook: str) -> str:
    """What makes two ideas the same idea: the hook with punctuation and case gone."""
    core = _NOISE.sub(" ", (hook or "").lower())
    return "writing:" + " ".join(core.split())


def make_id(hook: str) -> str:
    return "p-" + hashlib.sha256(fingerprint(hook).encode("utf-8")).hexdigest()[:ID_LEN]


# ---------------------------------------------------------------------------
# material: what the week contained, deterministically
# ---------------------------------------------------------------------------

def _is_otto_session(rec: dict[str, Any], otto_ids: set[str]) -> bool:
    if rec.get("session") in otto_ids:
        return True
    intent = (rec.get("intent") or "").lstrip()
    if intent.startswith(_OTTO_INTENT_PREFIXES):
        return True
    return (rec.get("minutes") or 0) < MIN_SESSION_MINUTES


def _fmt_minutes(m: int) -> str:
    m = int(m or 0)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _window(days: int, today: date | None = None) -> tuple[date, date]:
    today = today or datetime.now().astimezone().date()
    return today - timedelta(days=days), today


def material(store: Store, days: int | None = None, today: date | None = None) -> str:
    """The week as text, from what the owner already produced. No model, no cost.

    Otto's own sessions are excluded by run session id and by the shape of their
    first prompt, because a week of "You are Otto's refresher" is not a week he
    lived. Sessions under three minutes are noise for the same reason.
    """
    days = days or config.WRITING_LOOKBACK_DAYS
    start, end = _window(days, today)
    otto_ids = {r.session_id for r in store.runs() if r.session_id}

    lines: list[str] = [f"MATERIAL: what {config.OWNER_NAME} actually did, {start} to {end}", ""]

    lines.append("DAYS (from his own Claude Code transcripts; title, stated intent)")
    all_days = store.days()
    any_day = False
    for key in sorted(all_days):
        try:
            d = date.fromisoformat(key)
        except ValueError:
            continue
        if not (start <= d <= end):
            continue
        roll = (all_days[key] or {}).get("rollup") or {}
        sessions = [s for s in (roll.get("sessions") or [])
                    if isinstance(s, dict) and not _is_otto_session(s, otto_ids)]
        if not sessions:
            continue
        any_day = True
        sessions.sort(key=lambda s: -(s.get("minutes") or 0))
        projects = ", ".join(list(roll.get("by_project") or {})[:4])
        lines.append(f"{key}  {_fmt_minutes(roll.get('wall_minutes') or 0)} engaged"
                     + (f", projects: {projects}" if projects else ""))
        for s in sessions[:SESSIONS_PER_DAY]:
            title = (s.get("title") or "").strip().replace("\n", " ")
            intent = " ".join((s.get("intent") or "").split())[:160]
            proj = (s.get("project") or "").replace("--", "/").replace("-", "/")
            bits = [f"  - {_fmt_minutes(s.get('minutes') or 0):>6}"]
            if title:
                bits.append(title[:90])
            if intent and intent[:60].lower() != title[:60].lower():
                bits.append(f'| asked: "{intent}"')
            if proj:
                bits.append(f"[{proj}]")
            lines.append(" ".join(bits))
        if len(sessions) > SESSIONS_PER_DAY:
            lines.append(f"    ({len(sessions) - SESSIONS_PER_DAY} shorter sessions not listed)")
    if not any_day:
        lines.append("  (no transcript activity in the window)")

    lines += ["", "DECISIONS (recorded in the window; the why is the material)"]
    any_dec = False
    for dec in store.decisions():
        try:
            when = date.fromisoformat(dec.decided[:10])
        except ValueError:
            continue
        if not (start <= when <= end) or not dec.live:
            continue
        any_dec = True
        lines.append(f"  - {dec.decided} {dec.title}")
        lines.append(f"      decided: {' '.join(dec.decision.split())[:240]}")
        lines.append(f"      why: {' '.join(dec.why.split())[:300]}")
        if dec.alternatives:
            lines.append(f"      rejected: {' '.join(dec.alternatives.split())[:200]}")
    if not any_dec:
        lines.append("  (none)")

    lines += ["", "WHAT OTTO RAISED (notices in the window)"]
    any_n = False
    for n in store.notices():
        if (n.at or "")[:10] < start.isoformat():
            continue
        if n.source in ("due", "prep"):
            continue  # a card coming due is a reminder, not an event
        any_n = True
        lines.append(f"  - {(n.at or '')[:10]} {n.level:<4} {n.title[:110]}")
        if len([ln for ln in lines if ln.startswith("  - ")]) > 60:
            break
    if not any_n:
        lines.append("  (none)")

    notes_dir = config.OTTO_HOME / "notes"
    if notes_dir.is_dir():
        floor = datetime.combine(start, datetime.min.time()).timestamp()
        recent = []
        for f in notes_dir.glob("*.md"):
            try:
                if f.stat().st_mtime >= floor:
                    recent.append(f)
            except OSError:
                continue
        if recent:
            lines += ["", "NOTES HE WROTE (files touched in the window, opening lines)"]
            for f in sorted(recent):
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                excerpt = " ".join(text.split())[:NOTE_EXCERPT_CHARS]
                lines.append(f"  - {f.name}: {excerpt}")

    existing = [p for p in store.posts() if p.status != "dropped"]
    dropped = [p for p in store.posts() if p.status == "dropped"]
    if existing or dropped:
        lines += ["", "ALREADY ON THE LIST (do not propose these again, or near-variants)"]
        for p in existing[:30]:
            lines.append(f"  - [{p.status}] {p.hook[:110]}")
        for p in dropped[:20]:
            lines.append(f"  - [dropped, he did not want it] {p.hook[:110]}")

    text = "\n".join(lines)
    if len(text) > MATERIAL_MAX_CHARS:
        text = text[:MATERIAL_MAX_CHARS] + "\n  (material truncated here)"
    return text


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_AWS = re.compile(r"(?<!\d)\d{12}(?!\d)")
_SLACK = re.compile(r"\b[UCGD]0[A-Z0-9]{8,10}\b")
_URL = re.compile(r"https?://\S+")
# Workstation names. The default catches the PREFIX-SERIAL shape most fleets use;
# an org whose hostnames carry its own token sets config.WRITING_HOST_PATTERN to a
# regex that knows it. Read with getattr because the pattern is optional and this
# module must import without it.
_HOST_DEFAULT = r"\b[A-Z]{2,5}-[A-Z0-9][A-Z0-9-]{2,}\b"
_HOST = re.compile(getattr(config, "WRITING_HOST_PATTERN", None) or _HOST_DEFAULT)
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM]\b)?")
_PCT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
# Three to eleven digits standing alone. Twelve is an AWS account id and has its own
# flag; a run inside a Slack id or a hostname is not a count.
_COUNT = re.compile(r"(?<![\w.\-/:$])\d{3,11}(?![\w.\-/:%])")


def _word(name: str, *, ci: bool) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])",
                      re.I if ci else 0)


def _roster() -> list[tuple[str, str]]:
    """(display name, last name) for everyone with a dossier, minus the owner."""
    try:
        from . import people
        rows = people.load()
    except Exception:  # noqa: BLE001 - a broken dossier must not break the gate
        return []
    # The author is not a leak. Either spelling of their name excludes them: the
    # calendar one (MEETINGS_OWNER) and the one Otto addresses them by (OWNER_NAME).
    me = {config.MEETINGS_OWNER.lower(), config.OWNER_NAME.lower()} - {""}
    out: list[tuple[str, str]] = []
    for d in rows:
        name = str(d.get("full_name") or d.get("display_name") or "").strip()
        if not name or name.lower() in me or name.lower() == d.get("slug"):
            continue
        last = str(d.get("lastName") or (name.split()[-1] if " " in name else "")).strip()
        out.append((name, last))
    return out


def scan(text: str) -> list[dict[str, str]]:
    """What a draft would reveal. Deterministic, and it never says "clean".

    Every hit is a decision for the owner, not a verdict: a vendor name is normal in a
    post about tooling and also maps the stack; a number may be right and still
    needs checking against its source before it goes out under his name. So the
    output is a list of things to look at, with why, and an empty list means the
    scan found nothing, which is not the same as the draft being safe.
    """
    if not text:
        return []
    hits: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, match: str, why: str) -> None:
        key = (kind, match.lower())
        if key in seen:
            return
        seen.add(key)
        hits.append({"kind": kind, "match": match, "why": why})

    for name in config.WRITING_CODENAMES:
        for m in _word(name, ci=True).finditer(text):
            add("codename", m.group(0), "internal name. Keep the shape, lose the name.")
    for name, last in _roster():
        for m in _word(name, ci=True).finditer(text):
            add("person", m.group(0), "a colleague by name. 'A teammate' unless they agreed.")
        if last and len(last) >= 4 and last.isalpha():
            for m in _word(last, ci=False).finditer(text):
                add("person", m.group(0), "a colleague's surname.")
    for m in _HOST.finditer(text):
        add("host", m.group(0), "looks like a hostname or asset tag.")
    for m in _EMAIL.finditer(text):
        add("email", m.group(0), "an email address.")
    for m in _IP.finditer(text):
        add("ip", m.group(0), "an IP address.")
    for m in _AWS.finditer(text):
        add("aws-account", m.group(0), "twelve digits: an AWS account id?")
    for m in _SLACK.finditer(text):
        add("slack-id", m.group(0), "a Slack id.")
    for m in _URL.finditer(text):
        add("url", m.group(0).rstrip(".,)"), "a link. Public?")
    for m in _MONEY.finditer(text):
        add("figure", m.group(0), "a dollar figure. Verify against the source today.")
    for m in _PCT.finditer(text):
        add("figure", m.group(0), "a percentage. Verify against the source today.")
    for m in _COUNT.finditer(text):
        raw = m.group(0)
        if raw.isdigit() and 1900 <= int(raw) <= 2099:
            continue  # a year
        add("figure", raw, "a count. Verify against the source today.")
    for name in config.WRITING_VENDORS:
        for m in _word(name, ci=False).finditer(text):
            add("vendor", m.group(0), "names a vendor in the stack. Fine to say, and it maps the stack.")
    for name in config.WRITING_PERSONAL:
        pat = _word(name, ci=(name.lower() == name))
        for m in pat.finditer(text):
            add("personal", m.group(0), "family. Yours to put there, on purpose.")
    for name in config.WRITING_EMPLOYER:
        for m in _word(name, ci=False).finditer(text):
            add("employer", m.group(0), "names the studio. It is on your profile anyway; your call.")
    return hits


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

IDEAS_PROMPT = """You are helping %(owner)s find things worth arguing about. Output ONLY JSON.

Today is %(today)s. Who %(owner)s is, what the role covers, and what the posts are
about is described under HIS VOICE below; read it as the brief. He writes short
public posts (LinkedIn) about the HUMAN side of that work. The posts exist to start
conversations with peers, not to be agreed with.

THE BAR. Table stakes is the failure mode. "AI agents need guardrails", "the
backlog never hits zero", "trust is task-specific": true, unarguable, forgettable.
Nobody replies to a post they nod at. Every idea here must carry a STANCE: a claim
%(owner)s actually holds, that a competent peer could disagree with, backed by
something that happened this week. Before you keep an idea, write its pushback:
the best objection a smart reader will raise. If you cannot write a real
objection, the idea is table stakes. Sharpen it or drop it.

Where the arguable ones live:
- A rule he broke on purpose, and why the rule is wrong for a studio this size.
- A consensus in security, IT, or AI practice that his week showed to be
  backwards, at least for a one-person function.
- A cost nobody admits: what a "best practice" actually charges a small team.
- The gap between how a job is described and how it is actually done.
- A trade he made that others pretend is not a trade (speed for coverage,
  a vendor for a person, an agent for judgment).
- What he would tell his replacement that HR would not put in the job posting.
- A thing that worked and should not have, or failed and everyone said it
  would work.

Below is the material: what he actually did this week, from his own records.

Propose at most %(max)d ideas, ORDERED BY HOW LIKELY EACH IS TO START AN ARGUMENT,
strongest first. Fewer is fine. Zero is fine if the week gave you nothing with a
stance in it; say so in `summary` rather than manufacturing one.

Rules that matter:
- Every idea MUST rest on something in the material. Quote the line it rests on in
  `evidence`, briefly. Never invent an event, a number, or a conversation.
- The stance must be one %(owner)s would actually defend, from the evidence. Do not
  hand him a hot take he did not have.
- The post will be public. Internal codenames, project names, milestone names,
  colleagues' names, hostnames, vendors, dollar figures and counts all need to be
  either dropped or generalized. List anything the idea depends on that would
  need hiding in `reveals`, so he can judge whether the idea survives without it.
- Prefer the generalizable over the local. "Why a one-person security team should
  bill itself for its own false positives" beats "what happened Tuesday".
- `hook` is the first line of the post, under 120 characters, and it STATES OR
  IMPLIES THE STANCE in his register: a plain statement of what was decided or
  what happened, the way he opens a Slack post to his peers ("We dual-write
  player telemetry to a locked bucket next to the warehouse. The second copy is
  cheap; here is what it buys."). NOT a confessional opener ("I told my own AI
  agent...", "I said no this week and I'd do it again"), not a teaser, not
  "here's what I learned", no colon tricks, no emoji. Read HOW HE WRITES below
  before writing a single hook. Hard rules on the first line: it does not start
  with "I ", "I'm", "My ", "This week", or "Twice this week"; it does not
  contain "this week" at all; it names the thing and the decision. Three hooks
  in his register, for the shape only:
    "Our alert triage runs a first pass through a model before anyone reads it. Buying that was the wrong call at our size."
    "Player telemetry gets written twice: the warehouse, and a locked bucket next to it. The second copy costs almost nothing."
    "A signup form broke because real testers don't sign up with the identity you designed for."
- `stance` is the claim in one sentence, as he would say it: a decision and its
  reason, not a slogan. "I built the first-pass triage instead of buying a SOAR,
  because at one person the product's overhead is the whole cost" is a stance.
  "A team of one has no business shopping for a SOAR" is a hot take, and he does
  not write those. The argument lives in the reasoning, not the phrasing.
- `pushback` is the strongest objection, in the objector's words, one or two
  sentences. Steelman it; a weak objection means a weak idea.
- `question` is what the post leaves the reader to answer about their own
  situation. A real question, not rhetorical.
- `angle` is two or three sentences on what the post says and where the human
  part is. Address %(owner)s directly ("you turned down...") or write it as he would
  say it ("I turned down..."). Never about him in the third person.
- `energy` is "low" if a tired person could write this in fifteen minutes because
  the story is simple and already in his head, else "medium".
- Do not repeat anything under ALREADY ON THE LIST, including near-variants.

Output EXACTLY this shape, no prose, no code fence:

{"ideas": [{"hook": "<first line, stating the stance>",
            "stance": "<the claim, one sentence>",
            "pushback": "<the best objection, in the objector's words>",
            "question": "<what the reader has to answer about their own situation>",
            "angle": "<2-3 sentences>",
            "themes": ["gamedev|ai|security|enterprise", ...],
            "evidence": ["<short quote from the material>", ...],
            "reveals": ["<thing that would need hiding>", ...],
            "energy": "low|medium"}],
 "summary": "<one line: what kind of week it was, in his terms>"}

HIS VOICE (rules he wrote):
%(voice)s

HOW HE WRITES (verbatim samples; match this register, never reuse the content):
%(samples)s

%(material)s
"""

DRAFT_PROMPT = """You are drafting a short public post for %(owner)s, in his voice. Output ONLY JSON.

Today is %(today)s. The post goes on LinkedIn under his own name. He will read it,
change it, and decide whether it goes out; you are getting it most of the way.

THE IDEA
hook:     %(hook)s
stance:   %(stance)s
pushback: %(pushback)s
question: %(question)s
angle:    %(angle)s
themes:   %(themes)s
evidence: %(evidence)s
%(reveals_block)s
%(notes_block)s
%(previous_block)s
%(exemplars_block)s
HOW HE ACTUALLY WRITES. Verbatim samples of his own Slack posts and mail. This is
the register to match, sentence by sentence: how he opens, how he moves from the
situation to the decision to the reasoning, how he names a trade, how much he
explains, what he leaves unsaid. Never reuse the content or the names in them;
they are here for the sound. If the rules below and these samples disagree, the
samples win.
%(samples)s

HIS VOICE. Rules he wrote. Instruction, not color.
%(voice)s

Hard rules, on top of the voice:
- Public means public. No internal codenames, project or milestone names, no
  colleague's name, no hostname, no account id, no link. Generalize: "the game",
  "our studio", "a teammate", "the tool I built", "our EDR".
- No number that is not in the evidence above. If the point needs a number and
  there is none, write around it ("most of the bill", "a handful", "weeks").
- Ninety to two hundred words. The first line stands alone, and you write it
  fresh in his register: the stored hook above is the idea's working title, not
  a sentence to reproduce. If it is confessional or a hot take, the first line
  you write is not. Same rules as the hooks: no "I " or "This week" opener, it
  names the thing and the decision.
- Short paragraphs separated by blank lines. No bullet lists. No emoji. No em
  dashes. American spelling.
- Say what happened and what it meant. Do not explain that you are being humble,
  honest, or vulnerable; be it.
- Hold the stance, in his way: state the decision and the reasoning and let the
  reader disagree with the reasoning. Do not sharpen the phrasing to make it
  provocative; that is the hot-take register and he does not write in it. Meet
  the pushback in one line, on its own terms, without hedging the claim away.
- Report register, not confessional. Open with what happened or what was
  decided, the way his Slack posts open. "I" is not the subject of every
  sentence. No "The interesting part wasn't X. It was Y." No "not X, but Y"
  reveals. No "that's the actual variable". No one-line punchline paragraphs.
  No "here's the thing". No restating a sentence with more emphasis. These are
  the tells he called "clauded" and they are what got the first drafts rejected.
- End on the reasoning, on the open question he actually has, or on what he
  would check next. Never on a question aimed at the audience for engagement,
  never on a call to action, never on a moral.
- If there is a draft below marked as EDITED BY THE AUTHOR, his edits are the strongest
  signal you have. Keep every change he made unless his note says otherwise,
  and continue in the direction they point. Do not restore anything he cut.

Output EXACTLY this shape, no prose, no code fence:

{"draft": "<the post, with blank lines between paragraphs>",
 "alt_hooks": ["<a different first line, same rules>", "<another>"],
 "reveals": ["<anything you had to generalize, and what you replaced it with>", ...]}
"""


def build_ideas_prompt(store: Store, today: date | None = None) -> str:
    ensure_voice()
    return IDEAS_PROMPT % {
        "owner": config.OWNER_NAME,
        "today": (today or datetime.now().astimezone().date()).isoformat(),
        "max": config.WRITING_IDEAS_MAX,
        "voice": config.voice_text() or VOICE_SEED,
        "samples": config.samples_text() or "(no samples recorded yet)",
        "material": material(store, today=today),
    }


def _block(title: str, lines: list[str]) -> str:
    if not lines:
        return ""
    return f"{title}\n" + "\n".join(lines) + "\n"


def build_draft_prompt(store: Store, post: Post, today: date | None = None) -> str:
    ensure_voice()
    notes = [f"  - {n.get('at', '')[:10]}: {n.get('text', '')}" for n in post.notes if n.get("text")]
    previous = [f"  {ln}" for ln in (post.draft or "").splitlines()] if post.draft else []
    prev_title = ("THE PREVIOUS DRAFT, EDITED BY THE AUTHOR HIMSELF (keep his changes; continue them)"
                  if post.edited else "THE PREVIOUS DRAFT (revise it; keep what works)")
    exemplars: list[str] = []
    for p in store.posts():
        if p.status == "posted" and p.draft and p.id != post.id:
            exemplars.append("  ---\n" + "\n".join(f"  {ln}" for ln in p.draft.splitlines()))
        if len(exemplars) >= EXEMPLARS:
            break
    return DRAFT_PROMPT % {
        "owner": config.OWNER_NAME,
        "today": (today or datetime.now().astimezone().date()).isoformat(),
        "hook": post.hook,
        "stance": post.stance or "(none recorded; take one from the angle and hold it)",
        "pushback": post.pushback or "(none recorded; imagine the sharpest peer and answer them)",
        "question": post.question or "(none recorded; end on the one the story raises)",
        "angle": post.angle,
        "themes": ", ".join(post.themes) or "unspecified",
        "evidence": " | ".join(post.evidence) or "(none recorded)",
        "reveals_block": _block("MUST BE GENERALIZED (the idea depends on these; hide them)",
                                [f"  - {r}" for r in post.reveals]),
        "notes_block": _block("WHAT HE ASKED FOR (his notes on this post, oldest first)", notes),
        "previous_block": _block(prev_title, previous),
        "exemplars_block": _block("POSTS HE HAS ALREADY PUBLISHED (match this register)", exemplars),
        "voice": config.voice_text() or VOICE_SEED,
        "samples": config.samples_text() or "(no samples recorded yet; fall back to the rules)",
    }


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

def _launcher(run_id: str, prompt_file: Path, kind: str) -> Path:
    # No tools and no MCP servers, so no permissions to skip. The empty config is
    # what drops the local servers and their 28k tokens of definitions; the deny
    # list is belt and braces for the built-ins. See detached._write_launcher.
    empty = config.LOG_DIR / f"{run_id[:6]}-writing.mcp.json"
    empty.write_text('{"mcpServers":{}}', encoding="utf-8")
    args = ["-p", "--output-format", "json",
            "--strict-mcp-config", "--mcp-config", str(empty),
            "--disallowed-tools", " ".join(DENIED_TOOLS)]
    if config.WRITING_BUDGET_USD:
        args += ["--max-budget-usd", str(config.WRITING_BUDGET_USD)]
    if config.WRITING_MODEL:
        args += ["--model", config.WRITING_MODEL]
    return launcher.write_claude(
        config.LOG_DIR / f"{run_id[:6]}-writing-{kind}",
        launcher.ClaudeSpec(cwd=str(config.CHAT_CWD), prompt_file=prompt_file, args=tuple(args)))


def _spawn(prompt: str, kind: str, post_id: str | None = None) -> Run:
    run_id = uuid.uuid4().hex
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = config.LOG_DIR / f"{run_id[:6]}-writing-{kind}.prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    log = config.LOG_DIR / f"{run_id[:6]}-writing-{kind}.log"

    cmd = launcher.command(_launcher(run_id, prompt_file, kind))
    fh = log.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            cmd, cwd=config.CHAT_CWD, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=detached._FLAGS_HEADLESS, shell=False,
        )
    finally:
        fh.close()
    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = None

    notes = f"mode=writing kind={kind}" + (f" post={post_id}" if post_id else "")
    return Run(
        id=run_id, name=f"writing-{kind}", runner="detached", status="running",
        domain=config.WORK, pid=proc.pid, pid_created=created,
        cwd=str(config.CHAT_CWD), cmd=[str(c) for c in cmd], log=str(log),
        model=config.WRITING_MODEL or None, notes=notes,
    )


def running(store: Store) -> list[Run]:
    return [r for r in store.runs()
            if r.status == "running" and "mode=writing" in (r.notes or "")]


def start_ideas(store: Store) -> Run:
    """Mine the week. One at a time: two miners would propose the same ideas."""
    if any("kind=ideas" in (r.notes or "") for r in running(store)):
        raise ValueError("an ideas run is already in flight")
    return _spawn(build_ideas_prompt(store), "ideas")


def start_draft(store: Store, post_id: str, note: str | None = None) -> tuple[Run, Post]:
    """Draft one post, or redraft it with a note. The post moves to `drafting`."""
    post = store.get_post(post_id)
    if post is None:
        raise ValueError(f"no post {post_id}")
    if post.status == "drafting":
        raise ValueError(f"{post.id} is already being drafted (run {post.draft_run_id or '?'})")
    if post.status == "posted":
        raise ValueError(f"{post.id} is posted; a redraft would rewrite history. "
                         "Set it back with: otto writing set " + post.id + " --status drafted")
    if note and note.strip():
        post.notes.append({"at": iso(utcnow()), "text": note.strip()})
    run = _spawn(build_draft_prompt(store, post), "draft", post.id)
    post.status = "drafting"
    post.draft_run_id = run.id
    post.error = None
    post.touch()
    store.upsert_post(post)
    return run, post


# ---------------------------------------------------------------------------
# harvest
# ---------------------------------------------------------------------------

def _post_from(idea: dict, run_id: str) -> Post | None:
    hook = " ".join(str(idea.get("hook") or "").split()).strip()
    if not hook:
        return None
    themes = [str(t).strip().lower() for t in (idea.get("themes") or [])
              if str(t).strip().lower() in THEMES]
    energy = str(idea.get("energy") or "").strip().lower()
    return Post(
        id=make_id(hook),
        hook=hook[:200],
        angle=" ".join(str(idea.get("angle") or "").split())[:600],
        stance=" ".join(str(idea.get("stance") or "").split())[:300] or None,
        pushback=" ".join(str(idea.get("pushback") or "").split())[:400] or None,
        question=" ".join(str(idea.get("question") or "").split())[:300] or None,
        themes=themes[:3],
        evidence=[str(e).strip()[:200] for e in (idea.get("evidence") or []) if str(e).strip()][:4],
        reveals=[str(r).strip()[:120] for r in (idea.get("reveals") or []) if str(r).strip()][:6],
        energy=energy if energy in ("low", "medium") else None,
        fingerprint=fingerprint(hook),
        origin_run_id=run_id,
    )


def _harvest_ideas(store: Store, run: Run, obj: dict) -> list[str]:
    raw = [i for i in (obj.get("ideas") or []) if isinstance(i, dict)]
    summary = " ".join(str(obj.get("summary") or "").split())[:200]
    existing = {p.fingerprint for p in store.posts() if p.fingerprint}
    filed: list[Post] = []
    dupes = 0
    for idea in raw:
        post = _post_from(idea, run.id)
        if post is None:
            continue
        if post.fingerprint in existing:
            dupes += 1
            continue
        if len(filed) >= config.WRITING_IDEAS_MAX:
            break
        existing.add(post.fingerprint)
        store.upsert_post(post)
        filed.append(post)

    notes = [f"writing ideas: {len(filed)} filed" + (f", {dupes} already on the list" if dupes else "")
             + (f" ({summary})" if summary else "")]
    if filed:
        body = "\n".join(f"{p.hook}" for p in filed)
        notify.post(store, f"{len(filed)} post idea{'s' if len(filed) != 1 else ''} from your week",
                    body=body, source="writing", command="otto writing",
                    key=f"writing:ideas:{run.id[:6]}", level="info")
    return notes


def _harvest_draft(store: Store, run: Run, obj: dict | None, error: str | None) -> list[str]:
    post = next((p for p in store.posts() if p.draft_run_id == run.id), None)
    if post is None:
        return [f"writing draft: run {run.id[:6]} matches no post"]
    if run.cost_usd:
        post.cost_usd = round(post.cost_usd + run.cost_usd, 4)

    text = str((obj or {}).get("draft") or "").replace("\r\n", "\n").strip() if obj else ""
    if error or not text:
        post.status = "drafted" if post.draft else "idea"
        post.error = error or "the draft came back empty"
        post.touch()
        store.upsert_post(post)
        return [f"writing draft for {post.id} failed: {post.error}"]

    if post.draft:
        post.versions.append({"at": iso(utcnow()), "draft": post.draft,
                              "run_id": post.draft_run_id})
        post.versions = post.versions[-10:]
    post.draft = text
    post.alt_hooks = [" ".join(str(h).split())[:200] for h in ((obj or {}).get("alt_hooks") or [])
                      if str(h).strip()][:4]
    generalized = [str(r).strip()[:160] for r in ((obj or {}).get("reveals") or []) if str(r).strip()]
    post.flags = scan(text)
    post.status = "drafted"
    post.edited = False
    post.error = None
    post.touch()
    store.upsert_post(post)

    n = len(post.flags)
    words = len(text.split())
    body = f"{words} words, " + (f"{n} thing{'s' if n != 1 else ''} to look at before it goes out"
                                 if n else "nothing flagged by the scan")
    if generalized:
        body += "\nGeneralized: " + "; ".join(generalized[:4])
    notify.post(store, f"Draft ready: {post.hook[:80]}", body=body, source="writing",
                command=f"otto writing show {post.id}", key=f"writing:draft:{post.id}",
                level="info")
    return [f"writing draft for {post.id}: {words} words, {n} flag(s)"]


def harvest(store: Store, run: Run) -> list[str]:
    """Turn a finished writing run into posts or a draft. Returns event-log notes."""
    if "mode=writing" not in (run.notes or ""):
        return []
    kind = "ideas" if "kind=ideas" in (run.notes or "") else "draft"

    result = detached._parse_result_json(run)
    error: str | None = None
    obj: dict | None = None
    if result is None:
        error = "no result from the session"
    elif result.get("is_error"):
        error = f"session failed: {result.get('subtype') or 'error'}"
    else:
        obj = extract_json(result.get("result") or "")
        if obj is None:
            error = "reply was not JSON"

    if kind == "draft":
        return _harvest_draft(store, run, obj, error)
    if error:
        return [f"writing ideas: {error}"]
    return _harvest_ideas(store, run, obj or {})


# ---------------------------------------------------------------------------
# edits
# ---------------------------------------------------------------------------

def set_status(store: Store, post_id: str, status: str | None = None, *,
               url: str | None = None, note: str | None = None,
               draft: str | None = None) -> Post:
    """The mutations the CLI and the dashboard are allowed to make.

    `posted` is the owner's confirmation and the only way a post gets there: nothing
    in harvest sets it. A draft edited by hand is re-scanned, so the flags on the
    card always describe the text on the card.
    """
    post = store.get_post(post_id)
    if post is None:
        raise ValueError(f"no post {post_id}")
    if status is not None:
        if status not in ("idea", "drafted", "posted", "dropped"):
            raise ValueError("status must be idea, drafted, posted, or dropped")
        if status == "drafted" and not post.draft:
            raise ValueError(f"{post.id} has no draft yet: otto writing draft {post.id}")
        if status == "posted":
            if not post.draft:
                raise ValueError(f"{post.id} has no draft to have posted")
            post.posted_at = iso(utcnow())
        post.status = status
    if url is not None:
        clean = url.strip() or None
        # The dashboard renders this as a link's href. Anything but http(s) (a
        # javascript: or data: URL) would run in the daemon's origin on click,
        # which is the one origin the browser guard trusts.
        if clean and not clean.lower().startswith(("https://", "http://")):
            raise ValueError("url must start with http:// or https://")
        post.url = clean
    if note and note.strip():
        post.notes.append({"at": iso(utcnow()), "text": note.strip()})
    if draft is not None:
        text = draft.strip()
        if text and text != (post.draft or ""):
            if post.draft:
                post.versions.append({"at": iso(utcnow()), "draft": post.draft, "run_id": None})
                post.versions = post.versions[-10:]
            post.draft = text
            post.flags = scan(text)
            post.edited = True
            if post.status == "idea":
                post.status = "drafted"
    post.touch()
    store.upsert_post(post)
    return post


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------

def counts(posts: list[Post]) -> dict[str, int]:
    out = {"idea": 0, "drafting": 0, "drafted": 0, "posted": 0, "dropped": 0}
    for p in posts:
        out[p.status] = out.get(p.status, 0) + 1
    return out


def summary(store: Store) -> dict[str, Any]:
    """The few numbers /api/state carries: enough for a rail badge, no more."""
    posts = store.posts()
    return {"counts": counts(posts), "running": len(running(store))}


def status(store: Store) -> dict[str, Any]:
    posts = store.posts()
    sched = next((s for s in store.schedules() if s.runner == "writing"), None)
    last_ideas = next((r for r in store.runs()
                       if "kind=ideas" in (r.notes or "") and r.status != "running"), None)
    return {
        "posts": [p.model_dump() for p in posts],
        "counts": counts(posts),
        "running": [{"id": r.id, "kind": "ideas" if "kind=ideas" in (r.notes or "") else "draft",
                     "post": (re.search(r"post=(\S+)", r.notes or "") or [None, None])[1],
                     "started": r.started}
                    for r in running(store)],
        "last_ideas": ({"at": last_ideas.ended or last_ideas.started, "status": last_ideas.status,
                        "cost_usd": last_ideas.cost_usd} if last_ideas else None),
        "schedule": ({"name": sched.name, "enabled": sched.enabled, "autostart": sched.autostart,
                      "cadence": sched.cadence.model_dump(), "last_run": sched.last_run,
                      "last_status": sched.last_status} if sched else None),
        "voice_path": str(config.WRITING_VOICE_PATH),
        "voice_set": config.WRITING_VOICE_PATH.exists()
                     and config.voice_text() != VOICE_SEED.strip(),
        "samples_path": str(config.WRITING_SAMPLES_PATH),
        "samples_set": bool(config.samples_text()),
        "themes": list(THEMES),
    }


def _age(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    s = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    if s < 5400:
        return f"{int(s / 60)}m ago"
    if s < 172800:
        return f"{s / 3600:.1f}h ago"
    return f"{s / 86400:.1f}d ago"


def render_one(post: Post) -> str:
    lines = [f"{post.id}  [{post.status}]  {post.hook}", ""]
    lines.append(f"  themes    {', '.join(post.themes) or '-'}"
                 + (f"   energy {post.energy}" if post.energy else ""))
    if post.stance:
        lines.append(f"  stance    {post.stance}")
    if post.pushback:
        lines.append(f"  pushback  {post.pushback}")
    if post.question:
        lines.append(f"  question  {post.question}")
    lines.append(f"  angle     {post.angle}")
    for e in post.evidence:
        lines.append(f"  evidence  {e}")
    for r in post.reveals:
        lines.append(f"  hides     {r}")
    if post.error:
        lines.append(f"  ERROR     {post.error}")
    if post.draft:
        lines += ["", "  DRAFT" + ("  (your edit, not yet sent back)" if post.edited else ""), ""]
        lines += [f"    {ln}" for ln in post.draft.splitlines()]
        lines.append("")
        lines.append(f"    ({len(post.draft.split())} words"
                     + (f", ${post.cost_usd:.2f} so far" if post.cost_usd else "") + ")")
        if post.alt_hooks:
            lines += ["", "  OTHER FIRST LINES"]
            lines += [f"    - {h}" for h in post.alt_hooks]
        lines += ["", "  BEFORE IT GOES OUT"]
        if post.flags:
            for f in post.flags:
                lines.append(f"    ! {f['kind']:<12} {f['match']:<28} {f['why']}")
        else:
            lines.append("    the scan found nothing. That is not the same as safe; read it once as a stranger.")
    if post.notes:
        lines += ["", "  YOUR NOTES"]
        lines += [f"    {n.get('at', '')[:10]}  {n.get('text', '')}" for n in post.notes]
    if post.versions:
        lines.append(f"\n  {len(post.versions)} earlier draft(s) kept")
    if post.url:
        lines.append(f"\n  posted    {(post.posted_at or '')[:10]}  {post.url}")
    lines += ["", "  otto writing edit " + post.id + "   |   otto writing draft " + post.id
              + (' --note "..."' if post.draft else "")
              + "   |   otto writing set " + post.id + " --status posted|dropped"]
    return "\n".join(lines)


def render(store: Store, show_dropped: bool = False) -> str:
    st = status(store)
    posts = [Post.model_validate(p) for p in st["posts"]]
    c = st["counts"]
    sched = st.get("schedule") or {}
    cad = sched.get("cadence") or {}
    when = (f"{cad.get('kind')} {','.join(cad.get('days') or [])} {cad.get('at')}".strip()
            if cad else "unscheduled")
    armed = "autostart on" if sched.get("autostart") and sched.get("enabled") else "autostart OFF"
    lines = [f"writing   ideas {when}, {armed}"
             + (f"   last ideas run {_age((st.get('last_ideas') or {}).get('at'))}"
                if st.get("last_ideas") else "   never mined")]
    lines.append(f"voice     {st['voice_path']}"
                 + ("" if st["voice_set"] else "   (still the seed; edit it, it is read every draft)"))
    lines.append(f"samples   {st['samples_path']}"
                 + ("" if st["samples_set"] else
                    "   (EMPTY: without your own writing in here every draft will sound like a model)"))
    for r in st["running"]:
        lines.append(f"running   {r['kind']}" + (f" for {r['post']}" if r.get("post") else "")
                     + f"  since {_age(r['started'])}")

    def section(title: str, items: list[Post]) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"{title} ({len(items)})")
        for p in items:
            themes = "/".join(p.themes) or "-"
            extra = ""
            if p.status == "drafted":
                n = len(p.flags)
                extra = f"  {n} flag{'s' if n != 1 else ''}" if n else "  no flags"
            elif p.status == "posted":
                extra = f"  {(p.posted_at or '')[:10]}" + (f"  {p.url}" if p.url else "")
            elif p.status == "idea" and p.energy == "low":
                extra = "  (quick)"
            lines.append(f"  {p.id}  {themes:<20} {p.hook[:78]}{extra}")
            if p.status == "idea":
                lines.append(f"            {(p.stance or p.angle)[:150]}")
                if p.pushback:
                    lines.append(f"            vs: {p.pushback[:140]}")

    section("DRAFTS", [p for p in posts if p.status in ("drafted", "drafting")])
    section("IDEAS", [p for p in posts if p.status == "idea"])
    section("POSTED", [p for p in posts if p.status == "posted"])
    if show_dropped:
        section("DROPPED", [p for p in posts if p.status == "dropped"])
    elif c.get("dropped"):
        lines.append(f"\n  {c['dropped']} dropped, --all to see them")
    if not any(p.status != "dropped" for p in posts):
        lines += ["", "  nothing yet. otto writing ideas mines the last "
                      f"{config.WRITING_LOOKBACK_DAYS} days for things worth saying."]
    return "\n".join(lines)
