from pathlib import Path

import pytest
from scripts import check_style


def write(tmp_path: Path, source: str, name: str = "sample.py") -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def messages(path: Path) -> list[tuple[int, str]]:
    return [(finding.line, finding.message) for finding in check_style.check_file(path)]


def test_clean_file_has_no_findings(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '#!/usr/bin/env python\n"""Plain module docstring."""\n\n'
        'VALUE = "# not a comment"\n',
    )

    assert messages(path) == []


def test_comments_are_reported(tmp_path: Path) -> None:
    path = write(tmp_path, "# full line\nVALUE = 1  # trailing\n")

    assert messages(path) == [(1, "code comment"), (2, "code comment")]


@pytest.mark.parametrize(
    "pragma",
    [
        "# noqa: E501",
        "# type: ignore[attr-defined]",
        "# pragma: no cover",
        "# shellcheck: disable=SC2086",
    ],
)
def test_pragmas_are_allowed(tmp_path: Path, pragma: str) -> None:
    path = write(tmp_path, f"VALUE = 1  {pragma}\n")

    assert messages(path) == []


def test_shebang_only_allowed_on_first_line(tmp_path: Path) -> None:
    path = write(tmp_path, "VALUE = 1\n#!/usr/bin/env python\n")

    assert messages(path) == [(2, "code comment")]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Wait\u2014then go.", "em dash in docstring"),
        ("Range 1\u20132.", "en dash in docstring"),
        ("Say \u201chi\u201d.", "curly quote in docstring"),
        ("Wait\u2026", "ellipsis character in docstring"),
        ("A \u2192 B.", "arrow in docstring"),
        ("Ten\u00a0bytes.", "non-breaking space in docstring"),
        ("Done \U0001f680.", "emoji in docstring"),
        ("Read it; then write.", "semicolon in docstring"),
        ("We leverage the queue.", "banned phrase 'leverage'"),
        ("It serves as a cache.", "banned phrase 'serves as'"),
    ],
)
def test_docstring_rules(tmp_path: Path, text: str, message: str) -> None:
    path = write(tmp_path, f'def run() -> None:\n    """{text}"""\n')

    assert messages(path) == [(2, message)]


def test_attribute_docstrings_are_checked(tmp_path: Path) -> None:
    path = write(tmp_path, 'VALUE = 1\n"""First line.\n\nUse it; or not."""\n')

    assert messages(path) == [(4, "semicolon in docstring")]


def test_string_literals_are_not_docstrings(tmp_path: Path) -> None:
    path = write(tmp_path, 'VALUE = "a; b \\u2014 robust"\n')

    assert messages(path) == []


def test_python_files_skips_build_dirs(tmp_path: Path) -> None:
    kept = write(tmp_path, "VALUE = 1\n", "kept.py")
    (tmp_path / "_build").mkdir()
    write(tmp_path / "_build", "VALUE = 1\n", "skipped.py")
    (tmp_path / "notes.md").write_text("# heading\n", encoding="utf-8")

    assert list(check_style.python_files([tmp_path])) == [kept]
    assert list(check_style.python_files([kept])) == [kept]


def test_main_reports_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, "VALUE = 1  # trailing\n")

    assert check_style.main([str(path)]) == 1
    assert capsys.readouterr().out == f"{path}:1: code comment\n"


def test_main_passes_clean_paths(tmp_path: Path) -> None:
    write(tmp_path, "VALUE = 1\n")

    assert check_style.main([str(tmp_path)]) == 0


def test_repository_passes() -> None:
    assert check_style.main([]) == 0
