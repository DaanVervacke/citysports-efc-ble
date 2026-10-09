"""Reject code comments and docstrings that break the project writing rules.

Comments are allowed only as pragmas or as a shebang on the first line.
Docstrings must not contain banned characters, banned words or semicolons.
"""

import argparse
import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATHS = ("src", "tests", "scripts", "docs")
SKIPPED_DIRS = frozenset(("_build", "__pycache__", ".venv"))
PRAGMA = re.compile(r"#\s*(noqa\b|type:\s*ignore\b|pragma:|shellcheck:)")
BANNED_CHARACTERS = {
    "\u2013": "en dash",
    "\u2014": "em dash",
    "\u2018": "curly quote",
    "\u2019": "curly quote",
    "\u201c": "curly quote",
    "\u201d": "curly quote",
    "\u2026": "ellipsis character",
    "\u2190": "arrow",
    "\u2192": "arrow",
    "\u21d2": "arrow",
    "\u00a0": "non-breaking space",
}
EMOJI = re.compile("[\u2600-\u27bf\U0001f300-\U0001faff]")
BANNED_WORDS = re.compile(
    r"\b(delv(e|es|ed|ing)|leverag\w*|robust\w*|streamlin\w*|seamless\w*"
    r"|utiliz\w*|worth noting|serves as|it is important to note)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation at a file position."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        """Format the finding as ``path:line: message``."""
        return f"{self.path}:{self.line}: {self.message}"


def python_files(paths: Sequence[Path]) -> Iterator[Path]:
    """Yield every Python file under ``paths`` in a stable order."""
    for path in paths:
        if path.is_file():
            yield path
            continue
        for candidate in sorted(path.rglob("*.py")):
            if not SKIPPED_DIRS.intersection(candidate.parts):
                yield candidate


def comment_findings(path: Path, source: str) -> Iterator[Finding]:
    """Yield a finding for every comment that is not a pragma or a shebang."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        line = token.start[0]
        if line == 1 and token.string.startswith("#!"):
            continue
        if PRAGMA.match(token.string):
            continue
        yield Finding(path, line, "code comment")


def docstrings(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Yield the line and text of every string that stands alone as a statement."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str):
                yield node.lineno, value


def docstring_findings(path: Path, source: str) -> Iterator[Finding]:
    """Yield a finding for every banned character, word or semicolon."""
    for start, text in docstrings(ast.parse(source, filename=str(path))):
        for offset, line in enumerate(text.splitlines()):
            number = start + offset
            names = {name for char, name in BANNED_CHARACTERS.items() if char in line}
            for name in sorted(names):
                yield Finding(path, number, f"{name} in docstring")
            if EMOJI.search(line):
                yield Finding(path, number, "emoji in docstring")
            if ";" in line:
                yield Finding(path, number, "semicolon in docstring")
            for match in BANNED_WORDS.finditer(line):
                yield Finding(path, number, f"banned phrase {match.group(0)!r}")


def check_file(path: Path) -> list[Finding]:
    """Return every finding in one Python file."""
    source = path.read_text(encoding="utf-8")
    return [*comment_findings(path, source), *docstring_findings(path, source)]


def main(argv: Sequence[str] | None = None) -> int:
    """Check the given paths and print each finding."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    paths = args.paths or [Path(name) for name in DEFAULT_PATHS]
    findings = [finding for path in python_files(paths) for finding in check_file(path)]
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
