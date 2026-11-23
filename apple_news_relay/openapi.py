"""OpenAPI 3.1 description of the relay, served at ``/openapi.json``."""

from __future__ import annotations

from typing import Dict, List

from . import __version__


def _ref(name: str) -> dict:
    return {"$ref": "#/components/schemas/" + name}


def _param(name: str) -> dict:
    return {"$ref": "#/components/parameters/" + name}


def _page_of(name: str) -> dict:
    return {"allOf": [_ref("Page"), {"type": "object", "properties": {"data": {"type": "array", "items": _ref(name)}}}]}


def _json(schema: dict, description: str = "OK") -> dict:
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _data(schema: dict) -> dict:
    return {"type": "object", "properties": {"data": schema}}


PROBLEM = {"description": "Problem", "content": {"application/problem+json": {"schema": _ref("Problem")}}}
ACCEPTED = {"description": "Job accepted; poll the Location header", "headers": {
    "Location": {"schema": {"type": "string"}}}, "content": {"application/json": {"schema": _data(_ref("Job"))}}}

ARTICLE_FILTERS = ["channel", "topic", "section", "q", "since", "until", "access", "completeness", "format",
                   "language", "saved", "ads"]

OPERATIONS: Dict[str, Dict[str, dict]] = {
    "/v1/status": {"get": {"responses": {"200": _json(_ref("Status")), "503": _json(_ref("Status"), "Degraded")}}},
    "/v1/stats": {"get": {"parameters": [{"name": "days", "in": "query", "schema": {"type": "integer", "default": 14}}],
                          "responses": {"200": _json({"type": "object"})}}},
    "/v1/articles": {"get": {"parameters": [_param(p) for p in ["limit", "cursor", "fields", "sort", "count"] + ARTICLE_FILTERS],
                             "responses": {"200": _json(_page_of("Article")), "400": PROBLEM}}},
    "/v1/front": {"get": {"parameters": [
        {"name": "per_section", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 60, "default": 15},
         "description": "Stories per section (the first page; links.next has the rest)"},
        {"name": "latest", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 100, "default": 45}},
        {"name": "saved", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 100, "default": 45}},
        {"name": "fetch_missing", "in": "query", "schema": {"type": "boolean"},
         "description": "Have News fetch saved stories the relay has no record of (each at most every 30 minutes)"},
        _param("fields"), _param("ads")],
        "responses": {"200": _json(_data(_ref("Front"))), "304": {"description": "Not modified (If-None-Match)"}}}},
    "/v1/articles/{id}": {"get": {"parameters": [_param("id"), _param("fetch"), _param("wait"), _param("ads"), _param("fields"),
        {"name": "max_chars", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0},
         "description": "Only the opening: about this much text, at most 3 pictures and 25 blocks (0: all)"},
        {"name": "from_block", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0},
         "description": "Start at this block (body.next_block of a previous part)"}],
                                  "responses": {"200": _json(_data(_ref("ArticleDetail"))), "404": PROBLEM}}},
    "/v1/interest/{client}": {
        "put": {"parameters": [{"name": "client", "in": "path", "required": True, "schema": {"type": "string"}}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "required": ["articles"],
                    "properties": {"articles": {"type": "array", "maxItems": 12, "items": {"type": "string"}}}}}}},
                "responses": {"200": _json(_data({"type": "object", "properties": {
                    "client": {"type": "string"}, "articles": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "state": {"type": "string", "enum": [
                            "cached", "queued", "running", "failed_recently", "skipped"]}}}}}})), "400": PROBLEM}},
        "delete": {"parameters": [{"name": "client", "in": "path", "required": True, "schema": {"type": "string"}}],
                   "responses": {"204": {"description": "Forgotten"}}}},
    "/v1/articles/{id}/fetch": {"post": {"parameters": [_param("id"), _param("wait"), {"name": "priority", "in": "query",
        "schema": {"type": "string", "enum": ["interactive", "normal", "background"], "default": "interactive"},
        "description": "background: a prefetch that steps aside for stories someone is waiting on"}],
                                         "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "404": PROBLEM, "429": PROBLEM}}},
    "/v1/articles/{id}/related": {"get": {"parameters": [_param("id"), _param("ads")], "responses": {"200": _json(_page_of("Article"))}}},
    "/v1/sections": {"get": {"parameters": [_param("ads"), {"name": "per_section", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 60, "default": 0}},
                                            {"name": "current", "in": "query", "schema": {"type": "boolean", "default": True}},
                                            _param("deep")],
                             "responses": {"200": _json(_page_of("Section"))}}},
    "/v1/sections/{id}": {"get": {"parameters": [_param("id"), _param("ads"), _param("deep")],
                                  "responses": {"200": _json(_data(_ref("Section"))), "404": PROBLEM}}},
    "/v1/sections/{id}/articles": {"get": {"parameters": [_param("id"), _param("limit"), _param("cursor"), _param("ads")],
                                           "responses": {"200": _json(_page_of("Article")), "400": PROBLEM, "404": PROBLEM}}},
    "/v1/sections/{id}/history": {"get": {"parameters": [_param("id"), _param("limit"), _param("cursor")],
                                          "responses": {"200": _json(_page_of("Placement"))}}},
    "/v1/sections/{id}/refresh": {"post": {"parameters": [_param("id"), _param("wait")], "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED}}},
    "/v1/today/refresh": {"post": {"parameters": [_param("wait"), {"name": "pages", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 40, "default": 8}}],
                                   "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "429": PROBLEM}}},
    "/v1/saved": {"get": {"parameters": [_param("limit"), _param("cursor"), {"name": "fetch_missing", "in": "query", "schema": {"type": "boolean"}}],
                          "responses": {"200": _json(_page_of("Article"))}}},
    "/v1/saved/{id}": {m: {"parameters": [_param("id"), _param("wait")],
                           "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "404": PROBLEM, "429": PROBLEM}}
                       for m in ("put", "delete")},
    "/v1/articles/{id}/reaction": {
        "put": {"parameters": [_param("id"), _param("wait")], "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "required": ["reaction"], "properties": {"reaction": {"type": "string", "enum": ["like", "dislike"]}}}}}},
            "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "400": PROBLEM, "404": PROBLEM}},
        "delete": {"parameters": [_param("id"), _param("wait")],
                   "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "404": PROBLEM}}},
    "/v1/handoff": {"get": {"responses": {"200": _json({"type": "object", "properties": {"data": {"oneOf": [
        {"type": "null"}, {"type": "object", "properties": {
            "article": _ref("Article"), "on_screen": {"type": "boolean", "description": "News is in front showing it now"},
            "since": {"type": "string", "format": "date-time"}, "seen_at": {"type": "string", "format": "date-time"}}}]}}})}}},
    "/v1/history": {"get": {"parameters": [_param("limit"), _param("cursor")], "responses": {"200": _json(_page_of("Article"))}}},
    "/v1/search": {"get": {"parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                                          _param("limit"), _param("cursor"), _param("ads"), _param("fields")],
                           "responses": {"200": _json(_page_of("Article")), "400": PROBLEM}}},
    "/v1/searches": {"post": {"parameters": [_param("wait")], "requestBody": {"content": {"application/json": {"schema": {
        "type": "object", "required": ["query"], "properties": {"query": {"type": "string", "maxLength": 120}}}}}},
        "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "400": PROBLEM}}},
    "/v1/jobs": {"get": {"parameters": [_param("limit"), {"name": "state", "in": "query", "schema": {"type": "string",
                 "enum": ["queued", "running", "succeeded", "failed", "cancelled"]}}], "responses": {"200": _json(_page_of("Job"))}}},
    "/v1/jobs/{id}": {"get": {"parameters": [_param("id"), _param("wait")], "responses": {"200": _json(_data(_ref("Job"))), "404": PROBLEM}},
                      "delete": {"parameters": [_param("id")], "responses": {"200": _json(_data(_ref("Job"))), "409": PROBLEM}}},
    "/v1/events": {"get": {"responses": {"200": {"description": "text/event-stream: hello (on connect, with front_version), "
                                                 "front.updated, articles.added, article.body_captured, "
                                                 "sections.updated, saved.updated, handoff.updated and job.updated events",
                                                 "content": {"text/event-stream": {"schema": {"type": "string"}}}}}}},
}

for _kind, _plural in (("Channel", "channels"), ("Topic", "topics")):
    OPERATIONS["/v1/%s" % _plural] = {"get": {"parameters": [_param("limit"), _param("cursor"),
        {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Name contains"},
        {"name": "sort", "in": "query", "schema": {"type": "string", "enum": ["-article_count", "name", "-latest"]}},
        {"name": "all", "in": "query", "schema": {"type": "boolean"}, "description": "Include ones with no archived articles"}],
        "responses": {"200": _json(_page_of("Tag"))}}}
    OPERATIONS["/v1/%s/{id}" % _plural] = {"get": {"parameters": [_param("id")], "responses": {"200": _json(_data(_ref("Tag"))), "404": PROBLEM}}}
    OPERATIONS["/v1/%s/{id}/articles" % _plural] = {"get": {"parameters": [_param("id")] + [_param(p) for p in
        ["limit", "cursor", "fields", "sort"] + [f for f in ARTICLE_FILTERS if f not in ("channel", "topic")]],
        "responses": {"200": _json(_page_of("Article"))}}}
    OPERATIONS["/v1/%s/{id}/refresh" % _plural] = {"post": {"parameters": [_param("id"), _param("wait")],
        "responses": {"200": _json(_data(_ref("Job"))), "202": ACCEPTED, "404": PROBLEM}}}

PARAMETERS = {
    "id": {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
    "limit": {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
    "cursor": {"name": "cursor", "in": "query", "schema": {"type": "string"}, "description": "Opaque; from links.next"},
    "fields": {"name": "fields", "in": "query", "schema": {"type": "string"}, "description": "Comma-separated top-level fields to return"},
    "sort": {"name": "sort", "in": "query", "schema": {"type": "string", "enum": ["-published_at", "published_at", "-first_seen_at", "first_seen_at", "-last_seen_at"]}},
    "count": {"name": "count", "in": "query", "schema": {"type": "boolean"}, "description": "Add meta.total"},
    "channel": {"name": "channel", "in": "query", "schema": {"type": "string"}, "description": "Channel id(s), comma-separated"},
    "topic": {"name": "topic", "in": "query", "schema": {"type": "string"}, "description": "Topic id(s), comma-separated"},
    "section": {"name": "section", "in": "query", "schema": {"type": "string"}, "description": "Today section id, e.g. top-stories"},
    "q": {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Full-text query"},
    "since": {"name": "since", "in": "query", "schema": {"type": "string"}, "description": "Published at or after (ISO 8601 or Unix seconds)"},
    "until": {"name": "until", "in": "query", "schema": {"type": "string"}, "description": "Published before"},
    "access": {"name": "access", "in": "query", "schema": {"type": "string", "enum": ["free", "news_plus", "any"]}},
    "completeness": {"name": "completeness", "in": "query", "schema": {"type": "string", "enum": ["full", "preview", "summary", "any"]}},
    "format": {"name": "format", "in": "query", "schema": {"type": "string"}, "description": "article, magazine, apple_original"},
    "language": {"name": "language", "in": "query", "schema": {"type": "string"}},
    "saved": {"name": "saved", "in": "query", "schema": {"type": "boolean"}},
    "ads": {"name": "ads", "in": "query", "schema": {"type": "string", "enum": ["exclude", "include"], "default": "exclude"},
            "description": "exclude drops sponsored stories, promotional sections and publisher promotions inside bodies"},
    "fetch": {"name": "fetch", "in": "query", "schema": {"type": "string", "enum": ["auto", "never"], "default": "auto"},
              "description": "auto asks News to download a missing body"},
    "wait": {"name": "wait", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 60, "default": 0},
             "description": "Seconds to wait for a News job before answering"},
    "deep": {"name": "deep", "in": "query", "schema": {"type": "boolean", "default": False},
             "description": "The section's full list: Today's stories, then more from its topic's or publisher's feed "
                            "(or what it carried in the last day)"},
}

IMAGE = {"type": ["object", "null"], "properties": {"url": {"type": "string"}, "width": {"type": ["integer", "null"]},
                                                     "height": {"type": ["integer", "null"]}}}
BLOCK = {"type": "object", "required": ["type"], "properties": {
    "type": {"type": "string", "enum": ["heading", "paragraph", "quote", "caption", "image", "gallery", "video", "audio",
                                        "embed", "divider", "link"]},
    "text": {"type": "string"}, "level": {"type": "integer"}, "url": {"type": "string"},
    "width": {"type": ["integer", "null"]}, "height": {"type": ["integer", "null"]}, "caption": {"type": "string"},
    "images": {"type": "array", "items": {"type": "object"}}, "poster": {"type": ["string", "null"]},
    "aspect_ratio": {"type": "number", "description": "video and audio: width / height"},
    "provider": {"type": "string"}, "links": {"type": "array", "items": {"type": "string"}}}}

SCHEMAS = {
    "Problem": {"type": "object", "properties": {"type": {"type": "string"}, "title": {"type": "string"},
                "status": {"type": "integer"}, "detail": {"type": "string"}, "instance": {"type": "string"}}},
    "Page": {"type": "object", "properties": {"meta": {"type": "object", "properties": {
        "count": {"type": "integer"}, "limit": {"type": "integer"}, "next_cursor": {"type": ["string", "null"]}}},
        "links": {"type": "object", "properties": {"self": {"type": "string"}, "next": {"type": ["string", "null"]}}}}},
    "Image": IMAGE,
    "Block": BLOCK,
    "Article": {"type": "object", "properties": {
        "id": {"type": "string"}, "type": {"const": "article"}, "title": {"type": "string"},
        "excerpt": {"type": ["string", "null"]}, "byline": {"type": ["string", "null"]},
        "authors": {"type": "array", "items": {"type": "string"}},
        "channel": {"type": ["object", "null"], "properties": {"id": {"type": "string"}, "name": {"type": ["string", "null"]}, "logo": {"type": ["string", "null"]}}},
        "published_at": {"type": ["string", "null"], "format": "date-time"},
        "updated_at": {"type": ["string", "null"], "format": "date-time"},
        "first_seen_at": {"type": "string", "format": "date-time"}, "last_seen_at": {"type": "string", "format": "date-time"},
        "url": {"type": ["string", "null"], "description": "The publisher's own URL"},
        "apple_news_url": {"type": "string"}, "image": IMAGE, "thumbnails": {"type": "array", "items": IMAGE},
        "access": {"type": "string", "enum": ["free", "news_plus"]},
        "completeness": {"type": "string", "enum": ["full", "preview", "summary"],
                         "description": "full: whole story; preview: opening only (News+ story, no subscription); summary: no body yet"},
        "format": {"type": "string"}, "sponsored": {"type": "boolean"}, "language": {"type": ["string", "null"]},
        "reading_minutes": {"type": ["integer", "null"]}, "colors": {"type": "object"}, "links": {"type": "object"}}},
    "ArticleDetail": {"allOf": [_ref("Article"), {"type": "object", "properties": {
        "topics": {"type": "array", "items": {"type": "object"}}, "related_ids": {"type": "array", "items": {"type": "string"}},
        "saved": {"type": "boolean"}, "saved_at": {"type": ["string", "null"]},
        "reaction": {"type": ["string", "null"], "enum": ["like", "dislike", None],
                     "description": "Suggest More (like) or Fewer (dislike) Stories Like This, as set through the relay"},
        "placements": {"type": "array", "items": _ref("Placement")},
        "body": {"type": ["object", "null"], "properties": {"blocks": {"type": "array", "items": _ref("Block")},
                 "subtitle": {"type": ["string", "null"]}, "byline": {"type": ["string", "null"]},
                 "word_count": {"type": "integer"}, "truncated": {"type": "boolean"}, "removed": {"type": "object"},
                 "block_count": {"type": "integer", "description": "Blocks in the whole story"},
                 "from_block": {"type": "integer"},
                 "next_block": {"type": ["integer", "null"], "description": "Where the rest starts (max_chars); null: no more"}}},
        "fetch_job": _ref("Job")}}]},
    "Section": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                "kind": {"type": "string"}, "position": {"type": ["integer", "null"]}, "current": {"type": "boolean"},
                "promotional": {"type": "boolean"}, "article_count": {"type": "integer"},
                "today_count": {"type": "integer", "description": "deep: how many of them the Today feed carries"},
                "articles": {"type": "array", "items": _ref("Article")}}},
    "Front": {"type": "object", "properties": {
        "version": {"type": "integer", "description": "Grows whenever a front page would change"},
        "sections": {"type": "array", "items": _ref("Section")},
        "latest": {"type": "object", "properties": {"articles": {"type": "array", "items": _ref("Article")},
                                                     "links": {"type": "object"}}},
        "saved": {"type": "object", "properties": {"articles": {"type": "array", "items": _ref("Article")},
                                                    "missing_article_ids": {"type": "array", "items": {"type": "string"}}}}}},
    "Placement": {"type": "object", "properties": {"id": {"type": "string"}, "section": {"type": "string"},
                  "best_position": {"type": "integer"}, "first_seen_at": {"type": "string"}, "last_seen_at": {"type": "string"},
                  "article": {"oneOf": [_ref("Article"), {"type": "null"}]}}},
    "Tag": {"type": "object", "properties": {"id": {"type": "string"}, "type": {"type": "string", "enum": ["channel", "topic", "section"]},
            "name": {"type": "string"}, "logo": {"type": ["string", "null"]}, "article_count": {"type": "integer"}}},
    "Job": {"type": "object", "properties": {"id": {"type": "string"}, "kind": {"type": "string",
            "enum": ["fetch_article", "open_tag", "refresh_today", "search", "story_action"]}, "target": {"type": ["string", "null"]},
            "state": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "cancelled"]},
            "priority": {"type": "string", "enum": ["interactive", "normal", "background"]},
            "result": {"type": ["object", "null"]}, "error": {"type": ["string", "null"]}}},
    "Status": {"type": "object"},
}


def spec(routes: List[tuple]) -> dict:
    paths: Dict[str, dict] = {}
    for method, _, template, _, summary in routes:
        if not template.startswith("/v1"):
            continue
        op = dict(OPERATIONS.get(template, {}).get(method.lower(), {"responses": {"200": {"description": "OK"}}}))
        op["summary"] = summary
        op["operationId"] = (method.lower() + template.replace("/v1", "").replace("{id}", "by_id")
                             .replace("/", "_")).rstrip("_") or method.lower()
        parts = template.split("/")
        op["tags"] = [parts[2] if len(parts) > 2 else "index"]
        paths.setdefault(template, {})[method.lower()] = op
    return {
        "openapi": "3.1.0",
        "info": {"title": "Apple News Relay", "version": __version__,
                 "description": "Read the Apple News app on a Mac as a JSON API: Today sections, articles with clean "
                                "bodies, channels, topics, saved stories, history and search, with ad filtering. "
                                "Not affiliated with Apple.",
                 "license": {"name": "MIT"}},
        "servers": [{"url": "http://localhost:8787"}],
        "paths": paths,
        "components": {"parameters": PARAMETERS, "schemas": SCHEMAS,
                       "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    }
