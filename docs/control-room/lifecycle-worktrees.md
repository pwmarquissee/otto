# herdr lifecycle, worktrees, migration

herdr (herdr.dev) is the harness every Claude session lives in; Otto is the logistics layer on top. This page covers how the server is kept alive, how a card gets its own branch, and how sessions that predate herdr get moved in.

## Lifecycle

- The daemon starts the herdr server in `lifespan()` when `OTTO_HERDR_AUTOSTART` is not `0` (default on). The outcome is one log line from source `herdr`; a missing or broken herdr never stops the daemon.
- `probe_herdr` in `otto/runners/external.py` is an Integration row like the EDR or AWS. Not installed reports `mode=unconfigured` (a warn, because a machine without herdr is a choice). Installed but not answering `api snapshot` reports `ok=False, mode=api`, which alerts and the board treat like any dead integration. Up reports the agent count as the metric.
- On every sync the herdr window title becomes `Otto · N idle · M working · K blocked` (`herdr terminal title set`), sent only when the counts change. `OTTO_HERDR_WINDOW_TITLE=0` turns it off.

## Worktrees

A herdr worktree is a workspace with git provenance: `herdr worktree create` runs `git worktree add`, opens the checkout as a workspace grouped under the repo's workspace, and returns the same workspace and root-pane ids `workspace create` does. Otto then launches claude into that pane exactly as `otto herdr open` does (`herdr.launch_claude_in_pane`, the piece `start_claude` was split around).

- `otto herdr worktree list [path]`: the repo's worktrees and which are open in herdr.
- `otto herdr worktree create <repo> <branch> [--base REF] [--label] [--name] [--no-claude]`: an existing local branch is checked out, a new one is created from `--base` or HEAD.
- `otto herdr worktree open <repo> --branch NAME | --path PATH [--no-claude]`: an existing worktree.
- API: `GET /api/herdr/worktrees?cwd=`, `POST /api/herdr/worktree/create` and `/open` with `{"cwd","branch","base","path","label","name","start_claude"}`, answering `{"ok","workspace_id","pane_id","agent"}`. A worktree that opened but whose claude did not come up is a 409 that names the ids, so the workspace is not lost.

## Migration

- `otto herdr adopt --all [--limit 10]`: every offline session whose directory still exists, newest first, resumed into its own pane with `claude --resume`. Sessions herdr already holds (matched by session id) are skipped, as are live ones, which would otherwise be open twice.
- `otto herdr open --all-repos`: one workspace per repo Otto knows (`/api/repos`, derived from what the registry, tasks and runs touched) that has no pane in it yet, matched by pane cwd. The agent is named from the directory, sanitized to `[a-z][a-z0-9_-]{0,31}`.

Both print each action and each skip with its reason, and exit 1 if any open failed.
