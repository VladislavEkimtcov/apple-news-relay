"""Handoff: which story the Mac is reading in News right now, for a client to pick up.

While News is the frontmost app, its front window's title ("<headline> - <publisher>") names the
story on screen. The watcher checks every few seconds (``lsappinfo`` first, ~15 ms, and the window
title only while News is in front), matches the title to the archive, and publishes a
``handoff.updated`` event when the story changes. A story counts once it has been on screen for
two checks in a row while no relay job was driving News. A story the relay itself opened in the
last half hour (for a TV, say) counts only after a minute in front: a glance at News showing what
the relay left there isn't reading it. The last story stays on offer for half an hour.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

NEWS = "com.apple.news"


def _norm(text: str) -> str:
    text = (text or "").lower()
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


class Handoff:
    def __init__(self, store, app, jobs, on_event: Optional[Callable[[str, dict], None]] = None,
                 interval: float = 4.0, keep: float = 1800.0):
        self.store = store
        self.app = app
        self.jobs = jobs
        self.on_event = on_event or (lambda kind, data: None)
        self.interval = interval
        self.keep = keep
        self.current: Optional[dict] = None   # {article_id, since, seen_at}
        self.on_screen = False
        self._candidate: Optional[str] = None
        self._candidate_since = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._loop, name="handoff", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("handoff check failed")

    def get(self, now: Optional[float] = None) -> Optional[dict]:
        """The story on offer, or None when nothing was read on the Mac in the last ``keep`` s."""
        now = now or time.time()
        if self.current and now - self.current["seen_at"] <= self.keep:
            return dict(self.current, on_screen=self.on_screen)
        return None

    def check(self, now: Optional[float] = None):
        now = now or time.time()
        was_on_screen = self.on_screen
        if self.app.front_bundle() != NEWS or self.jobs.busy():
            self.on_screen = False
            self._candidate = None
            if was_on_screen and self.current:
                self.on_event("handoff.updated", self._event())
            return
        article_id = self.match(self.app.front_window_title())
        if article_id is None or article_id != self._candidate:
            self._candidate = article_id   # needs a second look before it counts
            self._candidate_since = now
            self.on_screen = bool(article_id and self.current and self.current["article_id"] == article_id)
            if was_on_screen != self.on_screen and self.current:
                self.on_event("handoff.updated", self._event())
            return
        changed = self.current is None or self.current["article_id"] != article_id
        if changed and self.jobs.opened_recently(article_id) and now - self._candidate_since < 60:
            return  # the relay put it there; wait to see that someone stays with it
        if changed:
            self.current = {"article_id": article_id, "since": now, "seen_at": now}
        self.current["seen_at"] = now
        self.on_screen = True
        if changed or not was_on_screen:
            self.on_event("handoff.updated", self._event())

    def _event(self) -> dict:
        return {"article_id": self.current["article_id"] if self.current else None, "on_screen": self.on_screen}

    def match(self, title: Optional[str]) -> Optional[str]:
        """The archived story a News window title names, or None (Today, a channel, a search)."""
        if not title:
            return None
        whole = _norm(title)
        heads: List[str] = [whole]
        if " - " in whole:
            heads.append(whole.rsplit(" - ", 1)[0])
        conn = self.store.read()
        for head in heads:
            if len(head) < 12:
                continue
            prefix = head[:24].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = conn.execute("SELECT id, title FROM articles WHERE lower(title) LIKE ? ESCAPE '\\' "
                                "ORDER BY last_seen DESC LIMIT 25", (re.sub(r"['\"-]", "_", prefix) + "%",)
                                ).fetchall()
            for row in rows:
                if _norm(row[1]) == head:
                    return row[0]
        return None
