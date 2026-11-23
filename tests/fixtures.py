"""Builds a fake Apple News container on disk, with made-up stories.

Everything here is synthetic: no real article text, ids or images. The byte
layouts mirror what News.app writes (see docs/FORMATS.md).
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import tempfile
import time
from typing import Dict, List, Optional

from apple_news_relay.newscache import NewsPaths
from apple_news_relay.protobuf import encode_field as F, encode_timestamp, encode_varint

NOW = 1_790_000_000.0  # a fixed "now" for timestamps


def container(entries: List[bytes]) -> bytes:
    """``00 07`` header, a count, then one field-2 blob per entry."""
    body = F(1, len(entries))
    for e in entries:
        body += F(2, e)
    body += F(3, b"bplist00\xd0\x08" + b"\x00" * 24)  # trailing metadata, ignored
    return b"\x00\x07" + body


def entry(key: str, record: bytes, field: int = 7) -> bytes:
    k = key.encode()
    return b"\x02" + encode_varint(len(k)) + k + F(1, 5) + F(field, record)


def article_record(article_id: str, title: str, channel: str, published: float, *, excerpt="An excerpt.",
                   byline="A. Writer", url: Optional[str] = None, asset: Optional[str] = None, paid=False,
                   sponsored=False, topics=(), body_length=2300, kind=1) -> bytes:
    base = F(1, article_id) + F(2, "tag1") + F(4, encode_timestamp(published + 60))
    rec = F(1, base) + F(2, article_id) + F(3, title) + F(5, channel) + F(6, encode_timestamp(published))
    rec += F(9, "https://c.apple.news/P/1/Athumb%s/imgfile?a=x" % article_id) + F(10, 300 << 36 | 200 << 8 | 1)
    rec += F(32, "https://c.apple.news/P/1/Abig%s/imgfile?a=x" % article_id) + F(33, 1600 << 36 | 900 << 8 | 1)
    rec += F(11, "GENERAL") + F(13, excerpt) + F(14, byline) + F(16, 2)
    if url:
        rec += F(15, url)
    if asset:
        rec += F(18, "https://c.apple.news/P/1/%s/imgfile?a=doc" % asset)
    rec += F(21, int(paid)) + F(22, 0) + F(23, int(sponsored)) + F(45, int(paid))
    for t in topics:
        rec += F(25, t)
    rec += F(26, "TunrelatedTagXXXXXXXXXX")
    rec += F(68, "en") + F(69, kind) + F(74, body_length) + F(87, "#203040") + F(88, "#ff6600")
    rec += F(91, byline)
    return rec


def tag_record(tag_id: str, name: str, kind: int) -> bytes:
    base = F(1, tag_id)
    return F(1, base) + F(2, kind) + F(3, name) + F(22, "https://c.apple.news/P/1/Alogo/imgfile") + F(63, "en")


def anf_document(title: str, *, paragraphs: int = 6, with_ads=True, with_promo=True) -> dict:
    comps: List[dict] = [
        {"role": "title", "text": title},
        {"role": "byline", "text": "By A. Writer"},
        {"role": "photo", "imageIdentifier": "img1"},
        {"role": "caption", "text": "A caption."},
    ]
    if with_ads:
        comps.append({"role": "banner_advertisement", "bannerType": "any"})
    for i in range(paragraphs):
        comps.append({"role": "body", "text": "Paragraph %d of the story, long enough to read like prose. " % (i + 1) * 3})
        if i == 1:
            comps.append({"role": "heading2", "text": "A subhead"})
            if with_ads:
                comps.append({"role": "medium_rectangle_advertisement"})
    comps.append({"role": "container", "components": [{"role": "quote", "text": "A quoted line."}]})
    if with_promo:
        comps.append({"role": "aside", "components": [{"role": "body", "text": "Sign up for our newsletter."}]})
        comps.append({"role": "link_button", "text": "Subscribe", "action": {"URL": "https://example.com/subscribe"}})
        comps.append({"role": "body", "text": "Follow us in Apple News"})
    return {
        "version": "1.0", "identifier": "synthetic", "title": title, "language": "en",
        "metadata": {"canonicalURL": "https://example.com/" + title.lower().replace(" ", "-")},
        "resources": {"r1": {"URL": "asset://11111111-2222-3333-4444-555555555555", "imageIdentifier": "img1",
                             "dimensions": {"width": 1200, "height": 800}, "type": "image"}},
        "advertisement": {"enabled": True}, "advertisingSettings": {"frequency": 10},
        "components": comps,
    }


class FakeNews:
    """A temporary News container with helpers to change it like News.app would."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.paths = NewsPaths(container=os.path.join(root, "Library"), group=os.path.join(root, "Group"))
        os.makedirs(os.path.join(self.paths.caches, "com.apple.news.public-production-143441"))
        os.makedirs(self.paths.assets)
        os.makedirs(self.paths.private)
        os.makedirs(self.paths.group)
        self.articles: Dict[str, bytes] = {}
        self.tags: Dict[str, bytes] = {}
        self.write_entitlement(False)

    def close(self):
        import shutil
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def _touch_later(self, path: str):
        # mtime must move forward even when tests write twice within a second
        t = os.path.getmtime(path) + 2 if os.path.exists(path) else time.time()
        os.utime(path, (t, t))

    def add_tag(self, tag_id: str, name: str, kind: int = 2):
        self.tags[tag_id] = tag_record(tag_id, name, kind)
        self.write_tags()

    def write_tags(self):
        path = self.paths.record_source("tag-record-source")
        with open(path, "wb") as f:
            f.write(container([entry(k, v) for k, v in self.tags.items()]))
        self._touch_later(path)

    def add_article(self, article_id: str, title: str, channel: str = "TchannelAAAAAAAAAAAAAAA", **kw):
        kw.setdefault("published", NOW - len(self.articles) * 600)
        self.articles[article_id] = article_record(article_id, title, channel, **kw)
        self.write_articles()

    def drop_article(self, article_id: str):
        """News pruning its cache."""
        self.articles.pop(article_id, None)
        self.write_articles()

    def write_articles(self):
        path = self.paths.record_source("article-record-source")
        with open(path, "wb") as f:
            f.write(container([entry(k, v) for k, v in self.articles.items()]))
        self._touch_later(path)

    def add_document(self, asset: str, doc: dict):
        path = os.path.join(self.paths.assets, ":P:1:%s:imgfile" % asset)
        with open(path, "w") as f:
            json.dump(doc, f)
        self._touch_later(path)

    def remove_document(self, asset: str):
        os.remove(os.path.join(self.paths.assets, ":P:1:%s:imgfile" % asset))

    def write_feed(self, groups: List[dict], cursor: str = "CURSOR-2", created: float = NOW):
        db = self.paths.today_feed_db
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cursors (id TEXT PRIMARY KEY, createdDate DOUBLE, kind TEXT);
            CREATE TABLE IF NOT EXISTS groups (id TEXT, kindId TEXT, kind TEXT, tagId TEXT, createdDate DOUBLE,
                cursorId TEXT, itemIds BLOB, PRIMARY KEY (id, cursorId));""")
        conn.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?, 'repooled')", (cursor, created))
        for i, g in enumerate(groups):
            conn.execute("INSERT OR REPLACE INTO groups VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (g.get("id", "g%d" % i), g["kindId"], g.get("kind", "curated"), g.get("tagId"),
                          created + i, cursor, json.dumps(g["items"])))
        conn.commit()
        conn.close()
        self._touch_later(db)

    def write_saved(self, ids: List[str]):
        first = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$objects": ["$null"]}, fmt=plistlib.FMT_BINARY)
        import datetime as dt
        second = plistlib.dumps({"rl-" + i: {"articleID": i, "origin": 0,
                                             "dateAdded": dt.datetime(2026, 11, 1, 12, n)} for n, i in enumerate(ids)},
                                fmt=plistlib.FMT_BINARY)
        with open(self.paths.reading_list, "wb") as f:
            f.write(b"\x00\x07\x08\x03\x12" + b"sync-state" + first + b"\x00\x00" + second)
        self._touch_later(self.paths.reading_list)

    def write_entitlement(self, subscribed: bool):
        archive = {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": plistlib.UID(1)},
                   "$objects": ["$null", {"subscriptionState": 1 if subscribed else 3, "isPurchaser": subscribed,
                                          "isPaidBundleViaOfferActivated": False,
                                          "initialPurchaseTimestamp": 1.7e9 if subscribed else 0.0}]}
        with open(self.paths.widget_data, "wb") as f:
            plistlib.dump(archive, f, fmt=plistlib.FMT_BINARY)
