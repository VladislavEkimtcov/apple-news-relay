import socket
import unittest

from apple_news_relay.__main__ import bind_when_free


class BindWhenFreeTest(unittest.TestCase):
    def test_waits_for_a_taken_port_then_serves(self):
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        waits = []

        def sleep(seconds):  # the other relay stops while this one waits
            waits.append(seconds)
            if len(waits) == 3:
                held.close()

        with self.assertLogs("apple_news_relay", "WARNING") as logs:
            server = bind_when_free(None, "127.0.0.1", port, sleep=sleep, holder=lambda p: "pid 1 (Python)")
        try:
            self.assertEqual(server.server_address[1], port)
            self.assertEqual(len(waits), 3)
            # Said once, not on every try.
            self.assertEqual(len(logs.output), 1)
            self.assertIn("port %d is taken by pid 1 (Python)" % port, logs.output[0])
        finally:
            server.server_close()
            held.close()

    def test_a_relay_on_all_addresses_counts_as_taken(self):
        # macOS would let 127.0.0.1:port bind beside 0.0.0.0:port; it must wait instead.
        held = socket.socket()
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        port = held.getsockname()[1]
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            held.close()

        with self.assertLogs("apple_news_relay", "WARNING"):
            server = bind_when_free(None, "127.0.0.1", port, sleep=sleep, holder=lambda p: "pid 1 (Python)")
        server.server_close()
        self.assertEqual(len(waits), 1)

    def test_other_errors_still_raise(self):
        with self.assertRaises(OSError):
            bind_when_free(None, "203.0.113.1", 8787, taken=lambda p: False, sleep=lambda s: self.fail("must not wait"))


if __name__ == "__main__":
    unittest.main()
