import os
import shutil
from datetime import date
from pathlib import Path

# ponytail: no Pydantic Settings, just env fallback
CLIENT_ROOT = Path(os.getenv("FFAA_CLIENT_ROOT", "data/clients"))


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
) -> str:
    """Copy source file into {root}/{Client}/{Year}/{Month}/{category}/filename."""
    root = CLIENT_ROOT / _safe_name(client_name) / str(doc_date.year) / _month_folder(doc_date) / category
    root.mkdir(parents=True, exist_ok=True)
    dest = root / filename
    shutil.copy2(source_path, dest)
    return str(dest)


def list_client_folders(client_name: str) -> dict:
    """Return tree of client folder paths. Empty dict if no folder yet."""
    root = CLIENT_ROOT / _safe_name(client_name)
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
