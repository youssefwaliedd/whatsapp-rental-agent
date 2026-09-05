"""Keep a shortlist's announced count consistent with its visible vehicle rows."""
from __future__ import annotations

import re
from ..domain.selection import named_ids

_NUMBER = r'(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)'
_COUNT = re.compile(
    rf'\b(?P<lead>(?:these|those|this|the following|here are|here is|i have|we have|i found|we found|i can offer)\s+)'
    rf'(?P<count>{_NUMBER})(?P<tail>\s+(?:(?:available\s+)?(?P<noun>cars?|vehicles?|options?|models?)\b|available\b))',
    re.I,
)
_BULLET = re.compile(r'^\s*(?:[-*•]|\d+[.)])\s+')
_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']


def correct_counts(reply, fleet):
    """Count displayed rows, never inventory IDs or the search result's count.

    Only the introduction immediately preceding a vehicle list is edited.
    Dates, prices, passenger capacity and unrelated numbered lists are preserved.
    """
    lines = reply.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        if not _vehicle_row(lines[index], fleet):
            index += 1
            continue
        first = index
        count = 0
        while index < len(lines):
            if _vehicle_row(lines[index], fleet):
                count += 1
            elif lines[index].strip() and not lines[index].startswith(('  ', '\t')):
                break
            index += 1
        intro = first - 1
        while intro >= 0 and not lines[intro].strip():
            intro -= 1
        if intro < 0:
            continue
        announcements = list(_COUNT.finditer(lines[intro]))
        if not announcements:
            continue
        last = announcements[-1].start()

        def replace(match):
            if match.start() != last:
                return match.group()
            original = match['count']
            value = int(original) if original.isdigit() else _WORDS.index(original.lower())
            if value == count:
                return match.group()
            number = str(count) if original.isdigit() or count >= len(_WORDS) else _WORDS[count]
            if original.istitle():
                number = number.title()
            lead = match['lead']
            tail = match['tail']
            if count == 1:
                lead = re.sub(r'\bthese\b', 'this', lead, flags=re.I)
                lead = re.sub(r'\bthose\b', 'this', lead, flags=re.I)
                lead = re.sub(r'\bare\b', 'is', lead, flags=re.I)
            elif value == 1:
                lead = re.sub(r'\bis\b', 'are', lead, flags=re.I)
                lead = re.sub(r'\bthis\b', 'these', lead, flags=re.I)
            if match['noun']:
                noun = match['noun']
                corrected = noun.rstrip('sS') if count == 1 else noun if noun.lower().endswith('s') else noun + 's'
                tail = tail[:len(tail) - len(noun)] + corrected
            return lead + number + tail

        lines[intro] = _COUNT.sub(replace, lines[intro])
    return ''.join(lines)


def _vehicle_row(line, fleet):
    # A numbered vehicle list, a WhatsApp bullet, or a bold vehicle heading.
    stripped = line.lstrip()
    return bool((_BULLET.match(line) or stripped.startswith('*')) and named_ids(line, fleet))
