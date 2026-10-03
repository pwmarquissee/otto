# CONFORMANCE: /orchestrate

## How this runs

```
python -m otto skills audit --verify orchestrate
```

Assertions are evaluated against `claude/commands/orchestrate.md`.

## Assertions

<!-- otto-conformance
must-contain: tier-0-autonomous                        # the three tier names are still the gating vocabulary
must-contain: tier-1-approval
must-contain: tier-2-assistive
must-contain: triage promote                           # promotion goes through the gate, never a bare task mv
must-contain: Never promote                            # tier-2 is still explicitly not promotable
must-contain: treat it as tier-1 and ask               # unknown tier fails CLOSED, which is the whole safety property
must-contain: Guessing low is the expensive mistake    # the reason, kept next to the rule
must-contain: Three at a time                          # batch cap, so the first failure is still reactable
must-contain: dangerously-skip-permissions             # the definition still states what queued actually means
must-contain: does **not** retry                       # a failed task parks for a human
must-contain: /otto autodispatch/                      # the pre-flight that stops silent pile-up in queued
must-contain: Never promote a task you have not read   # no blind promotion
must-contain: OTTO_SLACK_CHANNEL_ID                    # the reporting channel comes from config, not a name lookup at run time
must-contain: /no other task\s+system/                 # the board is the only task store; nothing reads state from a wiki
must-not-contain: /query_data_sources/                 # no reading board state out of a wiki database
file-exists: otto/dispatch.py                          # the thing promotion triggers; if it moved, this doc's claims need re-reading
file-exists: claude/hardening/global/command/orchestrate/HARDENING.md
-->

## Not covered by these assertions

- They prove the *instructions* still say the right thing. They cannot prove a given
  run obeyed them. Whether a tier-1 task was actually promoted is visible only on the
  board: `otto task ls --json` plus the run history.
- They cannot check that a task's declared tier matches its real risk. A task
  mislabelled tier-0 at creation passes every gate here.
- The batch cap is asserted as text, not enforced anywhere in code. If that matters
  more than it currently does, it belongs in `dispatch.py`, not in prose.
