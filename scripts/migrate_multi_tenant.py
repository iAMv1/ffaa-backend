"""P1 multi-tenant migration. IDEMPOTENT — safe to run repeatedly.

Steps (each individually skipped when already applied):
  1. users table via metadata create_all (creates only missing tables)
  2. operator account (email from FFAA_OPERATOR_EMAIL, default operator@ffaa.local);
     one-time random 16-char password written to data/operator_credentials.txt
     ONLY on first creation
  3. clients table rebuild (SQLite has no ALTER CONSTRAINT): drop global
     UNIQUE(name), add owner_id column + composite UNIQUE(owner_id, name)
  4. backfill clients.owner_id -> operator id where NULL
  5. move data/clients/* folders under data/clients/<operator_id>/

Usage:
    py -3.12 scripts/migrate_multi_tenant.py [--db path/to/ffaa.db]
"""
import argparse
from datetime import datetime
import os
import secrets
import shutil
import string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# --db must be honored BEFORE app.database binds its engine (module import time).
if "--db" in sys.argv:
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(sys.argv[sys.argv.index('--db') + 1]).as_posix()}"

from sqlalchemy import inspect as sqla_inspect, text  # noqa: E402
from app import models  # noqa: E402

OPERATOR_EMAIL = os.environ.get("FFAA_OPERATOR_EMAIL", "operator@ffaa.local")


def _gen_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _client_root() -> Path:
    return Path(os.getenv("FFAA_CLIENT_ROOT", str(REPO_ROOT / "data" / "clients")))


def ensure_users_table(engine) -> None:
    models.Base.metadata.create_all(bind=engine)


def ensure_operator(engine, db) -> "models.User":
    from sqlalchemy import func

    user = (
        db.query(models.User)
        .filter(func.lower(models.User.email) == OPERATOR_EMAIL.lower())
        .first()
    )
    if user:
        print(f"[skip] operator {user.email} already exists (id={user.id})")
        return user

    password = _gen_password()
    from fastapi_users.password import PasswordHelper

    user = models.User(
        email=OPERATOR_EMAIL,
        hashed_password=PasswordHelper().hash(password),
        is_active=True,
        is_superuser=False,
        is_verified=False,
        created_at=datetime.now(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    creds_path = REPO_ROOT / "data" / "operator_credentials.txt"
    creds_path.write_text(
        f"{user.email} : {password}\n"
        "(one-time password; rotate after first login)\n",
        encoding="utf-8",
    )
    print(f"[ok] operator created: {user.email} (id={user.id}); "
          f"credentials written to {creds_path}")
    return user


def rebuild_clients_table(engine) -> None:
    """SQLite cannot drop a constraint: rename old table, create the new schema
    (owner_id + UNIQUE(owner_id, name), no global unique name), copy, drop."""
    insp = sqla_inspect(engine)
    tables = set(insp.get_table_names())
    if "clients_old" in tables:
        raise RuntimeError("clients_old exists from a failed prior run; resolve manually")
    if "clients" not in tables:
        print("[skip] no clients table yet")
        return
    cols = {c["name"] for c in insp.get_columns("clients")}
    if "owner_id" in cols:
        print("[skip] clients table already rebuilt")
        return

    with engine.begin() as conn:
        # the rename carries old indexes (incl. UNIQUE ix_clients_name) — drop
        # them or CREATE INDEX on the new table collides
        legacy = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='clients'"
            " AND name NOT LIKE 'sqlite_autoindex%'"
        )).scalars().all()
        for idx in legacy:
            conn.execute(text(f"DROP INDEX IF EXISTS {idx}"))
        conn.execute(text("ALTER TABLE clients RENAME TO clients_old"))
    try:
        # Only this table — Base.metadata may reference users etc. already present.
        models.Client.__table__.create(bind=engine)
        with engine.begin() as conn:
            conn.execute(text(
                """
                INSERT INTO clients (id, name, email, gst_number, address,
                                     auto_created, created_at, owner_id)
                SELECT id, name, email, gst_number, address,
                       COALESCE(auto_created, 0), created_at, NULL
                FROM clients_old
                """
            ))
            conn.execute(text("DROP TABLE clients_old"))
    except Exception:
        # Best-effort rollback of the rename so a retry starts clean.
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS clients"))
            conn.execute(text("ALTER TABLE clients_old RENAME TO clients"))
        raise
    print("[ok] clients rebuilt: owner_id added, UNIQUE(owner_id,name), global UNIQUE(name) dropped")


def backfill_owner(db, operator_id: int) -> None:
    n = (
        db.query(models.Client)
        .filter(models.Client.owner_id.is_(None))
        .update({models.Client.owner_id: operator_id}, synchronize_session=False)
    )
    db.commit()
    print(f"[ok] backfilled owner_id={operator_id} on {n} client(s)")


def move_data_dirs(operator_id: int) -> None:
    root = _client_root()
    own = str(operator_id)
    if not root.exists():
        print(f"[skip] no folder root at {root}")
        return
    moved = skipped = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name == own:
            continue
        dest = root / own / child.name
        if dest.exists():
            print(f"[warn] destination exists, left in place: {child} -> {dest}")
            skipped += 1
            continue
        (root / own).mkdir(exist_ok=True)
        shutil.move(str(child), str(dest))
        moved += 1
    print(f"[ok] folder namespaces: moved {moved} dir(s) under {root / own}"
          + (f", skipped {skipped}" if skipped else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None, help="path to sqlite db file (default DATABASE_URL env or ./ffaa.db)")
    args = ap.parse_args()

    if args.db:
        os.environ["DATABASE_URL"] = f"sqlite:///{Path(args.db).as_posix()}"

    from app.database import SessionLocal, engine
    print(f"[..] migrating {engine.url}")
    ensure_users_table(engine)

    rebuild_clients_table(engine)  # before operator/backfill: fresh DBs already have owner_id

    db = SessionLocal()
    try:
        operator = ensure_operator(engine, db)
        backfill_owner(db, operator.id)
        move_data_dirs(operator.id)
    finally:
        db.close()
        engine.dispose()
    print("done.")


if __name__ == "__main__":
    main()
