# CONFORMANCE: dispatch

## How this runs

```
python -m otto skills audit --verify dispatch
```

Directives are evaluated in pure Python against `otto/dispatch.py`. Nothing is
executed.

## Assertions

<!-- otto-conformance
must-contain: config.TASK_AUTODISPATCH                 # the master switch is still consulted, in both eligible() and dispatch()
must-contain: config.TASK_MAX_CONCURRENT               # the concurrency cap is still enforced
must-contain: config.TASK_MAX_ATTEMPTS                 # the attempts guard, which is what caught the parked task
must-contain: config.TASK_MIN_SECONDS_BETWEEN          # the throttle between autodispatches
must-contain: config.TASK_MAX_BUDGET_USD               # the ordinary per-task spend ceiling is still consulted
must-contain: config.DEEP_BUDGET_USD                   # the deep tier carries its own ceiling, so no dispatch path is uncapped
must-contain: /budget_usd\s*=\s*budget_usd/            # whichever ceiling model_for() picked still reaches the spawn
must-contain: /not t\.auto/                            # auto=False still parks a task in queued without running it
must-contain: permissions=level                        # the level is chosen (permissions_for: prepare=plan, else yolo), never inherited; HARDENING describes both
must-contain: /needs-you/                              # a failed run still parks for a human
must-not-contain: /status\s*=\s*["']queued["']/        # settle() must never write queued back, which would re-dispatch on the next tick
must-not-contain: /for\s+attempt\s+in\s+range/         # no retry loop, in any form
file-exists: claude/hardening/global/otto-module/dispatch/HARDENING.md   # the pair stays a pair
-->

## Not covered by these assertions

These prove the guard *names* are still referenced in the source. They do not prove
the guards are wired in the right order, that `dispatch_due()` is still the only tick
entry point, or that a spawned session honours anything at all. In particular:

- A guard could be referenced inside dead code and still pass.
- Nothing here checks the runtime value of `OTTO_AUTODISPATCH`. `otto autodispatch`
  reports that; a green conformance run says nothing about whether dispatch is
  currently armed.
- The blast radius of what a spawned session does is bounded by that definition's own
  HARDENING.md, not by anything checkable from here.
- The two budget assertions prove both ceilings are named, not that each path gets the
  right one. `model_for()` handing the deep budget to ordinary tasks would pass.
