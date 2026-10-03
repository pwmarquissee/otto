"""Per-person operational dossiers.

Stolen from Gaia's `context/people/`, with two deliberate differences, both of which
exist because Gaia's version of this file is the artifact that sat in a public repo
for twelve weeks:

  NOT IN A REPO   Dossiers live under OTTO_HOME (~/.claude/otto/people), which is not
                  a git repo and never has been. `sync` refuses to write if that ever
                  stops being true, because "we happen not to have run git init" is a
                  circumstance and not a control.
  OPERATIONAL     What goes in is what the owner needs to do the job: who they are, where
                  they are, what they run, what has broken before. What stays out is
                  anything evaluative or intimate -- compensation, performance,
                  health, personal circumstances. Gaia's dossiers blur that line;
                  Otto's must not, because the sole IT/security function keeping notes
                  on colleagues is only defensible while the notes are operational.

Two owners, one file. Okta owns the frontmatter and `sync` regenerates it. The owner owns
the prose body and `sync` never touches it -- same discipline as `put_day()` merging
rather than replacing, and for the same reason: a refresh that eats hand-written
context teaches you not to write any.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from . import config

PEOPLE_DIR = config.OTTO_HOME / "people"

# Okta profile fields worth carrying. Ordered as rendered.
OKTA_FIELDS: tuple[str, ...] = (
    "login", "email", "firstName", "lastName", "title", "department",
    "userType", "manager", "city", "state", "countryCode", "timezone",
    "startDate", "employeeNumber", "discordid", "mobilePhone",
)

# Deliberately NOT carried, with the reason, so nobody "helpfully" adds them back:
#   streetAddress / postalAddress / zipCode
#       Home addresses. Needed a few times a year for hardware shipping, and Okta
#       already holds them. Caching 60 home addresses for occasional need is a
#       standing risk in exchange for a lookup you can do on the day.
#   secondEmail, primaryPhone
#       Duplicates of email/mobilePhone in this tenant.
#   costCenter, division, organization, locale, preferredLanguage
#       Empty or constant for every user here; noise.
EXCLUDED_FIELDS = ("streetAddress", "postalAddress", "zipCode", "secondEmail",
                   "primaryPhone", "costCenter", "division", "organization")

BODY_MARKER = "<!-- Below is yours. `otto people sync` never touches it. -->"

# Section order is the reading order in the UI, and it puts the human half first
# deliberately: the operational facts are what Otto already knows, the relationship
# notes are what only the owner knows.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("Who they are", "Background, what they care about, what they are good at."),
    ("What we work on", "Recurring subjects across your DMs and shared projects."),
    ("Working style", "How they like to be approached. Comms preferences, meeting "
                      "tolerance, async vs live, timezone reality."),
    ("Relationship", "History with you. What has built trust, what has cost it, "
                     "where you stand."),
    ("Threads", "Open topics, things you owe them, what to pick up next time."),
    ("Devices", ""),
    ("Known issues", ""),
    ("Baselines", "Locations, hours, or auth patterns that look anomalous but are not."),
)

# Owner-written structured fields. These cannot live in the Okta frontmatter because
# `sync` regenerates that wholesale, so they sit in the body -- which sync never
# touches -- inside a comment Otto can still parse. `last_contact` is the one that
# makes this more than a filing cabinet: it is what a relationship nudge measures.
META_OPEN = "<!-- otto:meta"
META_CLOSE = "-->"
META_FIELDS = ("pronouns", "full_name", "monikers", "last_contact", "cadence",
               "circle", "slack_id")

# `pronouns` is first because it is the field that prevents a specific, avoidable
# harm. Okta carries no pronoun attribute, and a first name is not evidence: the
# seeded note for Fran Aisa said "her Okta profile" because a memory file said so and
# nothing in the pipeline knew better. Fran is a man. Recording it once, visibly, in
# the header chip means the next person writing about a colleague reads it before
# they write, rather than defaulting to a guess.
META_HINTS: dict[str, str] = {
    "pronouns": "he/him, she/her, they/them -- leave blank and Otto uses they/them",
    "full_name": "if they go by something shorter (Fran -> Francisco)",
    "monikers": "comma-separated other names they answer to, so a name Otto has "
                "not seen resolves to them instead of to nobody",
    "last_contact": "YYYY-MM-DD, last real conversation. DM exchanges advance "
                    "this automatically; hand-set it for calls and meetings",
    "cadence": "how often you want to reach out: weekly|monthly|quarterly",
    "circle": "inner|working|periphery -- how close this relationship needs to be",
    "slack_id": "U0... -- the Slack member id. Outreach REFUSES to send without it, "
                "because Slack will not accept an email as a message target and the "
                "alternative is letting a model guess an id",
}


def render_meta(meta: dict[str, str] | None = None) -> str:
    """The meta block, with guidance preserved.

    render_body round-trips every dossier, so if this dropped the trailing `# ...`
    hints then normalising a file once would silently strip the only documentation of
    what the fields mean.
    """
    meta = meta or {}
    lines = [META_OPEN]
    for f in META_FIELDS:
        val = meta.get(f, "")
        lines.append(f"{f}: {val}".ljust(22) + f"# {META_HINTS[f]}")
    lines.append(META_CLOSE)
    return "\n".join(lines)


_META_BLOCK = render_meta()


def _template_body() -> str:
    """Guidance lives in HTML comments, never in italic prose.

    The first version used `_italic hint_` lines, and they rendered in the dossier UI
    as if they were content -- two of the seven visible lines in a populated dossier
    were the template telling you what the section was for. A comment is invisible in
    the rendered view and still right there when editing the file.
    """
    parts = [BODY_MARKER, "", _META_BLOCK, ""]
    for heading, hint in SECTIONS:
        parts.append(f"## {heading}")
        if hint:
            parts.append(f"<!-- {hint} -->")
        parts.append("")
    return "\n".join(parts)


_TEMPLATE_BODY = _template_body()


class Person(NamedTuple):
    okta_id: str
    fields: dict[str, str]

    @property
    def login(self) -> str:
        return self.fields.get("login", "")

    @property
    def slug(self) -> str:
        """Filename stem: the login local-part, which is stable and unique here."""
        local = self.login.split("@")[0].lower()
        return re.sub(r"[^a-z0-9._-]", "-", local) or self.okta_id

    @property
    def display(self) -> str:
        name = " ".join(x for x in (self.fields.get("firstName"),
                                    self.fields.get("lastName")) if x)
        return name or self.login


# Non-human accounts, DECLARED not inferred. A dossier for `Reclaim Cal` is noise.
#
# An earlier version added "no department AND no userType" as a catch-all heuristic and
# it filtered out three external contractors and both partners at an outside studio:
# external contractors legitimately have neither field set, and they are precisely the
# people whose auth baselines matter most. So: explicit list plus two unambiguous
# prefixes, and nothing clever. This function deletes files; it does not get to guess.
SERVICE_LOGINS: frozenset[str] = frozenset({
    "ci-launcher", "infosec", "itsupport", "production",
    "svc-gitjira", "svc-okta", "svc-okta-ro", "svc-reclaim", "test02",
})
_SERVICE_PREFIXES = ("svc-", "svc_", "test")


def looks_like_service(fields: dict[str, str]) -> bool:
    local = fields.get("login", "").split("@")[0].casefold()
    return local in SERVICE_LOGINS or local.startswith(_SERVICE_PREFIXES)


def _fold_name(s: str) -> str:
    return _fold(s)


def _fold(s: str) -> str:
    """Accent-insensitive, case-insensitive key. `Gastón` must match `Gaston`."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c)).casefold().strip()


def parse_dump(path: Path) -> list[Person]:
    """Parse an `okta list_users` dump.

    The MCP returns each user as a two-element list: a Python-repr STRING of the
    profile dict, then the Okta id. So the outer envelope is JSON but the profiles
    are not -- `ast.literal_eval` handles them safely (no eval, literals only).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[Person] = []
    for item in raw.get("items", []):
        if not isinstance(item, list) or len(item) < 2:
            continue
        profile, okta_id = item[0], item[1]
        if isinstance(profile, str):
            try:
                profile = ast.literal_eval(profile)
            except (ValueError, SyntaxError):
                continue
        if not isinstance(profile, dict):
            continue
        fields = {k: str(v) for k, v in profile.items()
                  if k in OKTA_FIELDS and v not in (None, "")}
        if fields.get("login"):
            out.append(Person(str(okta_id), fields))
    return out


def _in_git_repo(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _split(text: str) -> str:
    """Return the hand-written body of an existing dossier, or '' if there is none."""
    idx = text.find(BODY_MARKER)
    return text[idx:] if idx >= 0 else ""


# Where a dossier's frontmatter comes from, and therefore who owns it.
#
#   okta   Regenerated by `sync` from an Okta dump. Do not hand-edit the frontmatter;
#          it will be overwritten on the next sync.
#          Body is the owner's and sync never touches it.
#   slack  An external person -- Slack Connect partner, vendor contact, anyone with no
#          Okta record. Frontmatter is written ONCE from their Slack profile and is
#          then the owner's to edit. `sync` must never touch these files at all: they are
#          not in any Okta dump, so a sync that "reconciled" the directory against
#          Okta would delete every external dossier.
SOURCE_OKTA = "okta"
SOURCE_SLACK = "slack"

# Slack profile fields carried on an external dossier. Deliberately narrower than the
# Okta set: a vendor contact's timezone and title are useful, their phone number is not
# ours to keep, and there is no manager or employee number to speak of.
SLACK_FIELDS: tuple[str, ...] = (
    "slack_id", "name", "email", "org", "title", "timezone", "first_seen",
)


def _frontmatter(p: Person) -> str:
    """Frontmatter only. No `# Name` heading: the UI renders the name from the
    frontmatter as the modal title, so emitting it again here showed it twice --
    once as the title and once as a heading directly beneath it."""
    lines = ["---", f"source: {SOURCE_OKTA}", f"okta_id: {p.okta_id}"]
    for f in OKTA_FIELDS:
        if f in p.fields:
            v = p.fields[f]
            lines.append(f"{f}: {json.dumps(v) if ':' in v or v.strip() != v else v}")
    lines += ["---", ""]
    return "\n".join(lines)


def _yaml_scalar(v: str) -> str:
    v = str(v)
    return json.dumps(v) if (":" in v or v.strip() != v or v.startswith(("#", "-"))) else v


def create_external(slug: str, fields: dict[str, str],
                    overwrite: bool = False) -> tuple[str, bool]:
    """Create (or refresh the frontmatter of) an external person's dossier.

    Returns (slug, created). By default an existing file is left completely alone --
    its frontmatter is the owner's once written, so a re-run of discovery must not quietly
    revert a title he corrected by hand.
    """
    repo = _in_git_repo(PEOPLE_DIR if PEOPLE_DIR.exists() else config.OTTO_HOME)
    if repo is not None:
        raise RuntimeError(
            f"refusing to write dossiers: {repo} is a git repo. These carry personal "
            f"data about third parties and must not be committable."
        )
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    path = PEOPLE_DIR / f"{slug}.md"
    exists = path.is_file()
    if exists and not overwrite:
        return slug, False

    lines = ["---", f"source: {SOURCE_SLACK}"]
    for f in SLACK_FIELDS:
        if fields.get(f):
            lines.append(f"{f}: {_yaml_scalar(fields[f])}")
    lines += ["---", ""]
    head = "\n".join(lines)

    if exists:
        old = path.read_text(encoding="utf-8")
        fm = re.match(r"^---\n.*?\n---\n?", old, re.S)
        body = old[fm.end():] if fm else _TEMPLATE_BODY
    else:
        body = _TEMPLATE_BODY
    path.write_text(head + body, encoding="utf-8")
    return slug, not exists


def external_slug(fields: dict[str, str]) -> str:
    """Filename stem for an external person.

    Prefixed `ext-` so the directory listing itself distinguishes an outside contact
    from a colleague, and so no external file can ever collide with an Okta login
    local-part. Falls back through email local-part, then name, then Slack id.
    """
    base = (fields.get("email", "").split("@")[0]
            or fields.get("name", "")
            or fields.get("slack_id", ""))
    base = re.sub(r"[^a-z0-9._-]+", "-", base.lower()).strip("-")
    return f"ext-{base or 'unknown'}"


def parse_meta(text: str) -> dict[str, str]:
    """Owner-written fields from the `otto:meta` comment block in the body.

    Trailing `# ...` guidance is stripped, so a field left at its template default
    reads as empty rather than as the help text.
    """
    i = text.find(META_OPEN)
    if i < 0:
        return {}
    j = text.find(META_CLOSE, i + len(META_OPEN))
    block = text[i + len(META_OPEN): j if j > 0 else len(text)]
    out: dict[str, str] = {}
    for line in block.splitlines():
        k, sep, v = line.partition(":")
        key = k.strip()
        if not sep or key not in META_FIELDS:
            continue
        val = v.split("#", 1)[0].strip()
        if val:
            out[key] = val
    return out


def pronouns_of(meta: dict[str, str]) -> str:
    """Never guess from a name. Unstated means they/them."""
    return meta.get("pronouns") or "they/them"


def monikers_of(meta: dict[str, str]) -> list[str]:
    """Other names this person answers to, in the order the owner wrote them.

    Asked for by Fran Aisa, who is "Fran" in Okta, "Francisco" on a contract, and
    "gazpachator" in Discord, and had to tell Otto so himself. A name is not one
    string per person, and the alternative to recording the others is that every
    consumer either guesses or asks the human -- which is the thing he was pointing
    at. Comma-separated because these are short tokens, and a list in a one-line
    comment block is a list of tokens or it is nothing.

    Deduplicated case-insensitively, first spelling wins, so a moniker that merely
    restates `full_name` does not show up twice on the card.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in str(meta.get("monikers", "")).split(","):
        name = " ".join(raw.split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def render(p: Person, existing: str = "") -> str:
    return _frontmatter(p) + (_split(existing) or _TEMPLATE_BODY)


class SyncResult(NamedTuple):
    created: list[str]
    updated: list[str]
    kept_bodies: int
    skipped_service: list[str] = []      # never silent: a wrong filter must be visible


def sync(dump: Path) -> SyncResult:
    """Regenerate frontmatter from an Okta dump, preserving every hand-written body."""
    repo = _in_git_repo(PEOPLE_DIR if PEOPLE_DIR.exists() else config.OTTO_HOME)
    if repo is not None:
        raise RuntimeError(
            f"refusing to write dossiers: {repo} is a git repo. These files carry "
            f"employee PII and must not be committable. Move OTTO_HOME outside the "
            f"repo, or remove {repo / '.git'}."
        )
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    kept = 0
    for p in parse_dump(dump):
        if looks_like_service(p.fields):
            skipped.append(p.slug)
            continue
        target = PEOPLE_DIR / f"{p.slug}.md"
        # Belt and braces. External dossiers use an `ext-` prefix so a collision should
        # be impossible, but sync regenerates frontmatter wholesale and getting this
        # wrong would silently overwrite a Slack-sourced profile with Okta fields.
        if target.is_file() and f"source: {SOURCE_SLACK}" in target.read_text(
                encoding="utf-8", errors="replace")[:400]:
            skipped.append(f"{p.slug} (slack-sourced, not touched)")
            continue
        old = target.read_text(encoding="utf-8") if target.is_file() else ""
        body = _split(old)
        if body:
            kept += 1
        new = render(p, old)
        if new != old:
            target.write_text(new, encoding="utf-8")
            (updated if old else created).append(p.slug)
    return SyncResult(sorted(created), sorted(updated), kept, sorted(skipped))


def parse_body(body: str) -> tuple[dict[str, list[str]], list[str]]:
    """Split a dossier body into {heading: content lines} plus provenance lines.

    Guidance comments, the marker, and the otto:meta block are dropped: they are
    regenerated, so carrying them through would duplicate them on every write.
    """
    sections: dict[str, list[str]] = {}
    prov: list[str] = []
    current: str | None = None
    in_meta = False
    for raw in body.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if in_meta:
            if META_CLOSE in s:
                in_meta = False
            continue
        if s.startswith(META_OPEN):
            in_meta = META_CLOSE not in s
            continue
        if s.startswith("<!--"):
            continue
        if s.startswith("_Seeded from"):
            prov.append(s)
            continue
        h = re.match(r"^#{1,4}\s+(.*)$", s)
        if h:
            current = h.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is None or not s:
            continue
        sections[current].append(line)
    return sections, prov


def render_body(sections: dict[str, list[str]], meta: dict[str, str],
                prov: list[str] | None = None) -> str:
    """Re-emit a body in canonical SECTIONS order.

    Headings absent from `sections` are emitted empty, which is how a dossier written
    before a new section existed picks it up without a separate migration.
    """
    parts = [BODY_MARKER, "", render_meta(meta), ""]
    for heading, hint in SECTIONS:
        parts.append(f"## {heading}")
        if hint:
            parts.append(f"<!-- {hint} -->")
        content = [c for c in sections.get(heading, []) if c.strip()]
        if content:
            parts.append("")
            parts.extend(content)
        parts.append("")
    # Content under a heading no longer in SECTIONS is kept, never silently dropped.
    for heading, content in sections.items():
        if heading in {h for h, _ in SECTIONS} or not [c for c in content if c.strip()]:
            continue
        parts += [f"## {heading}", ""] + content + [""]
    if prov:
        parts += prov + [""]
    return "\n".join(parts)


_BULLET = re.compile(r"^\s*[-*]\s*")


def _norm_bullet(s: str) -> str:
    return " ".join(_BULLET.sub("", s).lower().split())


class ApplyResult(NamedTuple):
    slug: str
    added: dict[str, int]
    skipped_duplicates: int
    last_contact: str | None


def apply_signals(slug: str, signals: dict[str, Any]) -> ApplyResult:
    """Merge extracted DM signals into one dossier. Additive and idempotent.

    Existing lines are never rewritten or reordered, and a bullet whose normalised
    text already appears under that heading is skipped, so re-running an extraction
    does not double the file.
    """
    path = PEOPLE_DIR / f"{slug}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no dossier at {path}")
    text = path.read_text(encoding="utf-8")
    fm = re.match(r"^---\n.*?\n---\n?", text, re.S)
    if not fm:
        raise ValueError(f"{slug}.md has no Okta frontmatter")
    head, body = fm.group(0).rstrip("\n") + "\n\n", text[fm.end():]

    sections, prov = parse_body(body)
    meta = parse_meta(body)

    mapping = (("What we work on", "topics"),
               ("Working style", "working_style"),
               ("Threads", "threads"))
    added: dict[str, int] = {}
    dupes = 0
    for heading, key in mapping:
        items = [str(x).strip() for x in (signals.get(key) or []) if str(x).strip()]
        if not items:
            continue
        existing = {_norm_bullet(l) for l in sections.get(heading, [])}
        fresh = []
        for it in items:
            if _norm_bullet(it) in existing:
                dupes += 1
                continue
            existing.add(_norm_bullet(it))
            fresh.append(f"- {it}")
        if fresh:
            sections.setdefault(heading, []).extend(fresh)
            added[heading] = len(fresh)

    lc = signals.get("last_contact")
    if lc and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(lc)):
        # Only ever move forward: a thinner later extraction must not walk the date back.
        if not meta.get("last_contact") or str(lc) > meta["last_contact"]:
            meta["last_contact"] = str(lc)

    path.write_text(head + render_body(sections, meta, prov), encoding="utf-8")
    return ApplyResult(slug, added, dupes, meta.get("last_contact"))


def _read_split(slug: str) -> tuple[Path, str, str]:
    """(path, frontmatter-with-trailing-blank, body). Raises if the file is not a dossier."""
    path = PEOPLE_DIR / f"{slug}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no dossier at {path}")
    if path.parent.resolve() != PEOPLE_DIR.resolve():
        raise ValueError("slug escapes the people directory")
    text = path.read_text(encoding="utf-8")
    fm = re.match(r"^---\n.*?\n---\n?", text, re.S)
    if not fm:
        raise ValueError(f"{slug}.md has no frontmatter")
    return path, fm.group(0).rstrip("\n") + "\n\n", text[fm.end():]


# Free text, because an enumeration would be the wrong shape here: these are the common
# cases offered as suggestions in the UI, not the permitted set.
PRONOUN_SUGGESTIONS = ("he/him", "she/her", "they/them", "she/they", "he/they")

# Dating a note is right for anything that describes a moment and wrong for anything
# that describes a person: "2026-08-04: grew up in Seattle" reads as nonsense, while an
# undated relationship note loses most of its value the week after you write it.
DATED_SECTIONS = frozenset({"Relationship", "Threads", "Known issues"})


def set_meta(slug: str, updates: dict[str, str]) -> dict[str, str]:
    """Write owner-written meta fields. Returns the resulting meta dict.

    An empty string clears a field, which is how you undo a wrong pronoun rather than
    being stuck with it.
    """
    unknown = sorted(set(updates) - set(META_FIELDS))
    if unknown:
        raise ValueError(f"not editable: {', '.join(unknown)}. "
                         f"Editable fields are {', '.join(META_FIELDS)}.")
    path, head, body = _read_split(slug)
    meta = parse_meta(body)
    for k, v in updates.items():
        v = " ".join(str(v).split())          # no newlines into a comment block
        if v:
            meta[k] = v
        else:
            meta.pop(k, None)
    sections, prov = parse_body(body)
    path.write_text(head + render_body(sections, meta, prov), encoding="utf-8")
    return meta


def add_note(slug: str, heading: str, text: str, *, date: str | None = None) -> int:
    """Append a note under `heading`. Returns the number of lines in that section after.

    Multi-line notes are preserved as written: parse_body collects every non-heading
    line and render_body re-emits them in order, so a paragraph survives the round-trip.
    """
    valid = {h for h, _ in SECTIONS}
    if heading not in valid:
        raise ValueError(f"unknown section {heading!r}. Valid: {', '.join(sorted(valid))}")
    text = text.strip()
    if not text:
        raise ValueError("empty note")

    path, head, body = _read_split(slug)
    sections, prov = parse_body(body)
    lines = text.splitlines()
    if heading in DATED_SECTIONS:
        from datetime import datetime as _d
        stamp = date or _d.now().astimezone().strftime("%Y-%m-%d")
        lines[0] = f"{stamp}: {lines[0]}"
    lines[0] = f"- {lines[0]}"
    sections.setdefault(heading, []).extend(lines)
    path.write_text(head + render_body(sections, parse_meta(body), prov), encoding="utf-8")
    return len(sections[heading])


def replace_note(slug: str, heading: str, old_text: str, new_text: str,
                 *, date: str | None = None) -> bool:
    """Rewrite one line in place. Returns False if no line matched.

    WHY THIS EXISTS AND `add_note` IS NOT ENOUGH. Threads go stale by date, and the
    date lives in the line. Appending "this is now resolved" leaves the original
    line untouched, so the thread keeps being reported as quiet forever and the
    dossier grows a second entry about the same thing. Observed on the first live
    triage run: Tim's Drive-folder thread was correctly marked complete and still
    showed as 21 days quiet, because both lines were now in the file.

    So resolving a thread is a REPLACE, not an append. The old text has to be
    matched exactly (modulo the leading bullet and date stamp the file adds), which
    is why callers pass the text they read rather than a line number: a dossier
    edited by hand between read and write must fail to match rather than overwrite
    whatever now sits at that index.
    """
    valid = {h for h, _ in SECTIONS}
    if heading not in valid:
        raise ValueError(f"unknown section {heading!r}. Valid: {', '.join(sorted(valid))}")
    new_text = new_text.strip()
    if not new_text:
        raise ValueError("empty note")

    path, head, body = _read_split(slug)
    sections, prov = parse_body(body)
    lines = sections.get(heading) or []

    def bare(line: str) -> str:
        s = str(line).lstrip("- ").strip()
        return re.sub(r"^\d{4}-\d\d-\d\d:\s*", "", s).strip()

    want = bare(old_text)
    idx = next((i for i, l in enumerate(lines) if bare(l) == want), None)
    if idx is None:
        return False

    if heading in DATED_SECTIONS:
        from datetime import datetime as _d
        stamp = date or _d.now().astimezone().strftime("%Y-%m-%d")
        lines[idx] = f"- {stamp}: {new_text}"
    else:
        lines[idx] = f"- {new_text}"
    sections[heading] = lines
    path.write_text(head + render_body(sections, parse_meta(body), prov), encoding="utf-8")
    return True


def load() -> list[dict[str, Any]]:
    """Every dossier's frontmatter, for consumers that want the roster as data."""
    out: list[dict[str, Any]] = []
    if not PEOPLE_DIR.is_dir():
        return out
    for f in sorted(PEOPLE_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not m:
            continue
        d: dict[str, Any] = {"slug": f.stem, "has_notes": _has_notes(text)}
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip():
                v = v.strip()
                d[k.strip()] = json.loads(v) if v.startswith('"') else v
        meta = parse_meta(text)
        d["meta"] = meta
        d["pronouns"] = pronouns_of(meta)
        d["pronouns_set"] = bool(meta.get("pronouns"))
        d["monikers"] = monikers_of(meta)
        d.setdefault("source", SOURCE_OKTA)
        d["external"] = d["source"] == SOURCE_SLACK
        # One display name for every consumer. Okta gives firstName/lastName; a Slack
        # profile gives a single `name`. Without this, every caller reinvents the
        # fallback chain and the UI shows a slug for external people.
        d["display_name"] = (
            meta.get("full_name")
            or " ".join(x for x in (d.get("firstName"), d.get("lastName")) if x)
            or d.get("name")
            or d["slug"]
        )
        # A name the owner recorded beats the Okta one: Okta has "Fran", the person is
        # Francisco, and the dossier is the only place that distinction is written down.
        if meta.get("full_name"):
            d["full_name"] = meta["full_name"]
        out.append(d)
    return out


def _norm(s: str) -> str:
    return " ".join(s.split())


def _has_notes(text: str) -> bool:
    """True if anything has been written into the body beyond the pristine template.

    Compares against the template rather than trying to classify lines. Two earlier
    attempts both failed on the same class of mistake: one regex with re.DOTALL matched
    across the whole body and deleted every real note before testing, and a line-based
    version treated the template's own two-line italic hint as content because neither
    line both starts and ends with an underscore. An exact diff against the template
    cannot misfire in either direction.
    """
    body = _split(text)
    if not body:
        return False
    # Provenance lines are machine-written, so they are not evidence of a human note.
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("_Seeded from")]
    return _norm("\n".join(lines)) != _norm(_TEMPLATE_BODY)


def get(login_or_slug: str) -> dict[str, Any] | None:
    key = login_or_slug.split("@")[0].lower()
    for d in load():
        if d.get("slug") == key or str(d.get("login", "")).split("@")[0].lower() == key:
            return d
    return None


# ---------------------------------------------------------------------------
# last_contact from DM direction facts
# ---------------------------------------------------------------------------
# Why this exists: `apply_signals` was built to carry last_contact and nothing was
# ever wired to call it, so every date in the directory was whatever the one-time
# seeding wrote. Observed 2026-08-24: Kit's dossier said 24 days without contact
# while the owner was in their DMs daily. A relationship field that silently stops being
# true is worse than no field, so the update is deterministic (no model, no
# judgement) and runs off facts the DM producer already collects.

def apply_contacts(contacts: list[dict[str, Any]],
                   window_days: int | None = None) -> list[str]:
    """Advance `last_contact` from the DM producer's per-conversation direction facts.

    Forward-only and idempotent, like apply_signals. A contact row counts only when
    BOTH directions exist and sit within CONTACT_EXCHANGE_WINDOW_DAYS of each other:
    an unanswered ping is not a conversation, and neither is a reply that lands weeks
    late. The recorded date is the newer side of the exchange, in local time.

    Matching is meta.slack_id first, then the peer's email against the dossier's
    email/login. A dossier matched by email with no slack_id recorded gets it
    backfilled -- outreach refuses to send without one, so every match here also
    removes a future refusal.
    """
    from datetime import datetime as _d

    window = (window_days if window_days is not None
              else config.CONTACT_EXCHANGE_WINDOW_DAYS) * 86400
    roster = load()
    by_sid: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    for d in roster:
        sid = (d.get("meta") or {}).get("slack_id")
        if sid:
            by_sid.setdefault(sid, d)
        for k in ("email", "login"):
            v = str(d.get(k) or "").casefold()
            if v:
                by_email.setdefault(v, d)

    notes: list[str] = []
    for c in contacts or []:
        try:
            t_in = float(c.get("last_inbound_ts") or 0)
            t_out = float(c.get("last_outbound_ts") or 0)
        except (TypeError, ValueError):
            continue
        if not t_in or not t_out or abs(t_in - t_out) > window:
            continue
        peer = str(c.get("peer") or "")
        d = by_sid.get(peer) or by_email.get(str(c.get("email") or "").casefold())
        if d is None:
            # An outside contact with no dossier is normal, not an error. External
            # dossiers are created by discovery, not by this path.
            continue
        when = _d.fromtimestamp(max(t_in, t_out)).astimezone().date().isoformat()
        meta = d.get("meta") or {}
        updates: dict[str, str] = {}
        if not meta.get("last_contact") or when > meta["last_contact"]:
            updates["last_contact"] = when
        if peer and not meta.get("slack_id"):
            updates["slack_id"] = peer
        if updates:
            set_meta(d["slug"], updates)
            meta.update(updates)  # the same peer twice in one batch stays forward-only
            notes.append(f"{d['slug']}: "
                         + ", ".join(f"{k}={v}" for k, v in sorted(updates.items())))
    return notes


# Gated on the spool file's mtime so a normal daemon tick costs one stat(). Module
# state rather than store state because re-processing after a restart is harmless:
# apply_contacts is forward-only, so the worst case is a no-op pass over one file.
_spool_seen_mtime: float = 0.0


def ingest_spool_contacts() -> list[str]:
    """Daemon tick step: apply the DM spool's contact facts once per new spool."""
    global _spool_seen_mtime
    path = config.SLACK_SPOOL_DIR / "new.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    if mtime <= _spool_seen_mtime:
        return []
    _spool_seen_mtime = mtime
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"spool unreadable for contact tracking: {e}"]
    return apply_contacts(data.get("contacts") or [])
