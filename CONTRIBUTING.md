# Contributing

Otto is a single-operator tool. Changes that make it multi-user or hosted are out of
scope. A new integration must stay inert by default. With nothing configured, Otto
runs on loopback and talks to nothing.

## Before a pull request

```
python -m pytest
python scripts/oss_scan.py
```

Both must pass, and the PR includes the pytest output. `oss_scan.py` fails on
anything that looks like private data (non-example email addresses, account numbers,
chat ids, home directory paths, credential shapes). If it flags a placeholder, change
the placeholder to an allowed form instead of adding an exception.

## Code floor

- No new suppression comments (`# noqa`, `# type: ignore`, `eslint-disable`,
  `@ts-ignore`, `//nolint`, `#[allow]`) without the reason on the same line.
- No skipping, deleting, or loosening a test to get to green. Fix the test and say
  why in the commit message.
- No stubs on a shipping path. No `NotImplementedError` placeholders, no empty
  `except` or `catch` blocks.
- Stop at the first unexpected failure. Reproduce it, fix the cause, add a guard,
  then continue.

## Commits

Conventional commits, `type(scope): description`. Types in use: `feat`, `fix`,
`docs`, `test`, `refactor`, `chore`.
