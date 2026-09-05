"""Keep a stated calendar date when the customer supplies its time later."""
from __future__ import annotations

import re
from datetime import datetime
from dateutil.parser import parse

MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
DATE = re.compile(rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}\s+(?:{MONTHS})(?:\s+\d{{4}})?\b|\b(?:{MONTHS})\s+\d{{1,2}}(?:,?\s+\d{{4}})?\b", re.I)
RELATIVE = re.compile(r"\b(?:today|tomorrow|tonight|next|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|بكرة|غدا|غداً|اليوم", re.I)
CLOCK = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.I)
DATE_RANGE = re.compile(rf"\b(\d{{1,2}})\s*(?:to|through|[-–])\s*(\d{{1,2}})\s+({MONTHS})(?:\s+(\d{{4}}))?\b", re.I)


def remember(state, message: str, now: datetime) -> None:
    span = DATE_RANGE.search(message)
    if span:
        message = DATE_RANGE.sub(lambda m: f"{m[1]} {m[3]} {m[4] or now.year} to {m[2]} {m[3]} {m[4] or now.year}", message)
    matches = list(DATE.finditer(message))
    if not matches:
        return
    try:
        dates = [parse(m.group(), default=now).date() for m in matches]
    except ValueError:
        return
    # A return-only change is not a new pickup date.
    before = message[:matches[0].start()]
    return_only = len(dates) == 1 and re.search(r"\b(?:return|returning|extend|until|back)\b", before, re.I)
    if return_only:
        state.return_date = dates[0]
    else:
        state.pickup_date = dates[0]
        if len(dates) > 1:
            state.return_date = dates[-1]


def anchor(moment, known_date, message: str):
    duration_change = re.search(r"\b(?:extend|another|extra|more|later|earlier)\b|مدد|إضافي", message, re.I)
    if moment is not None and not DATE.search(message) and not RELATIVE.search(message) and not duration_change:
        if known_date:
            return moment.replace(year=known_date.year, month=known_date.month, day=known_date.day)
        # A clock time alone does not mean tomorrow. Leave the date missing.
        if CLOCK.search(message) and not re.search(r"\d[/-]\d|\b(?:in|for) \d+ days?\b", message, re.I):
            return None
    return moment
