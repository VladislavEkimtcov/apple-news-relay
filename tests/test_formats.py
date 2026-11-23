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


if __name__ == "__main__":
    unittest.main()
