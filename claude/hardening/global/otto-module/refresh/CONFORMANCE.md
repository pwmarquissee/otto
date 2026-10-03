# CONFORMANCE: refresh

The deny list is the entire safety argument for autostarting a skip-permissions
session on a timer. These assertions exist to make its erosion loud.

## How this runs

```
python -m otto skills audit --verify refresh
```

## Assertions

<!-- otto-conformance
must-contain: /DENIED_TOOLS\s*=/                       # the deny list still exists as a named constant
must-contain: /["']Edit["']/                           # Edit denied
must-contain: /["']Write["']/                          # Write denied
must-contain: /["']NotebookEdit["']/                   # NotebookEdit denied
must-contain: /["']Task["']/                           # Task denied: the refresher cannot fan out into more sessions
must-contain: /["']KillShell["']/                      # KillShell denied
must-contain: --disallowed-tools                       # the deny list is actually passed to the process, not just declared
must-contain: /\.get\(domain, \{\}\)\.get\("deny"\)/   # per-domain extra denies are still unioned in
must-contain: Do not send, draft, reply to, label, archive or delete anything   # the read-only instruction survives in the prompt
must-contain: Never invent an item                     # the no-fabrication rule, which protects otto next rather than the mailbox
must-contain: unavailable                              # a dead connector must report itself rather than produce an empty-looking success
must-contain: /if domain not in config\.DOMAINS/       # unknown domain raises instead of defaulting
must-contain: Otto will not guess which connector      # an unconfigured domain raises rather than picking one
file-exists: claude/hardening/global/otto-module/refresh/HARDENING.md
-->

## Not covered by these assertions

- They prove the deny list is *declared and passed*. They cannot prove Claude Code
  honoured it, nor that the connector itself is read-only scoped.
- They cannot detect a mutating tool that was never on the list because it did not
  exist when the list was written. A new mail-mutating MCP tool would be permitted and
  every assertion here would still pass. That is the standing weakness of a deny list
  over an allow list, and it is the thing to revisit if this ever grows.
- Snapshot freshness is not checked here; that is `otto probe` / `otto next`.
