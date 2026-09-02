# Ontario Parks roofed accommodation: availability search engine

Everything here was verified live against `reservations.ontarioparks.ca` on
2026-09-01. No credentials, no API key, no headless browser required.

---

## 1. Why nothing "shows up" when you look

Three separate things are working against you, and only one of them is a
website problem.

**There are only 196 roofed units in the entire province.**

| Type | Units |
|---|---|
| Rustic Cabin | 100 |
| Soft-sided Shelter (yurts) | 73 |
| Cottage | 16 |
| Trailer Equipped (ready-to-camp) | 7 |
| **Total** | **196** |

Spread across 37 parks. Some parks have exactly one. That is the entire
supply for a province of 16 million people, so "sold out" is the normal
state, not a glitch.

**The booking window is a 5-month rolling door.** Inventory does not sit
there waiting to be found. Each day a new day's worth appears at 7:00 a.m.
Eastern and is gone in minutes. If you browse on a random afternoon you are
looking at a shelf that was cleared months ago. During those 7 a.m. openings
the site puts you in a **Queue-it virtual waiting room** (I found
`queue-it-init.js` loaded on every page), which is why it can also feel like
the site is broken at exactly the moment you want it.

**Ontario Parks' own "notify me when available" does not cover roofed
accommodation.** This is the finding that matters most, and it is not
documented anywhere on the site. Straight from their API:

```
GET /api/bookingcategories

  bookingCategoryId 0  "Campsite"               allowAvailabilityNotifications: true
  bookingCategoryId 2  "Roofed Accommodation"   allowAvailabilityNotifications: false
```

So the one feature that would solve your problem is switched off for exactly
the thing you want. Polling yourself is not a shortcut past a feature that
exists; it is the only option.

**The practical consequence:** you are not really hunting for the initial
release. You are hunting for **cancellations**, which appear at random hours
and get taken within hours. That is a monitoring problem, and monitoring is
easy to automate.

---

## 2. What I built

`op_roofed.py`: a single file, Python 3 standard library only, no `pip install`.

It scans **every roofed unit in Ontario across a 7-month horizon in about
3.5 seconds** (52 HTTP requests). That speed is the whole trick: it is cheap
enough to re-run every 15 minutes forever.

```bash
# 1. Build the unit inventory. Slow (~2 min), run once a season.
python op_roofed.py refresh

# 2. Pull availability for the next 210 days. ~3.5 seconds.
python op_roofed.py scan --days 210

# 3. Query what you just pulled. Instant, no network.
python op_roofed.py search --nights 2 --weekends --type cabin

# 4. Or leave it running and get alerted on cancellations.
python op_roofed.py watch --nights 2 --weekends --park Pinery --interval 900
```

`search` and `watch` share the same filters:

| Flag | Meaning |
|---|---|
| `--park Pinery` | substring match on park name |
| `--type cabin,yurt,cottage,trailer` | roofed type (comma list) |
| `--nights 2` | stay length |
| `--weekends` | Friday arrivals only |
| `--capacity 6` | minimum sleeping capacity |
| `--start` / `--end` | restrict arrival dates |
| `--loose` | also accept `PartiallyAvailable` |

Output is one line per bookable stay plus a **deep link that lands directly
on that unit** with the dates pre-filled. I confirmed those links open the
right park with the "Roofed Accommodations" tab selected and the cabin drawn
on the map.

`watch` re-scans on an interval, diffs against the previous pass, and prints
only genuinely new openings, rings the terminal bell, and can shell out via
`--notify-cmd` (`'{}'` is replaced with a summary) so you can wire it to a
text message or a Windows toast.

### It works, verified end to end

A live run found **1,032 bookable Friday-arrival 2-night stays** in the next
7 months. I then took one hit (Charleston Lake, "Tall Pines Cabin",
2026-09-11) and confirmed it three independent ways: the day-by-day scan, the
site's separate discrete-date code path (`getDailyAvailability=false`), and
the real browser UI. All three agree.

### Where the openings actually are

Bookable share of operating unit-nights over the next 120 days:

| Easiest | | Hardest | |
|---|---|---|---|
| Quetico | 75.2% | Awenda | 2.4% |
| Blue Lake | 70.4% | Algonquin - Kiosk | 5.6% |
| Sandbanks | 69.7% | Algonquin - Brent | 5.6% |
| Sleeping Giant | 59.2% | Neys | 7.1% |
| Balsam Lake | 51.1% | Pancake Bay | 7.6% |
| Bass Lake | 51.0% | Inverhuron | 8.6% |

If you are flexible on park, the northwest (Quetico, Blue Lake, Sleeping
Giant) is wide open right now. The Algonquin cabins and Awenda are the ones
you will need the watcher for.

---

## 2b. The map app

`server.py` + `web/` is a local web app: a map of every roofed park, coloured
and sized by how much availability matches your filters, with a searchable
result list and deep links into the booking site.

```bash
python op_roofed.py refresh     # once
python geocode.py               # once, ~1 min
python op_roofed.py scan --days 210
python server.py                # opens http://127.0.0.1:8765
```

Nothing is exposed off your machine; it binds to `127.0.0.1` by default.
Python standard library only on the server side. Leaflet comes from a CDN.

**What it does**

- **Map.** One marker per park, labelled with its number of matching stays.
  Grey = nothing, red = 1–9, amber = 10–49, green = 50+. Click a marker for a
  breakdown by unit type; click a park in the list to fly to it.
- **Filters** apply live to both map and list: nights, minimum sleeping
  capacity, arrival window, unit type, Fridays-only, include-partial, and a
  park name search.
- **Park detail drawer.** A day-by-day availability grid for every unit in the
  park, all sharing one horizontal scroller under a month ruler, so you can
  read straight down a date column and compare units. Hover any cell for the
  date and status. Below it, every bookable stay with a **Book** link that
  opens the reservation site on that exact unit and date.
- **Rescan** re-pulls live availability from Ontario Parks in a few seconds
  and redraws, so you can sit on the map and keep hitting it.

Ontario Parks' `gpsCoordinates` field is empty for all 129 parks, so
`geocode.py` resolves coordinates from OpenStreetMap Nominatim and caches
them in `cache/geo.json`. All 37 parks resolve; 36 land on the park itself,
and "Algonquin Backcountry" falls back to Whitney (it is a whole region, not
a point). Kiosk Campground needed a manual coordinate. That script is
rate-limited to Nominatim's 1 request/second and only needs to run once.

---

## 3. The API, documented

This is the reusable part. The platform is **Camis / "GoingToCamp"** (the
page footer reads "© 2026 Camis Inc."), the same engine behind several other
Canadian and US park systems, so this transfers.

### Use the `.ca` domain, not `.com`

| Host | Result |
|---|---|
| `reservations.ontarioparks.com` | **403**, Azure WAF CAPTCHA challenge |
| `reservations.ontarioparks.ca` | **200**, clean JSON, no challenge |

This is the single thing that stops most scraping attempts. Same application,
one host is shielded and the other is not. Everything below is plain `curl`
against `.ca` with an ordinary browser User-Agent.

### The one endpoint that matters

```
GET /api/availability/map
      ?mapId=<map holding the units>
      &bookingCategoryId=2          # Roofed Accommodation
      &startDate=YYYY-MM-DD
      &endDate=YYYY-MM-DD
      &getDailyAvailability=true    # <-- the good part
      &isReserving=true
      &equipmentCategoryId=-32768&subEquipmentCategoryId=-32768
      &filterData=[]&peopleCapacityCategoryCounts=[]
      &boatLength=0&boatDraft=0&boatWidth=0&numEquipment=0
      &cartUid=&cartTransactionUid=&bookingUid=&groupHoldUid=
      &seed=<ISO timestamp, cache buster>
```

`getDailyAvailability=true` returns a **per-day status array for every unit
on the map, for the whole range, in one request**. A full year for one park
is ~2 MB in about one second. The site itself only ever asks for the dates
in the search box; asking for a year is the same call with different
parameters.

Response:

```json
{ "mapId": -2147483326,
  "resourceAvailabilities": { "-2147469935": [ {"availability": 0}, ... ] },
  "mapLinkAvailabilities":  { "-2147483333": [6] } }
```

### The availability enum

Lifted verbatim from the app's JS bundle, not guessed:

| Value | Meaning |
|---|---|
| **0** | **Available** (bookable) |
| 1 | Unavailable (taken) |
| 2 | NotOperating |
| 3 | NonReservable |
| 4 | Closed (outside season) |
| 5 | Invalid |
| 6 | InvalidBookingCategory |
| **7** | **PartiallyAvailable** |
| 8 | Held (in someone's cart) |

For an N-night stay arriving on day D you need days `D … D+N-1` all `0`.
Verified against the site's own discrete-date search.

### The trap: availability is not bookability

**`/api/availability/map` reports per-night OCCUPANCY, not whether you can
book.** This is the single most important thing to know about this API, and
getting it wrong produces confident false positives.

A run of `Available` nights can still be unbookable, because the Angular
client layers a second set of rules on top, fetched separately from:

```
GET /api/dateschedule/resourcelocationid?resourceLocationId=<park>
```

Each resource carries a `dateScheduleId` pointing into that response. The
rules that matter:

| Field | Meaning |
|---|---|
| `minimumStayDays` | base minimum nights |
| `minStayOverrides` | `[{range, stayDurationLimitDays}]`, seasonal minimum |
| `maximumStayDays` | base maximum nights |
| `maxStayOverrides` | seasonal maximum |
| `allowedArrivalDepartureDays` | `[{range, daysOfWeek}]`, restricts arrival weekday |

`daysOfWeek` uses **Sunday = 0** (confirmed: a Dec 24–26 2026 rule of `[4, 6]`
is Thursday and Saturday).

**The worked example.** Balsam Lake "RA1" (Cottage) showed 6 free nights over
2026-09-12 to 2026-09-17. The availability endpoint returns `Available` for a
2-night stay on those dates on *both* code paths, daily and discrete. The real
site returns **"No Available Sites"**, because RA1 has a `minStayOverride` of
**6 nights** for 2026-05-09 to 2026-10-23. Ask for 2026-09-12 → 2026-09-18
(6 nights) and the same site says "RA1: Available".

Across the province this was **21% of results**: weekend 2-night matches fell
from 1,033 to 818 once the rules were applied. Eleven parks have roofed units
whose free nights cannot be booked in 2-night blocks at all. Sandbanks is
6 nights minimum year-round; Charleston Lake, Bonnechere and Presqu'ile go to
3 over holiday periods.

The tool now applies these rules, and rather than silently hiding results it
tells you *why*: the park row reads "needs 6+ nights" and the summary offers a
one-click "Try 6 nights". `--ignore-rules` restores the old naive behaviour if
you want to see raw free nights.

### Reference endpoints

| Endpoint | Gives you |
|---|---|
| `/api/resourceLocation` | all 129 parks, GPS, `rootMapId`, categories |
| `/api/resourcelocation/resources?resourceLocationId=` | every unit, its `mapIds` and capacity (~3 MB/park) |
| `/api/bookingcategories` | booking categories + the notification flags |
| `/api/resourcecategory` | unit type names |
| `/api/maps/root`, `/api/maps?resourceLocationId=` | map tree |
| `/api/parkalert/all` | closures and alerts |

### Key IDs

```
bookingCategoryId 2 = Roofed Accommodation   (allowedEquipmentCategories: [], so send no equipment)
searchTabGroupId  2 = the Roofed tab in the UI

resourceCategoryId -2147483646 = Soft-sided Shelter (yurt)
                   -2147483645 = Rustic Cabin
                   -2147483644 = Cottage
                   -2147483633 = Trailer Equipped
```

### Gotchas that cost me time, so they don't cost you

- **Query the unit's own map, not the park's root map.** Root maps return
  `6 / InvalidBookingCategory` for roofed searches. Get the real `mapId`
  from `/api/resourcelocation/resources`. Presqu'ile spreads 9 units over 6 maps.
- **Roofed accommodation takes no equipment.** `allowedEquipmentCategories`
  is `[]`. The equipment parameters are ignored; sending tent values does
  not break it, but do not expect them to filter anything.
- **A map returns every resource on it**, including canoe and ski rentals.
  Filter to your known roofed `resourceId`s or you will get nonsense hits.
- **Responses are gzipped** and `urllib` will not decompress automatically.
- Park JSON is UTF-8; on Windows `open()` defaults to cp1252 and will crash
  on "Presqu'ile". Pass `encoding='utf-8'`.

---

## 4. How to actually get the booking

The tool is only half of it. Strategy:

1. **Leave `watch` running.** Cancellations are the real supply. A 15-minute
   interval is 96 scans a day at ~52 requests each; that is a rounding error
   of traffic to them and it will beat anyone refreshing by hand.
2. **Be loose on park, strict on dates**, or the reverse. Being strict on
   both is what makes this feel impossible. The scarcity table above shows
   the same weekend is 75% open at Quetico and 2% open at Awenda.
3. **For a specific in-demand date, still be at the keyboard at 7:00 a.m.
   Eastern, 5 months ahead.** Expect the Queue-it waiting room. The watcher
   is for the other 99% of the time.
4. **Sign in and save your payment details beforehand.** The cart holds a
   unit only briefly, and a cancellation you find at 11 p.m. will be gone by
   morning.

---

## 5. On being a good citizen

This reads the same public, unauthenticated endpoints your browser calls,
at a far lower rate than a person clicking around; it does not touch
authenticated, cart, or payment endpoints, and it books nothing. That is a
reasonable thing to do for personal trip planning.

Some etiquette that is also self-interest, since aggressive polling is how
these endpoints end up behind the same WAF the `.com` host already uses:

- Keep `--interval` at 900s or higher. Every scan already covers 7 months,
  so polling faster buys you very little.
- Keep `--workers` low (default 4). The retry path backs off exponentially.
- The source is open, but please do not stand it up as a shared public
  service. One person watching for one trip is the intended use; an open
  instance multiplies the polling against Ontario Parks by every visitor it
  attracts. If you deploy it, put it behind auth so it stays yours.
- Do not use it to bulk reserve, resell, or scalp bookings.
- `refresh` is the expensive call (~110 MB). Run it once a season, not daily.

---

## Files

| File | |
|---|---|
| `op_roofed.py` | CLI: refresh / scan / search / watch |
| `geocode.py` | one-time park geocoding via OSM Nominatim |
| `server.py` | local web app (map + search) |
| `web/` | front end: `index.html`, `app.js`, `style.css` |
| `cache/inventory.json` | 37 parks, 196 units (from `refresh`) |
| `cache/availability.json` | last scan (from `scan`) |
| `cache/geo.json` | park coordinates (from `geocode.py`) |

## Quick start

```bash
python op_roofed.py refresh          # once a season (~2 min)
python geocode.py                    # once (~1 min)
python op_roofed.py scan --days 210  # ~4 seconds
python server.py                     # http://127.0.0.1:8765
```
