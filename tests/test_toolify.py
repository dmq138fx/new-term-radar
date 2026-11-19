import json
import sqlite3
import sys
import urllib.error
import xml.etree.ElementTree as ET

import pytest

from radar import toolify as t


def xml(root, tag, paths):
    tree = ET.Element(root, xmlns=t.NS[1:-1])
    for path in paths:
        entry = ET.SubElement(tree, tag)
        ET.SubElement(entry, "loc").text = t.HOST + path
        ET.SubElement(entry, "lastmod").text = "2026-11-01"
    return ET.tostring(tree)


@pytest.fixture
def site(monkeypatch):
    pages = {}
    def setup(shards):
        pages["/sitemap.xml"] = xml("sitemapindex", "sitemap", list(shards) + ["/sitemap_other.xml"])
        pages.update({key: xml("urlset", "url", paths) for key, paths in shards.items()})
    def fetch(url, *, attempts, strict):
        assert strict and attempts == 1
        value = pages[url.removeprefix(t.HOST)]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(t.sitemaps, "fetch", fetch)
    monkeypatch.setattr(t.time, "sleep", lambda seconds: None if seconds >= 5 else pytest.fail("crawl-delay"))
    return setup, pages


ONE = "/sitemap_tools_1.xml"
TWO = "/sitemap_tools_2.xml"


def test_lifecycle(site, tmp_path, monkeypatch):
    setup, _ = site
    setup({ONE: ["/tool/base", "/zh/tool/base", "/tool/base/?ref=x"]})
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    out = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["toolify", "--config", str(cfg), "--state", str(tmp_path), "--out", str(out)])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "mock")
    from io import BytesIO
    sent = []
    def send(url, data, timeout):
        sent.append(data)
        return BytesIO(b'{"ok":true}')
    monkeypatch.setattr(t, "urlopen", send)
    def run():
        assert t.main() == 0
        return json.loads(out.read_text())
    assert run()["baseline"] and not sent
    paths = ["/tool/pdf-converter"] + [f"/tool/brand-{n}" for n in range(10)]
    setup({ONE: ["/tool/base"], TWO: paths})
    result = run()
    assert result["new_count"] == 11 and len(sent) == 1
    assert result["category_counts"] == {"unclassified": 10, "utility": 1, "files_data": 1}
    assert result["new"][-1]["matches"] == {"files_data": ["pdf"], "utility": ["converter"]}
    assert run()["new"] == [] and len(sent) == 1
    setup({TWO: paths})
    assert run()["disappeared"] == [t.HOST + "/tool/base"]
    with sqlite3.connect(tmp_path / "radar.db") as db:
        assert db.execute("SELECT count(*) FROM products").fetchone() == (12,)


@pytest.mark.parametrize("broken", ["<html>challenge</html>", "<urlset", xml("urlset", "url", ["/news/x"]),
                                    urllib.error.HTTPError(t.HOST, 403, "Forbidden", {}, None)])
def test_bad_final_shard_preserves_baseline(site, broken):
    setup, pages = site
    db = sqlite3.connect(":memory:")
    setup({ONE: ["/tool/old"]})
    t.scan(db, t.RULES)
    before = db.execute("SELECT * FROM products").fetchall()
    setup({ONE: ["/tool/new"], TWO: ["/tool/another"]})
    pages[TWO] = broken
    assert t.scan(db, t.RULES)["status"] == "failed"
    assert db.execute("SELECT * FROM products").fetchall() == before
    assert db.execute("SELECT count(*) FROM product_runs").fetchone() == (2,)
