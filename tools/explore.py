#!/usr/bin/env python3
"""Learn what makes Apple News load what, by driving it through a running relay.

Runs a slow, rotating schedule of relay jobs (refresh Today with different
scroll depths, open channels and topics, fetch free and paid stories, search)
plus idle baselines, and records each outcome as one JSON line: how long it
took, how many new articles and bodies the archive gained. Summarize with
``--report``. Polite by default: one action every few minutes.

    python3 tools/explore.py --hours 6 --log explore.jsonl
    python3 tools/explore.py --report explore.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

SEARCH_TERMS = ["climate", "election", "space", "court ruling", "earthquake", "artificial intelligence",
                "public health", "housing", "transit", "wildlife", "education", "energy", "museum", "drought"]


class Relay:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None, timeout: float = 120):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def stats(self) -> dict:
        return self.call("GET", "/v1/stats")[1]

    def ids(self, query: str, n: int = 50):
        status, body = self.call("GET", "/v1/articles?" + query + "&fields=id&limit=%d" % n)
        return [a["id"] for a in (body or {}).get("data", [])]


def snapshot(relay: Relay) -> dict:
    s = relay.stats()
    return {"articles": s["articles"], "bodies": s["articles_with_body"], "placements": s["placements"]}


def run_job(relay: Relay, method: str, path: str, body=None, wait: int = 60) -> dict:
    sep = "&" if "?" in path else "?"
    status, resp = relay.call(method, path + sep + "wait=%d" % wait, body, timeout=wait + 30)
    job = (resp or {}).get("data") or {}
    return {"http": status, "state": job.get("state"), "result": job.get("result"), "error": job.get("error")}


def pick_action(relay: Relay, rng: random.Random):
    roll = rng.random()
    if roll < 0.20:
        pages = rng.choice([0, 4, 8, 16])
        return "refresh_today", {"pages": pages}, lambda: run_job(relay, "POST", "/v1/today/refresh?pages=%d" % pages)
    if roll < 0.40:
        status, body = relay.call("GET", "/v1/%s?limit=60" % rng.choice(["channels", "topics"]))
        tags = [t for t in (body or {}).get("data", [])]
        if tags:
            tag = rng.choice(tags)
            coll = "channels" if tag["type"] == "channel" else "topics"
            return "open_tag", {"tag": tag["id"], "name": tag["name"], "type": tag["type"]}, \
                lambda: run_job(relay, "POST", "/v1/%s/%s/refresh" % (coll, tag["id"]))
    if roll < 0.65:
        ids = relay.ids("completeness=summary&access=free")
        if ids:
            aid = rng.choice(ids)
            return "fetch_free", {"id": aid}, lambda: run_job(relay, "POST", "/v1/articles/%s/fetch" % aid)
    if roll < 0.75:
        ids = relay.ids("completeness=summary&access=news_plus")
        if ids:
            aid = rng.choice(ids)
            return "fetch_paid", {"id": aid}, lambda: run_job(relay, "POST", "/v1/articles/%s/fetch" % aid)
    if roll < 0.88:
        term = rng.choice(SEARCH_TERMS)
        return "search", {"query": term}, lambda: run_job(relay, "POST", "/v1/searches", {"query": term})
    return "idle", {}, lambda: (time.sleep(60), {"state": "succeeded"})[1]


def explore(args) -> int:
    relay = Relay(args.relay)
    rng = random.Random(args.seed)
    deadline = time.time() + args.hours * 3600
    with open(args.log, "a") as log:
        while time.time() < deadline:
            before = snapshot(relay)
            kind, params, action = pick_action(relay, rng)
            started = time.time()
            try:
                outcome = action()
            except Exception as e:  # keep exploring
                outcome = {"state": "error", "error": str(e)[:200]}
            time.sleep(5)  # let late arrivals land
            after = snapshot(relay)
            record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": kind, "params": params,
                      "seconds": round(time.time() - started, 1), "outcome": outcome,
                      "gained": {k: after[k] - before[k] for k in after}}
            log.write(json.dumps(record) + "\n")
            log.flush()
            print(json.dumps(record), flush=True)
            time.sleep(max(10, args.gap + rng.uniform(-30, 30)))
    return 0


def report(path: str) -> int:
    rows = [json.loads(line) for line in open(path) if line.strip()]
    by = defaultdict(list)
    for r in rows:
        key = r["action"] + (" pages=%s" % r["params"]["pages"] if r["action"] == "refresh_today" else "")
        by[key].append(r)
    print("%-24s %5s %8s %9s %9s %8s" % ("action", "runs", "ok", "s median", "+articles", "+bodies"))
    for key in sorted(by):
        rs = by[key]
        ok = sum(1 for r in rs if (r["outcome"] or {}).get("state") == "succeeded")
        print("%-24s %5d %7d%% %9.1f %9.1f %8.1f" % (
            key, len(rs), round(100 * ok / len(rs)), statistics.median(r["seconds"] for r in rs),
            statistics.mean(r["gained"]["articles"] for r in rs), statistics.mean(r["gained"]["bodies"] for r in rs)))
    fetches = [r for r in rows if r["action"].startswith("fetch_")]
    if fetches:
        captured = [r for r in fetches if ((r["outcome"] or {}).get("result") or {}).get("captured")]
        print("\nfetch capture rate: %d/%d" % (len(captured), len(fetches)))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--relay", default="http://127.0.0.1:8787")
    p.add_argument("--hours", type=float, default=6)
    p.add_argument("--gap", type=float, default=200, help="average seconds between actions")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--log", default="explore.jsonl")
    p.add_argument("--report", metavar="LOG", help="summarize a log instead of exploring")
    args = p.parse_args()
    return report(args.report) if args.report else explore(args)


if __name__ == "__main__":
    sys.exit(main())
