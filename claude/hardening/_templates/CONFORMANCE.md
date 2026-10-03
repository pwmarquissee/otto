# CONFORMANCE: <definition name>

> Template. Copy to the definition's doc directory and replace the placeholders.
> Delete this blockquote.
>
> This file is checks, not prose. Its job is to fail when the definition drifts away
> from what its `HARDENING.md` claims, because a stale HARDENING.md is a false
> assurance and worse than none. Prose belongs in HARDENING.md; put nothing here that
> a machine cannot decide.

## How this runs

```
python -m otto skills audit --verify <name>
```

Every directive inside an `otto-conformance` block is evaluated in pure Python
against the definition source. Nothing is executed, that is deliberate, since a doc
format that ran shell commands would make each of these files a new dispatch surface.

Directives:

| Directive | Argument | Passes when |
|---|---|---|
| `must-contain` | literal, or `/regex/` | the definition source contains it |
| `must-not-contain` | literal, or `/regex/` | it does not |
| `file-exists` | repo-relative path | the path exists |
| `file-absent` | repo-relative path | it does not |

Everything after ` # ` on a directive line is a note explaining what the assertion
protects. Write one for every directive; an assertion nobody can explain is an
assertion nobody will fix correctly when it fails.

## Assertions

<!-- otto-conformance
must-contain: <literal>             # <the invariant this protects>
must-not-contain: /<regex>/         # <the mistake this catches>
file-exists: <repo/relative/path>   # <the dependency this proves>
-->

## Not covered by these assertions

<What a passing run does NOT prove. Be honest here: text assertions verify that the
definition still SAYS the right thing, which is not the same as verifying it DOES the
right thing. Naming the gap stops a green check from being read as more than it is.>
