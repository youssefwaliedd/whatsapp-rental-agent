"""ToolContext — what a tool call is allowed to see.

One object carries the database session, the customer and conversation identity,
the clock, and a rental engine that already knows about live demo reservations.
Tools receive this rather than reaching for globals, so a test can run an entire
conversation against an in-memory database with a frozen clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .config import load_fleet
from .domain.models import ConversationState
from .engine.engine import RentalEngine
from .store import repositories as repo


@dataclass
class ToolContext:
    session: Session | None = None
    customer_id: str | None = None
    conversation_id: str | None = None
    now_fn: Callable[[], datetime] | None = None
    reference_date: date | None = None
    _engine_cache: dict[str | None, RentalEngine] = field(default_factory=dict, repr=False)
    #: Photos a tool has asked to be shown this turn. The transport drains it
    #: after the reply is built — kept here rather than in `ConversationState`
    #: because it describes one turn's delivery, not anything the agent knows.
    _pending_media: list[dict] = field(default_factory=list, repr=False)
    #: Figures rendered by the engine for this turn, to be sent after the
    #: reply. The model writes the sentence; this is the part it must not be
    #: trusted to reproduce from a JSON payload — it once quoted a total and
    #: never mentioned the deposit at all.
    _pending_cards: list[tuple[str | None, str]] = field(default_factory=list, repr=False)

    # -- construction ----------------------------------------------------

    @classmethod
    def read_only(cls, engine: RentalEngine) -> "ToolContext":
        """For read tools with no persistence — the simulator's console and the
        Milestone 1 tests."""
        ctx = cls(now_fn=engine.now, reference_date=engine.reference_date)
        ctx._engine_cache[None] = engine
        return ctx

    # -- media -----------------------------------------------------------

    def queue_media(self, item: dict) -> None:
        """Ask the transport to send a photo set alongside this turn's reply."""
        self._pending_media.append(item)

    def take_media(self) -> list[dict]:
        """Drain the queue. Draining rather than reading means a turn that is
        retried or abandoned cannot send the same photos twice."""
        queued, self._pending_media = list(self._pending_media), []
        return queued

    def queue_card(self, text: str, *, tag: str | None = None) -> None:
        """Send engine-rendered figures alongside this turn's reply.

        A tagged card supersedes an earlier one carrying the same tag. Only one
        quote can be the one being presented, and a model that called the quote
        tool twice in a turn — once for a car nobody had mentioned — would
        otherwise send two formal quotes and leave the customer to work out
        which was theirs. Observed in play: a Mercedes CLA250 quote arriving
        beside the BMW they had actually chosen.
        """
        if not text:
            return
        if tag is not None:
            self._pending_cards = [
                (kept_tag, body) for kept_tag, body in self._pending_cards if kept_tag != tag
            ]
        self._pending_cards.append((tag, text))

    def take_cards(self) -> list[str]:
        """Drain, for the same reason media drains: an abandoned turn must not
        send the same quote twice."""
        queued, self._pending_cards = list(self._pending_cards), []
        return [body for _, body in queued]

    def documents_received(self) -> int:
        """How many messages in this conversation actually carried a document.

        The document check rests on this. Without it the agent records a
        customer's paperwork on the strength of them typing "here you go",
        which is a compliance record built on nothing.
        """
        if self.session is None or not self.conversation_id:
            return 0
        return sum(
            1
            for m in self.messages.for_conversation(self.conversation_id)
            if m.direction == "inbound" and m.media
        )

    # -- clock -----------------------------------------------------------

    def set_now(self, moment: datetime) -> None:
        """Move the clock. Clears cached engines so availability and quote
        timestamps do not keep answering from the old moment."""
        self.now_fn = lambda: moment
        self._engine_cache.clear()

    def now(self) -> datetime:
        if self.now_fn is not None:
            return self.now_fn()
        operator, _ = load_fleet()
        return datetime.now(ZoneInfo(operator.timezone))

    # -- engine ----------------------------------------------------------

    def engine_excluding(self, reservation_id: str | None = None) -> RentalEngine:
        """A rental engine whose availability includes live demo reservations.

        `reservation_id` is excluded from the blocking set — required when
        modifying a booking, which would otherwise conflict with itself and be
        refused for dates the customer already holds.
        """
        if reservation_id in self._engine_cache:
            return self._engine_cache[reservation_id]

        session = self.session
        if session is None:
            provider = None
        else:
            def provider(vehicle_id: str):
                return repo.Reservations(session).blocking_windows(
                    vehicle_id, exclude_reservation_id=reservation_id
                )

        built = RentalEngine(
            now_fn=self.now_fn,
            reference_date=self.reference_date,
            extra_blocks_provider=provider,
        )
        self._engine_cache[reservation_id] = built
        return built

    @property
    def engine(self) -> RentalEngine:
        return self.engine_excluding(None)

    # -- repositories ----------------------------------------------------

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("This operation needs a database session")
        return self.session

    @property
    def customers(self) -> repo.Customers:
        return repo.Customers(self._require_session())

    @property
    def conversations(self) -> repo.Conversations:
        return repo.Conversations(self._require_session())

    @property
    def messages(self) -> repo.Messages:
        return repo.Messages(self._require_session())

    @property
    def tool_calls(self) -> repo.ToolCalls:
        return repo.ToolCalls(self._require_session())

    @property
    def quotes(self) -> repo.Quotes:
        return repo.Quotes(self._require_session())

    @property
    def reservations(self) -> repo.Reservations:
        return repo.Reservations(self._require_session())

    @property
    def escalations(self) -> repo.Escalations:
        return repo.Escalations(self._require_session())

    @property
    def counters(self) -> repo.Counters:
        return repo.Counters(self._require_session())

    # -- conversation state ----------------------------------------------

    def load_state(self) -> ConversationState:
        """The agent's memory. Always read this before asking the customer
        anything — it is the whole point of storing state apart from the
        transcript."""
        conversation = self.conversations.get(self.conversation_id or "")
        if conversation is None or not conversation.state:
            return ConversationState(
                conversation_id=self.conversation_id or "",
                customer_id=self.customer_id or "",
            )
        return ConversationState.model_validate(conversation.state)

    def save_state(self, state: ConversationState) -> None:
        conversation = self.conversations.get(self.conversation_id or "")
        if conversation is None:
            raise RuntimeError(f"No conversation {self.conversation_id!r} to save state onto")
        now = self.now()
        state.updated_at = now
        self.conversations.save_state(conversation, state.model_dump(mode="json"), now)
