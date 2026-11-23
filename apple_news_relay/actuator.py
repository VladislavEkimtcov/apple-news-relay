"""Make News.app load things, then wait for the harvester to see them.

Apple News only downloads what it is asked to show, so the relay "asks" it:

=================  ===============================================  =======================================
job                how News is driven                               what to expect (see docs/PLAYBOOK.md)
=================  ===============================================  =======================================
fetch_article      ``open -g applenewss://apple.news/<article id>``  full body cached in ~5-15 s, background
open_tag           ``open -g applenewss://apple.news/<tag id>``      ~50 records for the channel/topic
refresh_today      Accessibility: press "Today", menu View > Check  new feed cursor; page scrolls load more
                   for New Stories, scroll N pages                  sections, records and bodies
search             keystrokes: Cmd-F, Cmd-A, query, Return          result records + bodies of the first 8
story_action       open the story (as fetch_article), check the     Save/Unsave Story, Suggest More/Fewer
                   Window menu names it, then one File menu item    Stories Like This; ~3-6 s, background
=================  ===============================================  =======================================

Only these fixed actions exist; identifiers are validated before they reach
a command line, and nothing ever clicks inside story content, so ads,
"Subscribe" and purchase buttons are never touched. A story action clicks a
File menu item only once News's Window menu shows that very story. One job
runs at a time.
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .harvester import Harvester
from .store import Store

log = logging.getLogger(__name__)

ARTICLE_ID = re.compile(r"^A[A-Za-z0-9_-]{15,40}$")
TAG_ID = re.compile(r"^T[A-Za-z0-9_-]{15,40}$")

FIND_JS = r"""
function find(e, d, test) {
  if (d > 28) return null;
  let kids = [];
  try { kids = e.uiElements(); } catch (x) { return null; }
  for (const k of kids) {
    let desc = "", role = "";
    try { desc = k.description() || ""; role = k.role(); } catch (x) {}
    if (test(role, desc, k)) return k;
    const f = find(k, d + 1, test);
    if (f) return f;
  }
  return null;
}
const news = Application("System Events").processes.byName("News");
let win = null;
for (let i = 0; i < 24 && win === null; i++) {  // News swaps its window while opening some pages
  try { win = news.windows[0]; win.name(); } catch (x) { win = null; delay(0.5); }
}
if (win === null) throw new Error("News has no window");
"""


class ActuatorError(RuntimeError):
    pass


#: What a story action does: the File menu item to press, and the state it leads to.
STORY_ACTIONS = {
    "save": ("Save Story", "saved", True),
    "unsave": ("Unsave Story", "saved", False),
    "like": ("Suggest More Stories Like This", "reaction", "like"),
    "dislike": ("Suggest Fewer Stories Like This", "reaction", "dislike"),
    "clear_reaction": (None, "reaction", None),
}

STORY_STATE_JS = r"""
const p = Application("System Events").processes.byName("News");
const mb = p.menuBars[0];
const items = mb.menuBarItems.byName("File").menus[0].menuItems;
const names = items.name(), enabled = items.enabled(), file = {};
for (let i = 0; i < names.length; i++) {
  if (!names[i]) continue;
  let mark = "";
  try { mark = items[i].attributes.byName("AXMenuItemMarkChar").value() || ""; } catch (e) {}
  file[names[i]] = [enabled[i], mark];
}
const wn = mb.menuBarItems.byName("Window").menus[0].menuItems.name();
JSON.stringify({file: file, windows: wn.slice(wn.lastIndexOf("Arrange in Front") + 1).filter(n => n)});
"""


def _norm_title(text: str) -> str:
    text = (text or "").lower()
    for a, b in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2013", "-"), ("\u2014", "-")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class StoryState:
    """News's File menu for the story in its window, and that window's title."""
    windows: List[str]
    saved: Optional[bool]          # None while no story is open (both Save items disabled)
    reaction: Optional[str]        # like | dislike | None

    @classmethod
    def parse(cls, raw: dict) -> "StoryState":
        file = raw.get("file") or {}

        def on(name):
            return bool(file.get(name, [False])[0])

        def checked(name):
            return bool((file.get(name) or [False, ""])[1])
        saved = True if on("Unsave Story") else False if on("Save Story") else None
        reaction = "like" if checked("Suggest More Stories Like This") else \
            "dislike" if checked("Suggest Fewer Stories Like This") else None
        return cls(list(raw.get("windows") or []), saved, reaction)

    def shows(self, titles: List[str]) -> bool:
        """Exactly one News window, and it is one of these stories (window titles are
        "<headline> - <publisher>")."""
        if len(self.windows) != 1:
            return False
        window = _norm_title(self.windows[0])
        for t in titles:
            t = _norm_title(t)
            if len(t) < 8:
                continue
            if window == t or window.startswith(t + " - ") or (len(t) > 60 and window.startswith(t[:60])):
                return True
        return False


class NewsApp:
    """Low-level control of News.app. Every method is synchronous."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._run = runner

    def _cmd(self, args: List[str], timeout: float = 60) -> str:
        proc = self._run(args, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise ActuatorError((proc.stderr or proc.stdout or "command failed").strip()[:300])
        return (proc.stdout or "").strip()

    def jxa(self, body: str, timeout: float = 90) -> str:
        return self._cmd(["osascript", "-l", "JavaScript", "-e", FIND_JS + body], timeout)

    def is_running(self) -> bool:
        try:
            return self._cmd(["pgrep", "-x", "News"], 5) != ""
        except ActuatorError:
            return False

    def frontmost(self) -> Optional[str]:
        try:
            return self._cmd(["osascript", "-e", 'tell application "System Events" to get bundle identifier of '
                              'first process whose frontmost is true'], 10)
        except ActuatorError:
            return None

    def front_bundle(self) -> Optional[str]:
        """The frontmost app's bundle id, cheaply (``lsappinfo``, ~15 ms; no Accessibility)."""
        try:
            asn = self._cmd(["lsappinfo", "front"], 5)
            out = self._cmd(["lsappinfo", "info", "-only", "bundleid", asn], 5)
        except (ActuatorError, OSError, subprocess.SubprocessError):
            return None
        m = re.search(r'"CFBundleIdentifier"="([^"]+)"', out)
        return m.group(1) if m else None

    def front_window_title(self) -> Optional[str]:
        """The title of News's front window ("<headline> - <publisher>" for a story)."""
        try:
            return self._cmd(["osascript", "-e", 'tell application "System Events" to get name of front window '
                              'of process "News"'], 10) or None
        except ActuatorError:
            return None

    def activate(self, bundle_id: str):
        if re.match(r"^[A-Za-z0-9.-]+$", bundle_id or ""):
            self._cmd(["osascript", "-e", 'tell application id "%s" to activate' % bundle_id], 10)

    def open_article(self, article_id: str):
        if not ARTICLE_ID.match(article_id):
            raise ValueError("not an article id")
        self._cmd(["open", "-g", "applenewss://apple.news/" + article_id], 20)

    def open_tag(self, tag_id: str):
        if not TAG_ID.match(tag_id):
            raise ValueError("not a channel or topic id")
        self._cmd(["open", "-g", "applenewss://apple.news/" + tag_id], 20)

    def launch(self):
        self._cmd(["open", "-g", "-b", "com.apple.news"], 20)

    def show_today(self, attempts: int = 3):
        """Press "Today" in the sidebar. The tree can be mid-rebuild right after a story opens,
        and the sidebar may be hidden, so look again a few times."""
        for attempt in range(attempts):
            try:
                self.jxa('const t = find(win, 0, (r, d) => d === "Today"); '
                         'if (!t) throw new Error("Today not found"); t.actions.byName("AXPress").perform(); "ok"')
                return
            except ActuatorError:
                if attempt == attempts - 1:
                    raise
                try:
                    self._cmd(["osascript", "-e", 'tell application "System Events" to tell process "News" to '
                               'click menu item "Show Sidebar" of menu 1 of menu bar item "View" of menu bar 1'], 10)
                except ActuatorError:
                    pass  # sidebar already showing
                time.sleep(1.5)

    def check_for_new_stories(self):
        self._cmd(["osascript", "-e", 'tell application "System Events" to tell process "News" to click menu item '
                   '"Check for New Stories" of menu 1 of menu bar item "View" of menu bar 1'], 20)

    def scroll_feed(self, pages: int = 6, pause: float = 0.8, attempts: int = 2) -> int:
        """Page down the feed through Accessibility. The page is scrolled through any described
        element right of the sidebar (a story card or a section header). Returns pages scrolled."""
        script = (
            'const left = win.position()[0] + 330; '
            'const c = find(win, 0, (r, d, k) => r === "AXGenericElement" && d.length > 20 && '
            '(() => { try { return k.position()[0] > left; } catch (x) { return false; } })()); let n = 0; '
            'if (c) { for (let i = 0; i < %d; i++) { try { c.actions.byName("AXScrollDownByPage").perform(); n++; '
            'delay(%s); } catch (x) { break; } } } String(n)' % (max(1, min(pages, 40)), pause))
        for attempt in range(attempts):
            scrolled = int(self.jxa(script, timeout=30 + pages * 3) or 0)
            if scrolled or attempt == attempts - 1:
                return scrolled
            time.sleep(3)  # the feed may still be loading after a refresh
        return 0

    def visible_stories(self) -> List[str]:
        """Accessibility descriptions of the story cards on screen (publisher, flags, headline, age)."""
        out = self.jxa(
            'const out = []; function walk(e, d) { if (d > 28 || out.length > 80) return; let kids = []; '
            'try { kids = e.uiElements(); } catch (x) { return; } for (const k of kids) { let r = "", s = ""; '
            'try { r = k.role(); s = k.description() || ""; } catch (x) {} '
            'if (r === "AXGenericElement" && / ago$/.test(s)) out.push(s); walk(k, d + 1); } } '
            'walk(win, 0); out.join("\\n")')
        return [line for line in out.splitlines() if line]

    def story_state(self) -> StoryState:
        """The open story's Save and Suggest items, read from the menu bar (works while News is in
        the background or on another Space)."""
        return StoryState.parse(json.loads(self._cmd(["osascript", "-l", "JavaScript", "-e", STORY_STATE_JS], 20)))

    def press_file_item(self, name: str):
        if name not in {a[0] for a in STORY_ACTIONS.values() if a[0]}:
            raise ValueError("not a story menu item")
        self._cmd(["osascript", "-e", 'tell application "System Events" to tell process "News" to click menu item '
                   '"%s" of menu 1 of menu bar item "File" of menu bar 1' % name], 20)

    def search(self, query: str):
        """Type a search. Needs News in front for a moment; the caller restores focus."""
        clean = re.sub(r"[\"\\\r\n\t]", " ", query).strip()[:120]
        if not clean:
            raise ValueError("empty query")
        self._cmd(["osascript",
                   "-e", 'tell application id "com.apple.news" to activate',
                   "-e", "delay 0.6",
                   "-e", 'tell application "System Events" to tell process "News"',
                   "-e", 'keystroke "f" using command down',
                   "-e", "delay 0.3",
                   "-e", 'keystroke "a" using command down',
                   "-e", 'keystroke "%s"' % clean,
                   "-e", "key code 36",
                   "-e", "end tell"], 30)


@dataclass
class Job:
    kind: str
    target: Optional[str]
    params: Dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    priority: str = "normal"
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target, "state": self.state,
                "priority": self.priority, "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "result": self.result, "error": self.error}


#: Queue order: someone is waiting on the answer, then refreshes, then prefetching.
PRIORITIES = {"interactive": 0, "normal": 1, "background": 2}


class QueueFull(RuntimeError):
    pass


class Yielded(Exception):
    """A background job stepped aside for one someone is waiting on; it runs again later."""


class JobRunner:
    """Runs one News job at a time and records each in the store."""

    TIMEOUTS = {"fetch_article": 25.0, "open_tag": 25.0, "refresh_today": 60.0, "search": 30.0,
                "story_action": 20.0}

    def __init__(self, store: Store, harvester: Harvester, app: Optional[NewsApp] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None, max_queue: int = 50,
                 min_gap: float = 1.5, pace: float = 1.0):
        self.store = store
        self.harvester = harvester
        self.app = app or NewsApp()
        self.on_event = on_event or (lambda kind, data: None)
        self.max_queue = max_queue
        self.min_gap = min_gap
        self.pace = pace  # scales every wait below; tests run at a fraction of real time
        self.timeouts = dict(self.TIMEOUTS)
        self._queue: "queue.PriorityQueue" = queue.PriorityQueue()
        self._order = itertools.count()
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_action = 0.0
        self._opened: Dict[str, float] = {}  # stories the relay opened in News, and when

    # ------------------------------------------------------------ submit

    #: A fetch that failed is not retried for this long: News could not open that story.
    RETRY_AFTER = 6 * 3600.0

    def submit(self, kind: str, target: Optional[str] = None, priority: str = "normal", **params) -> Job:
        """Queues a job, or returns the identical one already pending (moved up if this need is
        more urgent). ``priority``: interactive, normal or background."""
        if kind not in self.TIMEOUTS:
            raise ValueError("unknown job kind")
        if priority not in PRIORITIES:
            raise ValueError("unknown priority")
        with self._lock:
            for job in self._jobs.values():  # identical pending job: reuse it
                if job.kind == kind and job.target == target and job.params == params \
                        and job.state in ("queued", "running"):
                    if job.state == "queued" and PRIORITIES[priority] < PRIORITIES[job.priority]:
                        job.priority = priority
                        self._enqueue(job)  # the old queue entry is skipped once this one has run
                    return job
            if kind == "fetch_article":
                recent = [j for j in self._jobs.values() if j.kind == kind and j.target == target
                          and j.done.is_set() and j.finished_at and time.time() - j.finished_at < self.RETRY_AFTER
                          and (j.state == "failed" or (j.result or {}).get("captured") is False)]
                if recent:
                    return max(recent, key=lambda j: j.finished_at or 0)
            if sum(1 for j in self._jobs.values() if j.state == "queued") >= self.max_queue:
                raise QueueFull("too many queued jobs")
            job = Job(kind, target, params, priority=priority)
            self._jobs[job.id] = job
            self._prune()
            self._enqueue(job)
        self.store.put_job(job.to_dict())
        self.on_event("job.updated", job.to_dict())
        return job

    def _enqueue(self, job: Job):
        self._queue.put((PRIORITIES[job.priority], next(self._order), job))

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def cancel(self, job_id: str) -> Optional[Job]:
        job = self._jobs.get(job_id)
        if job and job.state == "queued":
            self._finish(job, "cancelled")
        return job

    def _prune(self, keep: int = 500):
        done = [j for j in self._jobs.values() if j.done.is_set()]
        for job in sorted(done, key=lambda j: j.created_at)[:-keep or None] if len(done) > keep else []:
            self._jobs.pop(job.id, None)

    # ------------------------------------------------------------- worker

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._work, name="news-jobs", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 10.0):
        """Lets the job in hand finish, then ends the worker (queued jobs stay queued)."""
        self._queue.put((-1, next(self._order), None))
        if self._thread:
            self._thread.join(timeout)

    def opened_recently(self, article_id: str, within: float = 1800.0) -> bool:
        """The relay itself opened this story in News lately (to fetch or save it for someone)."""
        return time.time() - self._opened.get(article_id, 0) < within

    def _open(self, article_id: str):
        self._opened[article_id] = time.time()
        if len(self._opened) > 500:
            for k, _ in sorted(self._opened.items(), key=lambda kv: kv[1])[:100]:
                self._opened.pop(k, None)
        self.app.open_article(article_id)

    def busy(self, settle: float = 4.0) -> bool:
        """A job is driving News, or did a moment ago (what News shows is then the relay's doing)."""
        running = any(j.state == "running" for j in list(self._jobs.values()))
        return running or time.time() - self._last_action < settle

    def _work(self):
        while True:
            _, _, job = self._queue.get()
            if job is None:
                return
            if job.state != "queued":
                continue
            # Someone waiting on it goes almost at once; prefetching and refreshes keep their distance.
            gap = (min(self.min_gap, 0.25) if job.priority == "interactive" else self.min_gap) \
                - (time.time() - self._last_action)
            if gap > 0:
                time.sleep(gap)
            job.state, job.started_at = "running", time.time()
            self.store.put_job(job.to_dict())
            self.on_event("job.updated", job.to_dict())
            try:
                job.result = getattr(self, "_run_" + job.kind)(job)
                self._finish(job, "succeeded")
            except Yielded:
                job.state, job.started_at = "queued", None
                self.store.put_job(job.to_dict())
                self.on_event("job.updated", job.to_dict())
                with self._lock:
                    self._enqueue(job)
            except Exception as e:
                log.warning("job %s %s failed: %s", job.kind, job.target, e)
                job.error = str(e)[:300]
                self._finish(job, "failed")
            self._last_action = time.time()

    def _finish(self, job: Job, state: str):
        job.state, job.finished_at = state, time.time()
        self.store.put_job(job.to_dict())
        job.done.set()
        self.on_event("job.updated", job.to_dict())

    def _sleep(self, seconds: float):
        time.sleep(seconds * self.pace)

    def _urgent_waiting(self) -> bool:
        return any(j.state == "queued" and j.priority == "interactive" for j in list(self._jobs.values()))

    def _wait_for(self, predicate: Callable[[], bool], timeout: float, poll: float = 1.0,
                  yielding: bool = False) -> bool:
        """Harvests until ``predicate`` holds or ``timeout`` passes. ``yielding``: raise
        :class:`Yielded` as soon as an interactive job is queued."""
        timeout *= self.pace
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            self.harvester.run_once()
            if predicate():
                return True
            if yielding and self._urgent_waiting():
                raise Yielded()
            # News usually writes a story within a second: look often at first (a pass costs a few
            # ms when nothing changed), then settle to ``poll``.
            time.sleep((0.15 if time.time() - start < 4 else poll) * self.pace)
        self.harvester.run_once()
        return predicate()

    def _restore_focus(self, previous: Optional[str]):
        if previous and previous != "com.apple.news":
            try:
                self.app.activate(previous)
            except ActuatorError:
                pass

    # -------------------------------------------------------------- jobs

    def _has_body(self, article_id: str) -> bool:
        row = self.store.read().execute("SELECT has_body FROM articles WHERE id = ?", (article_id,)).fetchone()
        return bool(row and row[0])

    def _run_fetch_article(self, job: Job) -> dict:
        article_id = job.target or ""
        if self._has_body(article_id):
            return {"captured": True, "already_cached": True}
        start = time.time()
        self._open(article_id)
        ok = self._wait_for(lambda: self._has_body(article_id), self.timeouts["fetch_article"],
                            yielding=job.priority == "background")
        return {"captured": ok, "seconds": round(time.time() - start, 1)}

    def _count_tag(self, tag_id: str) -> int:
        return self.store.read().execute(
            "SELECT COUNT(*) FROM article_tags WHERE tag_id = ?", (tag_id,)).fetchone()[0]

    def _run_open_tag(self, job: Job) -> dict:
        tag_id = job.target or ""
        before = self._count_tag(tag_id)
        since = time.time()
        self.app.open_tag(tag_id)
        self._wait_for(lambda: self._count_tag(tag_id) > before, self.timeouts["open_tag"])
        self._sleep(3)  # records keep arriving for a few seconds after the first batch
        self.harvester.run_once()
        return {"new_articles": self._count_tag(tag_id) - before, "seconds": round(time.time() - since, 1)}

    def _feed_cursor(self) -> Optional[str]:
        row = self.store.read().execute("SELECT MAX(updated_at) FROM sections").fetchone()
        return row[0] if row else None

    def _run_refresh_today(self, job: Job) -> dict:
        previous = self.app.frontmost()
        start = time.time()
        total_before = self.store.read().execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        if not self.app.is_running():
            self.app.launch()
            self._sleep(4)
        try:
            self.app.show_today()
            window = True
        except ActuatorError as e:
            # News's window is out of Accessibility's reach (on another Space while someone uses
            # the Mac): the menu still works, scrolling the feed doesn't.
            log.info("refresh without the feed: %s", e)
            window = False
        self._sleep(1)
        self.app.check_for_new_stories()
        self._sleep(4)
        pages = int(job.params.get("pages", 8))
        scrolled = self.app.scroll_feed(pages) if pages and window else 0
        self._sleep(3)
        self.harvester.run_once()
        self._restore_focus(previous)
        total_after = self.store.read().execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return {"pages_scrolled": scrolled, "new_articles": total_after - total_before, "feed_reached": window,
                "seconds": round(time.time() - start, 1)}

    def _run_search(self, job: Job) -> dict:
        previous = self.app.frontmost()
        start = time.time()
        self.app.search(job.target or "")
        self._sleep(6)
        self.harvester.run_once()
        self._restore_focus(previous)
        self._sleep(2)
        self.harvester.run_once()
        rows = self.store.read().execute(
            "SELECT id FROM articles WHERE first_seen >= ? OR id IN "
            "(SELECT article_id FROM documents WHERE captured_at >= ?) ORDER BY first_seen DESC LIMIT 200",
            (start - 1, start - 1)).fetchall()
        return {"seconds": round(time.time() - start, 1), "loaded_article_ids": [r[0] for r in rows]}

    def _story_titles(self, article_id: str) -> List[str]:
        titles = []
        row = self.store.read().execute("SELECT title FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row and row[0]:
            titles.append(row[0])
        doc = self.store.document_for(article_id)
        if doc and doc.get("title"):
            titles.append(doc["title"])
        return titles

    def _run_story_action(self, job: Job) -> dict:
        """Opens the story, makes sure News shows it, then sets one thing about it: saved or not,
        liked, disliked or neither. Pressing an item that is already in the wanted state would undo
        it, so the state is read first and only a change is pressed."""
        article_id = job.target or ""
        item, key, want = STORY_ACTIONS[job.params["action"]]
        titles = self._story_titles(article_id)
        if not titles:
            raise ActuatorError("the relay doesn't know this story's title yet")
        start = time.time()
        self._open(article_id)
        deadline = start + self.timeouts["story_action"] * self.pace
        state = None
        while time.time() < deadline:
            state = self.app.story_state()
            if state.saved is not None and state.shows(titles):
                break
            self._sleep(0.3)
        else:
            if state is not None and len(state.windows) > 1:
                raise ActuatorError("News has %d windows open; close all but one" % len(state.windows))
            raise ActuatorError("News didn't show the story")
        before = getattr(state, key)
        if key == "reaction" and want is None and before is not None:
            item = STORY_ACTIONS[before][0]  # clearing: press the checked item again
        changed = before != want
        if changed:
            self.app.press_file_item(item)
            for _ in range(12):
                self._sleep(0.25)
                state = self.app.story_state()
                if getattr(state, key) == want:
                    break
            else:
                raise ActuatorError("News didn't take the change")
        if key == "saved":
            self.store.set_saved(article_id, bool(want))
            self.on_event("saved.updated", {"article_id": article_id, "saved": bool(want)})
        self.store.set_reaction(article_id, state.reaction)
        return {"saved": state.saved, "reaction": state.reaction, "changed": changed,
                "seconds": round(time.time() - start, 1)}
