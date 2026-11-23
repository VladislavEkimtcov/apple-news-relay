# Playbook: what makes News load what

News downloads only what it is about to show. These are the levers the relay pulls, what each
one does to the cache, and how long it takes. Measured on a MacBook (macOS 15.7, News 10, US
storefront, no Apple News+), first by hand while watching the screen, then by
`tools/explore.py` running unattended overnight (summary at the end).

## Levers

| Action | How the relay does it | What lands in the cache | Time | Focus |
|---|---|---|---|---|
| Open a story | `open -g applenewss://apple.news/<A…>` | the story's ANF body (also for Apple News+ stories the account can't read) | 1–13 s | stays where it was |
| Open a channel or topic | `open -g applenewss://apple.news/<T…>` | ~50–85 article records for it, bodies of its first 3 stories, a few tags | 10–35 s | stays |
| Refresh Today | AX: press "Today" in the sidebar, menu View › Check for New Stories | a new feed cursor; For You and other groups rebuilt; **old records pruned** (1,251 → 734 in one refresh) | 20–30 s | stays |
| Scroll Today | AX: `AXScrollDownByPage` on an element of the feed, N times | more groups (topic, channel, magazine), records and bodies: 12 pages gave +418 records, +50 bodies | ~1 s a page | **News comes forward**; the relay puts the previous app back |
| Search | keystrokes: ⌘F, ⌘A, query, Return | records for the results, bodies of the first 8 results, matching topics and channels | ~6 s | needs News in front for a moment |
| Nothing | — | News refreshes on its own when it's in front; the cache shrinks over time | — | — |

## Things that don't work

- There is no URL for Today or for a search (`applenewss://apple.news/search?q=…` does nothing,
  `applenews://` alone does nothing).
- Opening `https://apple.news/<id>` with `open` goes to the default browser, not News; use the
  `applenewss://` form.
- The News window's Accessibility tree is shallow for containers but exposes the sidebar rows
  (by description: "Today", "Saved Stories", …) and every story card (description: publisher,
  "Apple News Plus", headline, age). Coordinates aren't needed.
- Right after a story or channel opens, News may briefly have no window; scripts wait for one.

## Safety rules the relay follows

- Only fixed actions: open by validated id, press "Today", one menu item, page-scroll, type a
  search. No clicks at coordinates, so never on ads, "Subscribe", "Get Started" or "Start
  Reading" (which begin a purchase).
- One action at a time, at least 1.5 s apart; identical requests are merged; a story that won't
  open isn't retried for 6 hours.
- Focus goes back to whatever app was in front.

## Eviction

News keeps bodies for minutes to hours and records until a refresh prunes them: a refresh dropped
517 of 1,251 records; a WSJ body cached at 00:27 was gone by 00:33. The relay therefore copies
everything as soon as it appears (it checks every 5 s) and never deletes.

## Overnight run

`tools/explore.py` picks an action every ~3 minutes (refresh with 0–16 pages, open a channel or
topic, fetch a free or a News+ story, search, or idle) and records what the archive gained.
Results: see the summary appended below by `python3 tools/explore.py --report <log>`.
