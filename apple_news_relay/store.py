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
from typing import Iterable, List, Optional

from .newscache import ArticleRecord, HistoryItem, SavedStory, Section, TagRecord

SCHEMA_VERSION = 1

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
    """Thread-safe access to the relay database. Reads open short-lived
    connections; writes go through one lock."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._writer = self._open()
        with self._lock:
            self._writer.executescript(SCHEMA)
            self._writer.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._writer.commit()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        return conn

    def read(self):
        """A connection for reading. In-memory stores share the writer, one thread at a time."""
        if self.path == ":memory:":
            return _TakingTurns(self._writer, self._lock)
        return self._open()

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

    def update_sections(self, sections: List[Section], now: Optional[float] = None):
        """Record the current Today feed and extend placement history."""
        now = now or time.time()
        with self._lock:
            cur = self._writer.cursor()
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
                cur.execute(
                    """INSERT INTO sections (slug, kind, title, group_type, tag_id, promotional, position, article_ids,
                         updated_at, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(slug) DO UPDATE SET kind = excluded.kind, title = excluded.title,
                         group_type = excluded.group_type, tag_id = COALESCE(excluded.tag_id, sections.tag_id),
                         promotional = excluded.promotional, position = excluded.position,
                         article_ids = excluded.article_ids, updated_at = excluded.updated_at""",
                    (slug, s.kind, title, s.group_type, s.tag_id, int(s.promotional), pos, dumps(s.article_ids), now, now))
                for i, article_id in enumerate(s.article_ids):
                    cur.execute(
                        """INSERT INTO placements VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(section_slug, article_id) DO UPDATE SET
                             best_position = MIN(placements.best_position, excluded.best_position),
                             last_seen = excluded.last_seen""",
                        (slug, article_id, i, now, now))
            self._writer.commit()

    def update_saved(self, saved: List[SavedStory], now: Optional[float] = None):
        now = now or time.time()
        with self._lock:
            cur = self._writer.cursor()
            ids = [s.article_id for s in saved]
            for s in saved:
                cur.execute("""INSERT INTO saved VALUES (?, ?, NULL)
                               ON CONFLICT(article_id) DO UPDATE SET saved_at = excluded.saved_at, removed_at = NULL""",
                            (s.article_id, s.saved_at))
            if ids:
                cur.execute("UPDATE saved SET removed_at = ? WHERE removed_at IS NULL AND article_id NOT IN (%s)"
                            % ",".join("?" * len(ids)), [now] + ids)
            self._writer.commit()

    def update_history(self, items: List[HistoryItem]):
        with self._lock:
            self._writer.executemany(
                """INSERT INTO history VALUES (?, ?) ON CONFLICT(article_id) DO UPDATE SET
                   read_at = MAX(COALESCE(history.read_at, 0), COALESCE(excluded.read_at, 0))""",
                [(h.article_id, h.read_at) for h in items])
            self._writer.commit()

    # ----------------------------------------------------------------- jobs

    def put_job(self, job: dict):
        with self._lock:
            self._writer.execute(
                """INSERT OR REPLACE INTO jobs (id, kind, target, state, created_at, started_at, finished_at, result, error)
                   VALUES (:id, :kind, :target, :state, :created_at, :started_at, :finished_at, :result, :error)""",
                {**job, "result": dumps(job.get("result")) if job.get("result") is not None else None})
            self._writer.commit()
