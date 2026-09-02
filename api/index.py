"""Vercel serverless entry point for the roofed-accommodation finder.

Serverless changes two things about how this app gets its data:

* The filesystem is read-only and per-instance, so there is no shared
  cache/availability.json to read. Availability is fetched live from Ontario
  Parks and held in a module-level cache that survives warm invocations.
* Responses carry CDN cache headers, so repeated requests are served from
  Vercel's edge rather than re-scanning. That keeps our polling of Ontario
  Parks roughly constant no matter how much traffic the page gets.

Park inventory and coordinates are static and ship with the deployment.

All filtering stays in Python on purpose. The minimum-stay rules are subtle
(see the README), and reimplementing them in JavaScript for the browser would
mean maintaining the same tricky logic twice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

# op_roofed.py lives at the repo root; vercel.json bundles it via includeFiles.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import op_roofed  # noqa: E402

# How long a scan is reused before we go back to Ontario Parks.
SCAN_TTL = int(os.environ.get("SCAN_TTL_SECONDS", "900"))
SCAN_DAYS = int(os.environ.get("SCAN_DAYS", "210"))
# Edge cache: serve instantly, refresh in the background.
CDN_CACHE = f"public, s-maxage={SCAN_TTL}, stale-while-revalidate=1800"

_lock = threading.Lock()
_cache = {"inv": None, "geo": None, "scan": None, "at": 0.0}


def _find(*rel):
    """Locate a bundled data file, tolerating a few plausible layouts."""
    for base in (ROOT, os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        p = os.path.join(base, *rel)
        if os.path.isfile(p):
            return p
    return None


def load_static():
    if _cache["inv"] is None:
        p = _find("cache", "inventory.json")
        if not p:
            raise RuntimeError(
                "cache/inventory.json missing from the deployment. It is "
                "committed to the repo and bundled via includeFiles in "
                "vercel.json; check that config if this fires.")
        with io.open(p, encoding="utf-8") as fh:
            _cache["inv"] = json.load(fh)
    if _cache["geo"] is None:
        p = _find("cache", "geo.json")
        with io.open(p, encoding="utf-8") as fh:
            _cache["geo"] = json.load(fh) if p else {}
    return _cache["inv"], _cache["geo"]


def get_scan(force=False):
    """Live availability, reused for SCAN_TTL seconds within a warm instance."""
    with _lock:
        fresh = _cache["scan"] and (time.time() - _cache["at"]) < SCAN_TTL
        if fresh and not force:
            return _cache["scan"]

    load_static()
    ns = argparse.Namespace(park=None, start=None, end=None,
                            days=SCAN_DAYS, workers=6)
    # quiet: 52 progress lines per scan would flood the function logs
    scan = op_roofed.cmd_scan(ns, write=False, quiet=True)

    with _lock:
        _cache["scan"] = scan
        _cache["at"] = time.time()
    return scan


# ---------------------------------------------------------------------------
# payloads (same shapes the local server.py returns)
# ---------------------------------------------------------------------------

def parse_filters(qs):
    def one(key, default=None):
        v = qs.get(key, [default])[0]
        return v if v not in ("", None) else default

    types = one("types")
    cats = None
    if types:
        cats = {op_roofed.TYPE_ALIASES[t.strip().lower()]
                for t in types.split(",")
                if t.strip().lower() in op_roofed.TYPE_ALIASES} or None

    cap = one("capacity")
    return {
        "nights": max(1, min(30, int(one("nights", 2)))),
        "want_cats": cats,
        "park_filter": one("park"),
        "weekends_only": one("weekends") in ("1", "true", "yes"),
        "min_capacity": int(cap) if cap else None,
        "loose": one("loose") in ("1", "true", "yes"),
        "date_from": one("start"),
        "date_to": one("end"),
    }


def search_payload(qs):
    inv, geo = load_static()
    scan = get_scan()
    filters = parse_filters(qs)
    rejected = {}
    stays = op_roofed.find_stays(inv, scan, rejected=rejected, **filters)

    by_park = {}
    for s in stays:
        b = by_park.setdefault(s["park"], {
            "total": 0, "byType": {}, "units": set(), "earliest": None})
        b["total"] += 1
        b["byType"][s["type"]] = b["byType"].get(s["type"], 0) + 1
        b["units"].add(s["unit"])
        if b["earliest"] is None or s["arrive"] < b["earliest"]:
            b["earliest"] = s["arrive"]

    needle = (filters.get("park_filter") or "").lower()
    parks = []
    for pid, park in inv.items():
        name = park["name"]
        if needle and needle not in (name or "").lower():
            continue
        g = geo.get(pid) or {}
        agg = by_park.get(name)
        rej = rejected.get(name)
        parks.append({
            "id": pid, "name": name,
            "lat": g.get("lat"), "lon": g.get("lon"),
            "precision": g.get("precision"),
            "unitCount": len(park["units"]),
            "total": agg["total"] if agg else 0,
            "byType": agg["byType"] if agg else {},
            "matchedUnits": len(agg["units"]) if agg else 0,
            "earliest": agg["earliest"] if agg else None,
            "blockedMinNights": rej["minNights"] if rej else None,
        })
    parks.sort(key=lambda p: (-p["total"], p["name"]))

    return {
        "scannedAt": scan["scannedAt"], "start": scan["start"],
        "end": scan["end"], "total": len(stays), "parks": parks,
        "blocked": [{"park": k, "minNights": v["minNights"]}
                    for k, v in sorted(rejected.items(),
                                       key=lambda kv: -kv[1]["count"])],
    }


def park_payload(qs):
    inv, _geo = load_static()
    scan = get_scan()
    pid = qs.get("id", [None])[0]
    if not pid or pid not in inv:
        return {"error": "unknown park id"}

    park = inv[pid]
    filters = parse_filters(qs)
    filters["park_filter"] = None
    stays = [s for s in op_roofed.find_stays(inv, scan, **filters)
             if s["park"] == park["name"]]
    for s in stays:
        s["url"] = op_roofed.booking_url(s)

    start = dt.date.fromisoformat(scan["start"])
    pdata = scan["parks"].get(pid, {"units": {}})
    units = []
    for rid, series in pdata["units"].items():
        u = park["units"].get(rid)
        if not u:
            continue
        if filters["want_cats"] and u["category"] not in filters["want_cats"]:
            continue
        if filters["min_capacity"] and (u["maxCapacity"] or 0) < filters["min_capacity"]:
            continue
        sched = (park.get("schedules") or {}).get(str(u.get("dateScheduleId")))
        mins = {op_roofed.stay_rules(sched, start + dt.timedelta(days=i))[0]
                for i in range(0, len(series), 7)} or {1}
        units.append({
            "id": rid, "name": u["name"], "type": u["categoryName"],
            "capacity": u["maxCapacity"],
            "minStay": min(mins), "maxMinStay": max(mins),
            "series": series,
        })
    units.sort(key=lambda u: (u["type"], u["name"] or ""))

    return {"id": pid, "name": park["name"],
            "gridStart": start.isoformat(), "units": units,
            "stays": stays[:500], "stayTotal": len(stays)}


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------

def route_of(path, qs):
    """Work out the action under any of Vercel's rewrite shapes."""
    action = (qs.get("action") or [""])[0].strip().lower()
    if action:
        return action
    seg = [s for s in urllib.parse.urlparse(path).path.split("/") if s]
    last = seg[-1].lower() if seg else ""
    return "search" if last in ("", "api", "index") else last


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, cache=False):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", CDN_CACHE if cache else "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        action = route_of(self.path, qs)
        try:
            if action == "search":
                return self._send(200, search_payload(qs), cache=True)
            if action == "park":
                return self._send(200, park_payload(qs), cache=True)
            if action == "types":
                return self._send(200,
                                  sorted(set(op_roofed.ROOFED_CATEGORIES.values())),
                                  cache=True)
            if action in ("scan", "cron"):
                # `scan` is the Rescan button, `cron` is the scheduled warm-up.
                scan = get_scan(force=(action == "scan"))
                return self._send(200, {"ok": True,
                                        "scannedAt": scan["scannedAt"],
                                        "start": scan["start"],
                                        "end": scan["end"]})
            return self._send(404, {"error": f"unknown action '{action}'"})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": str(exc)})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def log_message(self, fmt, *a):
        pass
