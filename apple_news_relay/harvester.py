"""Copy what Apple News caches into the relay database, as soon as it appears.

The harvester watches file modification times and re-reads only what changed.
A pass takes well under a second on a typical cache (about a thousand article
records), so polling every few seconds costs nothing noticeable.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import anf
from .newscache import (NewsPaths, feed_tag, iter_cached_document_assets, read_articles, read_document,
                        read_feed_states, read_history, read_saved, read_sections, read_tag_feeds, read_tags)
from .store import Store

log = logging.getLogger(__name__)

#: Sections are rebuilt at least this often, since their time windows move on.
RECOMPOSE_EVERY = 600.0


@dataclass
class HarvestReport:
    started_at: float
    finished_at: float = 0.0
    new_articles: List[str] = field(default_factory=list)
    updated_articles: int = 0
    tags: int = 0
    documents: List[str] = field(default_factory=list)      # article ids whose body was captured
    feeds: List[str] = field(default_factory=list)          # tags whose channel/topic feed changed
    sections_changed: bool = False                          # the Today feed's sections, or the stories in them
    saved_changed: bool = False
    front_changed: bool = False                             # what a front page shows (sections, Latest, Saved)
    errors: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.new_articles or self.documents or self.sections_changed or self.saved_changed or self.tags
                    or self.feeds)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at, "duration_ms": round((self.finished_at - self.started_at) * 1000),
            "new_articles": len(self.new_articles), "updated_articles": self.updated_articles,
            "tags": self.tags, "bodies_captured": len(self.documents), "feeds_changed": len(self.feeds),
            "sections_changed": self.sections_changed, "saved_changed": self.saved_changed,
            "front_changed": self.front_changed, "errors": self.errors,
        }


class Harvester:
    """Incremental copier. ``run_once`` is safe to call from any thread."""

    def __init__(self, store: Store, paths: Optional[NewsPaths] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        self.store = store
        self.paths = paths or NewsPaths()
        self.on_event = on_event or (lambda kind, data: None)
        self._lock = threading.Lock()
        self._mtimes: Dict[str, float] = {}
        self._assets_since = 0.0
        self._stop = threading.Event()
        self._feeds: Optional[Dict[str, str]] = None   # feed id -> signature, as last read
        self._composed_at = 0.0
        self.last_report: Optional[HarvestReport] = None
        self.passes = 0

    def _changed(self, *files: str) -> bool:
        changed = False
        for f in files:
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            if self._mtimes.get(f) != m:
                self._mtimes[f] = m
                changed = True
        return changed

    def run_once(self, force: bool = False) -> HarvestReport:
        with self._lock:
            report = HarvestReport(started_at=time.time())
            p = self.paths
            if force:
                self._mtimes.clear()
            steps = (
                (self._harvest_tags, (p.record_source("tag-record-source"),)),
                (self._harvest_articles, (p.record_source("article-record-source"),)),
                (self._harvest_sections, (p.today_feed_db, p.today_feed_db + "-wal")),
                (self._harvest_feeds, (p.feed_db, p.feed_db + "-wal")),
                (self._harvest_library, (p.reading_list, p.history)),
            )
            for step, files in steps:
                if self._changed(*files):
                    try:
                        step(report)
                    except Exception as e:  # keep harvesting the other sources
                        log.exception("harvest step %s failed", step.__name__)
                        report.errors.append("%s: %s" % (step.__name__, e))
            try:
                self._harvest_documents(report)
            except Exception as e:
                log.exception("document harvest failed")
                report.errors.append("documents: %s" % e)
            try:
                self._compose(report)
            except Exception as e:
                log.exception("composing sections failed")
                report.errors.append("compose: %s" % e)
            report.finished_at = time.time()
            self.last_report = report
            self.passes += 1
        self._announce(report)
        return report

    def run_documents(self) -> List[str]:
        """Only look for new story bodies (a couple of milliseconds): for a job waiting on one."""
        with self._lock:
            report = HarvestReport(started_at=time.time())
            try:
                self._harvest_documents(report)
            except Exception:
                log.exception("document harvest failed")
            report.finished_at = time.time()
        self._announce(report)
        return report.documents

    def _announce(self, report: HarvestReport):
        if report.new_articles:
            self.on_event("articles.added", {"count": len(report.new_articles), "ids": report.new_articles[:50]})
        for article_id in report.documents:
            self.on_event("article.body_captured", {"id": article_id})
        if report.sections_changed:
            self.on_event("sections.updated", {})
        if report.saved_changed:
            self.on_event("saved.updated", {})
        if report.front_changed:
            self.on_event("front.updated", {"version": self.store.front_version})

    def _compose(self, report: HarvestReport):
        """Rebuild the sections' story lists when anything they draw on changed (or now and then,
        as their time windows move), and move the front version on if a front page would change."""
        due = time.time() - self._composed_at > RECOMPOSE_EVERY
        inputs = report.new_articles or report.updated_articles or report.tags or report.feeds or report.sections_changed
        if not (inputs or due or report.saved_changed):
            return
        if inputs or due:
            self._composed_at = time.time()
            if self.store.compose_sections():
                report.sections_changed = True
        latest = self.store.latest_changed() if (report.new_articles or report.updated_articles) else False
        if report.sections_changed or report.saved_changed or latest:
            self.store.bump_front()
            report.front_changed = True

    def _harvest_tags(self, report: HarvestReport):
        report.tags = self.store.upsert_tags(read_tags(self.paths))

    def _harvest_articles(self, report: HarvestReport):
        articles = list(read_articles(self.paths))
        report.new_articles = self.store.upsert_articles(articles)
        report.updated_articles = len(articles) - len(report.new_articles)
        # A new record may point at a body that is already cached.
        for a in articles:
            if a.document_asset and not self.store.has_document(a.document_asset):
                self._capture(a.document_asset, report)

    def _harvest_sections(self, report: HarvestReport):
        sections = read_sections(self.paths)
        if sections and self.store.update_sections(sections):
            report.sections_changed = True

    def _harvest_feeds(self, report: HarvestReport):
        """Channel and topic feeds: read again only the feeds News refreshed or extended."""
        states = read_feed_states(self.paths)
        if not states:
            return
        if self._feeds is None:
            self._feeds = self.store.feed_signatures()
        changed = {f: s for f, s in states.items() if self._feeds.get(f) != s}
        if not changed:
            return
        tags = sorted({feed_tag(f) for f in changed})
        items = read_tag_feeds(self.paths, None if not self._feeds else tags)
        report.feeds = sorted(self.store.upsert_feed_items(items, changed))
        self._feeds.update(changed)

    def _harvest_library(self, report: HarvestReport):
        if self.store.update_saved(read_saved(self.paths)):
            report.saved_changed = True
        self.store.update_history(read_history(self.paths))

    def _harvest_documents(self, report: HarvestReport):
        newest = self._assets_since
        for asset_id, mtime in iter_cached_document_assets(self.paths, since=self._assets_since - 2):
            newest = max(newest, mtime)
            if not self.store.has_document(asset_id):
                self._capture(asset_id, report)
        self._assets_since = newest

    def _capture(self, asset_id: str, report: HarvestReport):
        doc = read_document(self.paths, asset_id)
        if doc is None:
            return
        converted = anf.convert(doc)
        text = "\n".join(b.get("text", "") for b in converted.blocks)
        article_id = self.store.save_document(asset_id, doc, text)
        if article_id:
            self.store.remember_converted(article_id, False, converted)  # the first open needs no conversion
            report.documents.append(article_id)

    # --------------------------------------------------------------- loop

    def run_forever(self, interval: float = 5.0):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("harvest pass failed")
            self._stop.wait(interval)

    def start(self, interval: float = 5.0) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, args=(interval,), name="harvester", daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()
