"""Command line: ``python3 -m apple_news_relay <command>``.

    serve      run the API (and the harvester and job runner)
    harvest    copy the News cache into the database once and print a report
    doctor     check that everything the relay needs is reachable
    openapi    print the OpenAPI description
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time

from . import __version__
from .actuator import JobRunner, NewsApp
from .api import EventHub, RelayApp, serve
from .handoff import Handoff
from .harvester import Harvester
from .newscache import NewsPaths, read_entitlement
from .store import Store, default_path


def build(args) -> RelayApp:
    store = Store(args.db, section_window=getattr(args, "section_window_hours", 48.0) * 3600)
    events = EventHub()
    paths = NewsPaths()
    harvester = Harvester(store, paths, on_event=events.publish)
    jobs = JobRunner(store, harvester, NewsApp(), on_event=events.publish,
                     warm_gap=getattr(args, "warm_gap", 0.6),
                     pause_warm_in_front=getattr(args, "pause_warm_when_news_in_front", False))
    allow = [a for a in (args.allow or "").split(",") if a]
    handoff = None if getattr(args, "no_handoff", True) else Handoff(store, jobs.app, jobs, on_event=events.publish)
    return RelayApp(store, harvester, jobs, paths, events, token=args.token or os.environ.get("NEWS_RELAY_TOKEN"),
                    allow=allow, handoff=handoff)


def port_holder(port: int) -> str:
    """Who listens on the port, as "pid 123 (Python)", for the log; "another program" if lsof can't tell."""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fpc"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "another program"
    pid = next((l[1:] for l in out.splitlines() if l.startswith("p")), None)
    cmd = next((l[1:] for l in out.splitlines() if l.startswith("c")), None)
    return "pid %s (%s)" % (pid, cmd) if pid else "another program"


def answering(port: int) -> bool:
    """Whether something already listens on the port here. Binding alone can't tell: with
    SO_REUSEADDR (HTTPServer sets it) macOS lets 127.0.0.1:8787 bind beside a relay on 0.0.0.0:8787."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def bind_when_free(app, host: str, port: int, wait: float = 5.0, sleep=time.sleep, holder=port_holder,
                   note_every: float = 600.0, taken=answering):
    """The server, once the port is free. A relay already serving (say one started in a terminal)
    keeps it: this one waits quietly, says so in the log now and every ten minutes, and takes over
    when that one stops, so launchd's KeepAlive never sees a crash to restart."""
    log = logging.getLogger("apple_news_relay")
    noted = None
    while True:
        try:
            if not taken(port):
                return serve(app, host, port)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
        now = time.monotonic()
        if noted is None or now - noted >= note_every:
            log.warning("port %d is taken by %s; waiting for it to stop, then serving", port, holder(port))
            noted = now
        sleep(wait)


def cmd_serve(args) -> int:
    app = build(args)
    # SIGTERM (launchctl, kill) stops it cleanly, also while it waits for the port.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # Bind before harvesting or running jobs: a second relay must not drive News while it waits.
    try:
        server = bind_when_free(app, args.host, args.port)
    except KeyboardInterrupt:
        return 0
    app.harvester.run_once(force=True)
    app.harvester.start(args.interval)
    app.jobs.start()
    if app.handoff:
        app.handoff.start()
    keep_awake = None
    if args.keep_awake:
        keep_awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    bonjour = None
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.no_bonjour:
        # Lets clients on the LAN find the relay without configuration (_newsrelay._tcp).
        try:
            bonjour = subprocess.Popen(["dns-sd", "-R", "Apple News Relay", "_newsrelay._tcp", "local", str(args.port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            bonjour = None
    logging.getLogger("apple_news_relay").info("listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.harvester.stop()
        for proc in (keep_awake, bonjour):
            if proc:
                proc.terminate()
    return 0


def cmd_harvest(args) -> int:
    store = Store(args.db)
    report = Harvester(store).run_once(force=True)
    print(json.dumps(report.to_dict(), indent=2))
    return 1 if report.errors else 0


def cmd_doctor(args) -> int:
    paths = NewsPaths()
    checks = []

    def check(name, ok, detail=""):
        checks.append(ok)
        print("%s %-34s %s" % ("ok  " if ok else "FAIL", name, detail))

    check("News container readable", os.access(paths.container, os.R_OK), paths.container)
    check("article cache present", os.path.exists(paths.record_source("article-record-source")),
          paths.record_source("article-record-source"))
    check("Today feed database present", os.path.exists(paths.today_feed_db))
    check("asset store readable", os.access(paths.assets, os.R_OK))
    app = NewsApp()
    check("News.app running", app.is_running(), "(the relay launches it when needed)")
    try:
        front = app.frontmost()
        check("Accessibility / System Events", front is not None, "frontmost app: %s" % front)
    except Exception as e:  # noqa: BLE001
        check("Accessibility / System Events", False, str(e))
    ent = read_entitlement(paths)
    check("Apple News+ entitlement read", True, ent.detail)
    check("database writable", os.access(os.path.dirname(args.db) or ".", os.W_OK) or not os.path.exists(os.path.dirname(args.db)), args.db)
    return 0 if all(checks[:4]) else 1


def cmd_openapi(args) -> int:
    app = RelayApp.__new__(RelayApp)
    from .api import Router
    from .openapi import spec
    app.router = Router()
    RelayApp._routes(app)
    print(json.dumps(spec(app.router.routes), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="apple-news-relay", description="A local JSON API over Apple News on a Mac.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default=default_path(), help="database file (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="run the API server")
    p.add_argument("--host", default="127.0.0.1", help="address to bind (0.0.0.0 for the LAN)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between harvest passes")
    p.add_argument("--token", help="require this bearer token (or set NEWS_RELAY_TOKEN)")
    p.add_argument("--allow", help="comma-separated client addresses or networks, e.g. 127.0.0.1,192.168.1.0/24")
    p.add_argument("--keep-awake", action="store_true", help="keep the Mac from idle-sleeping while serving")
    p.add_argument("--no-bonjour", action="store_true", help="don't advertise _newsrelay._tcp on the LAN")
    p.add_argument("--no-handoff", action="store_true",
                   help="don't watch which story News shows on this Mac (GET /v1/handoff stays empty)")
    p.add_argument("--warm-gap", type=float, default=0.6,
                   help="seconds between background story fetches that clients asked for ahead (default 0.6)")
    p.add_argument("--pause-warm-when-news-in-front", action="store_true",
                   help="hold background story fetches while News is the app in front on this Mac")
    p.add_argument("--section-window-hours", type=float, default=48.0,
                   help="how far back a section reaches into its topic's or publisher's feed (default 48)")
    p.set_defaults(func=cmd_serve)
    sub.add_parser("harvest", help="harvest once and print a report").set_defaults(func=cmd_harvest)
    sub.add_parser("doctor", help="check access to News and its cache").set_defaults(func=cmd_doctor)
    sub.add_parser("openapi", help="print the OpenAPI description").set_defaults(func=cmd_openapi)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
