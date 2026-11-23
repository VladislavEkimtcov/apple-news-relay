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

    def show_today(self):
        self.calls.append(("show_today",))

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

    def test_saved_prefetch_is_background(self):
        self.get("/v1/saved?fetch_missing=true")
        jobs = [j for j in self.jobs.list() if j.kind == "fetch_article" and j.target == aid(999)]
        self.assertEqual([j.priority for j in jobs], ["background"])

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
