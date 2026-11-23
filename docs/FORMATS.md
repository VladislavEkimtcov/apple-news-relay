# What the News app keeps on a Mac

Observed on macOS 15 (Sequoia) with News 10, US storefront. None of this is documented by
Apple; it was worked out by decoding the files and watching them change while driving the app.
Field numbers marked *inferred* are educated guesses that the relay relies on lightly.

Paths are relative to `~/Library/Containers/com.apple.news/Data/Library/`.

## Overview

| Path | Format | Holds |
|---|---|---|
| `Caches/News/com.apple.news.public-production-<storefront>/article-record-source` | record container | ~500–1,500 article records: headline, summary, byline, publisher URL, thumbnails, flags |
| `…/tag-record-source` | record container | channels, topics and channel sections |
| `…/today-feed-db` | SQLite | the Today feed: sections (groups) and their article ids |
| `…/feeddatabase`, `…/today-feed-layout-database` | SQLite | feed bookkeeping and layouts (ids only; `ad` layout slots) |
| `Caches/News/shared-assets-assetstore/:P:1:<asset>:imgfile` | ANF JSON, fonts, images | article bodies (Apple News Format), cached for minutes |
| `Application Support/com.apple.news/com.apple.news.public-com.apple.news.private-production/reading-list` | container + binary plists | Saved Stories |
| `…/history` | record container | reading history |
| `~/Library/Group Containers/group.com.apple.news/…/todayWidgetDropboxData` | NSKeyedArchiver plist | subscription state (Apple News+) |

The storefront is `143441` for the United States.

## Record containers

```
00 07                       header
08 <varint>                 field 1: entry count / version
12 <len> <entry>            field 2, repeated: one entry each
1a <len> bplist00…          field 3: metadata, ignored
```

Each entry: `02 <key length varint> <key> <protobuf message>`. The key is the record id
(`A…` article, `T…` tag, `art-A…` history). In the message, field 7 is the record (field 8 for
history), field 1 a small integer.

Dates inside records are 9 bytes: `01` followed by a little-endian double of Unix seconds.
Image sizes are packed into one varint: `width << 36 | height << 8 | 1`.

## Article record

| Field | Type | Meaning |
|---|---|---|
| 1 | message | record base: 1 id, 2 change tag, 3/4/8 dates (4: last modified) |
| 2 | string | article id |
| 3 | string | title |
| 5 | string | channel (publisher) tag id |
| 6 | date | published |
| 9 / 10 | string / size | thumbnail URL and size (also 32/33, 37/38, 39/40: larger sizes) |
| 11 | string | maturity rating (`GENERAL`) |
| 13 | string | short excerpt |
| 14 | string | byline |
| 15 | string | the publisher's canonical URL (absent for Apple-only stories) |
| 16 | int | content type (2 = Apple News Format) |
| 18 | string | URL of the body document; its `/P/1/<asset>/` names the cached ANF file |
| 20 | string | video URL |
| 21, 45 | bool | Apple News+ (paid, bundle-paid) |
| 22 | bool | magazine-issue only |
| 23 | bool | sponsored (*inferred*: set on an affiliate "deal" post) |
| 24 | strings | primary topic + channel |
| 25 | strings | topics (some are channels) |
| 26 | strings | other tags, not topics |
| 28 | strings | storefronts the story is available in |
| 30, 71 | strings | related / more-from-publisher article ids |
| 43 | int | minimum News version (2000000 on News+ stories) |
| 55 | messages | per-topic scores |
| 67 | string | magazine issue id |
| 68 | string | language |
| 69 | int | kind: 1 article, 6 magazine, 5/8 Apple originals (*inferred*) |
| 74 | int | body text length in characters (matches the ANF body within a few percent) |
| 79–82 | strings | audio narration metadata and URLs |
| 87–90 | strings | colours: background, accent, text, dark background |
| 91 | strings | authors |
| 98 | message | an embedding (768 half floats) |

## Tag record

| Field | Meaning |
|---|---|
| 2 | kind: 1 topic, 2 channel, 3 channel section |
| 3 | name |
| 22 / 78 | logo URL |
| 63 | language |
| 91 | topic path (e.g. `Football`, `Sports`) |

## Today feed (`today-feed-db`)

`cursors` holds one row per feed build; each refresh adds a cursor and the older ones linger.
`groups` holds the sections of a cursor: `kindId` (`TOP_STORIES:DEFAULT`, `EXTERNAL:FOR_YOU`,
`EXTERNAL:TRENDING`, `CUSTOM:POLITICS,_HEADLINES`, `EXTERNAL:TOPIC`, `EXTERNAL:CHANNEL`, …),
`kind` (`curated`, `forYou`, `trending`, `engagement`, `mySports`, …), `tagId` and `itemIds` (a
JSON array of article ids, in order). `blobs` holds each group's layout program (JSON).

The current feed is the groups of the newest cursor that has groups. `engagement` groups
(`SMARTER_MESSAGING`) are Apple News+ upsell slots.

## Article bodies (Apple News Format)

Bodies are standard [Apple News Format](https://developer.apple.com/documentation/apple_news/apple_news_format)
documents, cached under `shared-assets-assetstore/` as `:P:1:<asset>:imgfile` (the name is the
same for images, fonts and documents; documents start with `{`). The asset id comes from the
article record's field 18.

Images inside a body are `asset://<uuid>` resources. The CDN serves them at
`https://c.apple.news/<key>`, where the key is base64url (no padding) of
`02 01 <len(uuid)> <uuid> 00 "17"`.

Ads are components with roles `banner_advertisement`, `medium_rectangle_advertisement`,
`replica_advertisement`, plus the document-level `advertisement`, `autoplacement` and
`advertisingSettings`.

News caches the bodies of stories it opens or expects to show (Top Stories, the first search
results, the first stories of an opened channel) and evicts them within minutes to hours.

## Saved Stories (`reading-list`)

A record container whose sync-state entry embeds two binary plists: an NSKeyedArchiver change
token, then a dictionary `rl-<article id>` → `{articleID, dateAdded, origin}`.

## Reading history (`history`)

Record container, keys `art-<article id>`, record in field 8: 1 article id, 5 and 6 dates.

## Subscription (`todayWidgetDropboxData`)

An NSKeyedArchiver plist with an `FCBundleSubscription` object: `subscriptionState`,
`isPurchaser`, `isPaidBundleViaOfferActivated`, `initialPurchaseTimestamp`, `bundleChannelIDs`
(the ~1,500 News+ channels). The relay treats the account as subscribed only on a positive
purchase signal.

## URL scheme

`applenews:` and `applenewss:` are registered by News. `applenewss://apple.news/<article id>`
opens a story and `applenewss://apple.news/<tag id>` a channel or topic; `open -g` does it
without bringing News forward. There is no Today or search URL.
