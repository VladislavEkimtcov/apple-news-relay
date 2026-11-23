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
Modified`. For most routes the tag is a hash of the response, so a 304 saves the transfer but not
the work; `/v1/front` tags its answer with the front version and answers 304 before building
anything. Complete articles are `Cache-Control: max-age=60`, everything else `no-cache`.
Responses over 1 KB are gzipped when the client accepts it. Connections are kept alive (idle ones
close after 120 s).

**CORS** is open (`Access-Control-Allow-Origin: *`) so a browser dashboard can call the relay.

**Request ids.** Every response has `X-Request-Id`, which also appears in the relay's log.

## Jobs: making News load things

Some requests need the News app to do something: download a story, refresh Today, open a
channel, search. These are jobs. They run one at a time, most urgent first (`priority` on each
job):

- `interactive`: someone is waiting on it: a story opened with `GET /v1/articles/{id}`, a fetch,
  a search.
- `normal`: refreshes of Today, sections, channels and topics.
- `background`: prefetching, such as an interest list (below), `/v1/saved?fetch_missing=true` or
  `POST /v1/articles/{id}/fetch?priority=background`. A background fetch steps aside as soon as
  an interactive job is queued, and gives up after 8 s (`timed_out: true`; someone opening the
  story still gets a full try).

Interactive jobs start within 0.25 s of the last News action, background story fetches 0.6 s
after it (`--warm-gap`), refreshes and channel opens 1.5 s. A fetch looks for the body every
0.15 s for its first 4 s: a story News already has opens in about half a second. An
`open_tag` steps aside for a story someone opens once it has run 3 s.

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

### Interest: fetching ahead of the reader

A client that knows where the reader is heading (the card in focus, its neighbours, the next
story) sends the stories it may open, most likely first, at most 12:

```sh
curl -X PUT localhost:8787/v1/interest/tv-1a2b3c -d '{"articles": ["A…", "A…", "A…"]}'
```

```json
{"data": {"client": "tv-1a2b3c", "articles": [{"id": "A…", "state": "cached"},
          {"id": "A…", "state": "queued"}, {"id": "A…", "state": "failed_recently"}]}}
```

The relay has News fetch the ones it has no body for, in that order, as background jobs. A newer
list from the same client replaces the older one: fetches it no longer names (and no other client
wants) are cancelled, or stopped at once if running (`result: {"superseded": true}`); the ones
still named move to their new place. A story someone opens meanwhile is never dropped. These jobs
aren't recorded or announced until they start. `DELETE /v1/interest/{client}` forgets the list
(when the app goes to the background). The client id is the client's own choice, 3–40 letters,
digits, `-` or `_`. `GET /v1/status` counts the warming jobs (`jobs.warming`).

Every fetch opens the story in News, so it also lands in News's reading history on the Mac.

### Partial bodies

`max_chars=N` returns only the story's opening: about N characters of text, at most 3 pictures
and 25 blocks, a caption kept with its picture. `body.next_block` says where the rest starts
(null when the story ends there); fetch it with `from_block`:

```sh
curl 'localhost:8787/v1/articles/A1b2C3…?wait=14&max_chars=2400'   # one trip; ~2 screens
curl 'localhost:8787/v1/articles/A1b2C3…?fetch=never&from_block=6'
```

`body.block_count` and `body.word_count` always describe the whole story. Bodies are converted
once and kept in memory (300 of them).

| job | started by | typical time |
|---|---|---|
| `fetch_article` | `GET /v1/articles/{id}`, `POST /v1/articles/{id}/fetch`, `/v1/saved?fetch_missing=true` | 2–15 s |
| `open_tag` | `POST /v1/channels/{id}/refresh`, `/v1/topics/{id}/refresh`, `/v1/sections/{id}/refresh` | 10–35 s |
| `refresh_today` | `POST /v1/today/refresh?pages=N` | 30–60 s |
| `search` | `POST /v1/searches` | ~10 s |
| `story_action` | `PUT`/`DELETE /v1/saved/{id}`, `PUT`/`DELETE /v1/articles/{id}/reaction` | 2–3 s |

## Saving, liking and disliking

These change the story in News itself (and so on every device signed in to the same Apple ID):

```sh
curl -X PUT    'localhost:8787/v1/saved/A1b2C3…?wait=25'        # Save Story
curl -X DELETE 'localhost:8787/v1/saved/A1b2C3…?wait=25'        # Unsave Story
curl -X PUT    'localhost:8787/v1/articles/A1b2C3…/reaction?wait=25' -d '{"reaction": "like"}'
curl -X DELETE 'localhost:8787/v1/articles/A1b2C3…/reaction?wait=25'   # neither
```

`like` is News's *Suggest More Stories Like This* and `dislike` *Suggest Fewer Stories Like
This*. The two exclude each other. The finished job's `result` is the story's state in News
afterwards, e.g. `{"saved": true, "reaction": null, "changed": true}`. Asking for the state it is
already in changes nothing (`changed: false`).

How it's done: the relay opens the story (as for a fetch), waits until News's Window menu names
that very story, reads the File menu (Save Story or Unsave Story; a check mark on Suggest More or
Fewer), presses one item, and reads the menu again to confirm. If News shows another story or has
more than one window, the job fails without pressing anything. `GET /v1/articles/{id}` reports
`saved` and `reaction` (as last set or seen through the relay).

## Handoff: what the Mac is reading

`GET /v1/handoff` names the story open in News on the Mac, so another screen can carry on with
it:

```json
{"data": {"article": {"id": "A…", "title": "…", …}, "on_screen": true,
          "since": "2026-11-23T14:02:10Z", "seen_at": "2026-11-23T14:06:58Z"}, "meta": {"watching": true}}
```

`on_screen` is true while News is the frontmost app and shows it. A story stays on offer for half
an hour after it was last on screen; then `data` is `null`. The relay watches every 4 s (the
frontmost app with `lsappinfo`, and News's window title only while News is in front), counts a
story after two looks in a row, and ignores what's on screen while one of its own jobs is driving
News. A story the relay itself opened in the last half hour (to fetch it for a client) counts only
after a minute in front. Each change is also a `handoff.updated` event. Start the relay with `--no-handoff` to turn
the watching off.

## Events

`GET /v1/events` is a [Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
stream:

```
event: articles.added
id: 17
data: {"count": 37, "ids": ["A…", …], "seq": 17, "at": "2026-11-23T07:01:12Z"}
```

Events: `hello`, `front.updated`, `articles.added`, `article.body_captured`, `sections.updated`,
`saved.updated`, `handoff.updated`, `job.updated`. A comment line every 15 s keeps proxies from
closing the connection.

- `hello` comes first on every connection: `{"version", "front_version", "features"}`. A client
  that reconnects compares `front_version` with the page it shows.
- `front.updated {version}`: something a front page shows changed (a section's stories, Latest,
  Saved). Captured bodies don't count; they come as `article.body_captured {id}`.
- `sections.updated` is sent only when a section really changed, not on every write News makes.

## The front page in one call

`GET /v1/front` returns what a reader's front page needs:

```json
{"data": {"version": 1795470655186,
  "sections": [{"id": "topic-tech", "title": "Tech", "today_count": 3, "article_count": 60,
                "articles": [… first 15 …],
                "links": {"articles": "/v1/sections/topic-tech/articles", "next": "…?limit=15&cursor=…"}}],
  "latest": {"articles": [… 45 …], "links": {"next": "/v1/articles?limit=45&cursor=…"}},
  "saved": {"articles": [… 45 …], "missing_article_ids": ["A…"]}}}
```

`per_section` (0–60, default 15), `latest` and `saved` (0–100, default 45), `fields`, `ads` and
`fetch_missing` work as elsewhere. The ETag is `W/"front-<version>-…"`; the version grows whenever
any of it changes. `completeness` in a cached page can lag behind; `article.body_captured` says
when a body arrives.

## Today

`GET /v1/sections` follows News's Today feed. Each refresh in News starts the feed afresh and adds
sections only as they scroll onto the screen, so a refresh that couldn't scroll (News's window on
another Space) has Top Stories alone. The relay then keeps the sections of the feed before it, up
to 36 hours old, after the new ones, until a newer feed has them again.

### Deep sections

News's Today feed shows three or four stories of a topic or publisher. With `deep=true` (on
`/v1/sections` and `/v1/sections/{id}`, and always on `/v1/front` and
`/v1/sections/{id}/articles`), a section carries Today's stories first, in News's order, then:

- **a topic or channel whose own feed News has** (`feeddatabase`): that feed's stories published
  in the last 48 hours (`--section-window-hours`), at least 10 from the last week, newest first,
  at most two tellings of the same story;
- **a channel without one:** the archive's stories from that publisher, the same way;
- **Top Stories, For You, Trending and the like:** the stories the section carried in the last
  24 hours, at most 20, most recently carried first.

Only stories the relay has a record of are listed (News lists far more than it has loaded);
sponsored stories are left out; at most 60 per section. `article_count` is the whole list,
`today_count` how many the Today feed carries. `GET /v1/sections/{id}/articles?limit=&cursor=`
pages through it; the cursor names the last story shown, so it follows on even when the list was
rebuilt in between.

## History

The relay keeps every article it has seen (`first_seen_at`, `last_seen_at`), and for every Today
section the stories it carried: `GET /v1/sections/{id}/history` lists them newest first with the
best position each reached. `GET /v1/articles/{id}` includes the article's `placements`.
`GET /v1/stats?days=30` gives articles per day and the most frequent channels.
