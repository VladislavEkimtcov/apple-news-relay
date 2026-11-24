"""Harvester, store, jobs and the HTTP API, end to end against a fake News app."""

import json
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from apple_news_relay.actuator import JobRunner, NewsApp, StoryState
from apple_news_relay.api import EventHub, RelayApp, Request
from apple_news_relay.harvester import Harvester
from apple_news_relay.store import Store

from fixtures import NOW, FakeNews, anf_document

CH = "TchannelAAAAAAAAAAAAAAA"
CH2 = "TchannelBBBBBBBBBBBBBBB"
TOPIC = "TtopicCCCCCCCCCCCCCCCCC"
TECH = "TtechDDDDDDDDDDDDDDDDDD"


def aid(n: int) -> str:
    return "Astory%017d" % n


class FakeNewsApp(NewsApp):
    """Plays News.app: opening a story caches its body, opening a channel adds stories."""

    def __init__(self, news: FakeNews):
        super().__init__(runner=None)
        self.news = news
        self.calls = []
        self.unopenable = set()
        self.title_of = lambda article_id: "?"
        self.showing = None          # the story in News's window
        self.extra_windows = []
        self.saved = set()
        self.reactions = {}
        self.stubborn = False        # News ignores menu presses
        self.front = "com.apple.Terminal"
        self.window = None

    def front_bundle(self):
        return self.front

    def front_window_title(self):
        return self.window

    def is_running(self):
        return True

    def frontmost(self):
        return "com.apple.Terminal"

    def activate(self, bundle_id):
        self.calls.append(("activate", bundle_id))

    def open_article(self, article_id):
        super_check = __import__("apple_news_relay.actuator", fromlist=["ARTICLE_ID"]).ARTICLE_ID
        assert super_check.match(article_id)
        self.calls.append(("open_article", article_id))
        self.showing = article_id
        if article_id not in self.unopenable:
            self.news.add_document("Adoc" + article_id[-6:], anf_document("Story " + article_id[-3:]))

    def story_state(self):
        windows = ([self.title_of(self.showing) + " - The Daily Example"] if self.showing else []) + self.extra_windows
        file = {}
        if self.showing:
            file["Unsave Story" if self.showing in self.saved else "Save Story"] = [True, ""]
            r = self.reactions.get(self.showing)
            file["Suggest More Stories Like This"] = [True, "\u2713" if r == "like" else ""]
            file["Suggest Fewer Stories Like This"] = [True, "\u2713" if r == "dislike" else ""]
        return StoryState.parse({"file": file, "windows": windows})

    def press_file_item(self, name):
        self.calls.append(("press", name))
        if self.stubborn:
            return
        s = self.showing
        if name == "Save Story":
            self.saved.add(s)
        elif name == "Unsave Story":
            self.saved.discard(s)
        else:
            r = "like" if "More" in name else "dislike"
            self.reactions[s] = None if self.reactions.get(s) == r else r

    def open_tag(self, tag_id):
        self.calls.append(("open_tag", tag_id))
        for n in range(500, 503):
            self.news.add_article(aid(n), "Channel story %d" % n, tag_id, published=NOW - n)
        self.news.set_tag_feed(tag_id, [{"id": aid(n), "published": NOW - n} for n in range(500, 503)])

    def show_today(self):
        self.calls.append(("show_today",))

    def new_window(self):
        self.calls.append(("new_window",))

    def check_for_new_stories(self):
        self.calls.append(("check",))
        self.news.add_article(aid(900), "Fresh story", CH, published=NOW + 60)

    def scroll_feed(self, pages=6, pause=0.8):
        self.calls.append(("scroll", pages))
        return pages

    def search(self, query):
        self.calls.append(("search", query))
        self.news.add_article(aid(700), "Search hit about %s" % query, CH2, published=NOW - 5)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.news = FakeNews()
        self.news.add_tag(CH, "The Daily Example", 2)
        self.news.add_tag(CH2, "Another Paper", 2)
        self.news.add_tag(TOPIC, "Rivers", 1)
        for n in range(1, 31):
            self.news.add_article(aid(n), "Story number %d about rivers" % n if n % 2 else "Story number %d" % n,
                                  CH if n % 3 else CH2, published=NOW - n * 3600,
                                  asset="Adoc" + aid(n)[-6:], paid=(n % 5 == 0), sponsored=(n == 7),
                                  topics=[TOPIC] if n % 2 else [])
        for n in (1, 5):
            self.news.add_document("Adoc" + aid(n)[-6:], anf_document("Story %d" % n, paragraphs=8))
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [aid(3), aid(1), aid(7)]},
                              {"kindId": "EXTERNAL:SMARTER_MESSAGING_(C)", "kind": "engagement", "items": []},
                              {"kindId": "CUSTOM:POLITICS,_HEADLINES", "items": [aid(2), aid(4)]}])
        self.news.write_saved([aid(2), aid(999)])
        self.store = Store(":memory:")
        self.events = EventHub()
        self.harvester = Harvester(self.store, self.news.paths, on_event=self.events.publish)
        self.fake = FakeNewsApp(self.news)
        self.fake.title_of = lambda i: self.store.read().execute("SELECT title FROM articles WHERE id = ?", (i,)).fetchone()[0]
        self.jobs = JobRunner(self.store, self.harvester, self.fake, on_event=self.events.publish, min_gap=0, pace=0.02)
        self.jobs.start()
        self.app = RelayApp(self.store, self.harvester, self.jobs, self.news.paths, self.events)
        self.report = self.harvester.run_once(force=True)

    def tearDown(self):
        self.jobs.stop()  # the worker may still be writing into the fake News folder
        self.news.close()

    def get(self, target, method="GET", body=b"", headers=None):
        resp = self.app.handle(Request(method, target, headers or {}, body, "127.0.0.1"))
        payload = json.loads(resp.body_bytes()) if resp.body_bytes() else None
        return resp.status, payload, resp

    # ------------------------------------------------------------ harvest

    def test_first_harvest(self):
        self.assertEqual(len(self.report.new_articles), 30)
        self.assertEqual(sorted(self.report.documents), [aid(1), aid(5)])
        self.assertTrue(self.report.sections_changed)

    def test_second_pass_is_a_no_op(self):
        again = self.harvester.run_once()
        self.assertFalse(again.changed)

    def test_archive_keeps_what_news_prunes(self):
        self.news.drop_article(aid(1))
        self.news.remove_document("Adoc" + aid(1)[-6:])
        self.harvester.run_once()
        status, body, _ = self.get("/v1/articles/%s?fetch=never" % aid(1))
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["completeness"], "full")
        self.assertTrue(body["data"]["body"]["blocks"])

    def test_body_seen_before_its_record_is_linked_later(self):
        self.news.add_document("Adoclate01", anf_document("Late story"))
        self.harvester.run_once()
        self.news.add_article("Alatestory0000000000001", "Late story", CH, asset="Adoclate01")
        self.harvester.run_once()
        status, body, _ = self.get("/v1/articles/Alatestory0000000000001?fetch=never")
        self.assertEqual(body["data"]["completeness"], "full")

    # --------------------------------------------------------- articles

    def test_pagination_walks_everything_once(self):
        seen, url = [], "/v1/articles?limit=7&ads=include"
        while url:
            status, body, resp = self.get(url)
            self.assertEqual(status, 200)
            seen += [a["id"] for a in body["data"]]
            url = body["links"]["next"]
            if url:
                self.assertIn('rel="next"', resp.headers["Link"])
        self.assertEqual(len(seen), 30)
        self.assertEqual(len(set(seen)), 30)
        self.assertEqual(seen[0], aid(1), "newest first")

    def test_sponsored_stories_are_filtered_by_default(self):
        _, body, _ = self.get("/v1/articles?limit=100&count=true")
        ids = [a["id"] for a in body["data"]]
        self.assertNotIn(aid(7), ids)
        self.assertEqual(body["meta"]["total"], 29)
        _, body, _ = self.get("/v1/articles?limit=100&ads=include")
        self.assertIn(aid(7), [a["id"] for a in body["data"]])

    def test_filters(self):
        _, body, _ = self.get("/v1/articles?limit=100&channel=%s" % CH2)
        self.assertTrue(body["data"] and all(a["channel"]["id"] == CH2 for a in body["data"]))
        _, body, _ = self.get("/v1/articles?limit=100&access=news_plus")
        self.assertTrue(body["data"] and all(a["access"] == "news_plus" for a in body["data"]))
        _, body, _ = self.get("/v1/articles?limit=100&completeness=full")
        self.assertEqual([a["id"] for a in body["data"]], [aid(1)])
        _, body, _ = self.get("/v1/articles?limit=100&completeness=preview")
        self.assertEqual([a["id"] for a in body["data"]], [aid(5)])
        _, body, _ = self.get("/v1/articles?limit=100&topic=%s" % TOPIC)
        self.assertEqual(len(body["data"]), 14)  # odd numbers, minus the sponsored 7
        _, body, _ = self.get("/v1/articles?limit=100&section=top-stories")
        self.assertEqual(sorted(a["id"] for a in body["data"]), [aid(1), aid(3)])

    def test_sparse_fields(self):
        _, body, _ = self.get("/v1/articles?limit=1&fields=title")
        self.assertEqual(set(body["data"][0]), {"id", "type", "title"})

    def test_paid_story_is_a_preview_without_news_plus(self):
        _, body, _ = self.get("/v1/articles/%s?fetch=never" % aid(5))
        art = body["data"]
        self.assertEqual((art["access"], art["completeness"]), ("news_plus", "preview"))
        self.assertTrue(art["body"]["truncated"])
        paragraphs = [b for b in art["body"]["blocks"] if b["type"] == "paragraph"]
        self.assertLessEqual(len(paragraphs), 3)

    def test_paid_story_is_whole_with_news_plus(self):
        self.news.write_entitlement(True)
        self.app._entitlement = (0.0, None)
        _, body, _ = self.get("/v1/articles/%s?fetch=never" % aid(5))
        self.assertEqual(body["data"]["completeness"], "full")
        self.assertFalse(body["data"]["body"]["truncated"])

    def test_missing_body_is_fetched_from_news(self):
        _, body, _ = self.get("/v1/articles/%s?wait=10" % aid(2))
        self.assertEqual(body["data"]["completeness"], "full")
        self.assertEqual(body["data"]["fetch_job"]["state"], "succeeded")
        self.assertIn(("open_article", aid(2)), self.fake.calls)

    def test_failed_fetch_is_not_retried_at_once(self):
        self.fake.unopenable.add(aid(4))
        _, body, _ = self.get("/v1/articles/%s?wait=20" % aid(4))
        self.assertEqual(body["data"]["completeness"], "summary")
        self.assertFalse(body["data"]["fetch_job"]["result"]["captured"])
        opens = self.fake.calls.count(("open_article", aid(4)))
        _, body, _ = self.get("/v1/articles/%s?wait=5" % aid(4))
        self.assertEqual(self.fake.calls.count(("open_article", aid(4))), opens)

    def test_article_detail_has_placements_and_topics(self):
        _, body, _ = self.get("/v1/articles/%s?fetch=never" % aid(3))
        art = body["data"]
        self.assertEqual(art["placements"][0]["section"], "top-stories")
        self.assertEqual(art["placements"][0]["best_position"], 1)
        self.assertEqual([t["name"] for t in art["topics"]], ["Rivers"])

    # --------------------------------------------------------- sections

    def test_sections_hide_promotions(self):
        _, body, _ = self.get("/v1/sections?per_section=5")
        self.assertEqual([s["id"] for s in body["data"]], ["top-stories", "politics-headlines"])
        self.assertEqual([a["id"] for a in body["data"][0]["articles"]], [aid(3), aid(1)], "sponsored dropped")
        _, body, _ = self.get("/v1/sections?ads=include")
        self.assertIn("smarter-messaging", [s["id"] for s in body["data"]])

    def test_section_history(self):
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [aid(9)]}], cursor="CURSOR-3", created=NOW + 600)
        self.harvester.run_once()
        _, body, _ = self.get("/v1/sections/top-stories/history?limit=10")
        self.assertEqual({p["id"] for p in body["data"]}, {aid(1), aid(3), aid(7), aid(9)})
        _, body, _ = self.get("/v1/sections/top-stories")
        self.assertEqual([a["id"] for a in body["data"]["articles"]], [aid(9)])

    # ---------------------------------------------------- library/search

    def test_saved_reports_missing(self):
        _, body, _ = self.get("/v1/saved")
        self.assertEqual([a["id"] for a in body["data"]], [aid(2)])
        self.assertEqual(body["meta"]["missing_article_ids"], [aid(999)])

    def test_search(self):
        _, body, _ = self.get("/v1/search?q=rivers&limit=50")
        ids = [a["id"] for a in body["data"]]
        self.assertTrue(ids)
        self.assertNotIn(aid(7), ids, "sponsored dropped")
        self.assertIn("[", body["data"][0]["match"]["snippet"])

    def test_search_in_news(self):
        status, body, _ = self.get("/v1/searches?wait=10", "POST", json.dumps({"query": "otters"}).encode())
        self.assertEqual(status, 200)
        self.assertIn(aid(700), body["data"]["result"]["loaded_article_ids"])
        self.assertIn(("activate", "com.apple.Terminal"), self.fake.calls, "focus goes back")

    # ----------------------------------------------------------- jobs

    def test_refresh_today(self):
        status, body, _ = self.get("/v1/today/refresh?pages=3&wait=10", "POST")
        self.assertEqual(body["data"]["state"], "succeeded")
        self.assertEqual(body["data"]["result"]["new_articles"], 1)
        self.assertIn(("scroll", 3), self.fake.calls)

    def test_refresh_without_a_reachable_window(self):
        from apple_news_relay.actuator import ActuatorError

        def no_window():
            raise ActuatorError("News has no window")
        self.fake.show_today = no_window
        status, body, _ = self.get("/v1/today/refresh?pages=3&wait=10", "POST")
        self.assertEqual(body["data"]["state"], "succeeded")
        self.assertFalse(body["data"]["result"]["feed_reached"])
        self.assertIn(("check",), self.fake.calls)
        self.assertNotIn(("scroll", 3), self.fake.calls)

    def test_refresh_opens_a_window_when_news_has_none_here(self):
        from apple_news_relay.actuator import ActuatorError
        fake = self.fake

        def show_today():
            if ("new_window",) not in fake.calls:
                raise ActuatorError("News has no window")
            fake.calls.append(("today",))
        fake.show_today = show_today
        status, body, _ = self.get("/v1/today/refresh?pages=3&wait=10", "POST")
        self.assertEqual(body["data"]["state"], "succeeded")
        self.assertTrue(body["data"]["result"]["feed_reached"])
        self.assertIn(("new_window",), fake.calls)
        self.assertIn(("scroll", 3), fake.calls)

    def test_async_job_and_polling(self):
        status, body, resp = self.get("/v1/channels/%s/refresh" % CH2, "POST")
        self.assertIn(status, (200, 202))
        job_url = resp.headers.get("Location") or "/v1/jobs/" + body["data"]["id"]
        _, body, _ = self.get(job_url + "?wait=10")
        self.assertEqual(body["data"]["state"], "succeeded")
        self.assertEqual(body["data"]["result"]["new_articles"], 3)

    def test_identical_jobs_are_merged(self):
        gate = threading.Event()
        original = self.fake.open_tag
        self.fake.open_tag = lambda t: (gate.wait(5), original(t))
        a = self.jobs.submit("open_tag", CH)
        b = self.jobs.submit("open_tag", CH)
        gate.set()
        self.assertIs(a, b)
        self.assertTrue(a.done.wait(10))

    def test_waiting_reader_jumps_the_prefetch_queue(self):
        gate = threading.Event()
        original = self.fake.open_tag
        self.fake.open_tag = lambda t: (gate.wait(5), original(t))
        busy = self.jobs.submit("open_tag", CH)  # holds the worker while the queue fills
        later = [self.jobs.submit("fetch_article", aid(n), "background") for n in (11, 12)]
        urgent = self.jobs.submit("fetch_article", aid(13), "interactive")
        moved = self.jobs.submit("fetch_article", aid(12), "interactive")
        self.assertIs(moved, later[1])
        self.assertEqual(moved.priority, "interactive")
        gate.set()
        for job in [busy, urgent] + later:
            self.assertTrue(job.done.wait(10))
        opened = [c[1] for c in self.fake.calls if c[0] == "open_article"]
        self.assertEqual(opened, [aid(13), aid(12), aid(11)])
        with self.assertRaises(ValueError):
            self.jobs.submit("fetch_article", aid(14), "urgent")

    def test_background_fetch_steps_aside(self):
        self.fake.unopenable.add(aid(15))  # News never delivers this one: the job would wait it out
        slow = self.jobs.submit("fetch_article", aid(15), "background")
        deadline = time.time() + 5
        while slow.state != "running" and time.time() < deadline:
            time.sleep(0.005)
        urgent = self.jobs.submit("fetch_article", aid(16), "interactive")
        self.assertTrue(urgent.done.wait(10))
        self.assertTrue(urgent.result["captured"])
        self.assertFalse(slow.done.is_set())  # back in the queue, not failed
        self.assertTrue(slow.done.wait(10))
        self.assertFalse(slow.result["captured"])
        opened = [c[1] for c in self.fake.calls if c[0] == "open_article"]
        self.assertEqual(opened, [aid(15), aid(16), aid(15)])

    def test_client_prefetch_is_background(self):
        gate = threading.Event()
        original = self.fake.open_tag
        self.fake.open_tag = lambda t: (gate.wait(5), original(t))
        self.jobs.submit("open_tag", CH)  # holds the worker
        status, body, _ = self.get("/v1/articles/%s/fetch?priority=background" % aid(20), "POST")
        self.assertEqual((status, body["data"]["priority"]), (202, "background"))
        self.assertEqual(self.get("/v1/articles/%s/fetch?priority=soon" % aid(20), "POST")[0], 400)
        gate.set()

    def test_saved_prefetch_is_background(self):
        self.get("/v1/saved?fetch_missing=true")
        jobs = [j for j in self.jobs.list() if j.kind == "fetch_article" and j.target == aid(999)]
        self.assertEqual([j.priority for j in jobs], ["background"])

    # ------------------------------------------------ feeds and deep sections

    def test_topic_links_from_feeds(self):
        self.news.add_tag(TECH, "Gadgets", 1)
        self.news.set_tag_feed(TECH, [{"id": aid(2), "topics": [TOPIC, "TnotATagEEEEEEEEEEEEEEE"]}, {"id": aid(4)}])
        self.harvester.run_once()
        _, body, _ = self.get("/v1/topics/%s/articles" % TECH)
        self.assertEqual({a["id"] for a in body["data"]}, {aid(2), aid(4)})
        _, body, _ = self.get("/v1/topics/%s/articles?limit=100" % TOPIC)
        self.assertIn(aid(2), [a["id"] for a in body["data"]], "filed under a known topic by the feed")
        _, body, _ = self.get("/v1/articles/%s?fetch=never" % aid(4))
        self.assertIn("Gadgets", [t["name"] for t in body["data"]["topics"]])
        _, body, _ = self.get("/v1/topics?limit=50")
        self.assertEqual({t["name"]: t["article_count"] for t in body["data"]}["Gadgets"], 2)

    def deep_setup(self):
        self.store.clock = lambda: NOW + 600
        self.news.add_tag(TECH, "Gadgets", 1)
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [aid(3), aid(1), aid(7)]},
                              {"kindId": "EXTERNAL:TOPIC", "kind": "topic", "tagId": TECH, "items": [aid(2)]},
                              {"kindId": "EXTERNAL:CHANNEL", "kind": "channel", "tagId": CH2, "items": [aid(3)]}],
                             cursor="CURSOR-3", created=NOW + 1)
        stories = [{"id": aid(n), "published": NOW - n * 3600, "channel": CH} for n in (4, 6, 8, 10, 12)]
        stories += [{"id": aid(7), "published": NOW - 7 * 3600},                     # sponsored
                    {"id": aid(14), "published": NOW - 14 * 3600, "cluster": "Csame"},
                    {"id": aid(16), "published": NOW - 16 * 3600, "cluster": "Csame"},
                    {"id": aid(18), "published": NOW - 18 * 3600, "cluster": "Csame"},  # a third telling
                    {"id": "Anorecord00000000000001", "published": NOW},            # no record yet
                    {"id": aid(2)}]                                                  # Today has it
        self.news.set_tag_feed(TECH, stories)
        self.harvester.run_once()

    def deep_ids(self, slug):
        _, body, _ = self.get("/v1/sections/%s?deep=true" % slug)
        return [a["id"] for a in body["data"]["articles"]], body["data"]

    def test_deep_topic_section(self):
        self.deep_setup()
        ids, data = self.deep_ids("topic-gadgets")
        self.assertEqual(ids, [aid(n) for n in (2, 4, 6, 8, 10, 12, 14, 16)])
        self.assertEqual((data["today_count"], data["article_count"]), (1, 8))
        _, body, _ = self.get("/v1/sections/topic-gadgets")
        self.assertEqual([a["id"] for a in body["data"]["articles"]], [aid(2)], "without deep, as in 0.2")

    def test_channel_section_filled_from_archive(self):
        self.deep_setup()
        ids, _ = self.deep_ids("channel-another-paper")
        self.assertEqual(ids, [aid(n) for n in range(3, 31, 3)])

    def test_sections_without_a_feed_keep_what_they_carried(self):
        self.deep_setup()
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [aid(9), aid(11)]}],
                             cursor="CURSOR-4", created=NOW + 2)
        self.harvester.run_once()
        ids, data = self.deep_ids("top-stories")
        self.assertEqual(ids, [aid(9), aid(11), aid(3), aid(1)], "Today's first, then earlier ones; sponsored dropped")
        self.assertEqual(data["today_count"], 2)

    def test_section_pages_walk_everything_once(self):
        self.deep_setup()
        seen, url = [], "/v1/sections/topic-gadgets/articles?limit=3"
        while url:
            status, body, _ = self.get(url)
            self.assertEqual(status, 200)
            seen += [a["id"] for a in body["data"]]
            self.assertEqual(body["meta"]["total"], 8)
            url = body["links"]["next"]
        self.assertEqual(seen, self.deep_ids("topic-gadgets")[0])
        self.assertEqual(self.get("/v1/sections/topic-gadgets/articles?cursor=bogus")[0], 400)

    def test_feed_rescan_is_incremental(self):
        from apple_news_relay import harvester as H
        self.news.set_tag_feed(TECH, [{"id": aid(2)}])
        self.news.set_tag_feed(TOPIC, [{"id": aid(3)}])
        self.harvester.run_once()
        calls, original = [], H.read_tag_feeds
        H.read_tag_feeds = lambda paths, tags=None: (calls.append(tags), original(paths, tags))[1]
        try:
            self.news.set_tag_feed(TECH, [{"id": aid(2)}, {"id": aid(4)}])
            report = self.harvester.run_once()
        finally:
            H.read_tag_feeds = original
        self.assertEqual(calls, [[TECH]])
        self.assertEqual(report.feeds, [TECH])

    def test_no_change_no_event(self):
        version, seen = self.store.front_version, []
        self.harvester.on_event = lambda kind, data: seen.append(kind)
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [aid(3), aid(1), aid(7)]},
                              {"kindId": "EXTERNAL:SMARTER_MESSAGING_(C)", "kind": "engagement", "items": []},
                              {"kindId": "CUSTOM:POLITICS,_HEADLINES", "items": [aid(2), aid(4)]}])
        self.harvester.run_once()
        self.assertEqual(self.store.front_version, version)
        self.assertNotIn("sections.updated", seen)
        self.assertNotIn("front.updated", seen)
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [aid(1), aid(3)]}], cursor="C9", created=NOW + 5)
        self.harvester.run_once()
        self.assertGreater(self.store.front_version, version)
        self.assertIn("front.updated", seen)

    # ------------------------------------------------------------ front page

    def test_front_and_not_modified(self):
        status, body, resp = self.get("/v1/front?per_section=1&latest=3")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual([s["id"] for s in data["sections"]], ["top-stories", "politics-headlines"])
        top = data["sections"][0]
        self.assertEqual([a["id"] for a in top["articles"]], [aid(3)])
        self.assertTrue(top["links"]["next"])
        self.assertEqual([a["id"] for a in data["latest"]["articles"]], [aid(1), aid(2), aid(3)])
        self.assertEqual([a["id"] for a in data["saved"]["articles"]], [aid(2)])
        self.assertEqual(data["saved"]["missing_article_ids"], [aid(999)])
        self.assertTrue(resp.etag)
        status, body, _ = self.get("/v1/front?per_section=1&latest=3", headers={"If-None-Match": resp.etag})
        self.assertEqual((status, body), (304, None))
        self.assertNotEqual(self.get("/v1/front?per_section=2&latest=3")[2].etag, resp.etag, "the query counts")
        self.news.add_article(aid(950), "Breaking", CH, published=NOW + 3600)
        self.harvester.run_once()
        status, body, again = self.get("/v1/front?per_section=1&latest=3", headers={"If-None-Match": resp.etag})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["latest"]["articles"][0]["id"], aid(950))
        self.assertNotEqual(again.etag, resp.etag)

    def test_front_fields_and_fetch_missing(self):
        _, body, _ = self.get("/v1/front?per_section=1&fields=title&fetch_missing=true")
        self.assertEqual(set(body["data"]["sections"][0]["articles"][0]), {"id", "type", "title"})
        self.get("/v1/front?per_section=1&fields=title&fetch_missing=true")
        jobs = [j for j in self.jobs.list() if j.target == aid(999)]
        self.assertEqual(len(jobs), 1, "asked once, not on every load")

    def test_saving_moves_the_front_version(self):
        version = self.store.front_version
        self.get("/v1/saved/%s?wait=10" % aid(3), "PUT")
        self.assertGreater(self.store.front_version, version)

    # -------------------------------------------------------------- bodies

    def test_partial_body_head_and_rest(self):
        _, full, _ = self.get("/v1/articles/%s?fetch=never" % aid(1))
        blocks = full["data"]["body"]["blocks"]
        _, head, _ = self.get("/v1/articles/%s?fetch=never&max_chars=150" % aid(1))
        hb = head["data"]["body"]
        self.assertTrue(0 < len(hb["blocks"]) < len(blocks))
        self.assertEqual((hb["block_count"], hb["word_count"]), (len(blocks), full["data"]["body"]["word_count"]))
        _, rest, _ = self.get("/v1/articles/%s?fetch=never&from_block=%d" % (aid(1), hb["next_block"]))
        self.assertEqual(hb["blocks"] + rest["data"]["body"]["blocks"], blocks)
        self.assertIsNone(rest["data"]["body"]["next_block"])
        self.assertIsNone(full["data"]["body"]["next_block"])

    def test_split_keeps_captions_with_their_picture(self):
        from apple_news_relay.queries import split_blocks
        blocks = [{"type": "paragraph", "text": "x" * 50}, {"type": "image"}, {"type": "caption", "text": "c"},
                  {"type": "paragraph", "text": "y"}]
        part, nxt = split_blocks(blocks, 0, 40)
        self.assertEqual((len(part), nxt), (1, 1))
        part, nxt = split_blocks(blocks, 1, 1)
        self.assertEqual((part, nxt), (blocks[1:3], 3))
        many = [{"type": "image"}] * 5
        self.assertEqual(split_blocks(many, 0, 1000), (many[:3], 3), "three pictures at most")

    def test_body_cache_follows_a_new_capture(self):
        _, a, _ = self.get("/v1/articles/%s?fetch=never" % aid(1))
        self.store.save_document("Adoc" + aid(1)[-6:], anf_document("Story 1", paragraphs=1), "text")
        _, b, _ = self.get("/v1/articles/%s?fetch=never" % aid(1))
        self.assertLess(b["data"]["body"]["block_count"], a["data"]["body"]["block_count"])

    # ------------------------------------------------- interest (warming)

    def hold_worker(self):
        gate = threading.Event()
        original = self.fake.open_tag
        self.fake.open_tag = lambda t: (gate.wait(5), original(t))
        busy = self.jobs.submit("open_tag", CH)
        deadline = time.time() + 5
        while busy.state != "running" and time.time() < deadline:
            time.sleep(0.002)
        self.addCleanup(gate.set)
        return gate

    def put_interest(self, client, ids):
        return self.get("/v1/interest/" + client, "PUT", json.dumps({"articles": ids}).encode())

    def wait_fetches(self, ids):
        deadline = time.time() + 10
        while time.time() < deadline:
            jobs = [j for j in self.jobs.list() if j.kind == "fetch_article" and j.target in ids]
            if jobs and all(j.done.is_set() for j in jobs):
                return jobs
            time.sleep(0.005)
        self.fail("fetches didn't finish")

    def test_interest_supersedes(self):
        gate = self.hold_worker()
        A, B, C, D = aid(2), aid(4), aid(6), aid(8)
        status, body, _ = self.put_interest("tv-test", [A, B, C])
        self.assertEqual([s["state"] for s in body["data"]["articles"]], ["queued"] * 3)
        self.jobs.set_interest("win-test", [A])  # another client wants A too
        self.put_interest("tv-test", [D, B])
        dropped = [j for j in self.jobs.list() if j.target == C][0]
        self.assertEqual((dropped.state, dropped.result), ("cancelled", {"superseded": True}))
        gate.set()
        self.wait_fetches([A, B, D])
        opened = [c[1] for c in self.fake.calls if c[0] == "open_article"]
        self.assertNotIn(C, opened)
        self.assertEqual(sorted(opened), sorted([A, B, D]))
        self.assertLess(opened.index(D), opened.index(B), "in the order of the newest list")
        self.assertEqual(self.get("/v1/interest/x", "PUT", b'{"articles":[]}')[0], 400)
        self.assertEqual(self.get("/v1/interest/tv-test", "PUT", b'{"articles":"A"}')[0], 400)
        self.assertEqual(self.get("/v1/interest/tv-test", "DELETE")[0], 204)

    def test_interest_skips_cached_stories(self):
        self.hold_worker()
        _, body, _ = self.put_interest("tv-test", [aid(1), aid(2), "not-an-id"])
        self.assertEqual(body["data"]["articles"], [{"id": aid(1), "state": "cached"}, {"id": aid(2), "state": "queued"}])
        self.assertEqual(self.jobs.warming(), 1)
        self.assertEqual(self.get("/v1/status")[1]["jobs"]["warming"], 1)

    def test_superseded_fetch_stops_at_once(self):
        self.jobs.BACKGROUND_FETCH_TIMEOUT = 1000  # it would otherwise wait for News much longer
        self.fake.unopenable.add(aid(22))
        self.jobs.set_interest("tv-test", [aid(22)])
        job = [j for j in self.jobs.list() if j.target == aid(22)][0]
        deadline = time.time() + 5
        while job.state != "running" and time.time() < deadline:
            time.sleep(0.002)
        self.jobs.set_interest("tv-test", [])
        self.assertTrue(job.done.wait(5))
        self.assertEqual((job.state, job.result), ("cancelled", {"superseded": True}))

    def test_background_fetch_gives_up_sooner(self):
        self.fake.unopenable.add(aid(24))
        self.jobs.set_interest("tv-test", [aid(24)])
        job = self.wait_fetches([aid(24)])[0]
        self.assertEqual((job.result["captured"], job.result["timed_out"]), (False, True))
        self.assertEqual(self.jobs.set_interest("tv-test", [aid(24)]), [{"id": aid(24), "state": "failed_recently"}])
        # someone opening it still gets a real try
        again = self.jobs.submit("fetch_article", aid(24), "interactive")
        self.assertIsNot(again, job)

    def test_reader_adopts_a_warming_fetch(self):
        gate = self.hold_worker()
        self.jobs.set_interest("tv-test", [aid(2)])
        _, body, _ = self.get("/v1/articles/%s?wait=0" % aid(2))
        job = self.jobs.get(body["data"]["fetch_job"]["id"])
        self.assertEqual((job.priority, job.owners), ("interactive", set()))
        self.jobs.set_interest("tv-test", [])
        self.assertEqual(job.state, "queued", "a newer list doesn't drop what someone waits for")
        gate.set()
        self.assertTrue(job.done.wait(10))
        self.assertEqual(job.state, "succeeded")

    def test_warming_runs_closer_together(self):
        runner = JobRunner(self.store, self.harvester, self.fake, min_gap=0.5, warm_gap=0.05)
        times, original = [], self.fake.open_article
        self.fake.open_article = lambda a: (times.append(time.time()), original(a))
        runner.start()
        try:
            runner.set_interest("tv-test", [aid(2), aid(4), aid(6)])
            deadline = time.time() + 10
            while len(times) < 3 and time.time() < deadline:
                time.sleep(0.01)
        finally:
            runner.stop()
        self.assertEqual(len(times), 3)
        self.assertTrue(all(b - a < 0.4 for a, b in zip(times, times[1:])), times)

    def test_open_tag_counts_feed_links(self):
        status, body, _ = self.get("/v1/topics/%s/refresh?wait=10" % TOPIC, "POST")
        self.assertEqual(body["data"]["state"], "succeeded")
        self.assertEqual(body["data"]["result"]["new_articles"], 3)

    # ------------------------------------------------------------- plumbing

    def test_store_reuses_read_connections(self):
        import os
        import sqlite3
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(os.path.join(tmp.name, "relay.db"))
        first = store.read()
        self.assertIs(store.read(), first)
        other = []
        t = threading.Thread(target=lambda: other.append(store.read()))
        t.start()
        t.join()
        self.assertIsNot(other[0], first)
        with self.assertRaises(sqlite3.OperationalError):
            first.execute("INSERT INTO meta VALUES ('x', 'y')")
        store.set_meta("k", "v")
        self.assertEqual(first.execute("SELECT value FROM meta WHERE key = 'k'").fetchone()[0], "v", "sees new writes")

    def test_events_start_with_hello(self):
        resp = self.app.handle(Request("GET", "/v1/events", {}, b"", "127.0.0.1"))
        chunks = []

        def write(chunk):
            chunks.append(chunk)
            if len(chunks) == 2:
                raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            resp.stream(write)
        self.assertTrue(chunks[1].startswith(b"event: hello\n"))
        hello = json.loads(chunks[1].split(b"data: ")[1])
        self.assertEqual(hello["front_version"], self.store.front_version)
        self.assertIn("interest", hello["features"])
        self.assertIn("front", self.get("/v1")[1]["features"])

    # --------------------------------------------------------- handoff

    def handoff(self):
        from apple_news_relay.handoff import Handoff
        seen = []
        h = Handoff(self.store, self.fake, self.jobs, on_event=lambda k, d: seen.append((k, d)))
        self.app.handoff = h
        return h, seen

    def test_handoff_offers_the_story_read_in_news(self):
        h, seen = self.handoff()
        self.assertIsNone(self.get("/v1/handoff")[1]["data"])
        self.fake.front, self.fake.window = "com.apple.news", "Story number 9 about rivers - The Daily Example"
        t = time.time()
        h.check(now=t - 4)
        self.assertIsNone(h.get(now=t - 4), "one look isn't enough")
        h.check(now=t)
        _, body, _ = self.get("/v1/handoff")
        self.assertEqual(body["data"]["article"]["id"], aid(9))
        self.assertTrue(body["data"]["on_screen"])
        self.assertEqual(seen[-1][1]["article_id"], aid(9))
        self.fake.front = "com.apple.Terminal"  # the reader moved on: still on offer, not on screen
        h.check(now=t + 4)
        self.assertFalse(h.get(now=t + 4)["on_screen"])
        self.assertIsNone(h.get(now=t + 3600), "offered for half an hour")

    def test_handoff_ignores_what_the_relay_opened(self):
        h, _ = self.handoff()
        self.fake.front, self.fake.window = "com.apple.news", "Story number 9 about rivers - The Daily Example"
        self.jobs._last_action = time.time()
        h.check()
        h.check()
        self.assertIsNone(h.get())

    def test_handoff_waits_on_a_story_the_relay_opened(self):
        h, _ = self.handoff()
        self.get("/v1/articles/%s/fetch?wait=10" % aid(9), "POST")  # a TV asked for it: News shows it now
        self.jobs._last_action = 0  # long after the job
        self.fake.front, self.fake.window = "com.apple.news", "Story number 9 about rivers - The Daily Example"
        t = time.time()
        h.check(now=t)
        h.check(now=t + 4)
        self.assertIsNone(h.get(now=t + 4), "a glance at what the relay left on screen")
        h.check(now=t + 64)
        self.assertEqual(h.get(now=t + 64)["article_id"], aid(9), "a minute in front is reading")

    def test_handoff_ignores_pages_that_are_not_stories(self):
        h, _ = self.handoff()
        self.fake.front = "com.apple.news"
        for title in ("Today", "The Daily Example", "Story number 1", None):
            self.fake.window = title
            h.check()
            h.check()
        self.assertIsNone(h.get())

    # -------------------------------------------------- story actions

    def test_save_and_unsave(self):
        status, body, _ = self.get("/v1/saved/%s?wait=10" % aid(4), "PUT")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["result"]["saved"], True)
        self.assertIn(("press", "Save Story"), self.fake.calls)
        self.assertTrue(self.get("/v1/articles/%s?fetch=never" % aid(4))[1]["data"]["saved"])
        self.assertIn(aid(4), [a["id"] for a in self.get("/v1/saved")[1]["data"]])
        status, body, _ = self.get("/v1/saved/%s?wait=10" % aid(4), "DELETE")
        self.assertEqual(body["data"]["result"]["saved"], False)
        self.assertFalse(self.get("/v1/articles/%s?fetch=never" % aid(4))[1]["data"]["saved"])

    def test_saving_twice_presses_once(self):
        self.get("/v1/saved/%s?wait=10" % aid(4), "PUT")
        _, body, _ = self.get("/v1/saved/%s?wait=10" % aid(4), "PUT")
        self.assertFalse(body["data"]["result"]["changed"])
        self.assertEqual([c for c in self.fake.calls if c[0] == "press"], [("press", "Save Story")])

    def test_like_dislike_and_take_back(self):
        put = lambda r: self.get("/v1/articles/%s/reaction?wait=10" % aid(6), "PUT", json.dumps({"reaction": r}).encode())
        self.assertEqual(put("like")[1]["data"]["result"]["reaction"], "like")
        self.assertEqual(self.get("/v1/articles/%s?fetch=never" % aid(6))[1]["data"]["reaction"], "like")
        self.assertEqual(put("dislike")[1]["data"]["result"]["reaction"], "dislike")
        _, body, _ = self.get("/v1/articles/%s/reaction?wait=10" % aid(6), "DELETE")
        self.assertIsNone(body["data"]["result"]["reaction"])
        self.assertEqual([c[1] for c in self.fake.calls if c[0] == "press"],
                         ["Suggest More Stories Like This", "Suggest Fewer Stories Like This",
                          "Suggest Fewer Stories Like This"])
        self.assertIsNone(self.get("/v1/articles/%s?fetch=never" % aid(6))[1]["data"]["reaction"])
        self.assertEqual(put("love")[0], 400)

    def test_story_action_never_touches_another_story(self):
        self.fake.unopenable.add(aid(8))
        original = self.fake.open_article
        self.fake.open_article = lambda i: (original(i), setattr(self.fake, "showing", aid(9)))
        _, body, _ = self.get("/v1/saved/%s?wait=10" % aid(8), "PUT")
        self.assertEqual(body["data"]["state"], "failed")
        self.assertNotIn("press", [c[0] for c in self.fake.calls])
        self.fake.open_article = original
        self.fake.extra_windows = ["Something else - Another Paper"]
        _, body, _ = self.get("/v1/saved/%s?wait=10" % aid(10), "PUT")
        self.assertIn("windows", body["data"]["error"])
        self.assertNotIn("press", [c[0] for c in self.fake.calls])

    def test_story_action_reports_a_change_news_ignored(self):
        self.fake.stubborn = True
        _, body, _ = self.get("/v1/saved/%s?wait=10" % aid(4), "PUT")
        self.assertEqual(body["data"]["state"], "failed")
        self.assertFalse(self.get("/v1/articles/%s?fetch=never" % aid(4))[1]["data"]["saved"])

    def test_save_and_unsave_are_separate_jobs(self):
        gate = threading.Event()
        original = self.fake.open_tag
        self.fake.open_tag = lambda t: (gate.wait(5), original(t))
        self.jobs.submit("open_tag", CH)
        a = self.jobs.submit("story_action", aid(4), "interactive", action="save")
        b = self.jobs.submit("story_action", aid(4), "interactive", action="unsave")
        c = self.jobs.submit("story_action", aid(4), "interactive", action="unsave")
        gate.set()
        self.assertIsNot(a, b)
        self.assertIs(b, c)
        self.assertTrue(b.done.wait(10))
        self.assertFalse(b.result["saved"])

    # --------------------------------------------------------- protocol

    def test_problems(self):
        status, body, resp = self.get("/v1/articles?limit=0")
        self.assertEqual((status, body["parameter"]), (400, "limit"))
        self.assertEqual(resp.content_type, "application/problem+json")
        self.assertEqual(self.get("/v1/articles/Anothere0000000000000")[0], 404)
        self.assertEqual(self.get("/v1/articles/not-an-id")[0], 404)
        status, _, resp = self.get("/v1/articles", "DELETE")
        self.assertEqual(status, 405)
        self.assertIn("GET", resp.headers["Allow"])
        self.assertEqual(self.get("/v1/nothing")[0], 404)
        self.assertEqual(self.get("/v1/articles?cursor=garbage")[0], 400)
        self.assertEqual(self.get("/v1/articles?since=yesterday")[0], 400)

    def test_cursor_is_bound_to_its_sort(self):
        _, body, _ = self.get("/v1/articles?limit=2")
        cursor = parse_qs(urlsplit(body["links"]["next"]).query)["cursor"][0]
        self.assertEqual(self.get("/v1/articles?limit=2&sort=published_at&cursor=" + cursor)[0], 400)

    def test_token_and_allowlist(self):
        app = RelayApp(self.store, self.harvester, self.jobs, self.news.paths, token="s3cret", allow=["192.0.2.0/24"])
        def call(client, headers):
            return app.handle(Request("GET", "/v1", headers, b"", client)).status
        self.assertEqual(call("198.51.100.5", {"Authorization": "Bearer s3cret"}), 403)
        self.assertEqual(call("192.0.2.12", {}), 401)
        self.assertEqual(call("192.0.2.12", {"Authorization": "Bearer s3cret"}), 200)

    def test_openapi_lists_every_route(self):
        _, spec, _ = self.get("/openapi.json")
        self.assertEqual(spec["openapi"], "3.1.0")
        for method, _, template, _, _ in self.app.router.routes:
            if template.startswith("/v1"):
                self.assertIn(method.lower(), spec["paths"][template])

    def test_stats(self):
        _, body, _ = self.get("/v1/stats")
        self.assertEqual(body["articles"], 30)
        self.assertEqual(body["articles_with_body"], 2)
        self.assertEqual(body["sponsored_articles"], 1)


if __name__ == "__main__":
    unittest.main()
