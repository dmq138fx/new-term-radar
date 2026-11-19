"""Discover default-language Toolify products from complete sitemap scans."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from . import checkpoint, outbox, sitemaps

HOST = "https://www.toolify.ai"
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
RULES = {
    "image_design": ["image", "photo", "art", "design", "logo"],
    "video_audio": ["video", "audio", "voice", "music", "speech"],
    "text_writing": ["text", "writer", "writing", "essay", "copywriting"],
    "files_data": ["pdf", "csv", "data", "excel", "document"],
    "developer": ["code", "api", "developer", "debug", "sdk"],
    "productivity": ["task", "meeting", "notes", "calendar", "workflow"],
    "utility": ["converter", "calculator", "generator"],
}


def entries(url, root, child, metrics):
    start = time.monotonic()
    metrics[url] = {"bytes": None}
    try:
        body = sitemaps.fetch(url, attempts=1, strict=True)
    finally:
        metrics[url]["seconds"] = time.monotonic() - start
    metrics[url]["bytes"] = len(body.encode()) if isinstance(body, str) else len(body)
    tree = ET.fromstring(body)
    if tree.tag != NS + root or not len(tree):
        raise ValueError(f"invalid {root}: {url}")
    for entry in tree:
        loc = (entry.findtext(NS + "loc") or "").strip()
        if entry.tag != NS + child or not loc:
            raise ValueError(f"invalid {child}: {url}")
        yield loc, entry.findtext(NS + "lastmod")


def crawl(metrics):
    shards = sorted({url for url, _ in entries(HOST + "/sitemap.xml", "sitemapindex", "sitemap", metrics)
                     if re.fullmatch(re.escape(HOST) + r"/sitemap_tools_[0-9]+\.xml", url)})
    if not shards:
        raise ValueError("no product shards")
    products = {}
    for shard in shards:
        time.sleep(5)
        defaults = set()
        for url, modified in entries(shard, "urlset", "url", metrics):
            parts = urlsplit(url)
            match = re.fullmatch(r"/(?:(zh|tw|ko|ja|pt|es|de|fr|vi)/)?tool/([^/\s]+)/?", parts.path)
            if parts.scheme != "https" or parts.netloc != "www.toolify.ai" or not match:
                raise ValueError(f"unexpected product URL in {shard}")
            locale, slug = match.groups()
            if slug in (".", "..") or re.search(r"%2f|%5c|%2e|\\", slug, re.I):
                raise ValueError(f"invalid product slug in {shard}")
            if not locale:
                products[HOST + "/tool/" + slug] = modified
                defaults.add(slug)
        metrics[shard]["products"] = len(defaults)
        if not defaults:
            raise ValueError(f"no default products in {shard}")
    return products


def classify(slug, rules):
    tokens = set(re.findall(r"[a-z0-9]+", slug.lower()))
    return {label: sorted(tokens.intersection(words)) for label, words in rules.items()
            if tokens.intersection(words)}


def validate_counts(old, current, total, settings):
    missing = old["shards"].keys() - current.keys()
    if missing:
        raise ValueError(f"missing shards: {sorted(missing)}")
    drop = float(settings.get("max_shard_drop", 0.2))
    change = float(settings.get("max_total_change", 0.3))
    if not 0 <= drop <= 1 or not 0 <= change:
        raise ValueError("invalid integrity thresholds")
    for shard, count in old["shards"].items():
        if current[shard] < count * (1-drop):
            raise ValueError(f"shrinking shard: {shard}")
    if abs(total-old["total"]) > old["total"] * change:
        raise ValueError("abnormal total product count")


def scan(db, rules, *, initialize=False, settings=None, notify=False):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        source TEXT, source_product_url TEXT, slug TEXT, first_seen_at TEXT, last_seen_at TEXT,
        sitemap_lastmod TEXT, categories TEXT, classification_method TEXT, matches TEXT,
        discovery_status TEXT, PRIMARY KEY(source, source_product_url));
    CREATE TABLE IF NOT EXISTS product_runs (
        id INTEGER PRIMARY KEY, source TEXT, fetched_at TEXT, result TEXT);
    """)
    db.execute(outbox.SCHEMA)
    start = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    result = dict(fetched_at=now, source="toolify", status="success", new=[], disappeared=[],
                  category_counts={}, priority=[])
    last = db.execute("""SELECT id,result FROM product_runs WHERE source='toolify'
        AND json_extract(result,'$.status')='success' ORDER BY id DESC LIMIT 1""").fetchone()
    metrics = result["requests"] = {}
    try:
        if not last and not initialize:
            raise ValueError("explicit Toolify initialization required")
        current = crawl(metrics)
        shards = {url: m["products"] for url, m in metrics.items() if "products" in m}
        result.update(shards=shards, total=len(current))
        if last:
            old = json.loads(last[1])
            result["previous_success_at"] = old["fetched_at"]
            if "shards" in old:
                validate_counts(old, shards, len(current), settings or {})
            elif not initialize:
                raise ValueError("explicit migration required for legacy shard metadata")
    except (OSError, HTTPException, ValueError, ET.ParseError) as exc:
        result.update(status="failed", error=str(exc))
    with db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute("""SELECT max(id) FROM product_runs WHERE source='toolify'
            AND json_extract(result,'$.status')='success'""").fetchone()[0]
        if latest != (last[0] if last else None):
            result.update(status="failed", error="superseded scan")
        if result["status"] == "success":
            previous = dict(db.execute("SELECT source_product_url, discovery_status FROM products WHERE source='toolify'"))
            result["baseline"] = not previous
            counts = Counter()
            for url, modified in sorted(current.items()):
                slug = url.rsplit("/", 1)[1]
                matches = classify(slug, rules)
                categories = sorted(matches) or ["unclassified"]
                db.execute("""INSERT INTO products VALUES ('toolify',?,?,?,?,?,?,'slug_inferred',?,'present')
                    ON CONFLICT(source,source_product_url) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at, sitemap_lastmod=excluded.sitemap_lastmod,
                    categories=excluded.categories, matches=excluded.matches,
                    discovery_status='present'""",
                           (url, slug, now, now, modified, json.dumps(categories), json.dumps(matches)))
                if previous and url not in previous:
                    result["new"].append(dict(source_product_url=url, slug=slug, sitemap_lastmod=modified,
                                              categories=categories, matches=matches, classification_method="slug_inferred"))
                    counts.update(categories)
                    if matches:
                        result["priority"].append(slug)
            result["category_counts"] = dict(counts)
            result["disappeared"] = sorted(url for url, status in previous.items()
                                           if status == "present" and url not in current)
            db.executemany("UPDATE products SET discovery_status='missing' WHERE source='toolify' AND source_product_url=?",
                           [(url,) for url in result["disappeared"]])
        result["new_count"] = len(result["new"])
        result["seconds"] = time.monotonic()-start
        cursor = db.execute("INSERT INTO product_runs(source,fetched_at,result) VALUES ('toolify',?,?)",
                   (now, json.dumps(result, ensure_ascii=False)))
        if notify and result["new_count"]:
            batch = f"toolify-{cursor.lastrowid}"
            summary = f"{batch} | new {result['new_count']} | missing {len(result['disappeared'])}"
            db.execute("INSERT INTO telegram_outbox(batch_id,summary) VALUES (?,?)", (batch, summary))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--config", type=Path, default=Path("config.example.json"))
    parser.add_argument("--out", type=Path, default=Path("toolify.json"))
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--queue", action="store_true")
    parser.add_argument("--deliver", action="store_true")
    parser.add_argument("--authority", type=Path)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text()).get("toolify", {})
    args.state.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.state / "radar.db", timeout=180)) as db:
        if args.deliver:
            token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
            def save():
                if args.authority:
                    checkpoint.publish(args.authority, args.state, checkpoint.actions_checkpoint_run())
            outbox.deliver(db, token, chat, save, urlopen)
            return 0
        result = scan(db, settings.get("rules", RULES), initialize=args.initialize,
                      settings=settings, notify=args.queue)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Toolify {result['status']} | new {result['new_count']} | {result.get('error', '')}")
    return int(result["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
