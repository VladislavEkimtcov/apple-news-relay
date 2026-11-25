# Playbook: what makes News load what

News downloads only what it is about to show. These are the levers the relay pulls, what each
one does to the cache, and how long it takes. Measured on a MacBook (macOS 15.7, News 10, US
storefront, no Apple News+), first by hand while watching the screen, then by
`tools/explore.py` running unattended overnight (summary at the end).

## Levers

| Action | How the relay does it | What lands in the cache | Time | Focus |
|---|---|---|---|---|
| Open a story | `open -g applenewss://apple.news/<A…>` | the story's ANF body (also for Apple News+ stories the account can't read) | usually 0.2–0.3 s to the body on disk; up to 13 s | stays where it was |
| Open a channel or topic | `open -g applenewss://apple.news/<T…>` | ~50–85 article records for it, bodies of its first 3 stories, a few tags | 10–35 s | stays |
| Refresh Today | AX: press "Today" in the sidebar, menu View › Check for New Stories | a new feed cursor; For You and other groups rebuilt; **old records pruned** (1,251 → 734 in one refresh) | 20–30 s | stays |
| Scroll Today | AX: `AXScrollDownByPage` on the feed's scroll area, N times. Found with one `entire contents` Apple Event (~0.5 s), its path remembered between refreshes; a story card is the fallback | more groups (topic, channel, magazine), records and bodies: 12 pages gave +418 records, +50 bodies | ~0.5 s to find, then ~1 s a page | News stayed in the background (2026-11-25, 4 scrolls); the relay puts the previous app back anyway |
| Search | keystrokes: ⌘F, ⌘A, query, Return | records for the results, bodies of the first 8 results, matching topics and channels | ~6 s | needs News in front for a moment |
| Save, like, dislike a story | open it, then menu File › Save Story / Unsave Story, Suggest More / Fewer Stories Like This | the change itself (Saved Stories syncs through iCloud) | 2.2–2.4 s | stays |
| Nothing | — | News refreshes on its own when it's in front; the cache shrinks over time | — | — |

## Things that don't work

- **Walking News's tree element by element.** Accessibility costs one Apple Event per attribute read,
  and each re-resolves the element's path from the window: ~20 ms at the window, 55–100 ms at
  depth 20–25. A depth-first search reading role and description per element took 27.5 s for ~200
  elements (the old Today press and scroll timed out at 36–42 s). `entire contents` returns every
  element's path and class in one event (0.25–0.5 s), and `kids.description()` reads all siblings
  at once (27 sidebar rows: 74 ms against 2.2 s). Today press now 0.3–0.6 s, find-and-scroll 2
  pages 1.4 s. App Nap isn't the cause: 80–84 ms per event warm and after 2 minutes idle.
- **The sidebar only lists rows on screen.** Scrolled down, "Today" isn't in the tree at all;
  `AXScrollUpByPage` on a sidebar row brings it back.
- **A story page's outer content scroll area can't page** (no `AXScrollDownByPage`); the story's
  own scroller inside it can. On Today the feed's scroll area (depth 14) pages directly.

- There is no URL for Today or for a search (`applenewss://apple.news/search?q=…` does nothing,
  `applenews://` alone does nothing).
- Opening `https://apple.news/<id>` with `open` goes to the default browser, not News; use the
  `applenewss://` form.
- The News window's Accessibility tree is shallow for containers but exposes the sidebar rows
  (by description: "Today", "Saved Stories", …) and every story card (description: publisher,
  "Apple News Plus", headline, age). Coordinates aren't needed.
- Right after a story or channel opens, News may briefly have no window; scripts wait for one.
- A Today refresh starts a new feed cursor, and News adds a section to it only as the feed
  scrolls to that section on screen. A refresh that couldn't scroll left Top Stories alone, and
  the relay showed only that; it now keeps the earlier cursor's sections (up to 36 hours old)
  after the newest one's until a newer feed has them.
- While someone works on the Mac with News on another Space, System Events lists **no** News
  windows at all, so the feed can't be pressed or scrolled; the menu bar still works. A Today
  refresh then only clicks Check for New Stories (`feed_reached: false` in its result).
- The menus work without a window and without News in front: News's **Window menu lists each
  window's title** ("<headline> - <publisher>" for a story), which is how a story action checks
  that News shows the right story before pressing anything. In the File menu, Save Story turns
  into Unsave Story once a story is saved, and Suggest More / Fewer Stories Like This carry a
  check mark (`AXMenuItemMarkChar` "✓") when set; they exclude each other, and pressing a checked
  one again clears it.

## Safety rules the relay follows

- Only fixed actions: open by validated id, press "Today", one menu item from a fixed list,
  page-scroll, type a search. A story action presses its menu item only once the Window menu
  names the requested story, and only when that changes something. No clicks at coordinates, so never on ads, "Subscribe", "Get Started" or "Start
  Reading" (which begin a purchase).
- One action at a time: 0.25 s after the last when someone is waiting, 0.6 s when fetching
  stories a client is heading for (at most 12 per client; a newer list replaces the older, and a
  background fetch gives up after 8 s), 1.5 s for refreshes and channel opens. Identical requests
  are merged; a story that won't open isn't retried for 6 hours.
- Focus goes back to whatever app was in front.

## Eviction

News keeps bodies for minutes to hours and records until a refresh prunes them: a refresh dropped
517 of 1,251 records; a WSJ body cached at 00:27 was gone by 00:33. The relay therefore copies
everything as soon as it appears (it checks every 5 s) and never deletes.

## Overnight run

`tools/explore.py` picks an action every ~3 minutes (refresh with 0–16 pages, open a channel or
topic, fetch a free or a News+ story, search, or idle) and records what the archive gained.
Summarize a log with `python3 tools/explore.py --report <log>`.

First run: 99 actions over 6 hours (2026-11-23, 00:50–07:03), with the relay's archive growing
alongside (about 2,000 stories by the end), so "+articles" counts only stories it had never
seen.

```
action                    runs       ok  s median +articles  +bodies
fetch_free                  17     100%       6.1       5.8      1.0
fetch_paid                   5     100%       6.1       0.0      1.0
idle                        14     100%      65.0       0.0      0.0
open_tag                    22     100%      33.5      40.3      1.3
refresh_today pages=0        8     100%      17.2       6.4      0.0
refresh_today pages=4        4      75%      53.6      16.2      0.0
refresh_today pages=8        7      57%      60.1       7.9      0.0
refresh_today pages=16       6      67%      57.2       3.5      0.0
search                      16     100%      14.5       1.7      0.4
```

What it says:

- **Opening a story always works**: 22 of 22 bodies captured, ~6 s, News+ stories included
  (their bodies are cached even without a subscription; the relay serves only a preview of
  them).
- **Opening a channel is the best way to grow the archive**: a median of +25 new stories per
  channel (up to +120), +12 per Today section, and usually nothing new for a topic.
- **Refreshing Today with a few pages is enough**: 4 pages found the most new stories; 8 and 16
  pages found fewer (the feed repeats itself further down) and often ran past the 60 s `wait`
  (the "ok" misses are jobs still running at 60 s, plus one "Today not found" while News had no
  window). Clients should poll the job rather than rely on `wait` for big refreshes.
- **Search adds little**: ~2 new stories, most results were already archived.
- **Idling adds nothing**: News doesn't refresh in the background while it isn't in front.

## Warming run (relay 0.3.0)

Three interest lists of 10 stories each (30 stories with no body, from the deep sections),
2026-11-23 15:56, News in the background on another Space:

```
captured           30 of 30, none timed out, all done in 40 s
fetch (open→body)  median 0.4 s, p90 1.3 s, max 4.9 s
start to start     median 1.0 s (0.6 s gap + the fetch), max 5.5 s
News footprint     169 MB before, ~840 MB after, still ~840 MB three minutes later
```

The footprint grows with how many stories News opens, not with the gap; on a 32 GB Mac with 43%
free it didn't matter. Real interest lists are shorter (12 at most per client, each replacing the
last), so a burst like this is the worst case. If News ever feels heavy, `--warm-gap 1.0` spaces
the fetches out.
