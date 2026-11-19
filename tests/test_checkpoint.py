import gzip
import json
import sqlite3
import subprocess
from urllib.error import HTTPError

import pytest

from radar import checkpoint as c, outbox
from test_toolify import site


def local_remote(tmp_path):
    repo = tmp_path / "repo"
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "ALLOW_INITIALIZE").touch()
    c.git(repo, "add", ".")
    c.git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "init")
    c.git(repo, "remote", "add", "origin", str(bare))
    return repo


def test_restore_from_authority_and_reject_old_runs(tmp_path):
    repo = local_remote(tmp_path)
    state = tmp_path / "state"
    with pytest.raises(FileNotFoundError):
        c.restore(repo, state, 1)
    c.restore(repo, state, 1, initialize=True)
    with sqlite3.connect(state / "radar.db") as db:
        db.execute("CREATE TABLE leads(term TEXT)")
    (state / "sitemaps").mkdir()
    data = gzip.compress(b'["old snapshot"]')
    (state / "sitemaps/site.json.gz").write_bytes(data)
    c.publish(repo, state, 1)
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(tmp_path / "remote.git"), str(fresh)], check=True)
    restored = tmp_path / "empty-runner"
    c.restore(fresh, restored, 2)
    assert (restored / "sitemaps/site.json.gz").read_bytes() == data
    with pytest.raises(ValueError, match="already initialized"):
        c.restore(repo, tmp_path / "reset", 2, initialize=True)
    c.publish(fresh, restored, 2)
    with pytest.raises(subprocess.CalledProcessError):
        c.publish(repo, state, 3)
    with pytest.raises(ValueError, match="stale"):
        c.restore(fresh, tmp_path / "rerun", 1)
    (fresh / "state.zip").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        c.restore(fresh, tmp_path / "bad", 3)
    (fresh / "state.zip").unlink()
    with pytest.raises(FileNotFoundError):
        c.restore(fresh, tmp_path / "missing", 3)


def test_outbox_retry_and_uncertain():
    from io import BytesIO
    db = sqlite3.connect(":memory:")
    db.execute(outbox.SCHEMA)
    db.execute("INSERT INTO telegram_outbox(batch_id,summary) VALUES ('a','summary')")
    def rejected(*args, **kwargs):
        raise HTTPError("mock", 429, "", {}, None)
    def deliver(send):
        outbox.deliver(db, "mock", "mock", lambda: None, send)
    with pytest.raises(RuntimeError):
        deliver(rejected)
    assert db.execute("SELECT status FROM telegram_outbox").fetchone() == ("pending",)
    deliver(lambda *a, **kw: BytesIO(b'{"ok":true,"result":{"message_id":17}}'))
    assert db.execute("SELECT status,message_id FROM telegram_outbox").fetchone() == ("sent",17)
    deliver(lambda *a, **kw: pytest.fail("duplicate delivery"))
    db.execute("INSERT INTO telegram_outbox(batch_id,summary) VALUES ('b','summary')")
    def timeout(*args, **kwargs):
        raise TimeoutError
    with pytest.raises(RuntimeError):
        deliver(timeout)
    assert db.execute("SELECT status FROM telegram_outbox WHERE batch_id='b'").fetchone() == ("uncertain",)


def test_valid_xml_shrink_preserves_products(site, tmp_path):
    from radar import toolify as t
    setup, _ = site
    setup({"/sitemap_tools_1.xml": [f"/tool/p{i}" for i in range(10)]})
    db = sqlite3.connect(":memory:")
    assert t.scan(db, t.RULES)["status"] == "failed"
    assert t.scan(db, t.RULES, initialize=True)["baseline"]
    setup({"/sitemap_tools_1.xml": ["/tool/p1"]})
    assert t.scan(db, t.RULES)["status"] == "failed"
    assert db.execute("SELECT count(*) FROM products WHERE discovery_status='present'").fetchone() == (10,)


@pytest.mark.parametrize("changes", [
    {"version": 99},
    {"timestamp": "2020-01-01T00:00:00+00:00"},
    {"last_success_batch": 11},
])
def test_reject_invalid_manifest(tmp_path, changes):
    repo = local_remote(tmp_path)
    state = tmp_path / "state"
    c.restore(repo, state, 1, initialize=True)
    sqlite3.connect(state / "radar.db").close()
    c.publish(repo, state, 1)
    manifest = repo / "manifest.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), **changes}))
    with pytest.raises(ValueError):
        c.restore(repo, tmp_path / "runner", 2)
    assert not (tmp_path / "runner").exists()


def test_claim_must_be_published_before_send():
    db = sqlite3.connect(":memory:")
    db.execute(outbox.SCHEMA)
    db.execute("INSERT INTO telegram_outbox(batch_id,summary) VALUES ('b','message')")
    def failed_publish():
        raise RuntimeError("push rejected")
    with pytest.raises(RuntimeError, match="push rejected"):
        outbox.deliver(db, "mock", "mock", failed_publish, lambda *a, **kw: pytest.fail("premature send"))


def test_first_actions_run_after_manual_baseline(tmp_path, monkeypatch):
    repo = local_remote(tmp_path)
    baseline = tmp_path / "manual"
    c.restore(repo, baseline, 1, initialize=True)
    sqlite3.connect(baseline / "radar.db").close()
    c.publish(repo, baseline, 1)
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "1")
    number = c.actions_checkpoint_run()
    assert number == 2
    runner = tmp_path / "runner"
    c.restore(repo, runner, number)
    c.publish(repo, runner, number)
    with pytest.raises(ValueError, match="stale"):
        c.restore(repo, tmp_path / "retry", number)
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "2")
    c.restore(repo, tmp_path / "next", c.actions_checkpoint_run())
