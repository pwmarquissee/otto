"""Assistant scope: post ideas from the week and drafts in the owner's voice."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

from .. import config, persona
from ..store import Store
from ..client import Client
from ._fmt import C_DIM, C_RED, C_YEL, _c
from .runs import _wait_run


def cmd_writing(args, client: Client) -> int:
    """Post ideas from the week, drafts in the owner's voice, and what went out.

    Reads are local so the list works with the daemon down. Everything that
    starts a session or changes a post goes through the daemon.
    """
    from .. import writing as _w

    sub = args.writing_cmd

    if sub == "voice":
        p = _w.ensure_voice()
        print(f"  rules    {p}")
        if config.voice_text() == _w.VOICE_SEED.strip():
            print(_c("           still the seed. Otto reads this file before every draft; make it yours.", C_DIM))
        print(f"  samples  {config.WRITING_SAMPLES_PATH}")
        if not config.samples_text():
            print(_c("           empty. Paste in things you wrote and were happy with. This file, not the "
                     "rules, is what makes a draft sound like you.", C_DIM))
        return 0

    if sub == "ideas":
        try:
            res = client.writing_ideas()
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        run = res.get("run") or {}
        print(f"  mining the last {config.WRITING_LOOKBACK_DAYS} days for post ideas "
              f"(run {persona.short(run.get('id', ''))}, pid {run.get('pid')})")
        if args.no_wait:
            print(_c("  check back with: otto writing", C_DIM))
            return 0
        if not _wait_run(client, run["id"], args.timeout):
            print(_c(f"  still running after {args.timeout}s. Check: otto writing", C_DIM))
            return 0
        print()
        print(_w.render(Store()))
        return 0

    if sub == "edit":
        # Round-trip the draft through an editor, then store it as HIS text. The
        # next `draft` run is told the previous draft was his and keeps his changes.
        import tempfile

        post = Store().get_post(args.id)
        if post is None:
            print(f"  no post {args.id}", file=sys.stderr)
            return 1
        if not post.draft:
            print(_c(f"  {post.id} has no draft yet: otto writing draft {post.id}", C_YEL),
                  file=sys.stderr)
            return 1
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "notepad"
        fd, tmp = tempfile.mkstemp(prefix=f"otto-{post.id}-", suffix=".md")
        os.close(fd)
        Path(tmp).write_text(post.draft + "\n", encoding="utf-8")
        try:
            subprocess.run([*editor.split(), tmp], check=False)
            text = Path(tmp).read_text(encoding="utf-8").strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        if not text or text == post.draft.strip():
            print(_c("  unchanged", C_DIM))
            return 0
        try:
            updated = client.patch_post(post.id, draft=text)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        n = len(updated.get("flags") or [])
        print(f"  {updated['id']} saved as your edit, {n} flag(s)")
        print(_c(f"  send it back for another pass: otto writing draft {updated['id']}"
                 " --note \"...\"", C_DIM))
        return 0

    if sub == "draft":
        if args.from_file:
            # His edited text becomes the current draft first, so the run sees it
            # as his and builds on it rather than on whatever it wrote last time.
            try:
                text = Path(args.from_file).read_text(encoding="utf-8")
            except OSError as e:
                print(_c(f"  could not read {args.from_file}: {e}", C_RED), file=sys.stderr)
                return 2
            try:
                client.patch_post(args.id, draft=text)
            except RuntimeError as e:
                print(_c(f"  {e}", C_YEL), file=sys.stderr)
                return 1
        try:
            res = client.writing_draft(args.id, args.note)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        run, post = res.get("run") or {}, res.get("post") or {}
        verb = "redrafting" if post.get("versions") or post.get("draft") else "drafting"
        print(f"  {verb} {post.get('id')}: {post.get('hook', '')[:70]}")
        print(_c(f"  run {persona.short(run.get('id', ''))}, model {run.get('model') or 'default'}", C_DIM))
        if args.no_wait:
            print(_c(f"  check back with: otto writing show {post.get('id')}", C_DIM))
            return 0
        if not _wait_run(client, run["id"], args.timeout):
            print(_c(f"  still running after {args.timeout}s. Check: otto writing show {post.get('id')}", C_DIM))
            return 0
        fresh = Store().get_post(post["id"])
        if fresh is None:
            print(_c("  the post vanished mid-draft", C_RED), file=sys.stderr)
            return 1
        print()
        print(_w.render_one(fresh))
        return 0 if fresh.status == "drafted" and not fresh.error else 1

    if sub == "show":
        post = Store().get_post(args.id)
        if post is None:
            print(f"  no post {args.id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(post.model_dump(), indent=2))
        else:
            print(_w.render_one(post))
        return 0

    if sub == "set":
        payload: dict = {}
        if args.status:
            payload["status"] = args.status
        if args.url is not None:
            payload["url"] = args.url
        if args.note:
            payload["note"] = " ".join(args.note)
        if args.draft_file:
            try:
                payload["draft"] = Path(args.draft_file).read_text(encoding="utf-8")
            except OSError as e:
                print(_c(f"  could not read {args.draft_file}: {e}", C_RED), file=sys.stderr)
                return 2
        if not payload:
            print(_c("  nothing to change: --status, --url, --note, or --draft-file", C_YEL),
                  file=sys.stderr)
            return 2
        try:
            post = client.patch_post(args.id, **payload)
        except RuntimeError as e:
            print(_c(f"  {e}", C_YEL), file=sys.stderr)
            return 1
        print(f"  {post['id']} is {post['status']}"
              + (f", {len(post.get('flags') or [])} flag(s) on the draft" if post.get("draft") else ""))
        if post["status"] == "posted" and not post.get("url"):
            print(_c(f"  no link recorded. otto writing set {post['id']} --url <post url>", C_DIM))
        return 0

    # default: the list
    if args.json:
        print(json.dumps(_w.status(Store()), indent=2))
        return 0
    print(_w.render(Store(), show_dropped=args.all))
    return 0


def add_writing(sub) -> None:
    s = sub.add_parser("writing", help="post ideas from your week, drafts in your voice")
    s.add_argument("--all", action="store_true", help="include dropped ideas")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_writing, writing_cmd=None)
    wr_sub = s.add_subparsers(dest="writing_cmd")

    a = wr_sub.add_parser("ideas", help="mine the last week for post ideas now")
    a.add_argument("--no-wait", action="store_true")
    a.add_argument("--timeout", type=int, default=240)
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("draft", help="draft a post from an idea, or redraft it with a note")
    a.add_argument("id")
    a.add_argument("--note", help="what to change, in your words")
    a.add_argument("--from-file", help="your edited draft; it becomes the text the run revises")
    a.add_argument("--no-wait", action="store_true")
    a.add_argument("--timeout", type=int, default=240)
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("edit", help="open the draft in $EDITOR (or notepad); saved as your edit")
    a.add_argument("id")
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("show", help="one post in full, with what the scan flagged")
    a.add_argument("id")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_writing, all=False)

    a = wr_sub.add_parser("set", help="mark it posted or dropped, record the link, add a note")
    a.add_argument("id")
    a.add_argument("--status", choices=["idea", "drafted", "posted", "dropped"])
    a.add_argument("--url")
    a.add_argument("--note", nargs="+")
    a.add_argument("--draft-file", help="replace the draft with this file's text (re-scanned)")
    a.set_defaults(fn=cmd_writing, all=False, json=False)

    a = wr_sub.add_parser("voice", help="where your voice file is; seeds it if missing")
    a.set_defaults(fn=cmd_writing, all=False, json=False)


PARSERS = {
    "writing": add_writing,
}
