#!/usr/bin/env python3
"""
Geocode the roofed-accommodation parks so the web app can map them.

Ontario Parks' own `gpsCoordinates` field is empty for every park, so we
resolve coordinates from OpenStreetMap's Nominatim service instead and cache
the result in cache/geo.json. This runs once; after that the web app just
reads the cache.

Nominatim's usage policy caps us at 1 request/second and requires a real
User-Agent. We do ~37 lookups, once. Be a good citizen and don't loop this.

  python geocode.py            # fill in anything missing
  python geocode.py --force    # re-resolve everything
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
GEO_PATH = os.path.join(CACHE, "geo.json")

UA = ("ontario-parks-roofed-finder/1.0 "
      "(personal trip planning; contact via local user)")

# Nominatim cannot find these from their park names or their mailing
# addresses; they are remote Algonquin access points. Coordinates confirmed
# against the campground's own lake.
OVERRIDES = {
    "Algonquin - Kiosk Campground": (46.0847, -78.8931, "manual"),
}


def nominatim(query):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "ca",
    })
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        print(f"      ! {exc}")
        return None
    if not data:
        return None
    hit = data[0]
    return (round(float(hit["lat"]), 5),
            round(float(hit["lon"]), 5),
            hit.get("display_name", "")[:80])


def clean_park_name(name):
    """'Algonquin - Mew Lake Campground' -> 'Mew Lake Campground'."""
    n = re.sub(r"\s*-\s*Campground Area$", "", name)
    n = re.sub(r"\s*/\s*Sand Lake Gate$", "", n)
    if " - " in n:
        n = n.split(" - ", 1)[1]
    return n.strip()


def candidates(park, addr):
    """Ordered list of (query, precision) to try."""
    name = park["name"]
    short = clean_park_name(name)
    street, city, postal = addr

    out = []
    if short:
        out.append((f"{short}, Ontario, Canada", "park"))
    if short != name:
        out.append((f"{name}, Ontario, Canada", "park"))
    # a street address only helps when it's a real address, not directions
    if street and len(street) < 40 and not re.search(
            r"follow|from |take |hwy\s*\d+\.", street, re.I):
        out.append((f"{street}, {city}, Ontario, Canada", "address"))
    if city and postal:
        out.append((f"{city}, Ontario, {postal}, Canada", "city"))
    elif city:
        out.append((f"{city}, Ontario, Canada", "city"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-resolve parks already in the cache")
    args = ap.parse_args()

    inv_path = os.path.join(CACHE, "inventory.json")
    if not os.path.exists(inv_path):
        sys.exit("No inventory. Run:  python op_roofed.py refresh")
    with io.open(inv_path, encoding="utf-8") as fh:
        inv = json.load(fh)

    geo = {}
    if os.path.exists(GEO_PATH) and not args.force:
        with io.open(GEO_PATH, encoding="utf-8") as fh:
            geo = json.load(fh)

    # street/city/postal come from the raw park feed
    addrs = {}
    try:
        raw = json.load(io.open(os.path.join(CACHE, "parks_raw.json"),
                                encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None:
        # Ontario Parks 403s a non-browser User-Agent, so borrow the fetcher
        # that op_roofed already uses rather than rolling a second one.
        import op_roofed
        raw = op_roofed.get_json("/api/resourceLocation")
        with io.open(os.path.join(CACHE, "parks_raw.json"), "w",
                     encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False)
    for p in raw:
        en = next((l for l in p["localizedValues"]
                   if l["cultureName"] == "en-CA"), {})
        addrs[str(p["resourceLocationId"])] = (
            en.get("streetAddress") or "",
            en.get("city") or "",
            p.get("regionCode") or "",
        )

    todo = [(pid, p) for pid, p in inv.items() if pid not in geo]
    print(f"{len(inv)} parks, {len(todo)} to geocode "
          f"({len(inv) - len(todo)} cached)\n")

    for i, (pid, park) in enumerate(todo, 1):
        name = park["name"]
        print(f"[{i}/{len(todo)}] {name}")

        if name in OVERRIDES:
            lat, lon, prec = OVERRIDES[name]
            geo[pid] = {"name": name, "lat": lat, "lon": lon,
                        "precision": prec, "source": "manual override"}
            print(f"      -> {lat}, {lon}  (manual)")
            continue

        hit = None
        for query, precision in candidates(park, addrs.get(pid, ("", "", ""))):
            hit = nominatim(query)
            time.sleep(1.1)  # Nominatim policy: max 1 req/sec
            if hit:
                lat, lon, label = hit
                geo[pid] = {"name": name, "lat": lat, "lon": lon,
                            "precision": precision, "source": label}
                print(f"      -> {lat}, {lon}  ({precision}) {label[:52]}")
                break
        if not hit:
            print("      -> FAILED, no coordinates")

    os.makedirs(CACHE, exist_ok=True)
    with io.open(GEO_PATH, "w", encoding="utf-8") as fh:
        json.dump(geo, fh, ensure_ascii=False, indent=1)

    missing = [p["name"] for pid, p in inv.items() if pid not in geo]
    print(f"\nWrote {GEO_PATH}  ({len(geo)}/{len(inv)} located)")
    if missing:
        print("Still missing:", ", ".join(missing))


if __name__ == "__main__":
    main()
