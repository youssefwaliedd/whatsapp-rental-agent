"""Location normalisation.

Customers write "marina", "the marina", "dubai marina". Fee lookup needs one
canonical name. This is normalisation, not policy — the fees themselves live in
rules.json.
"""

from __future__ import annotations

import re

#: alias -> canonical zone name used in rules.json
_ALIASES: dict[str, str] = {
    "dubai marina": "Dubai Marina",
    "marina": "Dubai Marina",
    "jbr": "JBR",
    "jumeirah beach residence": "JBR",
    "downtown dubai": "Downtown Dubai",
    "downtown": "Downtown Dubai",
    "dubai mall": "Downtown Dubai",
    "burj khalifa": "Downtown Dubai",
    "business bay": "Business Bay",
    "palm jumeirah": "Palm Jumeirah",
    "the palm": "Palm Jumeirah",
    "palm": "Palm Jumeirah",
    "jlt": "JLT",
    "jumeirah lake towers": "JLT",
    "jumeirah": "Jumeirah",
    "deira": "Deira",
    "dubai silicon oasis": "Dubai Silicon Oasis",
    "silicon oasis": "Dubai Silicon Oasis",
    "sharjah": "Sharjah",
    "abu dhabi": "Abu Dhabi",
    "ras al khaimah": "Ras Al Khaimah",
    "rak": "Ras Al Khaimah",
    "dubai international airport": "Dubai International Airport (DXB)",
    "dubai airport": "Dubai International Airport (DXB)",
    "dxb": "Dubai International Airport (DXB)",
    "terminal 1": "Dubai International Airport (DXB)",
    "terminal 3": "Dubai International Airport (DXB)",
    "al maktoum airport": "Al Maktoum Airport (DWC)",
    "al maktoum": "Al Maktoum Airport (DWC)",
    "dwc": "Al Maktoum Airport (DWC)",
    "abu dhabi airport": "Abu Dhabi Airport (AUH)",
    "auh": "Abu Dhabi Airport (AUH)",
}

# Longest first so "abu dhabi airport" wins over "abu dhabi".
_ORDERED = sorted(_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)


def normalise_location(raw: str | None) -> str | None:
    """Return the canonical zone name, or None if it isn't a zone we know.

    An unknown location is not an error: the caller charges the standard
    delivery fee and the agent can confirm the address with the customer.
    """
    if not raw:
        return None
    text = re.sub(r"[^a-z0-9 ]+", " ", raw.lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if text in _ALIASES:
        return _ALIASES[text]
    for alias, canonical in _ORDERED:
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return canonical
    return None


def is_known_location(raw: str | None) -> bool:
    return normalise_location(raw) is not None
