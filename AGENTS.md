# AGENTS.md

## Code style

- No code comments. Pragmas such as `# noqa`, `# type: ignore` and
  `# pragma: no cover` are the only allowed `#` lines. Docstrings carry the
  documentation, in plain direct language, without semicolons, em dashes,
  curly quotes or other decorative characters.

## Repository rules

- The changelog is generated with git-cliff from conventional commit subjects.
  Regenerate with git-cliff instead of hand-writing entries. Release cuts
  follow the CONTRIBUTING changelog procedure.
- Protocol facts come from own captures and the published Trught notes. Do
  not copy vendor code.
