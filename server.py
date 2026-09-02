#!/usr/bin/env python3
"""
Local web app for Ontario Parks roofed-accommodation availability.

Serves a Leaflet map of every park with roofed accommodation, coloured by how
much availability matches your filters, plus a searchable result list with
deep links straight into the booking site.

  python server.py                 # http://127.0.0.1:8765
  python server.py --port 9000 --no-browser

Reads cache/inventory.json, cache/availability.json and cache/geo.json.
Build those first:

  python op_roofed.py refresh
  python geocode.py
  python op_roofed.py scan --days 210
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import op_roofed

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
CACHE = os.path.join(HERE, "cache")

# Guards the cached data while a rescan swaps it out underneath readers.
_lock = threading.Lock()
_state = {"inv": None, "scan": None, "geo": None, "scanning": False}


def load_all(reload_scan=True):
    with _lock:
        if _state["inv"] is None:
            _state["inv"] = op_roofed.load_inventory()
        if _state["geo"] is None:
            path = os.path.join(CACHE, "geo.json")
            if os.path.exists(path):
                with io.open(path, encoding="utf-8") as fh:
                    _state["geo"] = json.load(fh)
            else:
                _state["geo"] = {}
        if reload_scan and _state["scan"] is None:
            _state["scan"] = op_roofed.load_scan()
        return _state["inv"], _state["scan"], _state["geo"]


def parse_filters(qs):
    """Turn query-string params into arguments for op_roofed.find_stays."""
    def one(key, default=None):
        v = qs.get(key, [default])[0]
        return v if v not in ("", None) else default

    types = one("types")
    cats = None
    if types:
        cats = set()
        for t in types.split(","):
            t = t.strip().lower()
            if t in op_roofed.TYPE_ALIASES:
                cats.add(op_roofed.TYPE_ALIASES[t])
        cats = cats or None

    cap = one("capacity")
    return {
        "nights": int(one("nights", 2)),
        "want_cats": cats,
        "park_filter": one("park"),
        "weekends_only": one("weekends") in ("1", "true", "yes"),
        "min_capacity": int(cap) if cap else None,
        "loose": one("loose") in ("1", "true", "yes"),
        "date_from": one("start"),
        "date_to": one("end"),
    }


def search_payload(qs):
    inv, scan, geo = load_all()
    filters = parse_filters(qs)
    rejected = {}
    stays = op_roofed.find_stays(inv, scan, rejected=rejected, **filters)

    # aggregate per park for the map
    by_park = {}
    for s in stays:
        b = by_park.setdefault(s["park"], {
            "name": s["park"], "total": 0, "byType": {},
            "units": set(), "earliest": None,
        })
        b["total"] += 1
        b["byType"][s["type"]] = b["byType"].get(s["type"], 0) + 1
        b["units"].add(s["unit"])
        if b["earliest"] is None or s["arrive"] < b["earliest"]:
            b["earliest"] = s["arrive"]

    parks = []
    needle = (filters.get("park_filter") or "").lower()
    for pid, park in inv.items():
        name = park["name"]
        # the park-name box should narrow the map and list too, not just stays
        if needle and needle not in (name or "").lower():
            continue
        g = geo.get(pid) or {}
        agg = by_park.get(name)
        rej = rejected.get(name)
        parks.append({
            "id": pid,
            "name": name,
            "lat": g.get("lat"),
            "lon": g.get("lon"),
            "precision": g.get("precision"),
            "unitCount": len(park["units"]),
            "total": agg["total"] if agg else 0,
            "byType": agg["byType"] if agg else {},
            "matchedUnits": len(agg["units"]) if agg else 0,
            "earliest": agg["earliest"] if agg else None,
            # free nights that exist but are too short to book at this length
            "blockedMinNights": rej["minNights"] if rej else None,
        })
    parks.sort(key=lambda p: (-p["total"], p["name"]))

    return {
        "scannedAt": scan["scannedAt"],
        "start": scan["start"],
        "end": scan["end"],
        "total": len(stays),
        "parks": parks,
        "blocked": [{"park": k, "minNights": v["minNights"]}
                    for k, v in sorted(rejected.items(),
                                       key=lambda kv: -kv[1]["count"])],
    }


def park_payload(qs):
    """Full stay list + a day-by-day grid for one park."""
    inv, scan, _geo = load_all()
    pid = qs.get("id", [None])[0]
    if not pid or pid not in inv:
        return {"error": "unknown park id"}

    park = inv[pid]
    filters = parse_filters(qs)
    filters["park_filter"] = None  # we scope by id instead
    stays = [s for s in op_roofed.find_stays(inv, scan, **filters)
             if s["park"] == park["name"]]
    for s in stays:
        s["url"] = op_roofed.booking_url(s)

    # per-unit daily status strip, for the availability calendar
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
        # Minimum stay can vary by season, so report the span across the
        # scanned window rather than a single number.
        sched = (park.get("schedules") or {}).get(str(u.get("dateScheduleId")))
        mins = {op_roofed.stay_rules(sched, start + dt.timedelta(days=i))[0]
                for i in range(0, len(series), 7)} or {1}
        units.append({
            "id": rid,
            "name": u["name"],
            "type": u["categoryName"],
            "capacity": u["maxCapacity"],
            "minStay": min(mins),
            "maxMinStay": max(mins),
            "series": series,
        })
    units.sort(key=lambda u: (u["type"], u["name"] or ""))

    return {
        "id": pid,
        "name": park["name"],
        "gridStart": start.isoformat(),
        "units": units,
        "stays": stays[:500],
        "stayTotal": len(stays),
    }


def do_scan(days):
    """Re-pull availability. Blocks for a few seconds."""
    with _lock:
        if _state["scanning"]:
            return {"error": "a scan is already running"}
        _state["scanning"] = True
    try:
        ns = argparse.Namespace(park=None, start=None, end=None,
                                days=days, workers=4)
        scan = op_roofed.cmd_scan(ns, write=True)
        with _lock:
            _state["scan"] = scan
        return {"ok": True, "scannedAt": scan["scannedAt"],
                "start": scan["start"], "end": scan["end"]}
    finally:
        with _lock:
            _state["scanning"] = False


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # quieter console
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *a)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/search":
                return self._send(200, search_payload(qs))
            if path == "/api/park":
                return self._send(200, park_payload(qs))
            if path == "/api/types":
                return self._send(200, sorted(
                    set(op_roofed.ROOFED_CATEGORIES.values())))

            rel = "index.html" if path == "/" else path.lstrip("/")
            full = os.path.normpath(os.path.join(WEB, rel))
            if not full.startswith(WEB) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = CONTENT_TYPES.get(os.path.splitext(full)[1],
                                      "application/octet-stream")
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), ctype)
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": str(exc)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/scan":
            days = int(qs.get("days", ["210"])[0])
            try:
                return self._send(200, do_scan(days))
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    inv, scan, geo = load_all()
    located = sum(1 for p in inv if p in geo)
    print(f"Inventory : {len(inv)} parks, "
          f"{sum(len(p['units']) for p in inv.values())} roofed units")
    print(f"Geocoded  : {located}/{len(inv)}")
    print(f"Scan      : {scan['start']} .. {scan['end']} "
          f"(taken {scan['scannedAt']})")

    url = f"http://{args.host}:{args.port}/"
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\nServing {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
