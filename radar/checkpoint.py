"""Versioned full-state checkpoints. The caller supplies an authenticated private Git checkout."""
import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED


def actions_checkpoint_run():
    """Checkpoint runs 0 and 1 were consumed by the manual migration and baseline."""
    number = int(os.environ["GITHUB_RUN_NUMBER"])
    if number < 1:
        raise ValueError("invalid Actions run number")
    return number + 1


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(state):
    with closing(sqlite3.connect((state / "radar.db").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("database integrity check failed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "product_runs" not in tables:
            return None
        if "products" not in tables:
            raise ValueError("missing products table")
        row = db.execute("""SELECT max(id) FROM product_runs
            WHERE source='toolify' AND json_extract(result,'$.status')='success'""").fetchone()
        if row[0] and not db.execute("SELECT 1 FROM products WHERE source='toolify' LIMIT 1").fetchone():
            raise ValueError("missing product baseline")
        return row[0]


def restore(repo, state, run, initialize=False):
    manifest = repo / "manifest.json"
    if state.exists() and any(state.iterdir()):
        raise ValueError("restore requires an empty destination")
    head = git(repo, "rev-parse", "HEAD")
    if initialize:
        if manifest.exists() or git(repo, "log", "-1", "--format=%H", "--", "manifest.json"):
            raise ValueError("already initialized")
        if not (repo / "ALLOW_INITIALIZE").is_file():
            raise ValueError("explicit initialization marker required")
        state.mkdir(exist_ok=True)
    else:
        meta = json.loads(manifest.read_text())
        age = datetime.now(timezone.utc) - datetime.fromisoformat(meta["timestamp"])
        if meta["version"] != 1 or not timedelta(0) <= age <= timedelta(days=7):
            raise ValueError("unsupported or stale checkpoint")
        if run <= meta["run"]:
            raise ValueError("stale workflow run")
        archive = repo / "state.zip"
        if digest(archive) != meta["sha256"]:
            raise ValueError("checkpoint checksum mismatch")
        with TemporaryDirectory(dir=state.parent) as tmp:
            staged = Path(tmp) / "state"
            with ZipFile(archive) as z:
                z.extractall(staged)
            if inspect(staged) != meta["last_success_batch"]:
                raise ValueError("baseline mismatch")
            staged.replace(state)
    (state / ".checkpoint").write_text(head)


def publish(repo, state, run):
    head = git(repo, "rev-parse", "HEAD")
    if (state / ".checkpoint").read_text() != head:
        raise ValueError("local state generation mismatch")
    manifest = repo / "manifest.json"
    old = json.loads(manifest.read_text()) if manifest.exists() else {}
    if run < old.get("run", 0):
        raise ValueError("stale workflow run")
    batch = inspect(state)
    if old.get("last_success_batch") and (batch or 0) < old["last_success_batch"]:
        raise ValueError("successful batch would regress")
    snapshots = sorted(p.relative_to(state).as_posix() for p in state.rglob("*.json.gz"))
    if set(old.get("snapshots", [])) - set(snapshots):
        raise ValueError("previously saved snapshot file missing")
    for path in snapshots:
        with gzip.open(state / path, "rt", encoding="utf-8") as source:
            json.load(source)
    archive = repo / "state.zip"
    with TemporaryDirectory() as temp, ZipFile(archive, "w", ZIP_DEFLATED) as z:
        backup = Path(temp) / "radar.db"
        with closing(sqlite3.connect((state / "radar.db").resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(backup)) as dst:
                src.backup(dst)
        z.write(backup, "radar.db")
        for p in sorted(state.rglob("*")):
            if p.is_file() and p.relative_to(state).as_posix() not in {
                    ".checkpoint", "radar.db", "radar.db-wal", "radar.db-shm", "radar.db-journal"}:
                z.write(p, p.relative_to(state))
    meta = dict(version=1, generation=old.get("generation", 0)+1, run=run,
                timestamp=datetime.now(timezone.utc).isoformat(), sha256=digest(archive),
                last_success_batch=batch, snapshots=snapshots)
    manifest.write_text(json.dumps(meta, indent=2))
    git(repo, "add", "manifest.json", "state.zip")
    git(repo, "-c", "user.name=Radar", "-c", "user.email=radar@localhost", "commit", "-qm", "Radar state checkpoint")
    git(repo, "push", "origin", "HEAD:main")
    (state / ".checkpoint").write_text(git(repo, "rev-parse", "HEAD"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["restore", "initialize", "publish"])
    parser.add_argument("--repo", type=Path, default=Path("authority"))
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--run", type=int, help="explicit local checkpoint number; defaults to Actions run number + 1")
    args = parser.parse_args()
    if args.run is None:
        args.run = actions_checkpoint_run()
    if args.operation == "publish":
        publish(args.repo, args.state, args.run)
    else:
        restore(args.repo, args.state, args.run, args.operation == "initialize")
