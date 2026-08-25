import os
import shutil
from datetime import date
from pathlib import Path

# ponytail: no Pydantic Settings, just env fallback
CLIENT_ROOT = Path(os.getenv("FFAA_CLIENT_ROOT", "data/clients"))


def _client_root(owner_id: int | None = None) -> Path:
    """Per-owner namespace {FFAA_CLIENT_ROOT}/{owner_id}/ — prevents cross-tenant
    folder collisions on identical client names. None keeps the legacy root."""
    return CLIENT_ROOT / str(owner_id) if owner_id else CLIENT_ROOT


def _safe_name(name: str) -> str:
    # ponytail: minimal sanitization; enough for folder names
    return "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()


def _month_folder(d: date) -> str:
    return d.strftime("%B")


def archive_file(
    source_path: str,
    client_name: str,
    doc_date: date,
    category: str,
    filename: str,
    owner_id: int | None = None,
) -> str:
    """Copy source file into {root}/{owner_id}/{Client}/{Year}/{Month}/{category}/filename."""
    root = _client_root(owner_id) / _safe_name(client_name) / str(doc_date.year) / _month_folder(doc_date) / category
    root.mkdir(parents=True, exist_ok=True)
    dest = root / filename
    shutil.copy2(source_path, dest)
    return str(dest)


def list_client_folders(client_name: str, owner_id: int | None = None) -> dict:
    """Return tree of client folder paths. Empty dict if no folder yet."""
    root = _client_root(owner_id) / _safe_name(client_name)
    if not root.exists():
        return {}

    tree = {}
    for year_dir in root.iterdir():
        if not year_dir.is_dir():
            continue
        tree[year_dir.name] = {}
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            tree[year_dir.name][month_dir.name] = {}
            for cat_dir in month_dir.iterdir():
                if not cat_dir.is_dir():
                    continue
                tree[year_dir.name][month_dir.name][cat_dir.name] = [
                    f.name for f in cat_dir.iterdir() if f.is_file()
                ]
    return tree


def resolve_file_path(client_name: str, rel_path: str, owner_id: int | None = None) -> Path | None:
    """Resolve a relative path within client root. Block path traversal."""
    try:
        safe_client = _safe_name(client_name)
        base = (_client_root(owner_id) / safe_client).resolve()
        target = (base / rel_path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None
        if target.exists() and target.is_file():
            return target
    except Exception:
        return None
    return None
