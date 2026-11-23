"""The HTTP API.

A small, dependency-free REST layer on ``http.server``:

* resource-oriented JSON under ``/v1``, documented in ``/openapi.json``
* cursor pagination (``limit`` + opaque ``cursor``, ``links.next`` and a
  ``Link`` header), sparse fieldsets (``fields=``) and stable sort orders
* errors as RFC 9457 ``application/problem+json``
* conditional requests (``ETag`` / ``If-None-Match`` -> 304), gzip, CORS
* long-running News actions as jobs (``202 Accepted`` + ``Location``), with
  ``wait=`` to block briefly instead of polling
* a Server-Sent Events stream at ``/v1/events``
* optional bearer token and client address allowlist
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import ipaddress
import json
import logging
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from . import __version__, queries as Q
from .actuator import ARTICLE_ID, TAG_ID, JobRunner, QueueFull
from .harvester import Harvester
from .newscache import NewsPaths, read_entitlement
from .store import Store

log = logging.getLogger(__name__)

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
SECTION_ALL = 1000
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{3,40}$")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ApiError(Exception):
    """Rendered as ``application/problem+json``."""

    def __init__(self, status: int, title: str, detail: Optional[str] = None, slug: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None, **extra):
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.slug = slug or re.sub(r"[^a-z]+", "-", title.lower()).strip("-")
        self.headers = headers or {}
        self.extra = extra

    def body(self, instance: str) -> dict:
        out = {"type": "https://github.com/VladislavEkimtcov/apple-news-relay/blob/main/docs/API.md#" + self.slug,
               "title": self.title, "status": self.status, "instance": instance}
        if self.detail:
            out["detail"] = self.detail
        out.update(self.extra)
        return out


def bad_param(name: str, detail: str) -> ApiError:
    return ApiError(400, "Invalid parameter", "%s: %s" % (name, detail), slug="invalid-parameter", parameter=name)


def not_found(what: str, ident: str) -> ApiError:
    return ApiError(404, "Not found", "No %s with id %r." % (what, ident), slug="not-found")


# --------------------------------------------------------------------------
# Requests and responses
# --------------------------------------------------------------------------

class Request:
    def __init__(self, method: str, target: str, headers, body: bytes, client: str):
        parts = urlsplit(target)
        self.method = method
        self.path = parts.path.rstrip("/") or "/"
        self.raw_query = parts.query
        self.query: Dict[str, List[str]] = parse_qs(parts.query, keep_blank_values=True)
        self.headers = headers
        self.body = body
        self.client = client
        self.params: Dict[str, str] = {}

    # Typed query parameters. Every helper validates and raises a 400 problem.

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name)
        return values[-1] if values else default

    def int(self, name: str, default: int, lo: int, hi: int) -> int:
        raw = self.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = int(raw)
        except ValueError:
            raise bad_param(name, "expected an integer")
        if not lo <= value <= hi:
            raise bad_param(name, "must be between %d and %d" % (lo, hi))
        return value

    def choice(self, name: str, options: Sequence[str], default: Optional[str] = None) -> Optional[str]:
        raw = self.get(name)
        if raw is None or raw == "":
            return default
        if raw not in options:
            raise bad_param(name, "one of: " + ", ".join(options))
        return raw

    def bool(self, name: str, default: Optional[bool] = None) -> Optional[bool]:
        raw = self.get(name)
        if raw is None or raw == "":
            return default
        if raw.lower() in ("1", "true", "yes"):
            return True
        if raw.lower() in ("0", "false", "no"):
            return False
        raise bad_param(name, "expected true or false")

    def list(self, name: str, pattern: Optional[re.Pattern] = None) -> List[str]:
        out: List[str] = []
        for raw in self.query.get(name, []):
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                if pattern and not pattern.match(item):
                    raise bad_param(name, "%r is not a valid id" % item)
                out.append(item)
        return out

    def time(self, name: str) -> Optional[float]:
        """ISO 8601 (``2026-11-22``, ``2026-11-22T10:00:00Z``) or Unix seconds."""
        raw = self.get(name)
        if not raw:
            return None
        if re.match(r"^\d+(\.\d+)?$", raw):
            return float(raw)
        try:
            value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise bad_param(name, "expected an ISO 8601 date or time")
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.timestamp()

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            value = json.loads(self.body)
        except ValueError:
            raise ApiError(400, "Malformed JSON body", slug="malformed-body")
        if not isinstance(value, dict):
            raise ApiError(400, "Malformed JSON body", "Expected a JSON object.", slug="malformed-body")
        return value

    def url(self, **changes) -> str:
        """This request's URL with some query parameters replaced (None removes)."""
        query = {k: v[-1] for k, v in self.query.items()}
        for k, v in changes.items():
            if v is None:
                query.pop(k, None)
            else:
                query[k] = str(v)
        return self.path + ("?" + urlencode(query) if query else "")


class Response:
    def __init__(self, status: int = 200, payload: Any = None, headers: Optional[Dict[str, str]] = None,
                 content_type: str = "application/json; charset=utf-8", raw: Optional[bytes] = None,
                 cache: str = "no-cache", etag: Optional[str] = None):
        self.status = status
        self.payload = payload
        self.headers = dict(headers or {})
        self.content_type = content_type
        self.raw = raw
        self.cache = cache
        # A validator the handler already knows (so a 304 needs no payload); else one is hashed.
        self.etag = etag
        self.stream: Optional[Callable[[Callable[[bytes], None]], None]] = None

    def body_bytes(self) -> bytes:
        if self.raw is not None:
            return self.raw
        if self.payload is None:
            return b""
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sparse(item: dict, fields: Optional[List[str]]) -> dict:
    if not fields:
        return item
    keep = set(fields) | {"id", "type"}
    return {k: v for k, v in item.items() if k in keep}


def page(req: Request, data: List[dict], next_cursor: Optional[str], limit: int, **meta) -> Response:
    links = {"self": req.url(), "next": req.url(cursor=next_cursor) if next_cursor else None}
    headers = {}
    if next_cursor:
        headers["Link"] = '<%s>; rel="next"' % links["next"]
    fields = req.list("fields")
    body = {"data": [sparse(d, fields) for d in data],
            "meta": {"count": len(data), "limit": limit, "next_cursor": next_cursor, **meta},
            "links": links}
    return Response(200, body, headers)


def accepted(job: dict) -> Response:
    href = "/v1/jobs/" + job["id"]
    return Response(202, {"data": job, "links": {"self": href}}, {"Location": href, "Retry-After": "2"})


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

Handler = Callable[[Request], Response]


class Router:
    def __init__(self):
        self.routes: List[Tuple[str, re.Pattern, str, Handler, str]] = []

    def add(self, method: str, template: str, handler: Handler, summary: str = ""):
        pattern = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", template) + "$")
        self.routes.append((method, pattern, template, handler, summary))

    def match(self, method: str, path: str) -> Tuple[Handler, Dict[str, str]]:
        allowed = []
        for m, pattern, _, handler, _ in self.routes:
            found = pattern.match(path)
            if found:
                if m == method or (method == "HEAD" and m == "GET"):
                    return handler, found.groupdict()
                allowed.append(m)
        if allowed:
            raise ApiError(405, "Method not allowed", "Use %s." % ", ".join(sorted(set(allowed))),
                           headers={"Allow": ", ".join(sorted(set(allowed + ["OPTIONS"])))})
        raise ApiError(404, "Not found", "No route for %s." % path, slug="not-found")


# --------------------------------------------------------------------------
# Events (Server-Sent Events)
# --------------------------------------------------------------------------

class EventHub:
    def __init__(self):
        self._subscribers: List["queue.Queue[Tuple[str, dict]]"] = []
        self._lock = threading.Lock()
        self._seq = 0

    def publish(self, kind: str, data: dict):
        with self._lock:
            self._seq += 1
            for q in list(self._subscribers):
                try:
                    q.put_nowait((kind, {**data, "seq": self._seq, "at": Q.iso(time.time())}))
                except queue.Full:
                    pass

    def subscribe(self) -> "queue.Queue[Tuple[str, dict]]":
        q: "queue.Queue[Tuple[str, dict]]" = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------

class RelayApp:
    """Wires the store, harvester and job runner to HTTP routes."""

    def __init__(self, store: Store, harvester: Harvester, jobs: JobRunner, paths: Optional[NewsPaths] = None,
                 events: Optional[EventHub] = None, token: Optional[str] = None,
                 allow: Sequence[str] = (), cors_origin: str = "*", handoff=None):
        self.store = store
        self.handoff = handoff
        self.harvester = harvester
        self.jobs = jobs
        self.paths = paths or NewsPaths()
        self.events = events or EventHub()
        self.token = token
        self.allow = [ipaddress.ip_network(a, strict=False) for a in allow]
        self.cors_origin = cors_origin
        self.started_at = time.time()
        self._entitlement = (0.0, None)
        self._front_cache: "Dict[str, Tuple[bytes, List[str]]]" = {}
        self._front_lock = threading.Lock()
        self._missing_queued: Dict[str, float] = {}
        self.router = Router()
        self._routes()

    # ------------------------------------------------------------ helpers

    def entitled(self) -> bool:
        checked, value = self._entitlement
        if value is None or time.time() - checked > 300:
            value = read_entitlement(self.paths)
            self._entitlement = (time.time(), value)
        return value.news_plus

    def article_filter(self, req: Request, **overrides) -> Q.ArticleFilter:
        flt = Q.ArticleFilter(
            channels=req.list("channel", TAG_ID),
            topics=req.list("topic", TAG_ID),
            section=req.get("section") or None,
            q=req.get("q") or None,
            since=req.time("since"),
            until=req.time("until"),
            access=req.choice("access", ["free", "news_plus", "any"], "any"),
            completeness=req.choice("completeness", ["full", "preview", "summary", "any"], "any"),
            formats=[f for f in req.list("format") if f in ("article", "magazine", "apple_original", "other")],
            language=req.get("language") or None,
            saved=req.bool("saved"),
            include_ads=req.choice("ads", ["exclude", "include"], "exclude") == "include",
        )
        if flt.access == "any":
            flt.access = None
        if flt.completeness == "any":
            flt.completeness = None
        for k, v in overrides.items():
            setattr(flt, k, v)
        return flt

    def include_ads(self, req: Request) -> bool:
        return req.choice("ads", ["exclude", "include"], "exclude") == "include"

    def summaries(self, rows) -> List[dict]:
        entitled = self.entitled()
        return [Q.article_summary(r, entitled) for r in rows]

    def article_row(self, conn, article_id: str):
        if not ARTICLE_ID.match(article_id):
            raise not_found("article", article_id)
        row = Q.get_article_rows(conn, [article_id]).get(article_id)
        if row is None:
            raise not_found("article", article_id)
        return row

    def tag_row(self, conn, tag_id: str, kinds: Sequence[str], what: str):
        row = conn.execute("SELECT * FROM tags WHERE id = ? AND kind IN (%s)" % ",".join("?" * len(kinds)),
                           [tag_id] + list(kinds)).fetchone()
        if row is None:
            raise not_found(what, tag_id)
        return row

    def submit(self, kind: str, target: Optional[str] = None, priority: str = "normal", **params):
        try:
            return self.jobs.submit(kind, target, priority, **params)
        except QueueFull:
            raise ApiError(429, "Too many requests", "The News job queue is full; try again shortly.",
                           headers={"Retry-After": "10"})

    def wait_for(self, job, req: Request):
        wait = req.int("wait", 0, 0, 60)
        if wait and not job.done.is_set():
            job.done.wait(wait)

    # ------------------------------------------------------------- routes

    def _routes(self):
        r = self.router.add
        r("GET", "/", self.home, "Landing page")
        r("GET", "/openapi.json", self.openapi, "OpenAPI 3.1 description")
        r("GET", "/v1", self.index, "API index")
        r("GET", "/v1/status", self.status, "Health of the relay, News.app and the cache")
        r("GET", "/v1/stats", self.stats, "Archive statistics")
        r("GET", "/v1/front", self.front, "A whole front page in one call: sections, Latest, Saved")
        r("GET", "/v1/articles", self.articles, "List articles")
        r("GET", "/v1/articles/{id}", self.article, "Get one article, with its body")
        r("POST", "/v1/articles/{id}/fetch", self.article_fetch, "Ask News to download an article")
        r("GET", "/v1/articles/{id}/related", self.article_related, "Articles related to one article")
        r("GET", "/v1/sections", self.sections, "Today feed sections")
        r("GET", "/v1/sections/{id}", self.section, "One section with its articles in feed order")
        r("GET", "/v1/sections/{id}/articles", self.section_articles, "A section's stories, page by page, deep")
        r("GET", "/v1/sections/{id}/history", self.section_history, "Every article a section has carried")
        r("POST", "/v1/sections/{id}/refresh", self.section_refresh, "Load more of one section")
        r("POST", "/v1/today/refresh", self.today_refresh, "Refresh the Today feed in News")
        for kind, plural in (("channel", "channels"), ("topic", "topics")):
            r("GET", "/v1/%s" % plural, self._tag_list(kind), "List %s" % plural)
            r("GET", "/v1/%s/{id}" % plural, self._tag_get(kind), "Get one %s" % kind)
            r("GET", "/v1/%s/{id}/articles" % plural, self._tag_articles(kind), "Articles in a %s" % kind)
            r("POST", "/v1/%s/{id}/refresh" % plural, self._tag_refresh(kind), "Open a %s in News" % kind)
        r("GET", "/v1/saved", self.saved, "Saved Stories")
        r("PUT", "/v1/saved/{id}", self.story_action("save"), "Save a story in News")
        r("DELETE", "/v1/saved/{id}", self.story_action("unsave"), "Remove a story from Saved Stories")
        r("PUT", "/v1/articles/{id}/reaction", self.reaction_put, "Suggest more or fewer stories like this one")
        r("DELETE", "/v1/articles/{id}/reaction", self.story_action("clear_reaction"), "Take back a like or dislike")
        r("GET", "/v1/history", self.history, "Reading history")
        r("GET", "/v1/handoff", self.handoff_get, "The story being read in News on the Mac right now")
        r("GET", "/v1/search", self.search, "Full-text search of the archive")
        r("POST", "/v1/searches", self.search_remote, "Run a search in News")
        r("GET", "/v1/jobs", self.job_list, "Recent News jobs")
        r("GET", "/v1/jobs/{id}", self.job_get, "One News job")
        r("DELETE", "/v1/jobs/{id}", self.job_cancel, "Cancel a queued job")
        r("PUT", "/v1/interest/{client}", self.interest_put, "Stories a client is heading for: fetch them ahead")
        r("DELETE", "/v1/interest/{client}", self.interest_delete, "Forget a client's stories to fetch ahead")
        r("GET", "/v1/events", self.event_stream, "Server-Sent Events")

    # ----------------------------------------------------------- handlers

    def home(self, req: Request) -> Response:
        rows = "".join("<tr><td><code>%s</code></td><td><a href='%s'>%s</a></td><td>%s</td></tr>"
                       % (m, t, t, s) for m, _, t, _, s in self.router.routes if t.startswith("/v1"))
        html = ("<!doctype html><meta charset=utf-8><title>Apple News Relay</title>"
                "<style>body{font:15px -apple-system,sans-serif;max-width:860px;margin:40px auto;padding:0 16px}"
                "td{padding:4px 12px 4px 0;vertical-align:top}code{color:#c0392b}</style>"
                "<h1>Apple News Relay <small>%s</small></h1><p>JSON API over the Apple News app on this Mac. "
                "Machine-readable description: <a href='/openapi.json'>/openapi.json</a>.</p><table>%s</table>"
                % (__version__, rows))
        return Response(200, raw=html.encode(), content_type="text/html; charset=utf-8")

    def openapi(self, req: Request) -> Response:
        from .openapi import spec
        return Response(200, spec(self.router.routes), cache="max-age=300")

    #: What this relay can do beyond 0.2 (clients check before using it).
    FEATURES = ["front", "deep_sections", "partial_bodies", "interest", "events_hello"]

    def index(self, req: Request) -> Response:
        return Response(200, {"name": "apple-news-relay", "version": __version__, "features": self.FEATURES,
                              "links": {
            "status": "/v1/status", "stats": "/v1/stats", "front": "/v1/front", "articles": "/v1/articles",
            "sections": "/v1/sections",
            "channels": "/v1/channels", "topics": "/v1/topics", "saved": "/v1/saved", "history": "/v1/history",
            "handoff": "/v1/handoff", "interest": "/v1/interest/{client}",
            "search": "/v1/search", "jobs": "/v1/jobs", "events": "/v1/events", "openapi": "/openapi.json"}})

    def status(self, req: Request) -> Response:
        ent = read_entitlement(self.paths)
        import os
        report = self.harvester.last_report
        queued = sum(1 for j in self.jobs.list() if j.state == "queued")
        running = sum(1 for j in self.jobs.list() if j.state == "running")
        cache_ok = os.access(self.paths.production, os.R_OK)
        news_running = self.jobs.app.is_running()
        healthy = cache_ok
        return Response(200 if healthy else 503, {
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "uptime_seconds": round(time.time() - self.started_at),
            "news_app": {"running": news_running},
            "cache": {"readable": cache_ok, "path": self.paths.production},
            "account": {"news_plus": ent.news_plus, "detail": ent.detail},
            "harvester": {"passes": self.harvester.passes, "last": report.to_dict() if report else None},
            "jobs": {"queued": queued, "running": running, "warming": self.jobs.warming()},
            "front_version": self.store.front_version,
        })

    def stats(self, req: Request) -> Response:
        conn = self.store.read()
        one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        days = req.int("days", 14, 1, 365)
        since = time.time() - days * 86400
        per_day = [{"date": r[0], "articles": r[1]} for r in conn.execute(
            "SELECT date(first_seen, 'unixepoch') d, COUNT(*) FROM articles WHERE first_seen >= ? GROUP BY d ORDER BY d",
            (since,))]
        top = [{"id": r[0], "name": r[1], "articles": r[2]} for r in conn.execute(
            "SELECT a.channel_id, t.name, COUNT(*) n FROM articles a LEFT JOIN tags t ON t.id = a.channel_id "
            "WHERE a.first_seen >= ? GROUP BY a.channel_id ORDER BY n DESC LIMIT 20", (since,))]
        return Response(200, {
            "articles": one("SELECT COUNT(*) FROM articles"),
            "articles_with_body": one("SELECT COUNT(*) FROM articles WHERE has_body = 1"),
            "news_plus_articles": one("SELECT COUNT(*) FROM articles WHERE paid = 1"),
            "sponsored_articles": one("SELECT COUNT(*) FROM articles WHERE sponsored = 1"),
            "channels": one("SELECT COUNT(*) FROM tags WHERE kind = 'channel'"),
            "topics": one("SELECT COUNT(*) FROM tags WHERE kind = 'topic'"),
            "sections_tracked": one("SELECT COUNT(*) FROM sections"),
            "placements": one("SELECT COUNT(*) FROM placements"),
            "oldest_article_at": Q.iso(one("SELECT MIN(published_at) FROM articles")),
            "first_harvest_at": Q.iso(one("SELECT MIN(first_seen) FROM articles")),
            "window_days": days,
            "articles_per_day": per_day,
            "top_channels": top,
        })

    def articles(self, req: Request) -> Response:
        flt = self.article_filter(req)
        sort = req.choice("sort", list(Q.ARTICLE_SORTS), "-published_at") or "-published_at"
        limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        conn = self.store.read()
        try:
            rows, nxt = Q.list_articles(conn, flt, self.entitled(), sort, limit, req.get("cursor"))
        except Q.CursorError as e:
            raise bad_param("cursor", str(e))
        meta: Dict[str, Any] = {"sort": sort}
        if req.bool("count", False):
            meta["total"] = Q.count_articles(conn, flt, self.entitled())
        return page(req, self.summaries(rows), nxt, limit, **meta)

    def article(self, req: Request) -> Response:
        conn = self.store.read()
        row = self.article_row(conn, req.params["id"])
        fetch = req.choice("fetch", ["auto", "never"], "auto")
        from_block = req.int("from_block", 0, 0, 100000)
        max_chars = req.int("max_chars", 0, 0, 1000000)
        job = None
        if not row["has_body"] and fetch == "auto":
            job = self.submit("fetch_article", row["id"], "interactive")
            self.wait_for(job, req)
            row = self.article_row(conn, row["id"])
        body = Q.article_detail(conn, self.store, row, self.entitled(), self.include_ads(req), from_block, max_chars)
        headers = {}
        if job is not None and not job.done.is_set():
            body["fetch_job"] = job.to_dict()
            headers["Retry-After"] = "3"
        elif job is not None:
            body["fetch_job"] = job.to_dict()
        cache = "max-age=60" if body["completeness"] == "full" else "no-cache"
        return Response(200, {"data": sparse(body, req.list("fields"))}, headers, cache=cache)

    def article_fetch(self, req: Request) -> Response:
        conn = self.store.read()
        row = self.article_row(conn, req.params["id"])
        priority = req.choice("priority", ["interactive", "normal", "background"], "interactive")
        job = self.submit("fetch_article", row["id"], priority)
        self.wait_for(job, req)
        return accepted(job.to_dict()) if not job.done.is_set() else Response(200, {"data": job.to_dict()})

    def story_action(self, action: str) -> Handler:
        """Save, unsave, like, dislike: a job that opens the story in News and presses one menu
        item. Answers 202 with the job, or 200 with the result when ``wait`` was long enough."""
        def handler(req: Request) -> Response:
            row = self.article_row(self.store.read(), req.params["id"])
            job = self.submit("story_action", row["id"], "interactive", action=action)
            self.wait_for(job, req)
            return accepted(job.to_dict()) if not job.done.is_set() else Response(200, {"data": job.to_dict()})
        return handler

    def reaction_put(self, req: Request) -> Response:
        reaction = req.json().get("reaction") or req.get("reaction")
        if reaction not in ("like", "dislike"):
            raise bad_param("reaction", "like or dislike")
        return self.story_action(reaction)(req)

    def article_related(self, req: Request) -> Response:
        conn = self.store.read()
        row = self.article_row(conn, req.params["id"])
        ids = Q.loads(row["related_ids"], [])
        known = Q.get_article_rows(conn, ids)
        include_ads = self.include_ads(req)
        data = [Q.article_summary(known[i], self.entitled()) for i in ids
                if i in known and (include_ads or not known[i]["sponsored"])]
        return page(req, data, None, len(data), missing=[i for i in ids if i not in known])

    def section_item(self, conn, row, per: int, deep: bool, include_ads: bool,
                     fields: Optional[List[str]] = None) -> dict:
        """A section and up to ``per`` of its stories: the Today feed's (``deep`` false, as in 0.2)
        or its full list (Today's first, then more of the same; see ``Store.compose_sections``)."""
        entitled = self.entitled()
        item = Q.section_json(row)
        if deep:
            items = Q.section_ids(conn, row["slug"]) or [(i, "today") for i in Q.loads(row["article_ids"], [])]
            item["today_count"] = item["article_count"]
            item["article_count"] = len(items)
            item["links"]["articles"] = "/v1/sections/%s/articles" % row["slug"]
            item["links"]["next"] = None
            if per:
                arts, nxt = Q.section_page(conn, row["slug"], items, entitled, include_ads, per, None)
                item["articles"] = [sparse(a, fields) for a in arts]
                if nxt:
                    item["links"]["next"] = item["links"]["articles"] + "?" + urlencode({"limit": per, "cursor": nxt})
        elif per:
            ids = Q.loads(row["article_ids"], [])
            known = Q.get_article_rows(conn, ids)
            item["articles"] = [sparse(Q.article_summary(known[i], entitled), fields) for i in ids
                                if i in known and (include_ads or not known[i]["sponsored"])][:per]
        return item

    def sections(self, req: Request) -> Response:
        conn = self.store.read()
        include_ads = self.include_ads(req)
        current = req.bool("current", True)
        sql = "SELECT * FROM sections" + (" WHERE position IS NOT NULL" if current else "") + \
              " ORDER BY position IS NULL, position, title"
        per = req.int("per_section", 0, 0, 60)
        deep = req.bool("deep", False)
        data = [self.section_item(conn, row, per, deep, include_ads) for row in conn.execute(sql).fetchall()
                if include_ads or not row["promotional"]]
        return page(req, data, None, len(data))

    def _section_row(self, conn, req: Request):
        row = conn.execute("SELECT * FROM sections WHERE slug = ?", (req.params["id"],)).fetchone()
        if row is None:
            raise not_found("section", req.params["id"])
        if row["promotional"] and not self.include_ads(req):
            raise ApiError(404, "Not found", "That section is promotional; pass ads=include to see it.", slug="not-found")
        return row

    def section(self, req: Request) -> Response:
        conn = self.store.read()
        row = self._section_row(conn, req)
        include_ads = self.include_ads(req)
        if req.bool("deep", False):
            item = self.section_item(conn, row, SECTION_ALL, True, include_ads)
            return Response(200, {"data": item})
        ids = Q.loads(row["article_ids"], [])
        known = Q.get_article_rows(conn, ids)
        item = Q.section_json(row)
        item["articles"] = [Q.article_summary(known[i], self.entitled()) for i in ids
                            if i in known and (include_ads or not known[i]["sponsored"])]
        item["missing_article_ids"] = [i for i in ids if i not in known]
        return Response(200, {"data": item})

    def section_articles(self, req: Request) -> Response:
        """A section's full story list, a page at a time (the front page carries the first page)."""
        conn = self.store.read()
        row = self._section_row(conn, req)
        limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        items = Q.section_ids(conn, row["slug"]) or [(i, "today") for i in Q.loads(row["article_ids"], [])]
        try:
            data, nxt = Q.section_page(conn, row["slug"], items, self.entitled(), self.include_ads(req), limit,
                                       req.get("cursor"))
        except (Q.CursorError, ValueError, IndexError) as e:
            raise bad_param("cursor", str(e) or "cursor is not valid")
        return page(req, data, nxt, limit, total=len(items))

    def front(self, req: Request) -> Response:
        """Sections (deep, first page of each), Latest and Saved in one response. The ETag comes
        from the front version, so an unchanged page is answered 304 without building anything."""
        per = req.int("per_section", 15, 0, 60)
        latest_n = req.int("latest", 45, 0, MAX_LIMIT)
        saved_n = req.int("saved", 45, 0, MAX_LIMIT)
        fields = req.list("fields")
        include_ads = self.include_ads(req)
        fetch_missing = req.bool("fetch_missing", False)
        entitled = self.entitled()
        version = self.store.front_version
        query = sorted((k, v) for k, vs in req.query.items() for v in vs if k != "token")
        etag = 'W/"front-%d-%d-%s"' % (version, int(entitled),
                                       hashlib.sha1(json.dumps(query).encode()).hexdigest()[:10])
        if etag in (req.headers.get("If-None-Match") or ""):
            return Response(304, etag=etag)
        with self._front_lock:
            cached = self._front_cache.get(etag)
        if cached is None:
            conn = self.store.read()
            sections = [self.section_item(conn, row, per, True, include_ads, fields) for row in conn.execute(
                "SELECT * FROM sections WHERE position IS NOT NULL ORDER BY position, title").fetchall()
                if include_ads or not row["promotional"]]
            latest: Dict[str, Any] = {"articles": [], "links": {"next": None}}
            if latest_n:
                rows, nxt = Q.list_articles(conn, Q.ArticleFilter(include_ads=include_ads), entitled,
                                            "-published_at", latest_n, None)
                latest["articles"] = [sparse(a, fields) for a in self.summaries(rows)]
                if nxt:
                    latest["links"]["next"] = "/v1/articles?" + urlencode({"limit": latest_n, "cursor": nxt})
            saved_data, missing, _ = self._library_rows(conn, "saved", "saved_at", "WHERE removed_at IS NULL",
                                                        saved_n, 0) if saved_n else ([], [], None)
            body = {"data": {"version": version, "sections": sections, "latest": latest,
                             "saved": {"articles": [sparse(a, fields) for a in saved_data],
                                       "missing_article_ids": missing}}}
            cached = (json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), missing)
            with self._front_lock:
                self._front_cache[etag] = cached
                while len(self._front_cache) > 4:
                    self._front_cache.pop(next(iter(self._front_cache)))
        raw, missing = cached
        if fetch_missing and missing:
            self.queue_missing(missing)
        return Response(200, raw=raw, etag=etag)

    def queue_missing(self, ids: List[str]):
        """Background fetches for saved stories the relay has no record of; each at most every
        30 minutes (a story News can't open stays missing)."""
        now = time.time()
        for article_id in ids:
            if not ARTICLE_ID.match(article_id) or now - self._missing_queued.get(article_id, 0) < 1800:
                continue
            self._missing_queued[article_id] = now
            try:
                self.jobs.submit("fetch_article", article_id, "background")
            except QueueFull:
                break

    def section_history(self, req: Request) -> Response:
        conn = self.store.read()
        slug = req.params["id"]
        if conn.execute("SELECT 1 FROM sections WHERE slug = ?", (slug,)).fetchone() is None:
            raise not_found("section", slug)
        limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = 0
        if req.get("cursor"):
            try:
                c = Q.decode_cursor(req.get("cursor") or "")
                offset = int(c[1])
            except (Q.CursorError, IndexError, ValueError):
                raise bad_param("cursor", "cursor is not valid")
        rows = conn.execute("SELECT * FROM placements WHERE section_slug = ? ORDER BY first_seen DESC, article_id "
                            "LIMIT ? OFFSET ?", (slug, limit + 1, offset)).fetchall()
        nxt = Q.encode_cursor(["history", offset + limit]) if len(rows) > limit else None
        rows = rows[:limit]
        known = Q.get_article_rows(conn, [r["article_id"] for r in rows])
        data = []
        for r in rows:
            data.append({
                "id": r["article_id"], "type": "placement", "section": slug,
                "best_position": r["best_position"] + 1,
                "first_seen_at": Q.iso(r["first_seen"]), "last_seen_at": Q.iso(r["last_seen"]),
                "article": Q.article_summary(known[r["article_id"]], self.entitled()) if r["article_id"] in known else None,
            })
        return page(req, data, nxt, limit)

    def section_refresh(self, req: Request) -> Response:
        conn = self.store.read()
        row = conn.execute("SELECT * FROM sections WHERE slug = ?", (req.params["id"],)).fetchone()
        if row is None:
            raise not_found("section", req.params["id"])
        job = self.submit("open_tag", row["tag_id"]) if row["tag_id"] and TAG_ID.match(row["tag_id"]) \
            else self.submit("refresh_today", None, pages=req.int("pages", 8, 0, 40))
        self.wait_for(job, req)
        return accepted(job.to_dict()) if not job.done.is_set() else Response(200, {"data": job.to_dict()})

    def today_refresh(self, req: Request) -> Response:
        job = self.submit("refresh_today", None, pages=req.int("pages", 8, 0, 40))
        self.wait_for(job, req)
        return accepted(job.to_dict()) if not job.done.is_set() else Response(200, {"data": job.to_dict()})

    def _tag_list(self, kind: str) -> Handler:
        kinds = ["channel"] if kind == "channel" else ["topic", "section"]

        def handler(req: Request) -> Response:
            limit = req.int("limit", 50, 1, 200)
            sort = req.choice("sort", ["-article_count", "name", "-latest"], "-article_count") or "-article_count"
            try:
                rows, nxt = Q.list_tags(self.store.read(), kinds, req.get("q"), not req.bool("all", False),
                                        sort, limit, req.get("cursor"))
            except Q.CursorError as e:
                raise bad_param("cursor", str(e))
            return page(req, [Q.tag_json(r) for r in rows], nxt, limit, sort=sort)
        return handler

    def _tag_get(self, kind: str) -> Handler:
        kinds = ["channel"] if kind == "channel" else ["topic", "section"]

        def handler(req: Request) -> Response:
            conn = self.store.read()
            row = self.tag_row(conn, req.params["id"], kinds, kind)
            item = Q.tag_json(row)
            stats = conn.execute("SELECT COUNT(*), MAX(a.published_at) FROM article_tags x JOIN articles a "
                                 "ON a.id = x.article_id WHERE x.tag_id = ?", (row["id"],)).fetchone()
            item["article_count"], item["latest_published_at"] = stats[0], Q.iso(stats[1])
            item["links"]["refresh"] = item["links"]["self"] + "/refresh"
            return Response(200, {"data": item})
        return handler

    def _tag_articles(self, kind: str) -> Handler:
        kinds = ["channel"] if kind == "channel" else ["topic", "section"]

        def handler(req: Request) -> Response:
            conn = self.store.read()
            row = self.tag_row(conn, req.params["id"], kinds, kind)
            flt = self.article_filter(req, **({"channels": [row["id"]]} if kind == "channel" else {"topics": [row["id"]]}))
            sort = req.choice("sort", list(Q.ARTICLE_SORTS), "-published_at") or "-published_at"
            limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
            try:
                rows, nxt = Q.list_articles(conn, flt, self.entitled(), sort, limit, req.get("cursor"))
            except Q.CursorError as e:
                raise bad_param("cursor", str(e))
            return page(req, self.summaries(rows), nxt, limit, sort=sort)
        return handler

    def _tag_refresh(self, kind: str) -> Handler:
        kinds = ["channel"] if kind == "channel" else ["topic", "section"]

        def handler(req: Request) -> Response:
            conn = self.store.read()
            row = self.tag_row(conn, req.params["id"], kinds, kind)
            job = self.submit("open_tag", row["id"])
            self.wait_for(job, req)
            return accepted(job.to_dict()) if not job.done.is_set() else Response(200, {"data": job.to_dict()})
        return handler

    def _library_rows(self, conn, table: str, time_col: str, extra_where: str, limit: int, offset: int):
        rows = conn.execute("SELECT article_id, %s FROM %s %s ORDER BY %s DESC LIMIT ? OFFSET ?"
                            % (time_col, table, extra_where, time_col), (limit + 1, offset)).fetchall()
        nxt = Q.encode_cursor([table, offset + limit]) if len(rows) > limit else None
        rows = rows[:limit]
        known = Q.get_article_rows(conn, [r[0] for r in rows])
        data, missing = [], []
        for article_id, when in rows:
            if article_id in known:
                item = Q.article_summary(known[article_id], self.entitled())
                item[time_col.replace("_at", "") + "_at"] = Q.iso(when)
                data.append(item)
            else:
                missing.append(article_id)
        return data, missing, nxt

    def _library(self, req: Request, table: str, time_col: str, extra_where: str = "") -> Response:
        conn = self.store.read()
        limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = 0
        if req.get("cursor"):
            try:
                offset = int(Q.decode_cursor(req.get("cursor") or "")[1])
            except (Q.CursorError, IndexError, ValueError):
                raise bad_param("cursor", "cursor is not valid")
        data, missing, nxt = self._library_rows(conn, table, time_col, extra_where, limit, offset)
        if missing and req.bool("fetch_missing", False):
            # Prefetching: behind anything someone is waiting for, and never an error here.
            self.queue_missing(missing)
        return page(req, data, nxt, limit, missing_article_ids=missing)

    def saved(self, req: Request) -> Response:
        return self._library(req, "saved", "saved_at", "WHERE removed_at IS NULL")

    def history(self, req: Request) -> Response:
        return self._library(req, "history", "read_at")

    def search(self, req: Request) -> Response:
        q = (req.get("q") or "").strip()
        if not q:
            raise bad_param("q", "required")
        limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        offset = 0
        if req.get("cursor"):
            try:
                c = Q.decode_cursor(req.get("cursor") or "")
                if c[0] != q:
                    raise Q.CursorError("cursor belongs to a different query")
                offset = int(c[1])
            except (Q.CursorError, IndexError, ValueError) as e:
                raise bad_param("cursor", str(e) or "cursor is not valid")
        conn = self.store.read()
        include_ads = self.include_ads(req)
        hits = conn.execute(
            "SELECT article_id, snippet(search, -1, '[', ']', '…', 18) AS snip, bm25(search, 0, 10, 4, 2, 1) AS score "
            "FROM search WHERE search MATCH ? ORDER BY score LIMIT ? OFFSET ?",
            (Q.fts_query(q), limit + 1, offset)).fetchall()
        nxt = Q.encode_cursor([q, offset + limit]) if len(hits) > limit else None
        hits = hits[:limit]
        known = Q.get_article_rows(conn, [h[0] for h in hits])
        data = []
        for article_id, snip, score in hits:
            row = known.get(article_id)
            if row is None or (row["sponsored"] and not include_ads):
                continue
            item = Q.article_summary(row, self.entitled())
            item["match"] = {"snippet": snip or None, "score": round(-score, 3)}
            data.append(item)
        return page(req, data, nxt, limit, query=q)

    def search_remote(self, req: Request) -> Response:
        body = req.json()
        q = str(body.get("query") or req.get("q") or "").strip()
        if not q or len(q) > 120:
            raise bad_param("query", "required, at most 120 characters")
        job = self.submit("search", q, "interactive")
        self.wait_for(job, req)
        payload = job.to_dict()
        payload["links"] = {"results": "/v1/search?" + urlencode({"q": q})}
        return accepted(payload) if not job.done.is_set() else Response(200, {"data": payload})

    def handoff_get(self, req: Request) -> Response:
        """Like Handoff: the story open in News on the Mac (or read there in the last half hour),
        for a client to carry on with. ``data`` is null when there's none or watching is off."""
        current = self.handoff.get() if self.handoff else None
        if not current:
            return Response(200, {"data": None, "meta": {"watching": self.handoff is not None}})
        conn = self.store.read()
        rows = Q.get_article_rows(conn, [current["article_id"]])
        row = rows.get(current["article_id"])
        if row is None:
            return Response(200, {"data": None, "meta": {"watching": True}})
        return Response(200, {"data": {
            "article": Q.article_summary(row, self.entitled()),
            "on_screen": current["on_screen"],
            "since": Q.iso(current["since"]),
            "seen_at": Q.iso(current["seen_at"]),
        }, "meta": {"watching": True}})

    def job_list(self, req: Request) -> Response:
        limit = req.int("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
        state = req.choice("state", ["queued", "running", "succeeded", "failed", "cancelled"])
        jobs = [j.to_dict() for j in self.jobs.list() if not state or j.state == state][:limit]
        return page(req, jobs, None, limit)

    def job_get(self, req: Request) -> Response:
        job = self.jobs.get(req.params["id"])
        if job is None:
            raise not_found("job", req.params["id"])
        self.wait_for(job, req)
        return Response(200, {"data": job.to_dict()})

    def job_cancel(self, req: Request) -> Response:
        job = self.jobs.get(req.params["id"])
        if job is None:
            raise not_found("job", req.params["id"])
        if job.state != "cancelled" and job.done.is_set() or job.state == "running":
            raise ApiError(409, "Conflict", "Only queued jobs can be cancelled.", slug="conflict")
        self.jobs.cancel(job.id)
        return Response(200, {"data": job.to_dict()})

    def interest_put(self, req: Request) -> Response:
        """The stories a client may open next, most likely first (at most 12). The relay has News
        fetch the ones it lacks, in that order, in the background; a newer list from the same
        client replaces this one, and fetches nobody wants any more are dropped."""
        client = req.params["client"]
        if not CLIENT_ID.match(client):
            raise bad_param("client", "3-40 letters, digits, - or _")
        ids = req.json().get("articles")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise bad_param("articles", "a list of article ids")
        states = self.jobs.set_interest(client, [i for i in ids if ARTICLE_ID.match(i)])
        return Response(200, {"data": {"client": client, "articles": states}})

    def interest_delete(self, req: Request) -> Response:
        client = req.params["client"]
        if not CLIENT_ID.match(client):
            raise bad_param("client", "3-40 letters, digits, - or _")
        self.jobs.set_interest(client, [])
        return Response(204)

    def event_stream(self, req: Request) -> Response:
        resp = Response(200, content_type="text/event-stream; charset=utf-8", cache="no-store")
        hub = self.events

        def stream(write: Callable[[bytes], None]):
            q = hub.subscribe()
            try:
                write(b"retry: 5000\n\n")
                # Where things stand, so a client that reconnects knows whether it missed a change.
                write(("event: hello\ndata: %s\n\n" % json.dumps({
                    "version": __version__, "front_version": self.store.front_version,
                    "features": self.FEATURES})).encode())
                while True:
                    try:
                        kind, data = q.get(timeout=15)
                        write(("event: %s\nid: %s\ndata: %s\n\n" % (kind, data.get("seq"), json.dumps(data))).encode())
                    except queue.Empty:
                        write(b": keep-alive\n\n")
            finally:
                hub.unsubscribe(q)
        resp.stream = stream
        return resp

    # ---------------------------------------------------------- dispatch

    def authorize(self, req: Request):
        if self.allow:
            try:
                addr = ipaddress.ip_address(req.client)
            except ValueError:
                addr = None
            if addr is None or not any(addr in net for net in self.allow):
                raise ApiError(403, "Forbidden", "This address may not use the relay.", slug="forbidden")
        if self.token:
            header = req.headers.get("Authorization") or ""
            supplied = header[7:] if header.startswith("Bearer ") else req.get("token")
            if supplied != self.token:
                raise ApiError(401, "Unauthorized", "Send Authorization: Bearer <token>.", slug="unauthorized",
                               headers={"WWW-Authenticate": 'Bearer realm="apple-news-relay"'})

    def handle(self, req: Request) -> Response:
        try:
            if req.method == "OPTIONS":
                return Response(204, headers={"Allow": "GET, POST, PUT, DELETE, OPTIONS"})
            self.authorize(req)
            handler, params = self.router.match(req.method, req.path)
            req.params = params
            return handler(req)
        except ApiError as e:
            return Response(e.status, e.body(req.path), e.headers, content_type="application/problem+json")
        except Exception:
            log.exception("unhandled error for %s %s", req.method, req.path)
            err = ApiError(500, "Internal error", "See the relay log.", slug="internal-error")
            return Response(500, err.body(req.path), content_type="application/problem+json")


# --------------------------------------------------------------------------
# HTTP server glue
# --------------------------------------------------------------------------

def make_handler(app: RelayApp):
    class RelayHandler(BaseHTTPRequestHandler):
        server_version = "apple-news-relay/" + __version__
        protocol_version = "HTTP/1.1"
        # An idle kept-alive connection is closed after this; longer than clients keep one idle
        # (.NET: 100 s, Android: 5 s), so they never write into a closed one.
        timeout = 120

        def _serve(self):
            started = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b""
            req = Request(self.command, self.path, self.headers, body, self.client_address[0])
            resp = app.handle(req)
            request_id = hashlib.sha1(("%s%s%s" % (started, self.path, id(req))).encode()).hexdigest()[:12]
            headers = {
                "Access-Control-Allow-Origin": app.cors_origin,
                "Access-Control-Allow-Headers": "Authorization, Content-Type, If-None-Match, Prefer",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Expose-Headers": "ETag, Link, Location, Retry-After, X-Request-Id",
                "X-Request-Id": request_id,
            }
            headers.update(resp.headers)
            if resp.stream is not None:
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.content_type)
                self.send_header("Cache-Control", resp.cache)
                self.send_header("Connection", "close")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()

                def write(chunk: bytes):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                try:
                    resp.stream(write)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return
            status = resp.status
            payload = b"" if status == 304 else resp.body_bytes()
            if resp.etag and status in (200, 304):
                headers["ETag"] = resp.etag
                if status == 200 and resp.etag in (self.headers.get("If-None-Match") or ""):
                    status, payload = 304, b""
            elif status == 200 and payload:
                etag = 'W/"%s"' % hashlib.sha1(payload).hexdigest()[:20]
                headers["ETag"] = etag
                if etag in (self.headers.get("If-None-Match") or ""):
                    status, payload = 304, b""
            encoding = None
            if len(payload) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                payload = gzip.compress(payload, 5)
                encoding = "gzip"
            self.send_response(status)
            if status != 304 and status != 204:
                self.send_header("Content-Type", resp.content_type)
            self.send_header("Cache-Control", resp.cache)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Vary", "Accept-Encoding, Authorization")
            if encoding:
                self.send_header("Content-Encoding", encoding)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            log.info('%s "%s %s" %d %dB %.0fms', self.client_address[0], self.command, self.path, status,
                     len(payload), (time.time() - started) * 1000)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _serve

        def log_message(self, format, *args):  # noqa: A002 - the access log above replaces this
            pass

    return RelayHandler


def serve(app: RelayApp, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
