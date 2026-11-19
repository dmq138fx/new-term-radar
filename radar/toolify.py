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
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from . import sitemaps

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


def entries(url, root, child):
    tree = ET.fromstring(sitemaps.fetch(url, attempts=1, strict=True))
    if tree.tag != NS + root or not len(tree):
        raise ValueError(f"invalid {root}: {url}")
    for entry in tree:
        loc = (entry.findtext(NS + "loc") or "").strip()
        if entry.tag != NS + child or not loc:
            raise ValueError(f"invalid {child}: {url}")
        yield loc, entry.findtext(NS + "lastmod")


def crawl():
    shards = sorted({url for url, _ in entries(HOST + "/sitemap.xml", "sitemapindex", "sitemap")
                     if re.fullmatch(re.escape(HOST) + r"/sitemap_tools_[0-9]+\.xml", url)})
    if not shards:
        raise ValueError("no product shards")
    products = {}
    for shard in shards:
        time.sleep(5)
        defaults = 0
        for url, modified in entries(shard, "urlset", "url"):
            parts = urlsplit(url)
            match = re.fullmatch(r"/(?:(zh|tw|ko|ja|pt|es|de|fr|vi)/)?tool/([^/\s]+)/?", parts.path)
            if parts.scheme != "https" or parts.netloc != "www.toolify.ai" or not match:
                raise ValueError(f"unexpected product URL in {shard}")
            locale, slug = match.groups()
            if slug in (".", "..") or re.search(r"%2f|%5c|%2e|\\", slug, re.I):
                raise ValueError(f"invalid product slug in {shard}")
            if not locale:
                products[HOST + "/tool/" + slug] = modified
                defaults += 1
        if not defaults:
            raise ValueError(f"no default products in {shard}")
    return products


def classify(slug, rules):
    tokens = set(re.findall(r"[a-z0-9]+", slug.lower()))
    return {label: sorted(tokens.intersection(words)) for label, words in rules.items()
            if tokens.intersection(words)}


def scan(db, rules):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        source TEXT, source_product_url TEXT, slug TEXT, first_seen_at TEXT, last_seen_at TEXT,
        sitemap_lastmod TEXT, categories TEXT, classification_method TEXT, matches TEXT,
        discovery_status TEXT, PRIMARY KEY(source, source_product_url));
    CREATE TABLE IF NOT EXISTS product_runs (
        id INTEGER PRIMARY KEY, source TEXT, fetched_at TEXT, result TEXT);
    """)
    now = datetime.now(timezone.utc).isoformat()
    result = dict(fetched_at=now, source="toolify", status="success", new=[], disappeared=[],
                  category_counts={}, priority=[])
    with db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current = crawl()
        except (OSError, HTTPException, ValueError, ET.ParseError) as exc:
            result.update(status="failed", error=str(exc))
        else:
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
        db.execute("INSERT INTO product_runs(source,fetched_at,result) VALUES ('toolify',?,?)",
                   (now, json.dumps(result, ensure_ascii=False)))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--config", type=Path, default=Path("config.example.json"))
    parser.add_argument("--out", type=Path, default=Path("toolify.json"))
    args = parser.parse_args()
    settings = json.loads(args.config.read_text()).get("toolify", {})
    args.state.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(args.state / "radar.db", timeout=180)) as db:
        result = scan(db, settings.get("rules", RULES))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    summary = f"Toolify {result['status']} | new {result['new_count']} | missing {len(result['disappeared'])}"
    print(summary)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if result["new"] and token and chat:
        data = urlencode(dict(chat_id=chat, text=summary)).encode()
        try:
            with urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15) as response:
                if not json.load(response).get("ok"):
                    raise ValueError("sendMessage failed")
        except (OSError, ValueError):
            raise SystemExit("Telegram delivery unconfirmed; batch retained in product_runs")
    return int(result["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
