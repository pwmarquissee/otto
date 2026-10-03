# CONFORMANCE: launch

## How this runs

```
python -m otto skills audit --verify launch
```

## Assertions

<!-- otto-conformance
must-contain: def autorun_blocked                      # the unattended gate still exists as a single chokepoint
must-contain: config.SCHEDULE_AUTORUN                  # autorun master switch is consulted
must-contain: config.SCHEDULE_MAX_FAILURES             # circuit breaker on consecutive failures
must-contain: config.SCHEDULE_MAX_CONCURRENT           # autorun concurrency cap
must-contain: config.SCHEDULE_MIN_GAP_MINUTES          # relaunch floor, independent of cadence
must-contain: /sched\.autostart/                       # only explicitly armed schedules may autorun
must-contain: /disabled_reason/                        # a tripped breaker still refuses
must-contain: def active_for                           # double-click cannot double-launch
must-contain: def is_slash                             # slash vs shell discrimination still exists
must-contain: /if autorun else None/                   # unattended runs get a budget ceiling, watched ones do not
must-not-contain: /autostart\s*=\s*True/               # launch must never arm a schedule for itself
file-exists: claude/hardening/global/otto-module/launch/HARDENING.md
file-exists: otto/refresh.py                           # refresh is the ONLY other auto-launching runner; if it vanished the "list of one" claim needs re-checking
-->

## Not covered by these assertions

They prove each refusal is still referenced, not that `autorun_blocked()` is actually
called on every autorun path, a new caller that skipped it would pass every assertion
here. They also say nothing about whether a launched command behaved, or whether
`SCHEDULE_AUTORUN` is currently on; `otto autorun` reports that.
