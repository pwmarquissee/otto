# CONFORMANCE: meetings

The deny list is the entire safety argument for autostarting a skip-permissions session
on a timer, and here the credential behind it is workspace-wide read/write with no
scope-level backstop. These assertions exist to make its erosion loud.

## How this runs

```
python -m otto skills audit --verify meetings
```

Assertions are evaluated against `otto/meetings.py`.

## Assertions

<!-- otto-conformance
must-contain: /DENIED_TOOLS\s*=/                       # the deny list still exists as a named constant
must-contain: --disallowed-tools                       # and is actually passed to the process, not merely declared
must-contain: /["']Edit["']/                           # Edit denied
must-contain: /["']Write["']/                          # Write denied
must-contain: /["']NotebookEdit["']/                   # NotebookEdit denied
must-contain: /["']Task["']/                           # Task denied: the ingester cannot fan out into more sessions
must-contain: /["']KillShell["']/                      # KillShell denied
must-contain: /["']Bash["']/                           # Bash denied: no shell, so no way around any of the above
must-contain: /["']WebFetch["']/                       # egress denied. Meeting notes carry unreleased and personnel detail
must-contain: /["']CronCreate["']/                     # a read-only job that can schedule work is not read-only
must-contain: notion-create-pages                      # the Notion write list is still present
must-contain: notion-update-page
must-contain: notion-create-comment
must-contain: notion-move-pages
must-contain: /mcp__notion__/                          # denied under the Claude Code server name
must-contain: /mcp__claude_ai_Notion__/                # and under the account-level one, since the registries drift
must-not-contain: /["']--allowed-tools["']/            # never PASSED to the process: measured not to restrict under skip-permissions, and a control that does not control is worse than none. Prose about why is fine, which is why this matches the quoted argument rather than the bare word
must-contain: --max-budget-usd                         # an unattended schedule has a ceiling it cannot exceed
must-contain: /auto=config\.MEETINGS_AUTOQUEUE/        # cards do not self-dispatch; promotion stays with /orchestrate
must-contain: status="backlog"                         # and they land in backlog, never queued
must-contain: Never invent an action item              # the no-fabrication rule, which protects the owner's commitments rather than the notes
must-contain: unavailable                              # a dead connector reports itself rather than looking like an empty success
must-contain: /if summary\.startswith\("unavailable"\)/ # and that report must NOT roll the watermark forward
must-contain: /def _page_key/                          # ids are normalized before anything is keyed on them
must-contain: /def _valid_due/                         # a due date is validated, never taken on trust
file-exists: claude/hardening/global/otto-module/meetings/HARDENING.md
file-exists: otto/refresh.py                           # _extract is reused from there; if it moved, this module's JSON recovery is gone
-->

## Not covered by these assertions

- They prove the deny list is *declared and passed*. They cannot prove Claude Code
  honoured it. That was verified empirically instead, once, by reading the session's own
  `init` event: 249 tools without the list, 94 with it, and exactly nine Notion tools
  left, all reads. Re-run that check after any Claude Code upgrade, it is the only
  thing that actually proves the control works, and it is not automated.
- They cannot detect a Notion write tool that did not exist when the list was written.
  That is the standing weakness of a deny list, and here it is not mitigated by scope:
  the connector token can write the whole workspace. `--allowed-tools` would have fixed
  this and was measured not to work under skip-permissions.
- They cannot check that an extracted action item faithfully reflects what was said in
  the meeting. The verbatim `quote` on every card exists so a human can check that
  themselves; nothing here can.
- They cannot prove a due date is the one the meeting stated, only that it parsed and is
  not earlier than its own meeting.
