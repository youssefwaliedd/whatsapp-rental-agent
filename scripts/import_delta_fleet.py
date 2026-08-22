"""Build config/fleet.json from Delta Rentals Dubai's published fleet.

Every daily rate here is what deltarentalsdubai.com publishes. Nothing else is
invented quietly — anything not published is listed in `_assumed` at the top of
the generated file so it can be checked against the client rather than
discovered by a customer.

Weekly and monthly rates are deliberately null. Delta lists them as "available
on request" rather than publishing them, so the engine bills daily x days, which
is the honest reading. Inventing a weekly discount would put a number in front
of a customer that the company never agreed to.
"""

import json
from pathlib import Path

ROOT = Path("/Users/youssefwalied/Documents/Personal Projects/WhatsApp Rental Agent")

# make, model, daily AED, category, seats, bags, colour, year
# colour/year: "" means Delta does not publish it — see _assumed.
FLEET = [
    ("Range Rover",  "Sport V8",              799,  "luxury_suv",   5, 4, "", ""),
    ("Range Rover",  "Sport SVR",            1099,  "luxury_suv",   5, 4, "", ""),
    ("Mercedes-Benz","GLE 63 S",             1299,  "luxury_suv",   5, 4, "", ""),
    ("Mercedes-Benz","G63",                  1699,  "luxury_suv",   5, 3, "", ""),
    ("Mercedes-Benz","S580",                 1699,  "luxury_sedan", 5, 3, "", ""),
    ("Mercedes-Benz","V250 VIP Edition",     1899,  "luxury_sedan", 7, 5, "", ""),
    ("Range Rover",  "Vogue SV",             1999,  "luxury_suv",   5, 4, "", ""),
    ("Mercedes-Benz","S580 Maybach",         2399,  "luxury_sedan", 4, 3, "", ""),
    ("Bentley",      "Continental GTC",      2899,  "convertible",  4, 2, "", ""),
    ("McLaren",      "Artura",               2999,  "supercar",     2, 1, "", ""),
    ("Lamborghini",  "Huracan Evo Spyder",   3199,  "convertible",  2, 1, "", ""),
    ("Ferrari",      "296 GTB",              3499,  "supercar",     2, 1, "", ""),
    ("Ferrari",      "F8 Tributo Spider",    3499,  "convertible",  2, 1, "", ""),
    ("Lamborghini",  "Urus Performante",     3499,  "luxury_suv",   5, 3, "orange", ""),
    ("Rolls-Royce",  "Ghost Mansory",        3499,  "luxury_sedan", 5, 3, "", ""),
    ("Rolls-Royce",  "Wraith Black Badge",   3499,  "luxury_sedan", 4, 2, "", ""),
    ("Rolls-Royce",  "Ghost Black Badge",    3599,  "luxury_sedan", 5, 3, "", ""),
    ("Audi",         "R8 V10 Spyder",        1799,  "convertible",  2, 1, "", ""),
    ("Porsche",      "911 GT3 RS",           3999,  "sports",       2, 1, "white", ""),
    ("Rolls-Royce",  "Cullinan",             3999,  "luxury_suv",   5, 4, "", ""),
    ("Rolls-Royce",  "Cullinan Black Badge", 4199,  "luxury_suv",   5, 4, "", ""),
    ("Rolls-Royce",  "Cullinan",             5999,  "luxury_suv",   5, 4, "", "2025"),
    ("Rolls-Royce",  "Phantom",              6799,  "luxury_sedan", 5, 3, "", ""),
    ("Rolls-Royce",  "Spectre",              7499,  "luxury_sedan", 4, 2, "", ""),
    ("Lamborghini",  "Revuelto",            11999,  "supercar",     2, 1, "", ""),
    ("Ferrari",      "Purosangue",          12999,  "luxury_suv",   4, 3, "", ""),
]

# Availability is seeded as day offsets from the reference date, exactly as the
# fictional fleet was, so the demo scenarios still work: something desirable is
# free this weekend and something else is booked out, which is what makes the
# alternatives flow demonstrable. Delta's website is a catalogue, not a diary —
# it publishes no availability at all.
BLOCKS = {
    "Huracan Evo Spyder": [[0, 10]],      # booked out — drives find_alternatives
    "Revuelto": [[2, 6]],
    "Cullinan Black Badge": [[5, 9]],
    "Artura": [[1, 4]],
    "911 GT3 RS": [[8, 12]],
}

FEATURES = {
    "supercar": ["Launch control", "Carbon ceramic brakes", "Sport exhaust", "Apple CarPlay"],
    "convertible": ["Convertible roof", "Sport exhaust", "Apple CarPlay", "Heated seats"],
    "luxury_suv": ["Panoramic roof", "360 camera", "Apple CarPlay", "Heated and cooled seats"],
    "luxury_sedan": ["Rear entertainment", "Massage seats", "Apple CarPlay", "Panoramic roof"],
    "sports": ["Sport exhaust", "Carbon ceramic brakes", "Apple CarPlay", "Track telemetry"],
}


def build():
    vehicles = []
    for index, (make, model, daily, category, seats, bags, colour, year) in enumerate(FLEET, 1):
        blocked = BLOCKS.get(model, [])
        vehicles.append({
            "id": f"veh_{index:02d}",
            "make": make,
            "model": model,
            "year": int(year) if year else 2025,
            "category": category,
            "body_type": {"convertible": "convertible", "supercar": "coupe", "sports": "coupe",
                          "luxury_suv": "suv", "luxury_sedan": "sedan"}[category],
            "color": colour or "unspecified",
            "interior_color": "unspecified",
            "daily_price": daily,
            # Delta lists weekly and monthly as "available on request" rather
            # than publishing a figure. Null means the engine bills daily x days
            # instead of inventing a discount the company never offered.
            "weekly_price": None,
            "monthly_price": None,
            # "No deposit required (T&Cs apply)" on every listing. The T&Cs also
            # describe a AED 5,000-20,000 deposit "varying by vehicle, driver
            # and duration" — a range, not a value, so it cannot be encoded per
            # vehicle without asking. See _assumed.
            "deposit": 0,
            "included_km_per_day": 250,
            # T&Cs give AED 25-150/km, varying by vehicle. Unset rather than
            # guessed; the agent will say it checks rather than name a figure.
            "extra_km_price": 0,
            "features": FEATURES[category],
            "passenger_capacity": seats,
            "luggage_capacity": bags,
            "transmission": "automatic",
            "images": [f"assets/vehicles/veh_{index:02d}.png"],
            "status": "active",
            "blocked_ranges": [
                {"start_offset_days": a, "end_offset_days": b} for a, b in blocked
            ],
        })

    fleet = {
        "_comment": (
            "Delta Rentals Dubai (TRIPLE D RENTALS L.L.C.). Daily rates imported from "
            "deltarentalsdubai.com. THIS IS STILL A DEMONSTRATION CONFIGURATION — the "
            "demo disclosure stays on until the operator confirms every field marked "
            "in _assumed below. Real prices under a real company name make a wrong "
            "answer far more damaging, not less."
        ),
        "_source": "https://deltarentalsdubai.com — imported 22 Aug 2026",
        "_assumed": {
            "_comment": "Not published by Delta. Confirm each with the operator before go-live.",
            "colour": "Only the Urus (orange) and 911 GT3 RS (white) are stated. The rest are 'unspecified', so a customer asking for a black G63 gets a check rather than a claim.",
            "interior_colour": "Never published. All 'unspecified'.",
            "year": "Only the AED 5,999 Cullinan is dated (2025). Others defaulted to 2025 — this is a guess and appears in the name the customer sees.",
            "seats_and_luggage": "Manufacturer specification, not Delta's. Verifiable, but worth a glance.",
            "deposit": "Listings say no deposit; the T&Cs say AED 5,000-20,000 varying by vehicle, driver and duration. Encoded as 0. Needs a per-vehicle or per-category answer.",
            "extra_km_price": "T&Cs give AED 25-150/km varying by vehicle. Encoded as 0 rather than guessed.",
            "weekly_and_monthly": "Listed as 'on request'. Null, so the engine bills daily x days.",
            "availability": "Delta publishes none. Seeded blocks kept so the demo scenarios work.",
            "second_price_column": "The homepage shows a second price beside each daily rate. Its meaning is not stated anywhere and has NOT been imported.",
        },
        "operator": {
            "demo_company_name": "Delta Rentals Dubai (DEMO)",
            "currency": "AED",
            "timezone": "Asia/Dubai",
            "is_demonstration": True,
        },
        "vehicles": vehicles,
    }
    path = ROOT / "config" / "fleet.json"
    path.write_text(json.dumps(fleet, indent=2) + "\n", encoding="utf-8")
    return len(vehicles), path


if __name__ == "__main__":
    count, path = build()
    print(f"wrote {count} vehicles to {path}")
