"""The relay's own database.

Apple News prunes its cache aggressively (a refresh can drop a third of the
article records, and cached bodies are evicted within minutes), so the relay
copies everything it sees into its own SQLite file and keeps it. That makes the
API stable and the archive useful for looking back: every article remembers
when it was first and last seen, and every Today section remembers which
stories it carried and when.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from collections import Counter, OrderedDict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .newscache import ArticleRecord, FeedItem, HistoryItem, SavedStory, Section, TagRecord

SCHEMA_VERSION = 2

#: Section depth (see compose_sections): stories from a topic's or channel's feed published in the
#: last DEEP_WINDOW, at least DEEP_MIN when the feed has them within DEEP_FALLBACK; sections with no
#: feed keep up to EARLIER_MAX stories they carried in the last EARLIER_WINDOW.
DEEP_WINDOW = 48 * 3600.0
DEEP_FALLBACK = 7 * 86400.0
DEEP_MIN = 10
EARLIER_WINDOW = 24 * 3600.0
EARLIER_MAX = 20
SECTION_MAX = 60
PER_CLUSTER = 2
#: Converted bodies kept in memory (anf.convert costs up to ~10 ms for a long story).
BODY_CACHE = 300
LATEST_SIZE = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- channel | topic | other
    language TEXT,
    logo TEXT,
    path TEXT,                       -- JSON list of parent topic names
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tags_kind_name ON tags(kind, name);

CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    excerpt TEXT,
    byline TEXT,
    authors TEXT,                    -- JSON list
    url TEXT,
    channel_id TEXT,
    published_at REAL,
    modified_at REAL,
    thumbnails TEXT,                 -- JSON list of {url,width,height}
    document_asset TEXT,
    video_url TEXT,
    paid INTEGER NOT NULL DEFAULT 0,
    issue_only INTEGER NOT NULL DEFAULT 0,
    sponsored INTEGER NOT NULL DEFAULT 0,
    issue_id TEXT,
    kind TEXT,
    language TEXT,
    maturity TEXT,
    topic_ids TEXT,                  -- JSON list
    related_ids TEXT,                -- JSON list
    body_length INTEGER,
    colors TEXT,                     -- JSON object
    narration_url TEXT,
    has_body INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_published ON articles(published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS articles_first_seen ON articles(first_seen DESC, id DESC);
CREATE INDEX IF NOT EXISTS articles_channel ON articles(channel_id, published_at DESC);
CREATE INDEX IF NOT EXISTS articles_asset ON articles(document_asset);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    PRIMARY KEY (article_id, tag_id)
);
CREATE INDEX IF NOT EXISTS article_tags_tag ON article_tags(tag_id);

CREATE TABLE IF NOT EXISTS documents (
    asset_id TEXT PRIMARY KEY,
    article_id TEXT,                 -- NULL until the article record is seen
    document BLOB NOT NULL,          -- zlib-compressed ANF JSON
    title TEXT,
    captured_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_article ON documents(article_id);

CREATE TABLE IF NOT EXISTS sections (
    slug TEXT PRIMARY KEY,           -- stable id derived from the section kind
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    group_type TEXT,
    tag_id TEXT,
    promotional INTEGER NOT NULL DEFAULT 0,
    position INTEGER,                -- order in the current feed, NULL if gone
    article_ids TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL,
    first_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS placements (
    section_slug TEXT NOT NULL,
    article_id TEXT NOT NULL,
    best_position INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (section_slug, article_id)
);
CREATE INDEX IF NOT EXISTS placements_article ON placements(article_id);

CREATE TABLE IF NOT EXISTS saved (
    article_id TEXT PRIMARY KEY,
    saved_at REAL,
    removed_at REAL
);

CREATE TABLE IF NOT EXISTS reactions (
    article_id TEXT PRIMARY KEY,
    reaction TEXT NOT NULL,          -- like | dislike, as last set or seen through the relay
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    article_id TEXT PRIMARY KEY,
    read_at REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT,
    state TEXT NOT NULL,             -- queued | running | succeeded | failed | cancelled
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    article_id UNINDEXED, title, excerpt, byline, body, tokenize = 'porter unicode61'
);

-- Schema 2: channel and topic feeds, and the sections built from them.
CREATE TABLE IF NOT EXISTS feed_items (
    tag_id TEXT NOT NULL,            -- the channel or topic whose feed listed the story
    article_id TEXT NOT NULL,
    feed_order INTEGER,
    published_at REAL,
    cluster_id TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (tag_id, article_id)
);
CREATE INDEX IF NOT EXISTS feed_items_tag_published ON feed_items(tag_id, published_at DESC);
CREATE INDEX IF NOT EXISTS feed_items_article ON feed_items(article_id);

CREATE TABLE IF NOT EXISTS feed_state (
    feed_id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,         -- changes when News refreshes or extends the feed
    scanned_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS section_items (
    slug TEXT NOT NULL,
    position INTEGER NOT NULL,
    article_id TEXT NOT NULL,
    source TEXT NOT NULL,            -- today | feed | channel | earlier
    PRIMARY KEY (slug, position)
);

CREATE INDEX IF NOT EXISTS placements_section ON placements(section_slug, last_seen DESC);
"""


def default_path() -> str:
    return os.path.expanduser("~/Library/Application Support/apple-news-relay/relay.db")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def slugify(kind: str) -> str:
    """``CUSTOM:POLITICS,_HEADLINES`` -> ``politics-headlines``."""
    import re
    head, _, rest = kind.partition(":")
    name = rest if head in ("CUSTOM", "EXTERNAL") and rest else head
    name = re.sub(r"_\(.*$", "", name)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "section"


class _Rows(list):
    """Rows already fetched, with the cursor methods readers use."""

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


class _TakingTurns:
    """An in-memory database has a single connection; readers on other threads take turns on it
    under the store's lock and get their rows fetched in full."""

    def __init__(self, conn: sqlite3.Connection, lock):
        self._conn, self._lock = conn, lock

    def execute(self, sql: str, params=()) -> _Rows:
        with self._lock:
            return _Rows(self._conn.execute(sql, params).fetchall())


class Store:
    """Thread-safe access to the relay database. Each thread keeps one read-only connection;
    writes go through one lock."""

    def __init__(self, path: Optional[str] = None, section_window: float = DEEP_WINDOW):
        self.path = path or default_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._writer = self._open()
        self.section_window = section_window
        self.clock = time.time  # what "now" is for section windows (tests set it)
        # Grows whenever what a front page shows changes (sections, Latest, Saved); never goes back,
        # even across restarts, so clients can use it as a validator.
        self.front_version = int(time.time() * 1000)
        self._latest: Tuple[str, ...] = ()
        self._bodies: "OrderedDict[Tuple[str, bool], object]" = OrderedDict()
        self._bodies_lock = threading.Lock()
        with self._lock:
            self._writer.executescript(SCHEMA)
            self._writer.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._writer.commit()

    def _open(self, reader: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if reader:
            conn.execute("PRAGMA query_only=1")
            return conn
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        return conn

    def read(self):
        """A connection for reading, kept per thread. In-memory stores share the writer, one
        thread at a time."""
        if self.path == ":memory:":
            return _TakingTurns(self._writer, self._lock)
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._open(reader=True)
        return conn

    # --------------------------------------------------------- front version

    def bump_front(self) -> int:
        """Something a front page shows changed. Returns the new version."""
        with self._lock:
            self.front_version = max(self.front_version + 1, int(time.time() * 1000))
            return self.front_version

    def latest_changed(self) -> bool:
        """Whether the newest stories (the front page's Latest) differ from the last look."""
        with self._lock:
            ids = tuple(r[0] for r in self._writer.execute(
                "SELECT id FROM articles WHERE sponsored = 0 ORDER BY COALESCE(published_at, 0) DESC, id DESC "
                "LIMIT ?", (LATEST_SIZE,)))
            changed = ids != self._latest
            self._latest = ids
            return changed

    # ------------------------------------------------------ converted bodies

    def converted(self, article_id: str, keep_promotions: bool = False, conn=None):
        """The story's body as ``anf.Document``, converted once and kept (``BODY_CACHE`` of them)."""
        key = (article_id, bool(keep_promotions))
        with self._bodies_lock:
            hit = self._bodies.get(key)
            if hit is not None:
                self._bodies.move_to_end(key)
                return hit
        doc = self.document_for(article_id, conn=conn)
        if doc is None:
            return None
        from .anf import convert
        result = convert(doc, keep_promotions=keep_promotions)
        self.remember_converted(article_id, keep_promotions, result)
        return result

    def remember_converted(self, article_id: str, keep_promotions: bool, result):
        key = (article_id, bool(keep_promotions))
        with self._bodies_lock:
            self._bodies[key] = result
            self._bodies.move_to_end(key)
            while len(self._bodies) > BODY_CACHE:
                self._bodies.popitem(last=False)

    def forget_converted(self, article_id: str):
        with self._bodies_lock:
            for keep in (False, True):
                self._bodies.pop((article_id, keep), None)

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._writer.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        with self._lock:
            self._writer.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self._writer.commit()

    # ------------------------------------------------------------ ingestion

    def upsert_tags(self, tags: Iterable[TagRecord], now: Optional[float] = None) -> int:
        now = now or time.time()
        rows = [(t.id, t.name, t.kind, t.language, t.logo, dumps(t.path), now, now) for t in tags]
        with self._lock:
            before = self._writer.total_changes
            self._writer.executemany(
                """INSERT INTO tags (id, name, kind, language, logo, path, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET name = excluded.name, kind = excluded.kind,
                     language = excluded.language, logo = COALESCE(excluded.logo, tags.logo),
                     path = excluded.path, last_seen = excluded.last_seen""", rows)
            self._writer.commit()
            return self._writer.total_changes - before

    def upsert_articles(self, articles: Iterable[ArticleRecord], now: Optional[float] = None) -> List[str]:
        """Insert or refresh article records. Returns the ids that were new."""
        now = now or time.time()
        new_ids: List[str] = []
        with self._lock:
            cur = self._writer.cursor()
            for a in articles:
                exists = cur.execute("SELECT 1 FROM articles WHERE id = ?", (a.id,)).fetchone()
                cur.execute(
                    """INSERT INTO articles (id, title, excerpt, byline, authors, url, channel_id, published_at,
                         modified_at, thumbnails, document_asset, video_url, paid, issue_only, sponsored, issue_id,
                         kind, language, maturity, topic_ids, related_ids, body_length, colors, narration_url,
                         first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET title = excluded.title, excerpt = excluded.excerpt,
                         byline = excluded.byline, authors = excluded.authors, url = excluded.url,
                         channel_id = excluded.channel_id, published_at = excluded.published_at,
                         modified_at = excluded.modified_at, thumbnails = excluded.thumbnails,
                         document_asset = COALESCE(excluded.document_asset, articles.document_asset),
                         video_url = excluded.video_url, paid = excluded.paid, issue_only = excluded.issue_only,
                         sponsored = excluded.sponsored, issue_id = excluded.issue_id, kind = excluded.kind,
                         language = excluded.language, maturity = excluded.maturity, topic_ids = excluded.topic_ids,
                         related_ids = excluded.related_ids, body_length = excluded.body_length,
                         colors = excluded.colors, narration_url = excluded.narration_url,
                         last_seen = excluded.last_seen""",
                    (a.id, a.title, a.excerpt, a.byline, dumps(a.authors), a.url, a.channel_id, a.published_at,
                     a.modified_at, dumps([t.to_dict() for t in a.thumbnails]), a.document_asset, a.video_url,
                     int(a.paid), int(a.issue_only), int(a.sponsored), a.issue_id, a.kind, a.language, a.maturity,
                     dumps(a.topic_ids), dumps(a.related_ids), a.body_length, dumps(a.colors), a.narration_url,
                     now, now))
                cur.executemany("INSERT OR IGNORE INTO article_tags VALUES (?, ?)",
                                [(a.id, t) for t in ([a.channel_id] if a.channel_id else []) + a.topic_ids])
                if not exists:
                    new_ids.append(a.id)
                    cur.execute("INSERT INTO search (article_id, title, excerpt, byline, body) VALUES (?, ?, ?, ?, '')",
                                (a.id, a.title, a.excerpt or "", a.byline or ""))
                # A body captured before its record arrived can be linked now.
                if a.document_asset:
                    cur.execute("UPDATE documents SET article_id = ? WHERE asset_id = ? AND article_id IS NULL",
                                (a.id, a.document_asset))
                    if cur.rowcount:
                        self._index_body(cur, a.id)
            self._writer.commit()
        return new_ids

    def save_document(self, asset_id: str, doc: dict, body_text: str, now: Optional[float] = None) -> Optional[str]:
        """Keep an ANF document. Returns the article id it belongs to, if known."""
        now = now or time.time()
        blob = zlib.compress(dumps(doc).encode("utf-8"), 6)
        with self._lock:
            cur = self._writer.cursor()
            row = cur.execute("SELECT id FROM articles WHERE document_asset = ?", (asset_id,)).fetchone()
            article_id = row[0] if row else None
            cur.execute(
                """INSERT INTO documents (asset_id, article_id, document, title, captured_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(asset_id) DO UPDATE SET document = excluded.document,
                     article_id = COALESCE(excluded.article_id, documents.article_id),
                     title = excluded.title, captured_at = excluded.captured_at""",
                (asset_id, article_id, blob, doc.get("title"), now))
            if article_id:
                self._index_body(cur, article_id, body_text)
            self._writer.commit()
        if article_id:
            self.forget_converted(article_id)
        return article_id

    def _index_body(self, cur: sqlite3.Cursor, article_id: str, body_text: Optional[str] = None):
        cur.execute("UPDATE articles SET has_body = 1 WHERE id = ?", (article_id,))
        if body_text is None:
            doc = self.document_for(article_id, conn=cur.connection)
            if doc is None:
                return
            from .anf import convert
            body_text = "\n".join(b.get("text", "") for b in convert(doc).blocks)
        cur.execute("UPDATE search SET body = ? WHERE article_id = ?", (body_text, article_id))

    def document_for(self, article_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
        conn = conn or self.read()
        row = conn.execute(
            "SELECT document FROM documents WHERE article_id = ? ORDER BY captured_at DESC LIMIT 1", (article_id,)
        ).fetchone()
        if not row:
            return None
        return json.loads(zlib.decompress(row[0]).decode("utf-8"))

    def has_document(self, asset_id: str) -> bool:
        with self._lock:
            return self._writer.execute("SELECT 1 FROM documents WHERE asset_id = ?", (asset_id,)).fetchone() is not None

    def _current_sections(self, cur) -> Dict[str, tuple]:
        return {r[0]: tuple(r[1:]) for r in cur.execute(
            "SELECT slug, title, position, article_ids, promotional, tag_id FROM sections WHERE position IS NOT NULL")}

    def update_sections(self, sections: List[Section], now: Optional[float] = None) -> bool:
        """Record the current Today feed and extend placement history. Returns whether the
        sections (titles, order or stories) changed; placement history alone doesn't count."""
        now = now or time.time()
        with self._lock:
            cur = self._writer.cursor()
            before = self._current_sections(cur)
            cur.execute("UPDATE sections SET position = NULL")
            seen = set()
            for pos, s in enumerate(sections):
                title = s.title
                slug = slugify(s.kind)
                # Topic and channel groups are titled after their tag ("Drones").
                if s.tag_id and any(k in s.kind for k in ("TOPIC", "CHANNEL", "MAGAZINE")):
                    row = cur.execute("SELECT name FROM tags WHERE id = ?", (s.tag_id,)).fetchone()
                    if row:
                        title = row[0]
                        slug = "%s-%s" % (slug, slugify("CUSTOM:" + row[0]))
                if slug in seen:
                    slug = "%s-%d" % (slug, pos)
                seen.add(slug)
                ids = dumps(s.article_ids)
                cur.execute(
                    """INSERT INTO sections (slug, kind, title, group_type, tag_id, promotional, position, article_ids,
                         updated_at, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(slug) DO UPDATE SET kind = excluded.kind, title = excluded.title,
                         group_type = excluded.group_type, tag_id = COALESCE(excluded.tag_id, sections.tag_id),
                         promotional = excluded.promotional, position = excluded.position,
                         article_ids = excluded.article_ids,
                         updated_at = CASE WHEN sections.article_ids = excluded.article_ids
                                            AND sections.title = excluded.title
                                           THEN sections.updated_at ELSE excluded.updated_at END""",
                    (slug, s.kind, title, s.group_type, s.tag_id, int(s.promotional), pos, ids, now, now))
                for i, article_id in enumerate(s.article_ids):
                    cur.execute(
                        """INSERT INTO placements VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(section_slug, article_id) DO UPDATE SET
                             best_position = MIN(placements.best_position, excluded.best_position),
                             last_seen = excluded.last_seen""",
                        (slug, article_id, i, now, now))
            after = self._current_sections(cur)
            self._writer.commit()
        return before != after

    def update_saved(self, saved: List[SavedStory], now: Optional[float] = None) -> bool:
        """Mirror News's Saved Stories. Returns whether the set of saved stories changed."""
        now = now or time.time()
        with self._lock:
            cur = self._writer.cursor()
            before = self._saved_ids(cur)
            ids = [s.article_id for s in saved]
            for s in saved:
                cur.execute("""INSERT INTO saved VALUES (?, ?, NULL)
                               ON CONFLICT(article_id) DO UPDATE SET saved_at = excluded.saved_at, removed_at = NULL""",
                            (s.article_id, s.saved_at))
            if ids:
                cur.execute("UPDATE saved SET removed_at = ? WHERE removed_at IS NULL AND article_id NOT IN (%s)"
                            % ",".join("?" * len(ids)), [now] + ids)
            after = self._saved_ids(cur)
            self._writer.commit()
        return before != after

    @staticmethod
    def _saved_ids(cur) -> Set[str]:
        return {r[0] for r in cur.execute("SELECT article_id FROM saved WHERE removed_at IS NULL")}

    def set_saved(self, article_id: str, saved: bool, now: Optional[float] = None):
        """Records a save or unsave the relay made, before News's own list catches up. A change
        moves the front version on."""
        now = now or time.time()
        with self._lock:
            self.front_version = max(self.front_version + 1, int(time.time() * 1000))
            if saved:
                self._writer.execute("""INSERT INTO saved VALUES (?, ?, NULL) ON CONFLICT(article_id) DO UPDATE SET
                                        saved_at = COALESCE(saved.saved_at, excluded.saved_at), removed_at = NULL
                                        WHERE saved.removed_at IS NOT NULL OR saved.saved_at IS NULL""",
                                     (article_id, now))
                self._writer.execute("UPDATE saved SET saved_at = ? WHERE article_id = ? AND removed_at IS NULL "
                                     "AND saved_at IS NULL", (now, article_id))
            else:
                self._writer.execute("UPDATE saved SET removed_at = ? WHERE article_id = ? AND removed_at IS NULL",
                                     (now, article_id))
            self._writer.commit()

    def set_reaction(self, article_id: str, reaction: Optional[str], now: Optional[float] = None):
        with self._lock:
            if reaction:
                self._writer.execute("INSERT OR REPLACE INTO reactions VALUES (?, ?, ?)",
                                     (article_id, reaction, now or time.time()))
            else:
                self._writer.execute("DELETE FROM reactions WHERE article_id = ?", (article_id,))
            self._writer.commit()

    def update_history(self, items: List[HistoryItem]):
        with self._lock:
            self._writer.executemany(
                """INSERT INTO history VALUES (?, ?) ON CONFLICT(article_id) DO UPDATE SET
                   read_at = MAX(COALESCE(history.read_at, 0), COALESCE(excluded.read_at, 0))""",
                [(h.article_id, h.read_at) for h in items])
            self._writer.commit()

    # ------------------------------------------------- channel and topic feeds

    def feed_signatures(self) -> Dict[str, str]:
        with self._lock:
            return {r[0]: r[1] for r in self._writer.execute("SELECT feed_id, signature FROM feed_state")}

    def upsert_feed_items(self, items: Iterable[FeedItem], signatures: Dict[str, str],
                          now: Optional[float] = None) -> Set[str]:
        """Record which stories each channel's and topic's feed lists, and link the stories to the
        feed's tag, its publisher and the topics News filed them under (only tags the relay knows;
        most of the ids News files stories under have no tag record). ``signatures``: the feeds
        read, to remember. Returns the tags whose feeds gained or changed stories."""
        now = now or time.time()
        touched: Set[str] = set()
        with self._lock:
            cur = self._writer.cursor()
            known = {r[0] for r in cur.execute("SELECT id FROM tags")}
            rows: Dict[Tuple[str, str], tuple] = {}
            links: Set[Tuple[str, str]] = set()
            for it in items:
                key = (it.tag_id, it.article_id)
                prev = rows.get(key)
                if prev is None or (it.order or 0) > (prev[0] or 0):
                    rows[key] = (it.order, it.published_at, it.cluster_id)
                links.add((it.article_id, it.tag_id))
                if it.channel_id:
                    links.add((it.article_id, it.channel_id))
                for t in it.topic_ids:
                    if t in known:
                        links.add((it.article_id, t))
            for (tag_id, article_id), (order, published, cluster) in rows.items():
                cur.execute(
                    """INSERT INTO feed_items VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tag_id, article_id) DO UPDATE SET
                         feed_order = MAX(COALESCE(feed_items.feed_order, 0), COALESCE(excluded.feed_order, 0)),
                         published_at = COALESCE(excluded.published_at, feed_items.published_at),
                         cluster_id = COALESCE(excluded.cluster_id, feed_items.cluster_id),
                         last_seen = excluded.last_seen
                       WHERE feed_items.feed_order IS NOT excluded.feed_order
                          OR feed_items.cluster_id IS NOT excluded.cluster_id""",
                    (tag_id, article_id, order, published, cluster, now, now))
                if cur.rowcount:
                    touched.add(tag_id)
            cur.executemany("INSERT OR IGNORE INTO article_tags VALUES (?, ?)", sorted(links))
            cur.executemany("INSERT OR REPLACE INTO feed_state VALUES (?, ?, ?)",
                            [(f, s, now) for f, s in signatures.items()])
            self._writer.commit()
        return touched

    def compose_sections(self, now: Optional[float] = None) -> List[str]:
        """Build each current section's full story list (``section_items``): the Today feed's
        stories first, in News's order, then more of the same kind:

        * a topic or channel whose feed News has: that feed's stories from the last
          ``section_window`` (at least ``DEEP_MIN`` from the last week), newest first, at most
          ``PER_CLUSTER`` versions of one story;
        * a channel without a feed: the archive's stories from that publisher, the same way;
        * anything else (Top Stories, For You, Trending): stories the section carried in the last
          ``EARLIER_WINDOW``, at most ``EARLIER_MAX``, most recently carried first.

        Only stories the archive has a record for are listed; sponsored stories are left out of the
        deeper part. Returns the slugs whose lists changed."""
        now = now or self.clock()
        changed: List[str] = []
        with self._lock:
            cur = self._writer.cursor()
            current = cur.execute("SELECT slug, kind, tag_id, article_ids, promotional FROM sections "
                                  "WHERE position IS NOT NULL").fetchall()
            slugs = set()
            for row in current:
                slug = row["slug"]
                slugs.add(slug)
                today = [i for i in json.loads(row["article_ids"] or "[]") if isinstance(i, str)]
                items = [(i, "today") for i in dict.fromkeys(today)]
                if not row["promotional"]:
                    items += self._deeper(cur, slug, row["tag_id"], set(today), now)
                items = items[:SECTION_MAX]
                old = [(r[0], r[1]) for r in cur.execute(
                    "SELECT article_id, source FROM section_items WHERE slug = ? ORDER BY position", (slug,))]
                if old != items:
                    cur.execute("DELETE FROM section_items WHERE slug = ?", (slug,))
                    cur.executemany("INSERT INTO section_items VALUES (?, ?, ?, ?)",
                                    [(slug, n, i, src) for n, (i, src) in enumerate(items)])
                    changed.append(slug)
            for (slug,) in cur.execute("SELECT DISTINCT slug FROM section_items").fetchall():
                if slug not in slugs:
                    cur.execute("DELETE FROM section_items WHERE slug = ?", (slug,))
            self._writer.commit()
        return changed

    def _deeper(self, cur, slug: str, tag_id: Optional[str], seen: Set[str], now: float) -> List[Tuple[str, str]]:
        window = now - self.section_window
        oldest = now - DEEP_FALLBACK

        def take(rows, source: str) -> List[Tuple[str, str]]:
            out: List[Tuple[str, str]] = []
            clusters: Counter = Counter()
            for article_id, cluster, published in rows:
                if article_id in seen:
                    continue
                if len(out) >= DEEP_MIN and (published or 0) < window:
                    break
                if cluster:
                    if clusters[cluster] >= PER_CLUSTER:
                        continue
                    clusters[cluster] += 1
                seen.add(article_id)
                out.append((article_id, source))
            return out

        if tag_id:
            rows = cur.execute(
                """SELECT f.article_id, f.cluster_id, COALESCE(a.published_at, f.published_at) AS t
                   FROM feed_items f JOIN articles a ON a.id = f.article_id
                   WHERE f.tag_id = ? AND a.sponsored = 0 AND COALESCE(a.published_at, f.published_at) >= ?
                   ORDER BY t DESC, f.article_id LIMIT 300""", (tag_id, oldest)).fetchall()
            found = take(rows, "feed")
            if found:
                return found
            rows = cur.execute(
                """SELECT id, NULL, published_at FROM articles
                   WHERE channel_id = ? AND sponsored = 0 AND published_at >= ?
                   ORDER BY published_at DESC, id LIMIT 300""", (tag_id, oldest)).fetchall()
            found = take(rows, "channel")
            if found:
                return found
        out: List[Tuple[str, str]] = []
        for (article_id,) in cur.execute(
                """SELECT p.article_id FROM placements p JOIN articles a ON a.id = p.article_id
                   WHERE p.section_slug = ? AND p.last_seen >= ? AND a.sponsored = 0
                   ORDER BY p.last_seen DESC, p.best_position, p.article_id LIMIT ?""",
                (slug, now - EARLIER_WINDOW, EARLIER_MAX + len(seen))).fetchall():
            if article_id not in seen and len(out) < EARLIER_MAX:
                seen.add(article_id)
                out.append((article_id, "earlier"))
        return out

    # ----------------------------------------------------------------- jobs

    def put_job(self, job: dict):
        with self._lock:
            self._writer.execute(
                """INSERT OR REPLACE INTO jobs (id, kind, target, state, created_at, started_at, finished_at, result, error)
                   VALUES (:id, :kind, :target, :state, :created_at, :started_at, :finished_at, :result, :error)""",
                {**job, "result": dumps(job.get("result")) if job.get("result") is not None else None})
            self._writer.commit()
