#!/usr/bin/env python3
"""Build an RSS 2.0 feed for meedia.de, which does not publish one.

Standard library only. Each run reads the live /news listing, fetches only
articles it hasn't seen (cached in state.json), and rewrites the feed file.
The feed carries teasers and links, not the articles' full text.
"""

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://meedia.de"
LISTING_URL = f"{BASE}/news"
SITEMAP_URL = f"{BASE}/sitemaps/sitemap.news.xml"
USER_AGENT = "meedia-rss-personal/1.0"
REQUEST_DELAY = 1.0  # seconds between article fetches
SITEMAP_INTERVAL = timedelta(hours=24)
MAX_CACHED = 500

HERE = Path(__file__).resolve().parent
BERLIN = ZoneInfo("Europe/Berlin")
MONTHS = {
    name: i + 1
    for i, name in enumerate(
        "Januar Februar März April Mai Juni Juli August "
        "September Oktober November Dezember".split()
    )
}
ARTICLE_PATH = re.compile(r"/news/beitrag/(\d+)-[^\"'<>\s?#]+\.html")
SHOWN_DATE = re.compile(
    r'class="[^"]*\bnews-date"[^>]*>\s*'
    r"(\d{1,2})\.\s+(\S+)\s+(\d{4})\s+um\s+(\d{1,2}):(\d{2})"
)
JSON_LD = re.compile(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", re.S | re.I)
PAGE_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

DC = "http://purl.org/dc/elements/1.1/"
MEDIA = "http://search.yahoo.com/mrss/"
ATOM = "http://www.w3.org/2005/Atom"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def clean(text):
    """Decode entities, drop tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return " ".join(XML_ILLEGAL.sub("", text).split())


def find_news_article(page):
    for block in JSON_LD.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "NewsArticle":
            return data
    return None


def published_at(page, article):
    """Return the publication time as ISO 8601.

    Prefers the time shown on the page, which matches the sitemaps. The
    JSON-LD datePublished runs an hour early on current articles, so it is
    only a fallback.
    """
    match = SHOWN_DATE.search(page)
    if match:
        day, month, year, hour, minute = match.groups()
        month_number = MONTHS.get(html.unescape(month))
        if month_number:
            return datetime(
                int(year), month_number, int(day), int(hour), int(minute),
                tzinfo=BERLIN,
            ).isoformat()
    print(
        f"warning: no displayed date, using JSON-LD: {article['headline']}",
        file=sys.stderr,
    )
    return datetime.fromisoformat(article["datePublished"]).isoformat()


def image_url(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url")
    return value if isinstance(value, str) else None


def author_name(value):
    if isinstance(value, list):
        return ", ".join(filter(None, (author_name(v) for v in value)))
    if isinstance(value, dict):
        return clean(value.get("name"))
    return clean(value) if isinstance(value, str) else ""


def parse_article(path, page):
    """Extract feed fields, or return None if the page isn't an article.

    The site answers 200 for any unknown URL, so the status code proves
    nothing: a page only counts once it carries a NewsArticle block.
    """
    article = find_news_article(page)
    if not article or not article.get("headline"):
        return None
    keywords = article.get("keywords") or ""
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    # <title> is the visible headline; the JSON-LD one can have an SEO kicker
    # prepended ("MEEDIA - Die Jahresedition Die Vermarkter des Jahres").
    page_title = PAGE_TITLE.search(page)
    title = clean(page_title.group(1)) if page_title else ""
    return {
        "url": BASE + path,
        "title": title or clean(article["headline"]),
        "description": clean(article.get("description")),
        "author": author_name(article.get("author")),
        "published": published_at(page, article),
        "image": image_url(article.get("image")),
        "keywords": [k for k in (clean(k) for k in keywords) if k],
    }


def article_paths(text):
    """Map article id to site-relative path, in order of first appearance."""
    paths = {}
    for match in ARTICLE_PATH.finditer(text):
        paths.setdefault(match.group(1), match.group(0))
    return paths


def load_state(path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"articles": {}, "sitemap_checked": None}


def write_atomic(path, data):
    """Replace the file in one step so readers never see a partial write."""
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def discover(state, limit, now):
    """Return {id: path} of candidates; the listing failing is fatal."""
    candidates = article_paths(fetch(LISTING_URL))
    if not candidates:
        raise RuntimeError(f"no article links found at {LISTING_URL}")

    # The listing only reaches about 16 articles back, so the daily sitemap
    # seeds the first run and fills gaps after the machine has been off.
    checked = state.get("sitemap_checked")
    if not checked or now - datetime.fromisoformat(checked) >= SITEMAP_INTERVAL:
        try:
            newest = list(article_paths(fetch(SITEMAP_URL)).items())[:limit]
        except (OSError, urllib.error.URLError) as error:
            print(f"warning: sitemap unavailable: {error}", file=sys.stderr)
        else:
            candidates.update(dict(newest))
            state["sitemap_checked"] = now.isoformat()
    return candidates


def update_articles(state, candidates):
    """Fetch unseen candidates. Returns (fetched, failed) counts."""
    articles = state["articles"]
    fetched = failed = 0
    for article_id, path in candidates.items():
        if article_id in articles:
            continue
        if fetched or failed:
            time.sleep(REQUEST_DELAY)
        try:
            page = fetch(BASE + path)
            parsed = parse_article(path, page)
        except (OSError, urllib.error.URLError, ValueError, KeyError) as error:
            # Transient: not cached, so the next run retries it.
            print(f"warning: {path}: {error}", file=sys.stderr)
            failed += 1
            continue
        if parsed is None:
            print(f"note: not an article, ignoring: {path}", file=sys.stderr)
            articles[article_id] = {"skip": True}
        else:
            articles[article_id] = parsed
        fetched += 1
    return fetched, failed


def newest_first(articles):
    def key(entry):
        article_id, article = entry
        return datetime.fromisoformat(article["published"]), int(article_id)

    usable = [(i, a) for i, a in articles.items() if not a.get("skip")]
    return sorted(usable, key=key, reverse=True)


def prune(state):
    keep = dict(newest_first(state["articles"])[:MAX_CACHED])
    skipped = {i: a for i, a in state["articles"].items() if a.get("skip")}
    state["articles"] = {**keep, **skipped}


def build_feed(items, self_url, now):
    ET.register_namespace("dc", DC)
    ET.register_namespace("media", MEDIA)
    ET.register_namespace("atom", ATOM)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for tag, text in (
        ("title", "MEEDIA"),
        ("link", f"{BASE}/"),
        ("description", "Inoffizieller Feed der Nachrichten von meedia.de"),
        ("language", "de"),
        ("lastBuildDate", format_datetime(now)),
        ("ttl", "60"),
    ):
        ET.SubElement(channel, tag).text = text
    if self_url:
        ET.SubElement(
            channel,
            f"{{{ATOM}}}link",
            {"href": self_url, "rel": "self", "type": "application/rss+xml"},
        )
    for _, article in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = article["title"]
        ET.SubElement(item, "link").text = article["url"]
        guid = ET.SubElement(item, "guid", {"isPermaLink": "true"})
        guid.text = article["url"]
        published = datetime.fromisoformat(article["published"])
        ET.SubElement(item, "pubDate").text = format_datetime(published)
        if article["description"]:
            ET.SubElement(item, "description").text = article["description"]
        if article["author"]:
            ET.SubElement(item, f"{{{DC}}}creator").text = article["author"]
        for keyword in article["keywords"]:
            ET.SubElement(item, "category").text = keyword
        if article["image"]:
            ET.SubElement(
                item,
                f"{{{MEDIA}}}content",
                {"url": article["image"], "medium": "image"},
            )
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=HERE / "meedia.xml")
    parser.add_argument("--state", type=Path, default=HERE / "state.json")
    parser.add_argument("--limit", type=int, default=50, help="items in the feed")
    parser.add_argument("--self-url", help="public URL of the feed, if hosted")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    state = load_state(args.state)
    try:
        candidates = discover(state, args.limit, now)
    except (OSError, urllib.error.URLError, RuntimeError) as error:
        # Leaves the previous feed in place.
        print(f"error: {error}", file=sys.stderr)
        return 1

    fetched, failed = update_articles(state, candidates)
    prune(state)
    write_atomic(
        args.state,
        json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"),
    )

    items = newest_first(state["articles"])[: args.limit]
    if not items:
        print("error: no articles parsed, feed not written", file=sys.stderr)
        return 1
    write_atomic(args.output, build_feed(items, args.self_url, now))
    print(
        f"{len(items)} items in {args.output} "
        f"({fetched} fetched, {failed} failed)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
