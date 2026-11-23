# Apple News Relay: a local Apple News API for your Mac

**Read Apple News as JSON.** Apple News has no public API for readers. Apple News Relay is a small
server for macOS that turns the News app on your Mac into a clean, local REST API: Today's
sections, full articles as tidy content blocks, channels, topics, Saved Stories, reading history
and full-text search, with **ads and publisher promotions filtered out at the API level**.

It also *drives* News for you. Ask the API for a story whose text isn't on the Mac yet, and the
relay opens it in News in the background, waits for the download and answers with the body.
Refreshing Today, opening a channel or running a search work the same way.

```console
$ curl -s 'localhost:8787/v1/sections?per_section=2' | jq '.data[] | {id, title, first: .articles[0].title}'
{ "id": "top-stories", "title": "Top Stories", "first": "…" }
{ "id": "for-you",     "title": "For You",     "first": "…" }
```

- Pure Python standard library (3.9+, so the Mac's built-in `/usr/bin/python3` works). No dependencies.
- Keeps everything it sees in its own SQLite archive. News prunes its cache within minutes;
  the relay doesn't, so the archive is also good for looking back: when a story first appeared,
  which sections carried it and how high.
- Resource-oriented JSON, cursor pagination, sparse fieldsets, `ETag`/304, gzip, RFC 9457
  problem responses, an OpenAPI 3.1 description, Server-Sent Events, jobs with `202 Accepted`.
- Advertises itself with Bonjour (`_newsrelay._tcp`) so clients on the LAN find it with no setup.

> Not affiliated with Apple. It reads files the News app keeps on your own Mac and serves them to
> your own devices. Stories belong to their publishers; keep it personal.

## Contents

- [Quick start](#quick-start)
- [Permissions](#permissions)
- [A tour of the API](#a-tour-of-the-api)
- [Ads, promotions and Apple News+](#ads-promotions-and-apple-news)
- [How it works](#how-it-works)
- [Run it as a service](#run-it-as-a-service)
- [Clients](#clients)
- [Development](#development)
- [Limits](#limits)

## Quick start

Requirements: macOS 13 or later, the News app signed in and opened at least once.

```sh
git clone https://github.com/VladislavEkimtcov/apple-news-relay.git
cd apple-news-relay
python3 -m apple_news_relay doctor          # checks access to News and its cache
python3 -m apple_news_relay serve           # http://127.0.0.1:8787
```

Open <http://127.0.0.1:8787/> for a list of endpoints, or <http://127.0.0.1:8787/openapi.json>.

To serve other devices on your network (a TV, a tablet):

```sh
python3 -m apple_news_relay serve --host 0.0.0.0 --allow 127.0.0.1,192.168.1.0/24
# and/or require a token:  --token "$(openssl rand -hex 16)"
```

## Permissions

The relay needs two things from macOS, both granted to whatever runs it (Terminal, iTerm, or
`python3` when run by launchd):

| What | Why | Where |
|---|---|---|
| **Full Disk Access** (or "access data from other apps" when macOS asks) | read News's cache in `~/Library/Containers/com.apple.news` | System Settings › Privacy & Security |
| **Accessibility** and **Automation › System Events** | jobs: press "Today", "Check for New Stories", scroll the feed, type a search | System Settings › Privacy & Security |

Reading works without the second; only jobs need it. `doctor` tells you what's missing.

## A tour of the API

All responses are JSON. Collections look like this:

```json
{
  "data": [ { "id": "A…", "type": "article", "title": "…", "completeness": "full", … } ],
  "meta": { "count": 20, "limit": 20, "next_cursor": "WyItcHVi…", "sort": "-published_at" },
  "links": { "self": "/v1/articles?limit=20", "next": "/v1/articles?limit=20&cursor=WyItcHVi…" }
}
```

| Endpoint | What |
|---|---|
| `GET /v1/sections?per_section=10` | Today's sections in feed order (Top Stories, For You, Trending, topic and channel groups) with their stories |
| `GET /v1/sections/{id}` · `/history` | one section in order; every story it has carried, with best position and first/last seen |
| `GET /v1/articles` | the archive: filter by `channel`, `topic`, `section`, `q`, `since`/`until`, `access`, `completeness`, `format`, `language`, `saved`; `sort`, `limit`, `cursor`, `fields`, `count` |
| `GET /v1/articles/{id}` | one story with its body as blocks; fetches the body from News if needed (`wait=` seconds to block) |
| `POST /v1/articles/{id}/fetch` | ask News to download a story |
| `GET /v1/articles/{id}/related` | related stories News lists for it |
| `GET /v1/channels` · `/v1/topics` | publishers and topics, with article counts; `/{id}`, `/{id}/articles`, `POST /{id}/refresh` |
| `GET /v1/saved` · `/v1/history` | Saved Stories and reading history |
| `GET /v1/search?q=` | full-text search over everything archived, with highlighted snippets |
| `POST /v1/searches` `{"query": "…"}` | run the search in News itself, archiving what it finds |
| `POST /v1/today/refresh?pages=8` | refresh Today in News and scroll it to load more |
| `GET /v1/jobs/{id}?wait=30` | follow a News job; `DELETE` cancels a queued one |
| `GET /v1/events` | Server-Sent Events: `articles.added`, `article.body_captured`, `sections.updated`, `job.updated` |
| `GET /v1/status` · `/v1/stats` | health, account, harvester; archive statistics |

An article body is a list of blocks a client can render without knowing anything about Apple
News Format:

```json
"body": {
  "blocks": [
    { "type": "image", "url": "https://c.apple.news/AgEk…", "width": 1200, "height": 800, "caption": "…" },
    { "type": "paragraph", "text": "…" },
    { "type": "heading", "level": 2, "text": "…" },
    { "type": "quote", "text": "…" }
  ],
  "word_count": 812, "truncated": false, "removed": { "advertisement": 2, "promotion": 3 }
}
```

Every article says how much of it the relay has, so a client can always show *something*:

| `completeness` | meaning |
|---|---|
| `full` | the whole story |
| `preview` | the opening (an Apple News+ story and the Mac has no subscription) |
| `summary` | headline, summary and picture; the body hasn't been downloaded yet |

More in [docs/API.md](docs/API.md).

## Ads, promotions and Apple News+

With the default `ads=exclude`:

- sponsored stories are left out of every list and search;
- promotional feed slots (News+ upsell groups) are left out of `/v1/sections`;
- inside bodies, ad components (banner, MREC, replica ads) and the document's ad settings are
  dropped, and so are publisher promotions: newsletter sign-ups, "follow us", subscribe and
  donate buttons, and asides.

`ads=include` keeps sponsored stories and promotional sections, and marks promotions inside bodies
instead of removing them. Ad slots never carry story content, so they are always removed.

**Apple News+ stories** are served in full only when the Mac's account has Apple News+. Without
it, the API returns the opening of the story (`completeness: "preview"`, `body.truncated: true`),
the way a publisher shows a lede before its paywall.

## How it works

```
 News.app ──writes──▶ its cache (protobuf records, SQLite feed, ANF JSON bodies)
    ▲                         │ watched every few seconds
    │ open -g applenewss://…  ▼
    │ Accessibility       harvester ──▶ relay.db (SQLite + FTS5, keeps everything)
    │ (Today, refresh,                         │
    │  scroll, search)                         ▼
 job runner ◀──── POST /v1/… ────────── HTTP API ──▶ your devices
```

- The **harvester** decodes News's undocumented caches ([docs/FORMATS.md](docs/FORMATS.md)):
  article and channel records (schema-less protobuf), the Today feed (SQLite), cached article
  bodies (Apple News Format JSON), Saved Stories and history (binary plists).
- The **job runner** makes News download what was asked for. Opening `applenewss://apple.news/<id>`
  in the background caches a story's body within seconds; channels and topics open the same way;
  Today is refreshed and scrolled through the Accessibility API. Which action loads what, and how
  fast, is measured in [docs/PLAYBOOK.md](docs/PLAYBOOK.md). Jobs run one at a time, only ever
  use these fixed actions, never click inside a story (so never an ad or a purchase button), and
  give focus back to the app you were using.

## Run it as a service

```sh
./launchd/install.sh --host 0.0.0.0 --allow 127.0.0.1,192.168.1.0/24   # starts at login, restarts if it stops
./launchd/install.sh --undo
```

macOS asks once, the first time, for the permissions above for `python3`. Logs go to
`~/Library/Logs/apple-news-relay.log`; the archive lives in
`~/Library/Application Support/apple-news-relay/relay.db`.

## Clients

- [Broadsheet](https://github.com/VladislavEkimtcov/broadsheet-news-tv): an Android TV / Fire TV
  news reader built on this API (D-pad only, finds the relay by itself).
- Anything that speaks HTTP: `curl`, a home dashboard, a script that emails you Top Stories, an
  e-ink display.

## Development

```sh
python3 -m unittest discover -s tests       # synthetic fixtures; no Apple News data needed
python3 -m apple_news_relay openapi > openapi.json
python3 tools/explore.py --hours 1          # measure the playbook on your own Mac (needs a running relay)
```

The tests build a fake News container from synthetic bytes, so they run anywhere, including CI
on Linux.

## Limits

- The cache formats are undocumented and can change with any News update. Field meanings are
  listed in [docs/FORMATS.md](docs/FORMATS.md), with the ones that were inferred marked.
- Jobs need the Mac awake and logged in (with `--keep-awake`, the relay keeps it from idle-sleeping).
- Family-shared Apple News+ isn't detected yet, so it reads as no subscription.
- Single user, single Mac, by design.

## License

MIT. See [LICENSE](LICENSE).
