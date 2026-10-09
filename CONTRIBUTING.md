# Contributing to citysports-efc-ble

Thanks for your interest in contributing. This project is an async Python
library for EFC treadmills (CITYSPORTS and others) over Bluetooth Low
Energy, targeting Python >= 3.14.

## Setup

Use [uv](https://docs.astral.sh/uv/) (>= 0.12.21, < 0.13) to install the
environment:

```bash
uv sync
```

The lockfile excludes Intel macOS because the `habluetooth` dev dependency
ships no Intel macOS wheels.

## Hardware scripts

The scripts under `scripts/` talk to a real treadmill. Run them as modules:

```bash
uv run python -m scripts.probe_efc
uv run python -m scripts.drive_efc_client
```

## Running the checks

Before opening a pull request, run the full gate:

```bash
uv run python -m scripts.check
```

This is the canonical check. It stops at the first failure and runs exactly:

```text
version drift check
ruff format --check .
ruff check .
python -m scripts.check_style
mypy src tests scripts
coverage run --branch -m pytest
coverage report --show-missing
uv build --no-sources
uv audit --locked --preview-features audit-command
```

Coverage measures branches in `src/citysports_efc_ble` and requires 98%. Your
pull request must pass this gate completely.

The API documentation is not part of the local gate. CI builds it on every
pull request and fails on any warning. Build it locally with:

```bash
uv run --group docs sphinx-build -W -b html docs docs/_build
```

## Code style

- No code comments in Python. Pragmas such as `# noqa`, `# type: ignore`
  and `# pragma: no cover`, and a shebang on the first line, are allowed.
- Docstrings are the only documentation. They follow the Google convention
  and use plain, direct language.
- Docstrings must not contain semicolons, en or em dashes, curly quotes,
  ellipsis characters, arrows, emoji or non-breaking spaces.
- Docstrings must not use the words or phrases delve, leverage, robust,
  streamline, seamless, utilize, "worth noting", "serves as" or "it is
  important to note".

`python -m scripts.check_style` enforces these rules in the gate.

## Protocol and behavior changes

A protocol or behavior change is complete only when all of the following are
present:

1. A captured payload under `tests/fixtures/` backing the change. Do not
   guess wire shapes.
2. The protocol notes in the README updated when wire shapes change.
3. Tests covering the new behavior, including the failure paths.
4. A conventional commit subject, which git-cliff renders into `CHANGELOG.md`.

## Changelog

`CHANGELOG.md` is generated with git-cliff from conventional commit subjects.
Never rewrite entries by hand. Features, bug fixes, documentation, and
maintenance chores reach the changelog through their `feat:`, `fix:`, `docs:`,
and `chore:` subjects. Regenerate the unreleased section with
`git-cliff --unreleased --prepend CHANGELOG.md` and commit the result. At
release, rename the Unreleased heading to `## [X.Y.Z] - YYYY-MM-DD`, add the
`[X.Y.Z]:` compare link at the bottom of the file, and point the
`[Unreleased]:` link at the new tag. Bump the version, commit, and tag
`vX.Y.Z`. Push the tag, then publish the GitHub release that Release Drafter
prepared for it. Publishing the release runs the gate and uploads the build
to PyPI.

## Commit style

One conventional-commit subject line, no body. Write the description as an
imperative sentence, for example: `chore: cap bleak below 4`. Do not mention
the plan or issue number in the subject.

## Deprecation policy

- While the project is on 0.x: breaking changes are allowed in minor releases,
  provided their commit subject marks them breaking (`feat!:` or `fix!:`).
- From 1.0 onwards: deprecated APIs emit a `DeprecationWarning` for at least
  one minor release before being removed in a major release.

## Pull requests

Every pull request must carry one of the seven repository labels
(`breaking-change`, `new-feature`, `enhancement`, `bugfix`, `maintenance`,
`documentation`, `dependencies`). CI fails otherwise. Dependabot pull
requests are labeled `dependencies` automatically.
