"""Queries and JSON shapes for the API.

Keeps SQL and serialization out of the HTTP layer so both stay readable.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import anf

WORDS_PER_MINUTE = 230
CHARS_PER_WORD = 5.5


def iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def loads(value: Optional[str], default):
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Cursors
# --------------------------------------------------------------------------

class CursorError(ValueError):
    pass


def encode_cursor(payload: list) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> list:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(raw)
    except Exception:
        raise CursorError("cursor is not valid")
    if not isinstance(value, list):
        raise CursorError("cursor is not valid")
    return value


# --------------------------------------------------------------------------
# Articles
# --------------------------------------------------------------------------

ARTICLE_SORTS = {
    "-published_at": ("COALESCE(a.published_at, 0)", "DESC"),
    "published_at": ("COALESCE(a.published_at, 0)", "ASC"),
    "-first_seen_at": ("a.first_seen", "DESC"),
    "first_seen_at": ("a.first_seen", "ASC"),
    "-last_seen_at": ("a.last_seen", "DESC"),
}

ARTICLE_COLUMNS = """a.*, t.name AS channel_name, t.logo AS channel_logo"""
ARTICLE_FROM = "articles a LEFT JOIN tags t ON t.id = a.channel_id"


@dataclass
class ArticleFilter:
    channels: Sequence[str] = ()
    topics: Sequence[str] = ()
    section: Optional[str] = None
    q: Optional[str] = None
    since: Optional[float] = None
    until: Optional[float] = None
    access: Optional[str] = None            # free | news_plus
    completeness: Optional[str] = None      # full | preview | summary
    formats: Sequence[str] = ()
    language: Optional[str] = None
    saved: Optional[bool] = None
    include_ads: bool = False
    ids: Optional[Sequence[str]] = None

    def where(self, entitled: bool) -> Tuple[List[str], List[Any]]:
        clauses: List[str] = []
        args: List[Any] = []
        if not self.include_ads:
            clauses.append("a.sponsored = 0")
        if self.ids is not None:
            clauses.append("a.id IN (%s)" % ",".join("?" * len(self.ids)) if self.ids else "0")
            args += list(self.ids)
        if self.channels:
            clauses.append("a.channel_id IN (%s)" % ",".join("?" * len(self.channels)))
            args += list(self.channels)
        if self.topics:
            clauses.append("EXISTS (SELECT 1 FROM article_tags x WHERE x.article_id = a.id AND x.tag_id IN (%s))"
                           % ",".join("?" * len(self.topics)))
            args += list(self.topics)
        if self.section:
            clauses.append("a.id IN (SELECT value FROM sections s, json_each(s.article_ids) WHERE s.slug = ?)")
            args.append(self.section)
        if self.q:
            clauses.append("a.id IN (SELECT article_id FROM search WHERE search MATCH ?)")
            args.append(fts_query(self.q))
        if self.since is not None:
            clauses.append("a.published_at >= ?")
            args.append(self.since)
        if self.until is not None:
            clauses.append("a.published_at < ?")
            args.append(self.until)
        if self.access == "free":
            clauses.append("a.paid = 0")
        elif self.access == "news_plus":
            clauses.append("a.paid = 1")
        if self.completeness == "summary":
            clauses.append("a.has_body = 0")
        elif self.completeness == "full":
            clauses.append("a.has_body = 1" + ("" if entitled else " AND a.paid = 0"))
        elif self.completeness == "preview":
            clauses.append("0" if entitled else "a.has_body = 1 AND a.paid = 1")
        if self.formats:
            clauses.append("a.kind IN (%s)" % ",".join("?" * len(self.formats)))
            args += list(self.formats)
        if self.language:
            clauses.append("a.language = ?")
            args.append(self.language)
        if self.saved is True:
            clauses.append("a.id IN (SELECT article_id FROM saved WHERE removed_at IS NULL)")
        elif self.saved is False:
            clauses.append("a.id NOT IN (SELECT article_id FROM saved WHERE removed_at IS NULL)")
        return clauses, args


def fts_query(q: str) -> str:
    """Make free text safe for FTS5: every term quoted, all terms required."""
    terms = re.findall(r"[\w'’-]+", q, re.UNICODE)
    return " ".join('"%s"' % t.replace('"', "") for t in terms[:12]) or '""'


def list_articles(conn: sqlite3.Connection, flt: ArticleFilter, entitled: bool, sort: str, limit: int,
                  cursor: Optional[str]) -> Tuple[List[sqlite3.Row], Optional[str]]:
    expr, direction = ARTICLE_SORTS[sort]
    clauses, args = flt.where(entitled)
    if cursor:
        c = decode_cursor(cursor)
        if len(c) != 3 or c[0] != sort:
            raise CursorError("cursor belongs to a different sort order")
        op = "<" if direction == "DESC" else ">"
        clauses.append("(%s %s ? OR (%s = ? AND a.id %s ?))" % (expr, op, expr, op))
        args += [c[1], c[1], c[2]]
    sql = "SELECT %s, %s AS sort_key FROM %s" % (ARTICLE_COLUMNS, expr, ARTICLE_FROM)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY %s %s, a.id %s LIMIT ?" % (expr, direction, direction)
    rows = conn.execute(sql, args + [limit + 1]).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor([sort, last["sort_key"], last["id"]])
    return rows, next_cursor


def count_articles(conn: sqlite3.Connection, flt: ArticleFilter, entitled: bool) -> int:
    clauses, args = flt.where(entitled)
    sql = "SELECT COUNT(*) FROM articles a" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    return conn.execute(sql, args).fetchone()[0]


def get_article_rows(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, sqlite3.Row]:
    if not ids:
        return {}
    rows = conn.execute("SELECT %s FROM %s WHERE a.id IN (%s)" % (ARTICLE_COLUMNS, ARTICLE_FROM, ",".join("?" * len(ids))),
                        list(ids)).fetchall()
    return {r["id"]: r for r in rows}


def completeness(row: sqlite3.Row, entitled: bool) -> str:
    if not row["has_body"]:
        return "summary"
    if row["paid"] and not entitled:
        return "preview"
    return "full"


def article_summary(row: sqlite3.Row, entitled: bool) -> dict:
    thumbs = loads(row["thumbnails"], [])
    image = max(thumbs, key=lambda t: t.get("width") or 0) if thumbs else None
    minutes = None
    if row["body_length"]:
        minutes = max(1, round(row["body_length"] / CHARS_PER_WORD / WORDS_PER_MINUTE))
    article_id = row["id"]
    return {
        "id": article_id,
        "type": "article",
        "title": row["title"],
        "excerpt": row["excerpt"],
        "byline": row["byline"],
        "authors": loads(row["authors"], []),
        "channel": {
            "id": row["channel_id"],
            "name": row["channel_name"],
            "logo": row["channel_logo"],
        } if row["channel_id"] else None,
        "published_at": iso(row["published_at"]),
        "updated_at": iso(row["modified_at"]),
        "first_seen_at": iso(row["first_seen"]),
        "last_seen_at": iso(row["last_seen"]),
        "url": row["url"],
        "apple_news_url": "https://apple.news/" + article_id,
        "image": image,
        "thumbnails": thumbs,
        "access": "news_plus" if row["paid"] else "free",
        "completeness": completeness(row, entitled),
        "format": row["kind"],
        "sponsored": bool(row["sponsored"]),
        "language": row["language"],
        "reading_minutes": minutes,
        "colors": loads(row["colors"], {}),
        "links": {"self": "/v1/articles/" + article_id},
    }


def article_body(conn: sqlite3.Connection, store, row: sqlite3.Row, entitled: bool, include_ads: bool) -> Optional[dict]:
    if not row["has_body"]:
        return None
    doc = store.document_for(row["id"], conn=conn)
    if doc is None:
        return None
    converted = anf.convert(doc, keep_promotions=include_ads)
    blocks = converted.blocks
    state = completeness(row, entitled)
    if state == "preview":
        blocks = anf.preview(blocks)
    return {
        "blocks": blocks,
        "subtitle": converted.subtitle,
        "byline": anf.clean_byline(converted.byline, row["channel_name"]),
        "word_count": sum(len(b.get("text", "").split()) for b in blocks if "text" in b),
        "truncated": state == "preview",
        "removed": converted.removed,
    }


def article_detail(conn: sqlite3.Connection, store, row: sqlite3.Row, entitled: bool, include_ads: bool) -> dict:
    out = article_summary(row, entitled)
    tag_ids = loads(row["topic_ids"], [])
    topics = []
    if tag_ids:
        names = {r["id"]: r for r in conn.execute(
            "SELECT id, name, kind FROM tags WHERE id IN (%s)" % ",".join("?" * len(tag_ids)), tag_ids)}
        topics = [{"id": t, "name": names[t]["name"]} for t in tag_ids
                  if t in names and names[t]["kind"] == "topic"]
    placements = [{
        "section": r["section_slug"], "title": r["title"], "best_position": r["best_position"] + 1,
        "first_seen_at": iso(r["first_seen"]), "last_seen_at": iso(r["last_seen"]),
    } for r in conn.execute(
        "SELECT p.*, s.title FROM placements p LEFT JOIN sections s ON s.slug = p.section_slug "
        "WHERE p.article_id = ? ORDER BY p.first_seen", (row["id"],))]
    saved = conn.execute("SELECT saved_at FROM saved WHERE article_id = ? AND removed_at IS NULL", (row["id"],)).fetchone()
    reaction = conn.execute("SELECT reaction FROM reactions WHERE article_id = ?", (row["id"],)).fetchone()
    out.update({
        "topics": topics,
        "related_ids": loads(row["related_ids"], []),
        "issue_id": row["issue_id"],
        "maturity": row["maturity"],
        "video_url": row["video_url"],
        "narration_url": row["narration_url"],
        "saved": saved is not None,
        "saved_at": iso(saved[0]) if saved else None,
        "reaction": reaction[0] if reaction else None,
        "placements": placements,
        "body": article_body(conn, store, row, entitled, include_ads),
    })
    out["links"].update({
        "fetch": "/v1/articles/%s/fetch" % row["id"],
        "related": "/v1/articles/%s/related" % row["id"],
        "save": "/v1/saved/%s" % row["id"],
        "reaction": "/v1/articles/%s/reaction" % row["id"],
    })
    if row["channel_id"]:
        out["links"]["channel"] = "/v1/channels/" + row["channel_id"]
    return out


# --------------------------------------------------------------------------
# Tags (channels and topics)
# --------------------------------------------------------------------------

def tag_json(row: sqlite3.Row) -> dict:
    collection = "channels" if row["kind"] == "channel" else "topics"
    out = {
        "id": row["id"],
        "type": row["kind"],
        "name": row["name"],
        "logo": row["logo"],
        "language": row["language"],
        "path": loads(row["path"], []),
        "first_seen_at": iso(row["first_seen"]),
        "links": {"self": "/v1/%s/%s" % (collection, row["id"]), "articles": "/v1/%s/%s/articles" % (collection, row["id"])},
    }
    keys = row.keys()
    if "article_count" in keys:
        out["article_count"] = row["article_count"]
    if "latest_published_at" in keys:
        out["latest_published_at"] = iso(row["latest_published_at"])
    return out


def list_tags(conn: sqlite3.Connection, kinds: Sequence[str], q: Optional[str], only_with_articles: bool,
              sort: str, limit: int, cursor: Optional[str]) -> Tuple[List[sqlite3.Row], Optional[str]]:
    offset = 0
    if cursor:
        c = decode_cursor(cursor)
        if len(c) != 2 or c[0] != sort:
            raise CursorError("cursor belongs to a different sort order")
        offset = int(c[1])
    sql = ("SELECT t.*, COUNT(x.article_id) AS article_count, MAX(a.published_at) AS latest_published_at "
           "FROM tags t LEFT JOIN article_tags x ON x.tag_id = t.id LEFT JOIN articles a ON a.id = x.article_id "
           "WHERE t.kind IN (%s)" % ",".join("?" * len(kinds)))
    args: List[Any] = list(kinds)
    if q:
        sql += " AND t.name LIKE ? ESCAPE '\\'"
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql += " GROUP BY t.id"
    if only_with_articles:
        sql += " HAVING article_count > 0"
    order = {"name": "t.name COLLATE NOCASE ASC", "-article_count": "article_count DESC, t.name COLLATE NOCASE",
             "-latest": "latest_published_at DESC"}[sort]
    sql += " ORDER BY %s LIMIT ? OFFSET ?" % order
    rows = conn.execute(sql, args + [limit + 1, offset]).fetchall()
    next_cursor = encode_cursor([sort, offset + limit]) if len(rows) > limit else None
    return rows[:limit], next_cursor


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def section_json(row: sqlite3.Row) -> dict:
    ids = loads(row["article_ids"], [])
    return {
        "id": row["slug"],
        "type": "section",
        "title": row["title"],
        "kind": row["kind"],
        "group_type": row["group_type"],
        "position": row["position"],
        "current": row["position"] is not None,
        "promotional": bool(row["promotional"]),
        "tag_id": row["tag_id"],
        "article_count": len(ids),
        "updated_at": iso(row["updated_at"]),
        "first_seen_at": iso(row["first_seen"]),
        "links": {"self": "/v1/sections/" + row["slug"], "history": "/v1/sections/%s/history" % row["slug"]},
    }
