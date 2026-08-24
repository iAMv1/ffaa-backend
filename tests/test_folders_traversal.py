"""Path-traversal battery for app.folders.resolve_file_path / _safe_name.

Pins CURRENT behavior: traversal/absolute/backslash paths resolve to None;
the sanitizer strips everything except alphanumerics, space, dash,
underscore (note: the function is `_safe_name`, not `_safe_filename`).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app import folders


@pytest.fixture(autouse=True)
def isolated_client_root(tmp_path, monkeypatch):
    monkeypatch.setattr(folders, "CLIENT_ROOT", tmp_path)
    return tmp_path


# --- traversal attempts must all be rejected -----------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "../../.env",
        "../.env",
        "../../../../../etc/passwd",
        "..\\..\\secret.txt",
        "..\\..\\..\\ffaa.db",
        "invoices/../../../x.txt",
        "ok/../../then-up.txt",
        "a/b/../../../../../../Windows/win.ini",
    ],
)
def test_traversal_paths_rejected(isolated_client_root, rel_path):
    assert folders.resolve_file_path("Acme", rel_path) is None


@pytest.mark.parametrize(
    "rel_path",
    [
        "C:\\Windows\\notepad.exe",
        "C:/Windows/system.ini",
        "/etc/passwd",
        "\\Windows\\system32\\config",
    ],
)
def test_absolute_paths_rejected(isolated_client_root, rel_path):
    assert folders.resolve_file_path("Acme", rel_path) is None


def test_missing_file_inside_client_dir_returns_none(isolated_client_root):
    assert folders.resolve_file_path("Acme", "invoices/nope.pdf") is None


def test_directory_target_returns_none(isolated_client_root):
    d = isolated_client_root / "Acme" / "invoices"
    d.mkdir(parents=True)
    assert folders.resolve_file_path("Acme", "invoices") is None


# --- positive case ---------------------------------------------------------------


def test_real_file_inside_client_dir_resolves(isolated_client_root):
    target = isolated_client_root / "Acme" / "2024" / "April" / "invoices"
    target.mkdir(parents=True)
    doc = target / "inv-001.pdf"
    doc.write_bytes(b"%PDF-fake")
    got = folders.resolve_file_path("Acme", "2024/April/invoices/inv-001.pdf")
    assert got is not None
    assert Path(got) == doc.resolve()


def test_client_name_sanitized_for_resolution(isolated_client_root):
    # client name goes through the same sanitizer as the stored folder
    target = isolated_client_root / "AcmeCorp"
    target.mkdir()
    doc = target / "f.txt"
    doc.write_text("hi", encoding="utf-8")
    got = folders.resolve_file_path("Acme/Corp", "f.txt")
    assert got is not None
    assert Path(got) == doc.resolve()


# --- _safe_name neutralization ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme Corp", "Acme Corp"),
        ("../../etc/passwd", "etcpasswd"),
        (".env", "env"),        # dot stripped, letters kept
        ("a/b\\c", "abc"),
        ("bad:name*?", "badname"),
        ("  spaced  ", "spaced"),
        ("keep-_dash", "keep-_dash"),
        ("..", ""),             # only dots -> empty name
    ],
)
def test_safe_name_neutralizes(raw, expected):
    assert folders._safe_name(raw) == expected
