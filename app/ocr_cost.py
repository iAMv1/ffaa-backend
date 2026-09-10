"""Cost plumbing for the OCR escalation agent: usage ledger, result cache,
page selection. Pure helpers — all IO is fail-open (corruption or absence
never blocks extraction). The usage ledger records only when a monthly
budget is configured; the cache is a cost optimization only, never
correctness."""
import hashlib
import json
import os
import time

_PAGE_MIN_CHARS = 200  # pages below this look scanned/weak -> escalate them
_CACHE_MAX = 200


def _current_month() -> str:
    return time.strftime("%Y-%m")


def _usage_path() -> str:
    return os.environ.get("FFAA_OCR_ESCALATION_USAGE") or os.path.join(
        os.getcwd(), ".escalation_usage.json"
    )


def _budget() -> int:
    try:
        return int(os.environ.get("FFAA_OCR_ESCALATION_BUDGET", "0") or "0")
    except ValueError:
        return 0


def _read_usage() -> tuple[str, int]:
    try:
        with open(_usage_path(), encoding="utf-8") as f:
            d = json.load(f)
        return str(d.get("month", "")), int(d.get("pages", 0))
    except Exception:
        return "", 0


def _record_usage(pages: int) -> None:
    if _budget() <= 0:
        return  # no budget configured -> no ledger, nothing to track
    month, used = _read_usage()
    if month != _current_month():
        month, used = _current_month(), 0
    try:
        with open(_usage_path(), "w", encoding="utf-8") as f:
            json.dump({"month": month, "pages": used + max(0, pages)}, f)
    except Exception:
        pass  # a usage-file failure must never block extraction


def _budget_exhausted(pages_needed: int) -> bool:
    """Monthly page-budget guard for the free tier. Unset/0 = unlimited."""
    budget = _budget()
    if budget <= 0:
        return False
    month, used = _read_usage()
    if month != _current_month():
        return False
    return used + pages_needed > budget


def _count_pages(file_path: str) -> int:
    try:
        import fitz

        with fitz.open(file_path) as doc:
            return max(1, doc.page_count)
    except Exception:
        return 1


def _file_hash(file_path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def _cache_path() -> str:
    return os.environ.get("FFAA_OCR_ESCALATION_CACHE") or os.path.join(
        os.getcwd(), ".escalation_cache.json"
    )


def _cache_get(digest: str) -> dict | None:
    if not digest:
        return None
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            entries = json.load(f)
        e = entries.get(digest)
        if isinstance(e, dict) and isinstance(e.get("fields"), dict):
            return e
    except Exception:
        pass
    return None


def _cache_put(digest: str, fields: dict, verified: bool) -> None:
    if not digest:
        return
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, dict):
            entries = {}
    except Exception:
        entries = {}
    entries[digest] = {"fields": fields, "verified": verified, "ts": time.time()}
    if len(entries) > _CACHE_MAX:
        keep = sorted(entries, key=lambda k: entries[k].get("ts", 0))[-_CACHE_MAX:]
        entries = {k: entries[k] for k in keep}
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(entries, f)
    except Exception:
        pass


def _select_send_path(file_path: str) -> tuple[str, int, int]:
    """Page-selective escalation: for multi-page PDFs, send only weak
    (low-text) pages — strong text-layer pages were already read locally,
    so billing them again is waste. Returns (path_to_send, total_pages,
    pages_sent); non-PDF/corrupt/single-page falls back to the original."""
    try:
        import fitz

        with fitz.open(file_path) as doc:
            total = doc.page_count
            if total <= 1:
                return file_path, 1, 1
            weak = [
                i
                for i in range(total)
                if len(doc[i].get_text().strip()) < _PAGE_MIN_CHARS
            ]
            if not weak or len(weak) == total:
                return file_path, total, total
            import tempfile

            subset = fitz.open()
            for i in weak:
                subset.insert_pdf(doc, from_page=i, to_page=i)
            fd, tmp = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            subset.save(tmp)
            subset.close()
            return tmp, total, len(weak)
    except Exception:
        return file_path, 1, 1
