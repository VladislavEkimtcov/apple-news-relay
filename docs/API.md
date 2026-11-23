# API guide

The machine-readable description is `GET /openapi.json` (OpenAPI 3.1). This page explains the
conventions and the decisions behind them.

## Conventions

**Versioning.** Everything lives under `/v1`. Fields may be added within v1; nothing is renamed
or removed.

**Resources.** Every object has `id` and `type` (`article`, `section`, `channel`, `topic`,
`placement`), and a `links` object with at least `self`. Times are ISO 8601 in UTC
(`2026-11-22T13:00:00Z`).

**Collections** return `{data, meta, links}`:

| | |
|---|---|
| `limit` | 1–100, default 20 (channels and topics: up to 200, default 50) |
| `cursor` | opaque; take it from `links.next` (also sent as a `Link: <…>; rel="next"` header) |
| `sort` | articles: `-published_at` (default), `published_at`, `-first_seen_at`, `first_seen_at`, `-last_seen_at` |
| `fields` | sparse fieldset, e.g. `fields=title,image,channel` (`id` and `type` always come along) |
| `count=true` | adds `meta.total` (costs a COUNT query) |

Article lists use keyset pagination: a cursor stays correct while new stories arrive, and a
cursor only works with the sort order it was made for (else `400`).

**Filters** on `/v1/articles` (and the `/articles` of channels, topics and sections) combine
with AND; lists inside one filter combine with OR:

| | |
|---|---|
| `channel=T…,T…` / `topic=T…` | publisher(s), topic(s) |
| `section=top-stories` | stories currently in a Today section |
| `q=…` | full-text (title, summary, byline, body); every word must match |
| `since`, `until` | publication time, ISO 8601 date/time or Unix seconds |
| `access=free\|news_plus` | Apple News+ or not |
| `completeness=full\|preview\|summary` | how much of the story the relay has |
| `format=article,magazine,apple_original` | |
| `language=en` | |
| `saved=true\|false` | in Saved Stories |
| `ads=exclude\|include` | default `exclude`: see the README |

**Errors** are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem documents with
`Content-Type: application/problem+json`:

```json
{"type": ".../docs/API.md#invalid-parameter", "title": "Invalid parameter", "status": 400,
 "detail": "limit: must be between 1 and 100", "parameter": "limit", "instance": "/v1/articles"}
```

| status | slug | when |
|---|---|---|
| 400 | `invalid-parameter` | a query parameter is out of range or malformed (the `parameter` member names it) |
| 400 | `malformed-body` | a POST body isn't a JSON object |
| 401 | `unauthorized` | `--token` is set and the request lacks `Authorization: Bearer <token>` |
| 403 | `forbidden` | the client address is outside `--allow` |
| 404 | `not-found` | no such route or resource (also for promotional sections unless `ads=include`) |
| 405 | `method-not-allowed` | the route exists with other methods (`Allow` header lists them) |
| 409 | `conflict` | cancelling a job that already started |
| 429 | `too-many-requests` | the News job queue is full (`Retry-After`) |
| 500 | `internal-error` | a bug: see the relay log |

**Caching.** Every 200 carries a weak `ETag`; send it back in `If-None-Match` to get `304 Not
Modified`. Complete articles are `Cache-Control: max-age=60`, everything else `no-cache`.
Responses over 1 KB are gzipped when the client accepts it.

**CORS** is open (`Access-Control-Allow-Origin: *`) so a browser dashboard can call the relay.

**Request ids.** Every response has `X-Request-Id`, which also appears in the relay's log.

## Jobs: making News load things

Some requests need the News app to do something: download a story, refresh Today, open a
channel, search. These are jobs. They run one at a time, most urgent first (`priority` on each
job):

- `interactive`: someone is waiting on it: a story opened with `GET /v1/articles/{id}`, a fetch,
  a search.
- `normal`: refreshes of Today, sections, channels and topics.
- `background`: prefetching, such as `/v1/saved?fetch_missing=true`.

- `POST` endpoints that start a job answer `202 Accepted` with the job and a `Location` header
  (`/v1/jobs/{id}`), or `200` with the finished job if it completed within `wait`.
- Add `wait=N` (0–60 seconds) to block until the job finishes, instead of polling.
- `GET /v1/jobs/{id}?wait=30` blocks until that job is done.
- An identical job that's already queued or running is reused, not repeated (and moved up
  if the new request is more urgent).
- A story News couldn't open isn't retried for 6 hours; the job comes back `captured: false`.

`GET /v1/articles/{id}` starts a fetch by itself when the body is missing (`fetch=auto`, the
default; `fetch=never` to only read). With `wait`, it answers as soon as the body arrives:

```sh
curl 'localhost:8787/v1/articles/A1b2C3…?wait=15'
```

If the job is still running when `wait` runs out, the article comes back with
`completeness: "summary"`, a `fetch_job` member and `Retry-After`.

| job | started by | typical time |
|---|---|---|
| `fetch_article` | `GET /v1/articles/{id}`, `POST /v1/articles/{id}/fetch`, `/v1/saved?fetch_missing=true` | 2–15 s |
| `open_tag` | `POST /v1/channels/{id}/refresh`, `/v1/topics/{id}/refresh`, `/v1/sections/{id}/refresh` | 10–35 s |
| `refresh_today` | `POST /v1/today/refresh?pages=N` | 30–60 s |
| `search` | `POST /v1/searches` | ~10 s |

## Events

`GET /v1/events` is a [Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
stream:

```
event: articles.added
id: 17
data: {"count": 37, "ids": ["A…", …], "seq": 17, "at": "2026-11-23T07:01:12Z"}
```

Events: `articles.added`, `article.body_captured`, `sections.updated`, `saved.updated`,
`job.updated`. A comment line every 15 s keeps proxies from closing the connection.

## History

The relay keeps every article it has seen (`first_seen_at`, `last_seen_at`), and for every Today
section the stories it carried: `GET /v1/sections/{id}/history` lists them newest first with the
best position each reached. `GET /v1/articles/{id}` includes the article's `placements`.
`GET /v1/stats?days=30` gives articles per day and the most frequent channels.
