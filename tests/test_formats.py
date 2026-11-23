"""Readers for News.app's on-disk formats, against synthetic fixtures."""

import unittest

from apple_news_relay import anf, newscache as nc
from apple_news_relay.protobuf import DecodeError, Message, encode_field, encode_timestamp, encode_varint, read_varint

from fixtures import NOW, FakeNews, anf_document

A1 = "Aaaaaaaaaaaaaaaaaaaaa01"
A2 = "Aaaaaaaaaaaaaaaaaaaaa02"
CH = "TchannelAAAAAAAAAAAAAAA"
TOPIC = "TtopicBBBBBBBBBBBBBBBBB"


class ProtobufTest(unittest.TestCase):
    def test_varint_round_trip(self):
        for n in (0, 1, 127, 128, 300, 2 ** 35 + 7, 2 ** 63 - 1):
            self.assertEqual(read_varint(encode_varint(n), 0), (n, len(encode_varint(n))))

    def test_message_getters(self):
        buf = (encode_field(1, 42) + encode_field(2, "hello") + encode_field(2, "again")
               + encode_field(3, encode_field(1, "inner")) + encode_field(4, 1.5) + encode_field(5, encode_timestamp(NOW)))
        m = Message(buf)
        self.assertEqual(m.int(1), 42)
        self.assertEqual(m.string(2), "hello")
        self.assertEqual(m.strings(2), ["hello", "again"])
        self.assertEqual(m.message(3).string(1), "inner")
        self.assertEqual(m.double(4), 1.5)
        self.assertAlmostEqual(m.timestamp(5), NOW)
        self.assertIsNone(m.string(99))
        self.assertEqual(m.int(99, 7), 7)
        self.assertFalse(m.bool(99))

    def test_truncated_input_raises(self):
        with self.assertRaises(DecodeError):
            Message(encode_field(2, "hello")[:-2])


class CacheReaderTest(unittest.TestCase):
    def setUp(self):
        self.news = FakeNews()
        self.news.add_tag(CH, "The Daily Example", 2)
        self.news.add_tag(TOPIC, "Rivers", 1)
        self.news.add_article(A1, "First story", CH, url="https://example.com/a", asset="Adoc1", topics=[TOPIC])
        self.news.add_article(A2, "Paid story", CH, paid=True, sponsored=True)

    def tearDown(self):
        self.news.close()

    def test_articles(self):
        arts = {a.id: a for a in nc.read_articles(self.news.paths)}
        self.assertEqual(set(arts), {A1, A2})
        a = arts[A1]
        self.assertEqual(a.title, "First story")
        self.assertEqual(a.channel_id, CH)
        self.assertEqual(a.url, "https://example.com/a")
        self.assertEqual(a.document_asset, "Adoc1")
        self.assertEqual(a.topic_ids, [TOPIC], "field 26 is ignored, the channel is not a topic")
        self.assertEqual([(t.width, t.height) for t in a.thumbnails], [(300, 200), (1600, 900)])
        self.assertEqual(a.colors["accent"], "#ff6600")
        self.assertEqual(a.kind, "article")
        self.assertFalse(a.paid)
        self.assertTrue(arts[A2].paid)
        self.assertTrue(arts[A2].sponsored)

    def test_tags(self):
        tags = {t.id: t for t in nc.read_tags(self.news.paths)}
        self.assertEqual(tags[CH].kind, "channel")
        self.assertEqual(tags[TOPIC].kind, "topic")
        self.assertEqual(tags[TOPIC].name, "Rivers")

    def test_damaged_container_yields_what_it_can(self):
        path = self.news.paths.record_source("article-record-source")
        with open(path, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data[: len(data) // 2])
        list(nc.read_articles(self.news.paths))  # must not raise

    def test_saved(self):
        self.news.write_saved([A1, A2])
        saved = nc.read_saved(self.news.paths)
        self.assertEqual([s.article_id for s in saved], [A2, A1])  # newest first
        self.assertTrue(all(s.saved_at > 1.7e9 for s in saved))

    def test_sections_use_newest_cursor(self):
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [A1]}], cursor="OLD", created=NOW - 3600)
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [A2, A1]},
                              {"kindId": "CUSTOM:POLITICS,_HEADLINES", "items": [A1]},
                              {"kindId": "EXTERNAL:SMARTER_MESSAGING_(C)", "kind": "engagement", "items": []}],
                             cursor="NEW", created=NOW)
        sections = nc.read_sections(self.news.paths)
        self.assertEqual([s.title for s in sections], ["Top Stories", "Politics, Headlines", "Smarter Messaging"])
        self.assertEqual(sections[0].article_ids, [A2, A1])
        self.assertTrue(sections[2].promotional)

    def test_sections_of_a_sparse_refresh_keep_the_earlier_ones(self):
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [A1]},
                              {"kindId": "EXTERNAL:FOR_YOU", "kind": "forYou", "items": [A1]},
                              {"kindId": "EXTERNAL:TRENDING", "kind": "trending", "items": [A2]}],
                             cursor="FULL", created=NOW - 3600)
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [A2]}], cursor="SPARSE", created=NOW)
        sections = nc.read_sections(self.news.paths)
        self.assertEqual([s.title for s in sections], ["Top Stories", "For You", "Trending"])
        self.assertEqual(sections[0].article_ids, [A2], "the newest Top Stories")
        self.news.write_feed([{"kindId": "TOP_STORIES:DEFAULT", "items": [A1]}], cursor="ANCIENT", created=NOW - 90 * 3600)
        self.assertEqual(len(nc.read_sections(self.news.paths)), 3, "a feed days old doesn't come back")

    def test_tag_feeds(self):
        other = "TotherCCCCCCCCCCCCCCCCC"
        self.news.set_tag_feed(TOPIC, [{"id": A1, "published": NOW - 60, "cluster": "Cstory", "channel": CH,
                                         "topics": [TOPIC, other]},
                                        {"id": A2, "published": NOW - 120}])
        self.news.set_tag_feed(other, [{"id": A2}], variant="")
        items = list(nc.read_tag_feeds(self.news.paths, [TOPIC]))
        self.assertEqual([(i.tag_id, i.article_id) for i in items], [(TOPIC, A1), (TOPIC, A2)])
        a = items[0]
        self.assertEqual(a.feed_id, TOPIC + "@B$en-US")
        self.assertAlmostEqual(a.published_at, NOW - 60, places=2)
        self.assertEqual((a.cluster_id, a.channel_id, a.topic_ids), ("Cstory", CH, [TOPIC, other]))
        self.assertEqual(len(list(nc.read_tag_feeds(self.news.paths))), 3, "all feeds when no tags are named")
        states = nc.read_feed_states(self.news.paths)
        self.assertEqual(set(states), {TOPIC + "@B$en-US", other + "$en-US"})
        self.assertEqual(nc.feed_tag(other + "#P@A$en-US"), other)

    def test_damaged_feed_item_is_skipped(self):
        self.news.feeds[TOPIC + "@B$en-US"] = ([{"id": A1}], 5)
        self.news.write_feeddb(damaged=True)
        self.assertEqual([i.article_id for i in nc.read_tag_feeds(self.news.paths)], [A1])
        self.assertIsNone(nc.parse_feed_item(b"\x01\x03abc"))
        self.assertEqual(list(nc.read_tag_feeds(nc.NewsPaths(container="/nonexistent"), [TOPIC])), [])

    def test_entitlement(self):
        self.assertFalse(nc.read_entitlement(self.news.paths).news_plus)
        self.news.write_entitlement(True)
        self.assertTrue(nc.read_entitlement(self.news.paths).news_plus)

    def test_asset_image_url(self):
        # base64url of 02 01 <len> <uuid> 00 "17"
        url = nc.asset_image_url("0f0f0f0f-1111-4222-8333-444444444444")
        self.assertEqual(url, "https://c.apple.news/AgEkMGYwZjBmMGYtMTExMS00MjIyLTgzMzMtNDQ0NDQ0NDQ0NDQ0ADE3")


class AnfTest(unittest.TestCase):
    def test_ads_and_promotions_are_removed(self):
        d = anf.convert(anf_document("A title"))
        types = [b["type"] for b in d.blocks]
        self.assertNotIn("link", types)
        self.assertEqual(d.removed.get("advertisement"), 2)
        self.assertGreaterEqual(d.removed.get("promotion"), 3)
        self.assertIn("ad_settings", d.removed)
        text = " ".join(b.get("text", "") for b in d.blocks)
        self.assertNotIn("newsletter", text)
        self.assertNotIn("Follow us", text)

    def test_promotions_kept_on_request(self):
        d = anf.convert(anf_document("A title"), keep_promotions=True)
        self.assertIn("link", [b["type"] for b in d.blocks])
        self.assertEqual(d.removed.get("advertisement"), 2, "ads go regardless")

    def test_structure(self):
        d = anf.convert(anf_document("A title"))
        first = d.blocks[0]
        self.assertEqual(first["type"], "image")
        self.assertEqual(first["caption"], "A caption.", "the caption below a photo joins it")
        self.assertTrue(first["url"].startswith("https://c.apple.news/AgEk"))
        self.assertEqual((first["width"], first["height"]), (1200, 800))
        self.assertIn({"type": "heading", "level": 2, "text": "A subhead"}, d.blocks)
        self.assertIn({"type": "quote", "text": "A quoted line."}, d.blocks)
        self.assertEqual(d.byline, "By A. Writer")
        self.assertEqual(d.title, "A title")

    def test_html_text_is_flattened(self):
        doc = {"components": [{"role": "body", "format": "html", "text": "<p>One <b>bold</b> &amp; more</p>"}]}
        self.assertEqual(anf.convert(doc).blocks, [{"type": "paragraph", "text": "One bold & more"}])

    def test_preview_is_the_opening(self):
        blocks = anf.convert(anf_document("A title", paragraphs=10)).blocks
        cut = anf.preview(blocks)
        self.assertLess(len(cut), len(blocks))
        self.assertLessEqual(sum(1 for b in cut if b["type"] == "paragraph"), 3)
        self.assertEqual(cut[0]["type"], "image", "the lead picture stays")



class VideoTest(unittest.TestCase):
    DOC = {"resources": {
        "VideoResourceRef-1": {"URL": "https://cdn.example/v/index.m3u8", "classification": "video", "type": "video"},
        "img": {"URL": "https://cdn.example/still.jpg", "imageIdentifier": "still1", "type": "image",
                "dimensions": {"width": 1600, "height": 900}}},
        "components": [
            {"role": "video", "resourceIdentifier": "VideoResourceRef-1", "stillImageIdentifier": "still1",
             "caption": "Storms on the coast", "conditional": [{"hidden": False}], "hidden": True},
            {"role": "video", "resourceIdentifier": "VideoResourceRef-1", "stillImageIdentifier": "still1",
             "caption": "Storms on the coast", "conditional": [{"hidden": False}], "hidden": True},
            {"role": "body", "text": "Text."},
            {"role": "video", "URL": "https://cdn.example/b.mp4", "aspectRatio": 1.7778},
            {"role": "video", "URL": "https://cdn.example/hidden.mp4", "hidden": True}]}

    def test_stream_poster_caption_once(self):
        videos = [b for b in anf.convert(self.DOC).blocks if b["type"] == "video"]
        self.assertEqual(len(videos), 2)
        self.assertEqual(videos[0], {"type": "video", "url": "https://cdn.example/v/index.m3u8",
                                     "poster": "https://cdn.example/still.jpg", "caption": "Storms on the coast",
                                     "aspect_ratio": 1.7778})
        self.assertEqual(videos[1]["url"], "https://cdn.example/b.mp4")
        self.assertIsNone(videos[1]["poster"])


class StoryStateTest(unittest.TestCase):
    def test_reads_menu(self):
        from apple_news_relay.actuator import StoryState
        st = StoryState.parse({"file": {"Unsave Story": [True, ""], "Save Story": [False, ""],
                                        "Suggest Fewer Stories Like This": [True, "\u2713"]},
                               "windows": ["Fed\u2019s  rate call looms - Reuters"]})
        self.assertEqual((st.saved, st.reaction), (True, "dislike"))
        self.assertTrue(st.shows(["Fed's rate call looms"]))
        self.assertFalse(st.shows(["Another headline entirely"]))
        self.assertFalse(st.shows(["Fed's rate"]), "a shorter headline is another story")
        self.assertIsNone(StoryState.parse({"file": {"Save Story": [False, ""]}, "windows": []}).saved)


class BylineTest(unittest.TestCase):
    def test_names_only(self):
        self.assertIsNone(anf.clean_byline("Published on November 23, 2026", "FOX Weather"))
        self.assertEqual(anf.clean_byline("Updated 9:41 AM by Jane Doe", "X"), "Jane Doe")
        self.assertEqual(anf.clean_byline("Sudiksha Kochi - 11/23/26 06:00 AM EST", "The Hill"), "Sudiksha Kochi")
        self.assertEqual(anf.clean_byline("By Al Roe | November 23, 2026 | Reuters", "Reuters"), "Al Roe")
        from apple_news_relay.anf import clean_byline
        self.assertEqual(clean_byline("by Dennis Romero | NBC NEWS", "NBC News"), "Dennis Romero")
        self.assertEqual(clean_byline("By Ana Ruiz and Bo Li", "The Daily Example"), "Ana Ruiz and Bo Li")
        self.assertEqual(clean_byline("Ana Ruiz, Reuters", "Reuters"), "Ana Ruiz")
        self.assertEqual(clean_byline("Jean-Paul Sartre - Le Monde", "Le Monde"), "Jean-Paul Sartre")
        self.assertEqual(clean_byline("Jean-Paul Sartre", "Paul"), "Jean-Paul Sartre")
        self.assertEqual(clean_byline("NBC News", "NBC News"), "NBC News")
        self.assertIsNone(clean_byline(None, "X"))

if __name__ == "__main__":
    unittest.main()
