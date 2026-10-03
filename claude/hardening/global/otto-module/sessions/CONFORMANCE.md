# CONFORMANCE: sessions

Two safety arguments to keep honest. The install must stay marker-scoped and
additive, because the file it edits holds the owner's permission grants and other
tools' hooks. The hook must stay incapable of failing a turn, because it runs inside
every turn on the machine.

## How this runs

```
python -m otto skills audit --verify sessions
python scripts\sessions_conformance.py
```

The audit checks that the guarantees are still *stated in the source*. The
conformance harness checks that they still *hold*, including running the hook as a
real subprocess against a dead daemon. Both, not either.

## Assertions

<!-- otto-conformance
must-contain: /HOOK_MARKER\s*=/                          # removals stay scoped to a named marker
must-contain: /def _is_ours/                             # and go through one gate
must-contain: /if not _is_ours/                          # the merge only ever drops its own entries
must-contain: /"permission_prompt"/                      # Notification stays filtered to real permission prompts
must-contain: /\("StopFailure", "", "idle"\)/            # an errored turn cannot stick at busy
must-not-contain: /\("SubagentStop"/                     # fires mid-turn; would report a working session as idle
must-contain: refusing to touch it                       # an unparseable settings file is refused, never replaced
must-contain: /GLOBAL_SETTINGS = config\.CLAUDE_DIR \/ "settings\.json"/   # the ONLY user-level file Claude Code loads hooks from
must-contain: does NOT load hooks from                   # the finding that forced it is recorded next to the choice
must-contain: /def uninstall/                            # the install is undoable, entry by entry
must-contain: /offline_inferred/                         # an inferred offline is labelled as inferred
must-contain: /def sweep/                                # the silence sweep exists
must-contain: is deliberately NOT rewritten              # a stuck busy is reported, not resolved into a death
must-contain: /session_lock/                             # session writes do not queue behind unrelated state
must-contain: /raise ValueError\("hook payload carried no session_id"\)/   # no anonymous rows
file-exists: scripts/otto_hook.py
file-exists: scripts/sessions_conformance.py
file-exists: claude/hardening/global/otto-module/sessions/HARDENING.md
-->

## Assertions on the hook itself

The audit evaluates directives against one file, the surface's declared `source`, so
it cannot reach `scripts/otto_hook.py`. That file is half of this surface: it is what
actually runs inside every turn. Its guarantees are asserted in
`scripts/sessions_conformance.py` section 2c instead, which reads it directly, and
section 2 additionally executes it as a real subprocess against a dead daemon.

Run both. Neither alone covers this surface.

## Not covered by these assertions

- They prove the merge is *written* to be marker-scoped and additive. That it
  actually preserves a real foreign hook is asserted by `sessions_conformance.py`
  section 1, against a fixture shaped like a real shared `settings.json`.
- The audit reads `otto/sessions.py` only. A change to `scripts/otto_hook.py` alone
  would pass `--verify sessions` completely. This is a limitation of one-file
  surfaces in `hardening.py`, not a judgment that the hook matters less.
- They cannot prove Claude Code still sends the events in `HOOK_MAP`, still filters
  `Notification` on `permission_prompt`, or still runs hooks through a POSIX shell on
  Windows. All three are upstream behavior, verified by observation rather than by
  contract. If a Claude Code release changes any of them, every assertion here still
  passes and the panel quietly goes wrong. `otto sessions doctor` and the
  `gap:session-hooks-silent` detector exist for precisely that failure.
- The `~64ms` per-invocation cost is measured, not asserted. A regression that made
  the hook slow would pass every check here.
- Nothing here proves the daemon's `/api/sessions/{state}` endpoint stays cheap. It is
  on the per-turn path and that is a property of `daemon.py`, guarded only by the
  comment on the route.
