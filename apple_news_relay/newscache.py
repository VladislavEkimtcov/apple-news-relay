"""Readers for the files Apple News keeps on a Mac.

Everything here is read-only. Apple News (News.app on macOS) keeps its state
in its sandbox container; the formats were worked out by observation and are
documented in docs/FORMATS.md. Field numbers that were inferred rather than
confirmed are marked "inferred" below.
"""

from __future__ import annotations

import base64
import calendar
import glob
import json
import os
import plistlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .protobuf import DecodeError, Message, read_varint

CONTAINER = os.path.expanduser("~/Library/Containers/com.apple.news/Data/Library")
GROUP = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.news/"
    "com.apple.news.public-com.apple.news.private-production"
)


@dataclass
class NewsPaths:
    """Where the interesting files live. Overridable for tests."""

    container: str = CONTAINER
    group: str = GROUP

    @property
    def caches(self) -> str:
        return os.path.join(self.container, "Caches", "News")

    @property
    def production(self) -> str:
        """Per-storefront cache directory (``...-production-143441`` for the US).
        If there are several, the most recently written one is used."""
        found = glob.glob(os.path.join(self.caches, "com.apple.news.public-production-*"))
        if not found:
            return os.path.join(self.caches, "com.apple.news.public-production-143441")
        return max(found, key=os.path.getmtime)

    @property
    def assets(self) -> str:
        return os.path.join(self.caches, "shared-assets-assetstore")

    @property
    def private(self) -> str:
        return os.path.join(
            self.container, "Application Support", "com.apple.news",
            "com.apple.news.public-com.apple.news.private-production",
        )

    def record_source(self, name: str) -> str:
        return os.path.join(self.production, name)

    @property
    def today_feed_db(self) -> str:
        return os.path.join(self.production, "today-feed-db")

    @property
    def reading_list(self) -> str:
        return os.path.join(self.private, "reading-list")

    @property
    def history(self) -> str:
        return os.path.join(self.private, "history")

    @property
    def widget_data(self) -> str:
        return os.path.join(self.group, "todayWidgetDropboxData")


# --------------------------------------------------------------------------
# Record containers
# --------------------------------------------------------------------------

def read_container(path: str) -> Iterator[Tuple[str, Message]]:
    """Read a News key/value container (``article-record-source``,
    ``tag-record-source``, ``reading-list``, ``history``...).

    Layout: two header bytes, then a protobuf message whose repeated field 2
    holds entries. Each entry is ``[kind:u8][key length:varint][key][message]``.
    Yields ``(key, entry_message)``; damaged entries are skipped.
    """
    with open(path, "rb") as f:
        data = f.read()
    pos = 2
    end = len(data)
    while pos < end:
        try:
            key, pos = read_varint(data, pos)
            wire = key & 7
            if wire == 0:
                _, pos = read_varint(data, pos)
                continue
            if wire != 2:
                return
            size, pos = read_varint(data, pos)
            blob, pos = data[pos:pos + size], pos + size
        except DecodeError:
            return
        if key >> 3 != 2 or len(blob) < 2:
            continue
        try:
            key_len, start = read_varint(blob, 1)
            entry_key = blob[start:start + key_len].decode("utf-8")
            yield entry_key, Message(blob[start + key_len:])
        except (DecodeError, UnicodeDecodeError):
            continue


# --------------------------------------------------------------------------
# Domain records
# --------------------------------------------------------------------------

@dataclass
class Image:
    url: str
    width: Optional[int] = None
    height: Optional[int] = None

    def to_dict(self) -> dict:
        return {"url": self.url, "width": self.width, "height": self.height}


def unpack_size(value: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    """Thumbnail sizes are packed as ``width << 36 | height << 8 | 1``."""
    if not value:
        return None, None
    return value >> 36, (value >> 8) & 0xFFFFFFF


def asset_id_from_url(url: Optional[str]) -> Optional[str]:
    """``https://c.apple.news/P/1/<asset>/imgfile?...`` -> ``<asset>``."""
    if not url:
        return None
    m = re.search(r"/P/1/([A-Za-z0-9_-]+)/", url)
    return m.group(1) if m else None


def asset_image_url(uuid: str, variant: str = "17") -> str:
    """URL for an ``asset://<uuid>`` image referenced inside an article body.

    The CDN key is base64url of ``02 01 <len> <uuid> 00 <variant>``; variant
    ``17`` serves the full image.
    """
    raw = b"\x02\x01" + bytes([len(uuid)]) + uuid.encode() + b"\x00" + variant.encode()
    return "https://c.apple.news/" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


ARTICLE_KINDS = {1: "article", 5: "apple_original", 6: "magazine", 8: "apple_original"}


@dataclass
class ArticleRecord:
    id: str
    title: str
    channel_id: Optional[str]
    published_at: Optional[float]
    modified_at: Optional[float] = None
    excerpt: Optional[str] = None
    byline: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    url: Optional[str] = None
    thumbnails: List[Image] = field(default_factory=list)
    document_asset: Optional[str] = None
    video_url: Optional[str] = None
    paid: bool = False
    issue_only: bool = False
    sponsored: bool = False
    issue_id: Optional[str] = None
    kind: str = "article"
    language: Optional[str] = None
    maturity: Optional[str] = None
    topic_ids: List[str] = field(default_factory=list)
    related_ids: List[str] = field(default_factory=list)
    body_length: Optional[int] = None
    colors: Dict[str, str] = field(default_factory=dict)
    narration_url: Optional[str] = None

    @classmethod
    def from_message(cls, m: Message) -> Optional["ArticleRecord"]:
        article_id = m.string(2)
        title = m.string(3)
        if not article_id or not title:
            return None
        base = m.message(1)
        thumbs = []
        for url_field, size_field in ((9, 10), (32, 33), (37, 38), (39, 40)):
            url = m.string(url_field)
            if url:
                w, h = unpack_size(m.int(size_field))
                if not any(t.url == url for t in thumbs):
                    thumbs.append(Image(url, w, h))
        thumbs.sort(key=lambda t: t.width or 0)
        topics: List[str] = []
        for f in (24, 25):  # 26 holds unrelated tags
            for t in m.strings(f):
                if t not in topics and t != m.string(5):
                    topics.append(t)
        colors = {}
        for name, f in (("background", 87), ("accent", 88), ("text", 89), ("background_dark", 90)):
            c = m.string(f)
            if c:
                colors[name] = c
        return cls(
            id=article_id,
            title=title,
            channel_id=m.string(5),
            published_at=m.timestamp(6),
            modified_at=base.timestamp(4) if base else None,
            excerpt=m.string(13),
            byline=m.string(14),
            authors=m.strings(91),
            url=m.string(15),
            thumbnails=thumbs,
            document_asset=asset_id_from_url(m.string(18)),
            video_url=m.string(20),
            paid=m.bool(21) or m.bool(45),
            issue_only=m.bool(22),
            sponsored=m.bool(23),  # inferred: set on affiliate "deal" posts
            issue_id=m.string(67),
            kind=ARTICLE_KINDS.get(m.int(69, 1) or 1, "other"),
            language=m.string(68),
            maturity=m.string(11),
            topic_ids=topics,
            related_ids=m.strings(30) + [i for i in m.strings(71) if i not in m.strings(30)],
            body_length=m.int(74),
            colors=colors,
            narration_url=m.string(80),
        )


TAG_KINDS = {1: "topic", 2: "channel", 3: "section"}


@dataclass
class TagRecord:
    id: str
    name: str
    kind: str
    language: Optional[str] = None
    logo: Optional[str] = None
    path: List[str] = field(default_factory=list)

    @classmethod
    def from_message(cls, m: Message, key: Optional[str] = None) -> Optional["TagRecord"]:
        base = m.message(1)
        tag_id = key or (base.string(1) if base is not None else None)
        name = m.string(3)
        if not tag_id or not name:
            return None
        return cls(
            id=tag_id,
            name=name,
            kind=TAG_KINDS.get(m.int(2, 0) or 0, "other"),
            language=m.string(63),
            logo=m.string(22) or m.string(78),
            path=m.strings(91),
        )


def _record(entry: Message) -> Optional[Message]:
    return entry.message(7)


def read_articles(paths: NewsPaths) -> Iterator[ArticleRecord]:
    path = paths.record_source("article-record-source")
    if not os.path.exists(path):
        return
    for _, entry in read_container(path):
        m = _record(entry)
        if m is not None:
            rec = ArticleRecord.from_message(m)
            if rec:
                yield rec


def read_tags(paths: NewsPaths) -> Iterator[TagRecord]:
    path = paths.record_source("tag-record-source")
    if not os.path.exists(path):
        return
    for key, entry in read_container(path):
        m = _record(entry)
        if m is not None:
            rec = TagRecord.from_message(m, key)
            if rec:
                yield rec


# --------------------------------------------------------------------------
# Library: saved stories and reading history
# --------------------------------------------------------------------------

@dataclass
class SavedStory:
    article_id: str
    saved_at: Optional[float]


def read_saved(paths: NewsPaths) -> List[SavedStory]:
    """Saved Stories. ``reading-list`` holds two embedded binary plists: an
    NSKeyedArchiver sync token, then a dict of ``rl-<id>`` -> {articleID,
    dateAdded, origin}."""
    path = paths.reading_list
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        data = f.read()
    starts = [m.start() for m in re.finditer(b"bplist00", data)]
    for start in reversed(starts):
        try:
            obj = plistlib.loads(data[start:], fmt=plistlib.FMT_BINARY)
        except Exception:
            continue
        if isinstance(obj, dict) and any(isinstance(v, dict) and "articleID" in v for v in obj.values()):
            out = []
            for v in obj.values():
                if isinstance(v, dict) and v.get("articleID"):
                    out.append(SavedStory(v["articleID"], _plist_time(v.get("dateAdded"))))
            out.sort(key=lambda s: s.saved_at or 0, reverse=True)
            return out
    return []


def _plist_time(dt) -> Optional[float]:
    """plistlib returns naive datetimes that are in UTC."""
    if dt is None:
        return None
    return calendar.timegm(dt.timetuple()) + dt.microsecond / 1e6


@dataclass
class HistoryItem:
    article_id: str
    read_at: Optional[float]


def read_history(paths: NewsPaths) -> List[HistoryItem]:
    """Reading history: container keys ``art-<id>``; the entry's field 8 holds
    {1: article id, 5/6: dates}."""
    path = paths.history
    if not os.path.exists(path):
        return []
    out = []
    for key, entry in read_container(path):
        if not key.startswith("art-"):
            continue
        rec = entry.message(8)
        if rec is None:
            continue
        dates = [d for d in (rec.timestamp(5), rec.timestamp(6)) if d]
        out.append(HistoryItem(rec.string(1) or key[4:], max(dates) if dates else None))
    out.sort(key=lambda h: h.read_at or 0, reverse=True)
    return out


# --------------------------------------------------------------------------
# Today feed
# --------------------------------------------------------------------------

@dataclass
class Section:
    id: str
    kind: str            # e.g. "TOP_STORIES", "FOR_YOU", "TRENDING"
    title: str
    group_type: str      # curated / forYou / trending / engagement / mySports ...
    tag_id: Optional[str]
    created_at: Optional[float]
    article_ids: List[str]

    @property
    def promotional(self) -> bool:
        """Engagement groups are News+ upsell messaging, not stories."""
        return self.group_type == "engagement" or "SMARTER_MESSAGING" in self.kind


def section_title(kind_id: str) -> str:
    """``CUSTOM:POLITICS,_HEADLINES`` -> ``Politics, Headlines``;
    ``TOP_STORIES:DEFAULT`` -> ``Top Stories``."""
    head, _, rest = kind_id.partition(":")
    name = rest if head in ("CUSTOM", "EXTERNAL") and rest else head
    name = re.sub(r"_\(.*$", "", name)
    words = [w for w in re.split(r"_+", name) if w]
    special = {"FOR": "For", "YOU": "You", "MY": "My"}
    return " ".join(special.get(w, w.capitalize()) for w in words) or kind_id


def read_sections(paths: NewsPaths) -> List[Section]:
    """Today feed sections from ``today-feed-db`` (SQLite), in feed order."""
    db = paths.today_feed_db
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    try:
        # The feed is rebuilt into a new cursor on every refresh; older cursors
        # linger. The newest cursor that has groups is the feed on screen.
        rows = conn.execute(
            "SELECT g.id, g.kindId, g.kind, g.tagId, g.createdDate, g.itemIds FROM groups g "
            "WHERE g.cursorId = (SELECT g2.cursorId FROM groups g2 JOIN cursors c ON c.id = g2.cursorId "
            "ORDER BY c.createdDate DESC LIMIT 1) ORDER BY g.createdDate, g.rowid"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for gid, kind_id, group_type, tag_id, created, items in rows:
        try:
            ids = json.loads(items if isinstance(items, str) else items.decode())
        except Exception:
            ids = []
        out.append(Section(gid, kind_id or "", section_title(kind_id or gid), group_type or "", tag_id,
                           created, [i for i in ids if isinstance(i, str)]))
    return out


# --------------------------------------------------------------------------
# Article bodies (Apple News Format documents)
# --------------------------------------------------------------------------

def document_path(paths: NewsPaths, asset_id: str) -> str:
    return os.path.join(paths.assets, ":P:1:%s:imgfile" % asset_id)


def read_document(paths: NewsPaths, asset_id: str) -> Optional[dict]:
    """Load the ANF JSON for a document asset, or None if it isn't cached."""
    path = document_path(paths, asset_id)
    try:
        with open(path, "rb") as f:
            if f.read(1) != b"{":
                return None
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and "components" in doc else None


def iter_cached_document_assets(paths: NewsPaths, since: float = 0) -> Iterator[Tuple[str, float]]:
    """Yield ``(asset_id, mtime)`` for cached files that may be ANF documents."""
    try:
        names = os.listdir(paths.assets)
    except OSError:
        return
    for name in names:
        if not (name.startswith(":P:1:") and name.endswith(":imgfile")):
            continue
        full = os.path.join(paths.assets, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if mtime > since:
            yield name[5:-8], mtime


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------

@dataclass
class Entitlement:
    news_plus: bool
    detail: str


def read_entitlement(paths: NewsPaths) -> Entitlement:
    """Whether the signed-in account can read Apple News+ stories.

    Conservative: only a positive purchase signal counts. Family-shared
    subscriptions are not detected yet, so they read as "no".
    """
    try:
        with open(paths.widget_data, "rb") as f:
            archive = plistlib.load(f)
    except Exception as e:  # missing file, unreadable, not a plist
        return Entitlement(False, "subscription data unavailable (%s)" % type(e).__name__)
    objects = archive.get("$objects", [])

    def deref(v):
        return objects[v.data] if isinstance(v, plistlib.UID) else v

    for obj in objects:
        if isinstance(obj, dict) and "subscriptionState" in obj:
            purchaser = bool(deref(obj.get("isPurchaser")))
            offer = bool(deref(obj.get("isPaidBundleViaOfferActivated")))
            first = deref(obj.get("initialPurchaseTimestamp")) or 0
            entitled = purchaser or offer or (isinstance(first, (int, float)) and first > 0)
            return Entitlement(entitled, "News+ %s (state %s)" % ("active" if entitled else "not active", deref(obj.get("subscriptionState"))))
    return Entitlement(False, "no subscription record")
