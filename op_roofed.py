#!/usr/bin/env python3
"""
Ontario Parks roofed-accommodation availability search engine.

Talks to the public JSON API behind https://reservations.ontarioparks.ca
(the Camis / "GoingToCamp" platform). No auth, no API key, no browser needed.

Commands
--------
  refresh   Rebuild the park/unit inventory (slow, run rarely)
  scan      Pull day-by-day availability for every roofed unit (fast)
  search    Query the last scan for stays matching your criteria
  watch     Re-scan on an interval and alert on newly-freed stays

Typical use
-----------
  python op_roofed.py refresh
  python op_roofed.py scan --to 2027-03-31
  python op_roofed.py search --nights 2 --weekends --type cabin
  python op_roofed.py watch --nights 2 --weekends --park Pinery --interval 900
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import datetime as dt
import gzip
import io
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://reservations.ontarioparks.ca"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)

# bookingCategoryId for "Roofed Accommodation" (from /api/bookingcategories)
ROOFED_BOOKING_CATEGORY = 2

# resourceCategoryId -> human label (from /api/resourcecategory)
ROOFED_CATEGORIES = {
    -2147483646: "Soft-sided Shelter",   # yurts, canvas cabins
    -2147483645: "Rustic Cabin",
    -2147483644: "Cottage",
    -2147483633: "Trailer Equipped",     # ready-to-camp trailers
}

# Short aliases accepted by --type
TYPE_ALIASES = {
    "yurt": -2147483646,
    "shelter": -2147483646,
    "soft": -2147483646,
    "cabin": -2147483645,
    "rustic": -2147483645,
    "cottage": -2147483644,
    "trailer": -2147483633,
    "rtc": -2147483633,
}

# availability enum, lifted verbatim from the site's JS bundle
AVAIL = {
    0: "Available",
    1: "Unavailable",
    2: "NotOperating",
    3: "NonReservable",
    4: "Closed",
    5: "Invalid",
    6: "InvalidBookingCategory",
    7: "PartiallyAvailable",
    8: "Held",
}
BOOKABLE = {0}          # only a clean "Available" counts as a bookable night
SOFT_BOOKABLE = {0, 7}  # include PartiallyAvailable when --loose is passed


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def get_json(path, params=None, retries=4, timeout=90):
    """GET a JSON endpoint with retry/backoff. Returns parsed JSON."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-CA,en;q=0.9",
                "Accept-Encoding": "gzip",
                "Referer": BASE + "/",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            last = exc
            if attempt < retries - 1:
                # exponential backoff with jitter; be a polite guest
                time.sleep((2 ** attempt) + random.random())
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last}")


def en(localized, key="name"):
    """Pull the en-CA value out of a localizedValues list."""
    for item in localized or []:
        if item.get("cultureName") == "en-CA":
            return item.get(key)
    return None


# ----------------------------------------------------------------------------
# Stay rules
#
# /api/availability/map reports per-night OCCUPANCY, not bookability. Whether a
# given stay can actually be reserved also depends on the resource's date
# schedule: minimum and maximum stay length (with seasonal overrides) and, in
# a few cases, which weekdays you may arrive on. The Angular client applies
# these on top of the availability call, which is why a run of free nights can
# still come back as "No Available Sites" on the real site.
#
# Verified case: Balsam Lake RA1 (Cottage) has a minStayOverride of 6 nights
# for 2026-05-09..2026-10-23, so a 2-night stay on free nights is rejected
# while the same arrival for 6 nights is offered.
# ----------------------------------------------------------------------------

def _in_range(rng, day):
    """Is `day` (a date) inside an API range dict? Dates only, inclusive."""
    if not rng:
        return False
    start = (rng.get("start") or "")[:10]
    end = (rng.get("end") or "")[:10]
    if not start or not end:
        return False
    return start <= day.isoformat() <= end


def trim_schedule(s):
    """Keep only the fields the stay-rule check needs."""
    return {
        "minimumStayDays": s.get("minimumStayDays"),
        "maximumStayDays": s.get("maximumStayDays"),
        "minStayOverrides": s.get("minStayOverrides") or [],
        "maxStayOverrides": s.get("maxStayOverrides") or [],
        "allowedArrivalDepartureDays": s.get("allowedArrivalDepartureDays") or [],
    }


def stay_rules(sched, arrive):
    """Effective (min_nights, max_nights, allowed_weekdays) for an arrival.

    allowed_weekdays is None when unrestricted, else a set using the API's
    Sunday=0 convention.
    """
    if not sched:
        return 1, None, None

    lo = sched.get("minimumStayDays") or 1
    hi = sched.get("maximumStayDays")

    for ov in sched.get("minStayOverrides") or []:
        if _in_range(ov.get("range"), arrive) and ov.get("stayDurationLimitDays"):
            lo = max(lo, ov["stayDurationLimitDays"])
    for ov in sched.get("maxStayOverrides") or []:
        if _in_range(ov.get("range"), arrive) and ov.get("stayDurationLimitDays"):
            n = ov["stayDurationLimitDays"]
            hi = n if hi is None else min(hi, n)

    days = None
    for rule in sched.get("allowedArrivalDepartureDays") or []:
        if _in_range(rule.get("range"), arrive) and rule.get("daysOfWeek"):
            days = set(rule["daysOfWeek"]) if days is None \
                else days & set(rule["daysOfWeek"])
    return lo, hi, days


def sunday0(day):
    """Python's Monday=0 weekday -> the API's Sunday=0 convention."""
    return (day.weekday() + 1) % 7


def stay_allowed(sched, arrive, nights):
    """True if a stay of `nights` arriving on `arrive` satisfies the schedule."""
    lo, hi, days = stay_rules(sched, arrive)
    if nights < lo:
        return False
    if hi is not None and nights > hi:
        return False
    if days is not None and sunday0(arrive) not in days:
        return False
    return True


# ----------------------------------------------------------------------------
# refresh: build the inventory of roofed units
# ----------------------------------------------------------------------------

def cmd_refresh(args):
    os.makedirs(CACHE, exist_ok=True)
    print("Fetching park list ...")
    parks = get_json("/api/resourceLocation")

    roofed_cat_ids = set(ROOFED_CATEGORIES)
    candidates = []
    for p in parks:
        cats = set(p.get("resourceCategoryIds") or [])
        if cats & roofed_cat_ids:
            candidates.append(p)

    print(f"{len(candidates)} of {len(parks)} parks list roofed accommodation.")
    print("Fetching unit inventory per park (this is the slow part) ...")

    inventory = {}

    def fetch_park(p):
        rid = p["resourceLocationId"]
        res = get_json("/api/resourcelocation/resources",
                       {"resourceLocationId": rid})
        # Date schedules carry the min/max stay and arrival-day rules that
        # decide whether a run of free nights is actually bookable.
        try:
            raw_scheds = get_json("/api/dateschedule/resourcelocationid",
                                  {"resourceLocationId": rid})
        except Exception:  # noqa: BLE001 - a park without schedules still works
            raw_scheds = {}

        units = {}
        map_ids = set()
        needed = set()
        for _, r in res.items():
            cat = r.get("resourceCategoryId")
            if cat not in roofed_cat_ids:
                continue
            maps = r.get("mapIds") or []
            map_ids.update(maps)
            sid = r.get("dateScheduleId")
            if sid is not None:
                needed.add(str(sid))
            units[str(r["resourceId"])] = {
                "name": en(r.get("localizedValues")),
                "category": cat,
                "categoryName": ROOFED_CATEGORIES[cat],
                "maxCapacity": r.get("maxCapacity"),
                "mapIds": maps,
                "dateScheduleId": sid,
            }

        scheds = {}
        for key, s in (raw_scheds or {}).items():
            sid = str(s.get("scheduleId", key))
            if sid in needed:
                scheds[sid] = trim_schedule(s)
        return p, units, sorted(map_ids), scheds

    done = 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for p, units, map_ids, scheds in pool.map(fetch_park, candidates):
            done += 1
            name = en(p.get("localizedValues"), "fullName")
            if not units:
                print(f"  [{done}/{len(candidates)}] {name}: no roofed units, skipping")
                continue
            inventory[str(p["resourceLocationId"])] = {
                "name": name,
                "resourceLocationId": p["resourceLocationId"],
                "rootMapId": p.get("rootMapId"),
                "mapIds": map_ids,
                "gps": p.get("gpsCoordinates"),
                "units": units,
                "schedules": scheds,
            }
            mins = {s.get("minimumStayDays") or 1 for s in scheds.values()}
            note = f", min stay {sorted(mins)}" if mins - {1} else ""
            print(f"  [{done}/{len(candidates)}] {name}: "
                  f"{len(units)} units on {len(map_ids)} map(s){note}")

    path = os.path.join(CACHE, "inventory.json")
    with io.open(path, "w", encoding="utf-8") as fh:
        json.dump(inventory, fh, ensure_ascii=False, indent=1)

    total = sum(len(p["units"]) for p in inventory.values())
    print(f"\nWrote {path}")
    print(f"{len(inventory)} parks, {total} roofed units.")


def load_inventory():
    path = os.path.join(CACHE, "inventory.json")
    if not os.path.exists(path):
        sys.exit("No inventory yet. Run:  python op_roofed.py refresh")
    with io.open(path, encoding="utf-8") as fh:
        inv = json.load(fh)
    if inv and not any("schedules" in p for p in inv.values()):
        print("WARNING: inventory predates stay-rule support, so minimum-stay\n"
              "         rules cannot be applied and some results will not be\n"
              "         bookable. Re-run:  python op_roofed.py refresh\n")
    return inv


# ----------------------------------------------------------------------------
# scan: pull day-by-day availability
# ----------------------------------------------------------------------------

def cmd_scan(args, write=True, quiet=False):
    say = (lambda *a: None) if quiet else print
    inv = load_inventory()
    start = args.start or dt.date.today().isoformat()
    end = args.end or (dt.date.fromisoformat(start)
                       + dt.timedelta(days=args.days)).isoformat()

    # One request per (park, map) covering the whole horizon.
    jobs = []
    for pid, park in inv.items():
        if args.park and args.park.lower() not in (park["name"] or "").lower():
            continue
        for map_id in park["mapIds"]:
            jobs.append((pid, park["name"], map_id))

    if not jobs:
        sys.exit("No maps matched. Check --park.")

    say(f"Scanning {len(jobs)} map(s) across {start} .. {end}")

    def fetch(job):
        pid, pname, map_id = job
        params = {
            "mapId": map_id,
            "bookingCategoryId": ROOFED_BOOKING_CATEGORY,
            "equipmentCategoryId": -32768,
            "subEquipmentCategoryId": -32768,
            "cartUid": "", "cartTransactionUid": "",
            "bookingUid": "", "groupHoldUid": "",
            "startDate": start,
            "endDate": end,
            "getDailyAvailability": "true",
            "isReserving": "true",
            "filterData": "[]",
            "boatLength": 0, "boatDraft": 0, "boatWidth": 0,
            "peopleCapacityCategoryCounts": "[]",
            "numEquipment": 0,
            # the site sends a cache-busting timestamp; mirror that
            "seed": dt.datetime.now(dt.timezone.utc)
                      .replace(tzinfo=None).isoformat() + "Z",
        }
        data = get_json("/api/availability/map", params)
        return pid, map_id, data

    scan = {
        "start": start,
        "end": end,
        "scannedAt": dt.datetime.now().isoformat(timespec="seconds"),
        # Scans can be produced on a CI runner in UTC and read by a browser in
        # any timezone, so record an absolute instant too. `scannedAt` above
        # stays local for readable CLI output.
        "scannedEpoch": int(time.time()),
        "parks": {},
    }

    done = 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for pid, map_id, data in pool.map(fetch, jobs):
            done += 1
            park = inv[pid]
            bucket = scan["parks"].setdefault(
                pid, {"name": park["name"], "units": {}})
            for rid, series in (data.get("resourceAvailabilities") or {}).items():
                if rid not in park["units"]:
                    continue  # non-roofed resource sharing the same map
                bucket["units"][rid] = [d["availability"] for d in series]
            say(f"  [{done}/{len(jobs)}] {park['name']} map {map_id}: "
                f"{len(bucket['units'])} roofed units")

    # `watch` passes write=False so a filtered watch never clobbers the
    # full-province scan that `search` reads from.
    if write:
        path = os.path.join(CACHE, "availability.json")
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(scan, fh)
        say(f"\nWrote {path}")
    return scan


def load_scan():
    path = os.path.join(CACHE, "availability.json")
    if not os.path.exists(path):
        sys.exit("No scan yet. Run:  python op_roofed.py scan")
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------------------
# search
# ----------------------------------------------------------------------------

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
            "fri": 4, "sat": 5, "sun": 6}
# A weekend trip is one you arrive for on Friday or Saturday.
WEEKEND_ARRIVALS = {4, 5}


def parse_arrival(raw):
    """'weekend' | 'fri' | 'fri,sat' | 'any' -> set of weekday ints, or None."""
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw in ("any", "all", ""):
        return None
    if raw in ("weekend", "weekends"):
        return set(WEEKEND_ARRIVALS)
    days = set()
    for token in raw.split(","):
        token = token.strip().lower()[:3]
        if token in WEEKDAYS:
            days.add(WEEKDAYS[token])
    return days or None


def find_stays(inv, scan, nights, want_cats=None, park_filter=None,
               weekends_only=False, arrival_days=None, min_capacity=None,
               loose=False, date_from=None, date_to=None, ignore_rules=False,
               rejected=None):
    """Return a list of bookable stays matching the criteria.

    Free nights alone are not enough: the resource's date schedule can impose
    a minimum/maximum stay or restrict arrival weekdays. Those are applied
    here unless `ignore_rules` is set. Pass a dict as `rejected` to collect
    per-park counts of stays dropped by the min-stay rule, which is what
    usually surprises people.
    """
    ok = SOFT_BOOKABLE if loose else BOOKABLE
    start = dt.date.fromisoformat(scan["start"])
    results = []

    # `weekends_only` predates the arrival-day selector and means Fridays.
    if arrival_days is None and weekends_only:
        arrival_days = {4}

    lo = dt.date.fromisoformat(date_from) if date_from else None
    hi = dt.date.fromisoformat(date_to) if date_to else None

    for pid, pdata in scan["parks"].items():
        park = inv.get(pid)
        if not park:
            continue
        if park_filter and park_filter.lower() not in (pdata["name"] or "").lower():
            continue

        schedules = park.get("schedules") or {}

        for rid, series in pdata["units"].items():
            unit = park["units"].get(rid)
            if not unit:
                continue
            if want_cats and unit["category"] not in want_cats:
                continue
            if min_capacity and (unit["maxCapacity"] or 0) < min_capacity:
                continue

            sched = schedules.get(str(unit.get("dateScheduleId")))

            # slide an N-night window across the series
            for i in range(len(series) - nights + 1):
                window = series[i:i + nights]
                if not all(v in ok for v in window):
                    continue

                arrive = start + dt.timedelta(days=i)
                depart = arrive + dt.timedelta(days=nights)

                if lo and arrive < lo:
                    continue
                if hi and arrive > hi:
                    continue
                if arrival_days is not None and arrive.weekday() not in arrival_days:
                    continue

                if not ignore_rules and not stay_allowed(sched, arrive, nights):
                    if rejected is not None:
                        need = stay_rules(sched, arrive)[0]
                        if need > nights:
                            slot = rejected.setdefault(
                                pdata["name"], {"count": 0, "minNights": need})
                            slot["count"] += 1
                            slot["minNights"] = min(slot["minNights"], need)
                    continue

                results.append({
                    "park": pdata["name"],
                    "unit": unit["name"],
                    "type": unit["categoryName"],
                    "capacity": unit["maxCapacity"],
                    "arrive": arrive.isoformat(),
                    "depart": depart.isoformat(),
                    "nights": nights,
                    "partial": any(v == 7 for v in window),
                    "resourceLocationId": park["resourceLocationId"],
                    "mapIds": unit["mapIds"],
                })

    results.sort(key=lambda r: (r["arrive"], r["park"], r["unit"] or ""))
    return results


def booking_url(stay):
    """Deep link into the reservation site for this stay."""
    params = {
        "resourceLocationId": stay["resourceLocationId"],
        "mapId": stay["mapIds"][0] if stay["mapIds"] else "",
        "searchTabGroupId": 2,
        "bookingCategoryId": ROOFED_BOOKING_CATEGORY,
        "startDate": stay["arrive"],
        "endDate": stay["depart"],
        "nights": stay["nights"],
        "isReserving": "true",
        "equipmentId": -32768,
        "subEquipmentId": -32768,
        "partySize": 2,
    }
    return (BASE + "/create-booking/results?"
            + urllib.parse.urlencode(params))


def parse_types(raw):
    if not raw:
        return None
    cats = set()
    for token in raw.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token not in TYPE_ALIASES:
            sys.exit(f"Unknown --type '{token}'. "
                     f"Choose from: {', '.join(sorted(TYPE_ALIASES))}")
        cats.add(TYPE_ALIASES[token])
    return cats


def print_stays(stays, limit=60, show_urls=False):
    if not stays:
        print("No matching availability.")
        return
    print(f"{len(stays)} matching stay(s):\n")
    shown = stays[:limit]
    for s in shown:
        flag = " ~partial" if s["partial"] else ""
        cap = f"sleeps {s['capacity']}" if s["capacity"] else "?"
        print(f"  {s['arrive']} -> {s['depart']}  {s['park']}")
        print(f"      {s['unit']}  ({s['type']}, {cap}){flag}")
        if show_urls:
            print(f"      {booking_url(s)}")
    if len(stays) > limit:
        print(f"\n  ... and {len(stays) - limit} more "
              f"(raise --limit to see them)")


def cmd_search(args):
    inv = load_inventory()
    scan = load_scan()
    rejected = {}
    stays = find_stays(
        inv, scan,
        nights=args.nights,
        want_cats=parse_types(args.type),
        park_filter=args.park,
        weekends_only=args.weekends,
        arrival_days=parse_arrival(args.arrive),
        min_capacity=args.capacity,
        loose=args.loose,
        date_from=args.start,
        date_to=args.end,
        ignore_rules=args.ignore_rules,
        rejected=rejected,
    )
    print(f"(scan taken {scan['scannedAt']}, "
          f"covering {scan['start']} .. {scan['end']})\n")
    print_stays(stays, limit=args.limit, show_urls=not args.no_urls)

    if rejected:
        print(f"\nHidden by minimum-stay rules at {len(rejected)} park(s). "
              f"These have free nights, but too few in a row to book:")
        for name, info in sorted(rejected.items(),
                                 key=lambda kv: -kv[1]["count"])[:8]:
            print(f"  {name}: needs at least {info['minNights']} nights")


# ----------------------------------------------------------------------------
# watch
# ----------------------------------------------------------------------------

def stay_key(s):
    return f"{s['park']}|{s['unit']}|{s['arrive']}|{s['nights']}"


def cmd_watch(args):
    inv = load_inventory()
    want_cats = parse_types(args.type)
    seen = set()
    first_pass = True

    print("Watching for roofed availability. Ctrl+C to stop.")
    print(f"Interval: {args.interval}s\n")

    while True:
        try:
            scan = cmd_scan(args, write=False)
            stays = find_stays(
                inv, scan,
                nights=args.nights,
                want_cats=want_cats,
                park_filter=args.park,
                weekends_only=args.weekends,
                arrival_days=parse_arrival(args.arrive),
                min_capacity=args.capacity,
                loose=args.loose,
                date_from=args.start,
                date_to=args.end,
                ignore_rules=args.ignore_rules,
            )
            keys = {stay_key(s) for s in stays}
            fresh = [s for s in stays if stay_key(s) not in seen]

            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if first_pass:
                print(f"\n[{stamp}] baseline: {len(stays)} matching stay(s)")
                print_stays(stays, limit=args.limit,
                            show_urls=not args.no_urls)
                first_pass = False
            elif fresh:
                print(f"\n[{stamp}] *** {len(fresh)} NEW opening(s) ***")
                print_stays(fresh, limit=args.limit,
                            show_urls=not args.no_urls)
                sys.stdout.write("\a")  # terminal bell
                sys.stdout.flush()
                if args.notify_cmd:
                    msg = "; ".join(
                        f"{s['park']} {s['unit']} {s['arrive']}"
                        for s in fresh[:5])
                    try:
                        subprocess.run(args.notify_cmd.replace("{}", msg),
                                       shell=True, check=False)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  (notify-cmd failed: {exc})")
            else:
                print(f"\n[{stamp}] no change ({len(stays)} matching)")

            seen = keys
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except Exception as exc:  # noqa: BLE001 - keep the watcher alive
            print(f"  scan error: {exc}")

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def add_common(p, with_dates=True):
    p.add_argument("--park", help="substring match on park name")
    p.add_argument("--type", help="comma list: "
                                  "yurt, cabin, cottage, trailer")
    p.add_argument("--nights", type=int, default=2)
    p.add_argument("--weekends", action="store_true",
                   help="Friday arrivals only (same as --arrive fri)")
    p.add_argument("--arrive",
                   help="arrival days: weekend (Fri or Sat), any, or a comma "
                        "list like fri,sat")
    p.add_argument("--capacity", type=int,
                   help="minimum sleeping capacity")
    p.add_argument("--loose", action="store_true",
                   help="also accept PartiallyAvailable nights")
    p.add_argument("--ignore-rules", action="store_true",
                   help="skip min/max-stay and arrival-day checks "
                        "(shows free nights that are not actually bookable)")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--no-urls", action="store_true")
    if with_dates:
        p.add_argument("--start", help="YYYY-MM-DD (default: today)")
        p.add_argument("--end", help="YYYY-MM-DD")
        p.add_argument("--days", type=int, default=180,
                       help="horizon length if --end omitted")
        p.add_argument("--workers", type=int, default=4)


def main():
    ap = argparse.ArgumentParser(
        description="Ontario Parks roofed-accommodation availability engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh", help="rebuild park/unit inventory")
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("scan", help="pull availability for the horizon")
    p.add_argument("--park")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("search", help="query the last scan")
    add_common(p, with_dates=False)
    p.add_argument("--start", help="earliest arrival YYYY-MM-DD")
    p.add_argument("--end", help="latest arrival YYYY-MM-DD")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("watch", help="re-scan on an interval, alert on new")
    add_common(p, with_dates=True)
    p.add_argument("--interval", type=int, default=900,
                   help="seconds between scans (default 900 = 15 min)")
    p.add_argument("--notify-cmd",
                   help="shell command to run on new openings; "
                        "'{}' is replaced with a summary")
    p.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
