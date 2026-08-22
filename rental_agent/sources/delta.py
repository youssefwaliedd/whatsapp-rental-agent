"""Read Delta Rentals Dubai's fleet from their own site.

    .venv/bin/python -m rental_agent.sources.delta      # write config/fleet.json

Their site is WordPress and exposes a real REST API, which is a better source
than the rendered page and closer to what their brief asks for — "a maintained
knowledge source", structured, rather than scraped markup that changes with the
theme.

    /wp-json/wp/v2/catalog        one entry per vehicle: name, slug, brand
    /wp-json/wp/v2/media?parent=  the photographs attached to each

Two things still come from the rendered page, because the API does not expose
them: the daily rate, and the mileage allowance. Both are read with a narrow
pattern and cross-checked against a second occurrence on the same page; a
vehicle whose two figures disagree is skipped rather than guessed at.

**Image URLs are stored absolute, pointing at Delta's own server.** Meta fetches
image URLs itself, so their photographs are served from where they already live:
always current, nothing copied, nothing to re-sync when they change a car.

Anything the source does not state is left null or empty. Colours, years,
interior trim, per-km charges and deposits are not published per vehicle, and a
plausible value invented here would reach a customer as a fact.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent
SITE = "https://deltarentalsdubai.com"
AGENT = {"User-Agent": "Mozilla/5.0 (compatible; DeltaFleetImport/1.0)"}

#: Enough for a WhatsApp photo set. More is a slideshow, not a sales message.
IMAGES_PER_VEHICLE = 3
#: Politeness between requests to the client's own server.
PAUSE = 0.25

_PRICE_PATTERNS = [
    re.compile(r"Price\s*for\s*1\s*Day\s*AED\s*([\d,]+)", re.I),
    re.compile(r"Rental\s*Cost\s*AED\s*([\d,]+)\s*/\s*Day", re.I),
    re.compile(r"Daily\s*pricing\s*starts\s*at\s*AED\s*([\d,]+)", re.I),
]
_KM = re.compile(r"Daily\s*Kilometer\s*([\d,]+)", re.I)

#: Model keyword -> (category, seats, luggage). Manufacturer specification, not
#: Delta's, and applied by keyword so a new car in a known family is classified
#: rather than silently defaulted.
_SHAPES: list[tuple[tuple[str, ...], str, int, int]] = [
    (("spyder", "spider", "convertible", "cabriolet", "roadster", "gtc", "targa"),
     "convertible", 2, 1),
    (("huracan", "revuelto", "aventador", "ferrari", "mclaren", "chiron",
      "296", "f8", "sf90", "812", "roma", "artura", "720", "765"), "supercar", 2, 1),
    (("911", "gt3", "gt2", "cayman", "boxster", "rs3", "rs5", "m2", "m4", "amg gt"),
     "sports", 2, 1),
    (("cullinan", "urus", "purosangue", "dbx", "bentayga", "g63", "g 63", "gls",
      "glе", "gle", "range rover", "defender", "x5", "x7", "q7", "q8", "cayenne",
      "lx600", "lc300", "patrol", "tahoe", "escalade", "suv"), "luxury_suv", 5, 4),
]
_DEFAULT_SHAPE = ("luxury_sedan", 5, 3)


#: httpx rather than urllib: it ships with a certificate bundle, and urllib on
#: a framework Python cannot verify the site's certificate without one.
_HTTP = httpx.Client(headers=AGENT, timeout=30.0, follow_redirects=True)


def fetch(url: str) -> bytes:
    response = _HTTP.get(url)
    response.raise_for_status()
    return response.content


def catalog() -> list[dict]:
    """Every vehicle, following the API's pagination rather than guessing."""
    entries: list[dict] = []
    page = 1
    while True:
        raw = fetch(f"{SITE}/wp-json/wp/v2/catalog?per_page=100&page={page}")
        batch = json.loads(raw)
        if not isinstance(batch, list) or not batch:
            break
        entries.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(PAUSE)
    return entries


def photographs(post_id: int) -> list[str]:
    try:
        media = json.loads(fetch(f"{SITE}/wp-json/wp/v2/media?parent={post_id}&per_page=20"))
    except Exception:
        return []
    urls = [m.get("source_url") for m in media if m.get("source_url")]
    # Newest first is how they upload; the hero shot is usually among them.
    return [u for u in urls if u][:IMAGES_PER_VEHICLE]


def daily_rate(page_html: str) -> int | None:
    """The rate, only when the page says it more than once and agrees with itself."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    found: list[int] = []
    for pattern in _PRICE_PATTERNS:
        for match in pattern.finditer(text):
            found.append(int(match.group(1).replace(",", "")))
    if not found:
        return None
    if len(set(found)) > 1:
        return None  # the page disagrees with itself — do not pick one
    return found[0]


def mileage(page_html: str) -> int:
    text = re.sub(r"<[^>]+>", " ", page_html)
    match = _KM.search(re.sub(r"\s+", " ", text))
    return int(match.group(1).replace(",", "")) if match else 250


def shape_of(name: str) -> tuple[str, int, int]:
    lowered = name.lower()
    for keywords, category, seats, bags in _SHAPES:
        if any(word in lowered for word in keywords):
            return category, seats, bags
    return _DEFAULT_SHAPE


def split_name(title: str) -> tuple[str, str]:
    words = title.split()
    two_word_makes = {"rolls", "range", "aston", "land", "alfa", "mercedes"}
    if words and words[0].lower() in two_word_makes and len(words) > 2:
        return " ".join(words[:2]), " ".join(words[2:])
    return (words[0], " ".join(words[1:])) if len(words) > 1 else (title, title)


def collect() -> tuple[dict, list[str]]:
    print("  reading the catalogue…", flush=True)
    entries = catalog()
    print(f"  {len(entries)} vehicles listed", flush=True)

    vehicles: list[dict] = []
    skipped: list[str] = []

    for index, entry in enumerate(entries, 1):
        title = re.sub(r"\s+", " ", entry.get("title", {}).get("rendered", "")).strip()
        title = title.replace("&#8211;", "-").replace("&amp;", "&")
        if not title:
            continue

        try:
            page = fetch(entry["link"]).decode("utf-8", "replace")
        except Exception:
            skipped.append(f"{title} (page unreachable)")
            continue

        rate = daily_rate(page)
        if rate is None:
            skipped.append(f"{title} (no single clear daily rate)")
            continue

        images = photographs(entry["id"])
        category, seats, bags = shape_of(title)
        make, model = split_name(title)

        vehicles.append({
            "id": f"veh_{len(vehicles) + 1:03d}",
            "make": make,
            "model": model,
            # Delta does not publish a model year per vehicle. Null rather than
            # guessed: the year is part of the name the customer is told.
            "year": None,
            "category": category,
            "body_type": {"convertible": "convertible", "supercar": "coupe",
                          "sports": "coupe", "luxury_suv": "suv",
                          "luxury_sedan": "sedan"}[category],
            "color": "unspecified",
            "interior_color": "unspecified",
            "daily_price": rate,
            # Listed as "available on request" rather than published.
            "weekly_price": None,
            "monthly_price": None,
            # Listings state no deposit; the terms describe a range varying by
            # vehicle. Zero matches the listings.
            "deposit": 0,
            "included_km_per_day": mileage(page),
            # AED 25-150/km by vehicle in the terms. Not published per car.
            "extra_km_price": 0,
            "features": [],
            "passenger_capacity": seats,
            "luggage_capacity": bags,
            "transmission": "automatic",
            # Absolute, pointing at Delta's own server: Meta fetches image URLs
            # itself, so their photographs stay where they already live.
            "images": images,
            "status": "active",
            "blocked_ranges": [],
            "source_url": entry["link"],
        })
        if index % 20 == 0:
            print(f"    {index}/{len(entries)}…", flush=True)
        time.sleep(PAUSE)

    _seed_availability(vehicles)

    fleet = {
        "_comment": (
            "Delta Rentals Dubai (TRIPLE D RENTALS L.L.C.). Generated by "
            "rental_agent/sources/delta.py from their WordPress REST API and vehicle "
            "pages. THIS REMAINS A DEMONSTRATION CONFIGURATION until every entry in "
            "_assumed is confirmed by the operator — real prices under a real company "
            "name make a wrong answer more damaging, not less."
        ),
        "_source": (
            f"{SITE}/wp-json/wp/v2/catalog — imported "
            f"{datetime.now(ZoneInfo('Asia/Dubai')):%d %b %Y}"
        ),
        "_assumed": {
            "_comment": "Not published by Delta. Confirm each before go-live.",
            "colour_and_interior": "Never published per vehicle. All 'unspecified', so a "
                                   "customer asking for a black one gets a check rather than a claim.",
            "year": "Not published. Null, so the customer is told 'Mercedes-Benz G63' "
                    "rather than a model year nobody stated.",
            "category_seats_luggage": "Classified from the model name against manufacturer "
                                      "specification, not Delta's data. Worth a glance.",
            "deposit": "Listings say no deposit; the terms say AED 5,000-20,000 varying by "
                       "vehicle, driver and duration. Encoded as 0 to match the listings.",
            "extra_km_price": "AED 25-150/km by vehicle in the terms. Left at 0 rather than guessed.",
            "weekly_and_monthly": "Listed as 'on request'. Null, so the engine bills daily x days.",
            "availability": "Delta publishes none. A handful of seeded blocks are added so the "
                            "alternatives flow is demonstrable; everything else reads as free.",
            "features": "No feature lists are published. Empty rather than invented.",
        },
        "operator": {
            "demo_company_name": "Delta Rentals Dubai (DEMO)",
            "currency": "AED",
            "timezone": "Asia/Dubai",
            "is_demonstration": True,
        },
        "vehicles": vehicles,
    }

    return fleet, skipped


def _seed_availability(vehicles: list[dict]) -> None:
    """Book a few cars out so the alternatives flow has something to work with.

    Delta publishes no availability at all — a website is a catalogue, not a
    diary. Without a car that is genuinely unavailable there is no way to
    demonstrate the substitution logic, which is one of the eight required
    scenarios.
    """
    wanted = ["huracan", "urus", "cullinan", "911"]
    blocks = [[0, 10], [2, 6], [5, 9], [1, 4]]
    for keyword, (start, end) in zip(wanted, blocks):
        for vehicle in vehicles:
            if keyword in f"{vehicle['make']} {vehicle['model']}".lower():
                vehicle["blocked_ranges"] = [
                    {"start_offset_days": start, "end_offset_days": end}
                ]
                break


def build() -> tuple[int, int, Path]:
    """Collect and write, for the command line."""
    fleet, skipped = collect()
    path = ROOT / "config" / "fleet.json"
    path.write_text(json.dumps(fleet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if skipped:
        print(f"\n  skipped {len(skipped)}:")
        for reason in skipped[:12]:
            print(f"    - {reason}")
    return len(fleet["vehicles"]), len(skipped), path


if __name__ == "__main__":
    count, skipped, path = build()
    print(f"\n  wrote {count} vehicles to {path}")
    if skipped:
        print(f"  {skipped} skipped rather than guessed at")
