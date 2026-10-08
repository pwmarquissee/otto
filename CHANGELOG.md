# Changelog

## 0.1.0 (2026-10-07)

First tagged release. A local daemon and dashboard for running a lot of Claude Code
sessions at once: a board with a promotion gate, a scheduler with staleness alarms,
six Claude Code hooks, a cost ledger, and dispatch into herdr panes.

What changed since the first public commit, in response to an outside review:

- `pip install -e .` gives an `otto` console script; `requirements.lock` pins the
  exact set CI installs; the PowerShell shim is gone.
- `OTTO_SCOPE=core` (the default) loads the orchestrator only; `assistant` adds the
  owner's modules (outreach, writing, people, prep, wellbeing, summon, inbox,
  meetings). The import graph matches the README's claim.
- `otto/launcher.py` writes every per-run script, with a PowerShell and a bash
  backend; CI gates on Linux as well as Windows.
- Icons are vendored, so a page load leaves nothing on loopback; unset
  `OTTO_INTEGRATIONS` means no probes.
- `config.py` is a re-resolvable settings object: a settings write applies live,
  and only the keys bound at start (`config.RESTART_KEYS`) need a restart.
- `otto/daemon.py` holds the process; routes live in `otto/api/`, the CLI in
  `otto/cli/`, the dashboard in `otto/web/js/`. Tests pin the route table, the verb
  order, and the dashboard's script order.
- A landed board edit never returns 500 for a failed log line; run results keep up
  to 20k characters so a PREPARE proposal arrives whole; `otto task show`.
- The README says what the guard hook is: a seatbelt that catches mistakes, with
  the bypass list in SECURITY.md.
