#!/usr/bin/env python3
"""Pin the unit of the Deco's up_speed/down_speed with a known download.

=== AI/AGENT USAGE METADATA ===
  purpose     : homelab#1931 calibration. Downloads a file at a FIXED rate
                (curl --limit-rate) on the host running this, then compares
                the true byte rate with what the deployed exporter reported
                for this host's IP over the same window. Re-run after a Deco
                firmware update, or if the numbers stop looking right.
  result      : 2026-11-05, firmware 1.3.0 Build 26090112: kbit/s (decimal),
                the Deco reading ~5-10% above curl's payload rate (it counts
                headers). Fixtures in tests/fixtures/calibration/.
  method      : - Fixed rate, because an unthrottled download's AVERAGE does
                  not match the Deco's instantaneous sample (the 2026-11-05
                  unthrottled runs gave ratios 2x apart).
                - Polls in the first SETTLE_S of continuous traffic are
                  dropped: the Deco's figure ramps over ~2 polls (~400 at
                  +64 s, about half at +108 s, full by +128 s).
                - Back-to-back runs are one transfer for that purpose.
  output      : per-poll table, `VERDICT <unit>|INCONSISTENT|UNKNOWN ...`,
                then `CALIBRATION-COMPLETE`.
  side_effects: downloads RUNS x the file (default 2 x 1 GiB, ~7 min at
                5 MiB/s) on this host's internet link. Read-only otherwise.
  requires    : curl; kubectl with access to the Prometheus service proxy;
                the host must be a Deco client (its IP in the client list).
=== END METADATA ===

usage:
  bin/calibrate-units.py run [--url URL] [--limit 5M] [--runs 2] [--ip IP] [--save F]
  bin/calibrate-units.py analyze FILE.json   # {"runs": [...], "polls": [...]}
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
import urllib.parse

SETTLE_S = 120      # drop polls this soon after continuous traffic starts
END_MARGIN_S = 5    # a poll must start at least this long before the run ends
SCRAPE_AFTER_S = 5  # the value scraped this long after a poll start is that poll's
TOLERANCE = 0.15    # accept the nearest unit within +-15%
MAX_SPREAD = 1.25   # steady ratios further apart than this: INCONSISTENT
# true bytes/s divided by the raw value, if the raw value were in <unit>
UNITS = {"bytes/s": 1, "kB/s": 1000, "KiB/s": 1024, "kbit/s": 125, "Mbit/s": 125000}
PROM = "/api/v1/namespaces/monitoring/services/prometheus-kube-prometheus-prometheus:9090/proxy/api/v1"
URL = "https://ash-speed.hetzner.com/1GB.bin"


def transfers(runs):
    """Merge back-to-back runs into continuous transfers: [(start, end, run)]."""
    out, cur = [], None
    for r in sorted(runs, key=lambda r: r["start"]):
        if cur is None or r["start"] - cur > END_MARGIN_S:
            first = r["start"]
        out.append((first, r))
        cur = r["end"]
    return out


def steady_polls(runs, polls):
    keep = []
    for p in polls:
        for first, r in transfers(runs):
            if r["start"] <= p["start"] <= r["end"] - END_MARGIN_S and p["start"] >= first + SETTLE_S:
                keep.append({**p, "bytes_per_s": r["bytes_per_s"],
                             "ratio": r["bytes_per_s"] / p["raw"] if p["raw"] else math.inf})
                break
    return keep


def verdict(ratios):
    lo, hi = min(ratios), max(ratios)
    if hi / lo > MAX_SPREAD:
        return "INCONSISTENT", f"steady ratios {lo:.1f}..{hi:.1f} disagree (> x{MAX_SPREAD}); use --limit"
    r = statistics.median(ratios)
    unit, f = min(UNITS.items(), key=lambda kv: abs(math.log(r / kv[1])))
    off = r / f
    if abs(off - 1) > TOLERANCE:
        return "UNKNOWN", f"ratio {r:.1f} is near no candidate unit (closest {unit}, x{off:.2f})"
    return unit, f"ratio {r:.1f}; the Deco reads x{1 / off:.3f} of the true rate if {unit}"


def analyze(runs, polls):
    steady = steady_polls(runs, polls)
    if not steady:
        raise ValueError(f"no steady poll: each transfer needs > {SETTLE_S} s of traffic "
                         f"plus at least one poll (~64 s apart) before it ends")
    ratios = [p["ratio"] for p in steady]
    v, why = verdict(ratios)
    r = statistics.median(ratios)
    return {"steady": steady, "verdict": v, "why": why,
            "off_by": r / UNITS[v] if v in UNITS else None}


def polls_from(starts_resp, down_resp):
    starts = sorted({float(v) for s in starts_resp["data"]["result"] for _, v in s["values"]})
    series = sorted((float(t), float(v)) for s in down_resp["data"]["result"] for t, v in s["values"])
    out = []
    for s in starts:
        after = [v for t, v in series if t >= s + SCRAPE_AFTER_S]
        if after:
            out.append({"start": s, "raw": after[0]})
    return out


def report(runs, polls):
    hhmm = lambda t: time.strftime("%H:%M:%S", time.gmtime(t))
    for i, r in enumerate(runs, 1):
        print(f"run {i}: {hhmm(r['start'])} -> {hhmm(r['end'])} UTC, "
              f"{r['bytes_per_s']:.0f} B/s ({r['bytes_per_s'] * 8 / 1e6:.2f} Mbit/s true)")
    try:
        res = analyze(runs, polls)
    except ValueError as e:
        print(f"VERDICT NONE {e}")
        return 1
    steady = {p["start"] for p in res["steady"]}
    print(f"\n{'poll (UTC)':>10}  {'raw':>9}  steady  ratio")
    for p in polls:
        s = next((x for x in res["steady"] if x["start"] == p["start"]), None)
        print(f"{hhmm(p['start']):>10}  {p['raw']:9.0f}  {'yes' if p['start'] in steady else '-':6}  "
              + (f"{s['ratio']:.1f}" if s else ""))
    print(f"\nVERDICT {res['verdict']} {res['why']}")
    print("CALIBRATION-COMPLETE")
    return 0 if res["verdict"] in UNITS else 1


def kube_range(query, start, end):
    q = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": 5})
    return json.loads(subprocess.check_output(["kubectl", "get", "--raw", f"{PROM}/query_range?{q}"]))


def run(a):
    ip = a.ip or subprocess.check_output(["ip", "-4", "-o", "route", "get", "1.1.1.1"], text=True).split(" src ")[1].split()[0]
    print(f"host ip={ip} url={a.url} limit={a.limit or 'none'} runs={a.runs}", flush=True)
    runs = []
    for _ in range(a.runs):
        t0 = time.time()
        cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{speed_download}", a.url]
        if a.limit:
            cmd[1:1] = ["--limit-rate", a.limit]
        bps = float(subprocess.check_output(cmd, text=True))
        runs.append({"start": t0, "end": time.time(), "bytes_per_s": bps})
    print("waiting 70 s for the last poll to be scraped...", flush=True)
    time.sleep(70)
    s, e = runs[0]["start"] - 70, runs[-1]["end"] + 70
    polls = polls_from(kube_range("max(deco_last_success_timestamp_seconds)", s, e),
                       kube_range(f'max(deco_client_down_speed_raw{{ip="{ip}"}})', s, e))
    if a.save:
        json.dump({"ip": ip, "runs": runs, "polls": polls}, open(a.save, "w"), indent=1)
    return report(runs, polls)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", default=URL)
    r.add_argument("--limit", default="5M", help="curl --limit-rate; '' for unthrottled (not recommended)")
    r.add_argument("--runs", type=int, default=2, help="back to back; 2 x 1 GiB at 5M gives ~4 steady polls")
    r.add_argument("--ip", help="this host's LAN IP (default: the source IP of the default route)")
    r.add_argument("--save", help="write runs+polls JSON here, for `analyze` or a new fixture")
    x = sub.add_parser("analyze")
    x.add_argument("file")
    a = p.parse_args()
    if a.cmd == "run":
        sys.exit(run(a))
    d = json.load(open(a.file))
    sys.exit(report(d["runs"], d["polls"]))


if __name__ == "__main__":
    main()
