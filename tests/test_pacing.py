"""How a reply arrives, as opposed to what it says.

The property under test is a social one: a correct answer delivered as three
simultaneous messages reads as a machine, and the same answer paced reads as a
person. None of these tests actually wait — the sleeper is injected, so what is
asserted is the schedule rather than the wall clock.
"""

from __future__ import annotations

from rental_agent.whatsapp.client import SendResult, WhatsAppClient
from rental_agent.whatsapp.pacing import Pacer, Pacing
from rental_agent.whatsapp.settings import WhatsAppSettings


class Clock:
    """Records what would have been waited for, without waiting."""

    def __init__(self):
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def client(clock, **kwargs):
    sent: list[dict] = []

    def transport(payload):
        sent.append(payload)
        return {"messages": [{"id": f"wamid.{len(sent)}"}]}

    whatsapp = WhatsAppClient(
        WhatsAppSettings(phone_number_id="PID", access_token="TOK", **kwargs),
        transport=transport,
        pacer=Pacer(Pacing(), sleep=clock),
    )
    return whatsapp, sent


# --------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------


def test_the_first_message_is_never_delayed():
    """The customer is waiting on an answer. Spending the response-time budget
    on theatre before the first word is the one thing pacing must not do."""
    schedule = Pacing().schedule(["Right away.", "And the details.", "One more thing."])
    assert schedule[0] == 0.0
    assert all(s > 0 for s in schedule[1:])


def test_a_longer_message_takes_longer_to_compose():
    pacing = Pacing()
    assert pacing.compose_seconds("x" * 300) > pacing.compose_seconds("Sure.")


def test_composing_time_is_clamped_at_both_ends():
    """Below the floor it looks instant; above the ceiling the customer thinks
    the conversation has died."""
    pacing = Pacing(min_seconds=1.0, max_seconds=4.5)
    assert pacing.compose_seconds("k") == 1.0
    assert pacing.compose_seconds("x" * 100_000) == 4.5


def test_pacing_can_be_switched_off_entirely():
    """An operator who wants machine-gun speed should get it from config, not
    from a code change."""
    assert Pacing(enabled=False).compose_seconds("x" * 500) == 0.0


# --------------------------------------------------------------------------
# Applied to real sends
# --------------------------------------------------------------------------


def test_one_short_reply_is_sent_without_any_wait():
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_text("971500000001", "Sure — Friday at 7pm works.")

    assert len(sent) == 1
    assert clock.waits == []


def test_a_split_reply_is_paced_between_parts():
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_text("971500000001", ("word " * 900).strip())

    assert len(sent) > 1, "this reply is long enough to split"
    assert len(clock.waits) == len(sent) - 1, "one wait between each pair, none before the first"
    assert all(w > 0 for w in clock.waits)


def test_the_typing_bubble_is_reshown_before_each_paced_part():
    """A typing indicator expires. Without re-showing it the customer watches a
    silent gap and only then sees the next message land."""
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_text("971500000001", ("word " * 900).strip(), typing_for="wamid.IN")

    typing = [p for p in sent if p.get("typing_indicator")]
    texts = [p for p in sent if p.get("type") == "text"]
    assert len(typing) == len(texts) - 1


def test_photos_arrive_one_at_a_time_not_as_a_burst():
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_images(
        "971500000001",
        ["https://x/1.png", "https://x/2.png", "https://x/3.png"],
        caption="The black G63",
    )

    assert len([p for p in sent if p.get("type") == "image"]) == 3
    assert len(clock.waits) == 2  # between the three, not before the first


def test_a_photo_that_fails_reports_what_did_land():
    """The caller needs to know the customer saw something rather than nothing,
    so a part-way failure returns the ids that succeeded."""
    calls = {"n": 0}

    def transport(payload):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("meta rejected the fetch")
        return {"messages": [{"id": f"wamid.{calls['n']}"}]}

    whatsapp = WhatsAppClient(
        WhatsAppSettings(phone_number_id="PID", access_token="TOK"),
        transport=transport,
        pacer=Pacer(Pacing(), sleep=Clock()),
    )
    result = whatsapp.send_images("971500000001", ["https://x/1.png", "https://x/2.png"])

    assert result.ok is False
    assert result.message_ids == ["wamid.1"]
    assert "meta rejected" in (result.error or "")


# --------------------------------------------------------------------------
# The payloads themselves
# --------------------------------------------------------------------------


def test_a_typing_indicator_also_marks_the_message_read():
    """The Cloud API carries both on one request. Sending a separate read
    receipt would be a wasted call against the rate limit."""
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_typing("wamid.IN")

    assert sent == [{
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
        "typing_indicator": {"type": "text"},
    }]


def test_a_reaction_names_the_message_it_marks():
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_reaction("971500000001", "wamid.IN", "✅")

    assert sent[0]["type"] == "reaction"
    assert sent[0]["reaction"] == {"message_id": "wamid.IN", "emoji": "✅"}


def test_an_empty_emoji_sends_nothing_at_all():
    """An empty string is the Cloud API's way of *removing* a reaction, so a
    caller with nothing to say must send no request rather than silently
    clearing a mark that is already there."""
    clock = Clock()
    whatsapp, sent = client(clock)
    result = whatsapp.send_reaction("971500000001", "wamid.IN", "")

    assert result.ok is True
    assert sent == []


def test_a_caption_is_converted_to_whatsapp_markup():
    """The same habit that puts **bold** in a reply puts it in a caption."""
    clock = Clock()
    whatsapp, sent = client(clock)
    whatsapp.send_image("971500000001", "https://x/1.png", caption="**AED 1,080** a day")

    assert sent[0]["image"]["caption"] == "*AED 1,080* a day"


# --------------------------------------------------------------------------
# Captions
# --------------------------------------------------------------------------


def test_a_caption_that_repeats_the_reply_is_dropped():
    """Observed in a live run: the model captioned the photo with the exact
    sentence it had just sent, so the customer read it twice — once as a
    message, once under the picture."""
    from rental_agent.formatting import photo_caption

    reply = "Here is the black G63. It's a beauty."
    assert photo_caption(reply, "Mercedes-AMG G63 — 2025", reply) == "Mercedes-AMG G63 — 2025"


def test_a_near_duplicate_caption_is_also_dropped():
    """Punctuation and case must not be enough to make it look like a new
    sentence."""
    from rental_agent.formatting import photo_caption

    assert photo_caption(
        "here is the black g63",
        "Mercedes-AMG G63 — 2025",
        "Here is the black G63. It's a beauty!",
    ) == "Mercedes-AMG G63 — 2025"


def test_a_caption_that_adds_something_is_kept():
    from rental_agent.formatting import photo_caption

    assert photo_caption(
        "Ready for collection Friday 7pm",
        "Mercedes-AMG G63 — 2025",
        "Here is the black G63. It's a beauty.",
    ) == "Ready for collection Friday 7pm"


def test_no_caption_falls_back_to_the_car_it_is_a_photo_of():
    """With several cars discussed, an uncaptioned photo leaves the customer
    guessing which one they are looking at."""
    from rental_agent.formatting import photo_caption

    assert photo_caption(None, "Mercedes-AMG G63 — 2025", "Take a look.") == (
        "Mercedes-AMG G63 — 2025"
    )
