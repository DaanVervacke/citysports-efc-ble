- [ ] `uv run python -m scripts.check` passes completely.
- [ ] The PR carries one of the seven labels: `breaking-change`, `new-feature`, `enhancement`, `bugfix`, `maintenance`, `documentation`, `dependencies`.
- [ ] Every commit subject is a conventional commit, so git-cliff can render it into `CHANGELOG.md`.

For protocol or behavior changes, all of the following are present:

- [ ] A captured payload under `tests/fixtures/` backing the change.
- [ ] Protocol notes in the README updated when wire shapes change.
- [ ] Tests covering the new behavior, including the failure paths.
