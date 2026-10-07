from __future__ import annotations

import tomllib
from pathlib import Path

import citysports_efc_ble


def test_pyproject_version_matches_package_version() -> None:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert citysports_efc_ble.__version__ == data["project"]["version"]
