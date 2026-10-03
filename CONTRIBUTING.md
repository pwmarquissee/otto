# Contributing

## Code floor

These hold for every change. They do not get weakened to make a change pass.

- No new suppression comments (`# noqa`, `# type: ignore`, `eslint-disable`,
  `@ts-ignore`, `//nolint`, `#[allow]`) without the reason on the same line.
- No skipping, deleting, or loosening a test to get to green. A test that is wrong
  gets fixed, with the reason in the commit message.
- No stubs on a path that ships: no `NotImplementedError` placeholders, no empty
  `except` or `catch` blocks.
- Stop at the first unexpected failure. Reproduce it, fix the cause, add the guard,
  then continue.
- Run `python -m pytest` before claiming done, and include the output in the PR.

## Commits

Conventional commits: `type(scope): description`. Types in use: `feat`, `fix`,
`docs`, `test`, `refactor`, `chore`.

## Before a pull request

```
python -m pytest
python scripts/oss_scan.py
```

Both must pass. `oss_scan.py` fails on anything that looks like private data
(non-example email addresses, account numbers, chat ids, home directory paths,
credential shapes). If it flags something that is genuinely a placeholder, change
the placeholder to one of the allowed forms rather than adding an exception.

## Scope

Otto is a single-operator tool. Changes that turn it into a multi-user or hosted
service are out of scope. Changes that add an integration should keep the default
inert: with nothing configured, Otto must still run on loopback and talk to nothing.
