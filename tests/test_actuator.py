import json
import re
import subprocess
import unittest

from apple_news_relay.actuator import ActuatorError, NewsApp


class ScriptRunner:
    """Stands in for subprocess.run: hands back queued outputs (dict → JSON on stdout, or an
    exception to raise, or a (returncode, stderr) pair) and records what was sent."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, args, timeout=None, **kw):
        self.calls.append((args, timeout))
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, tuple):
            return subprocess.CompletedProcess(args, out[0], "", out[1])
        return subprocess.CompletedProcess(args, 0, out if isinstance(out, str) else json.dumps(out), "")

    def sent(self, i):
        m = re.search(r"^const A = (.*);$", self.calls[i][0][-1], re.M)
        return json.loads(m.group(1)) if m else None


def app(*outputs):
    runner = ScriptRunner(*outputs)
    a = NewsApp(runner=runner)
    a.RETRY_WAIT = 0
    a.SIDEBAR_WAIT = 0
    return a, runner


PATH = [["groups", 0], ["scrollAreas", 0]]


class ScrollFeedTest(unittest.TestCase):
    def test_scan_remembers_the_scroll_area(self):
        a, r = app({"n": 3, "via": "scan", "path": PATH}, {"n": 2, "via": "cache", "path": PATH})
        self.assertEqual(a.scroll_feed(3), 3)
        self.assertIsNone(r.sent(0)["path"])
        self.assertEqual(a.scroll_feed(2), 2)
        self.assertEqual(r.sent(1)["path"], PATH)

    def test_a_miss_forgets_the_path_and_tries_once_more_with_a_scan(self):
        a, r = app({"n": 3, "via": "scan", "path": PATH}, {"n": 0, "via": "none"}, {"n": 0, "via": "none"})
        a.scroll_feed(3)
        self.assertEqual(a.scroll_feed(3), 0)
        self.assertEqual(r.sent(1)["path"], PATH)
        self.assertIsNone(r.sent(2)["path"])
        self.assertEqual(len(r.calls), 3)

    def test_a_card_is_not_remembered(self):
        a, r = app({"n": 2, "via": "card", "path": None}, {"n": 1, "via": "card", "path": None})
        self.assertEqual(a.scroll_feed(2), 2)
        a.scroll_feed(1)
        self.assertIsNone(r.sent(1)["path"])

    def test_a_timeout_gives_zero_pages_not_an_error(self):
        a, r = app({"n": 1, "via": "scan", "path": PATH}, subprocess.TimeoutExpired("osascript", 29.4))
        a.scroll_feed(1)
        self.assertEqual(a.scroll_feed(8), 0)
        self.assertAlmostEqual(r.calls[1][1], 15 + 8 * 1.8)
        self.assertIsNone(a._feed_path)

    def test_pages_are_capped(self):
        a, r = app({"n": 40, "via": "scan", "path": PATH})
        a.scroll_feed(99)
        self.assertEqual(r.sent(0)["pages"], 40)

    def test_output_that_isnt_json_is_a_miss(self):
        a, r = app("weird", "weird")
        self.assertEqual(a.scroll_feed(2), 0)
        self.assertEqual(len(r.calls), 2)

    def test_no_access_fails_at_once(self):
        a, r = app((1, "execution error: System Events got an error: osascript is not allowed assistive access. (-1719)"))
        with self.assertRaises(ActuatorError):
            a.scroll_feed(2)
        self.assertEqual(len(r.calls), 1)


TODAY = [["groups", 0], ["uiElements", 3]]


class ShowTodayTest(unittest.TestCase):
    def test_pressed_remembers_the_row(self):
        a, r = app({"pressed": True, "found": True, "via": "scan", "path": TODAY},
                   {"pressed": True, "found": True, "via": "cache", "path": TODAY})
        a.show_today()
        a.show_today()
        self.assertIsNone(r.sent(0)["path"])
        self.assertEqual(r.sent(1)["path"], TODAY)
        self.assertTrue(r.sent(0)["press"])

    def test_not_found_shows_the_sidebar_between_tries(self):
        miss = {"pressed": False, "found": False, "via": "none", "path": None}
        a, r = app(miss, "", miss, "", miss)
        with self.assertRaisesRegex(ActuatorError, "Today not found"):
            a.show_today()
        self.assertEqual(len(r.calls), 5)
        self.assertIn("Show Sidebar", r.calls[1][0][-1])

    def test_no_access_fails_after_one_try(self):
        a, r = app((1, "59:146: execution error: System Events got an error: osascript is not allowed assistive access. (-1719)"))
        with self.assertRaisesRegex(ActuatorError, "-1719"):
            a.show_today()
        self.assertEqual(len(r.calls), 1)

    def test_no_window_keeps_its_words(self):
        # JobRunner opens a window when the error says "no window".
        a, r = app((1, "Error: News has no window"), "", (1, "Error: News has no window"), "", (1, "Error: News has no window"))
        with self.assertRaisesRegex(ActuatorError, "no window"):
            a.show_today()


if __name__ == "__main__":
    unittest.main()
