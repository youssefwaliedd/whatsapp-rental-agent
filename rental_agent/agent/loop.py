"""The conversational turn.

One inbound customer message in, one WhatsApp reply out, with any number of tool
calls in between.

A manual loop rather than the SDK tool runner: dispatch here is registry-driven
(one `execute_tool` entry point that owns idempotency and the audit log rather
than a set of decorated functions), the loop has to be able to stop early on
escalation, and the prototype is deliberately free of beta SDK dependencies.

History is rebuilt from the stored transcript each turn — customer and agent
messages only. Tool calls are not replayed into later turns: the facts they
established are carried by the state snapshot instead, which keeps context small
and stops a stale tool result from being mistaken for a current one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..context import ToolContext
from ..domain.enums import Stage
from ..tools.registry import execute_tool
from .providers.errors import ProviderUnavailable
from . import extraction as extraction_mod
from .prompt import build_system, render_state
from .schemas import TOOLS
from .settings import FALLBACK_BETA, AgentSettings

#: Which requirement a question is about. Deliberately loose — a false negative
#: costs the evaluator one signal, while a false positive would accuse the agent
#: of a mistake it did not make.
_SLOT_QUESTIONS = {
    "pickup_at": re.compile(
        r"\b(when|what time|what day)\b[^?]{0,80}\b(need|want|start|pick|collect|deliver|"
        r"drop it off to you)\b|\bwhat time.{0,40}\bdeliver",
        re.IGNORECASE,
    ),
    "return_at": re.compile(
        r"\b(when|what time|what day|how long)\b[^?]{0,80}\b(return|back|bring|drop it back|"
        r"finish|end|keep it)\b",
        re.IGNORECASE,
    ),
    "delivery_location": re.compile(
        r"\bwhere\b[^?]{0,80}\b(deliver|drop|bring|collect|pick|like it|want it)\b",
        re.IGNORECASE,
    ),
}

#: Sent when the model declines or the turn fails outright. The customer must
#: never be left with silence.
SAFE_FALLBACK_REPLY = (
    "Sorry — I'm having trouble with that one. Let me get a colleague to pick it up "
    "with you."
)

#: The model provider is down or overloaded. Not the customer's problem and not
#: worth escalating a colleague for — their message is already recorded, so ask
#: them to hold rather than dropping the conversation.
PROVIDER_BUSY_REPLY = (
    "Sorry, my system is running slow right now — give me a moment and send that "
    "again."
)


@dataclass
class AgentTurn:
    reply: str
    tool_calls: list[str] = field(default_factory=list)
    #: Only the calls that came back without an error envelope. `tool_calls`
    #: records what was *attempted*, which is the right signal for the evaluator
    #: and the wrong one for anything that tells the customer something happened
    #: — a failed booking must never earn a confirmation.
    tools_succeeded: list[str] = field(default_factory=list)
    #: Photos the agent chose to show, resolved to fleet image paths by the
    #: engine. The model picks the moment; it never picks the file.
    media: list[dict[str, Any]] = field(default_factory=list)
    duplicate: bool = False
    escalated: bool = False
    refusal: bool = False
    iterations: int = 0
    stop_reason: str | None = None
    #: Set when the provider failed rather than the conversation going wrong.
    provider_error: str | None = None
    #: Set when the extraction pass failed; the turn still ran without it.
    extraction_error: str | None = None


class Agent:
    def __init__(self, client: Any, settings: AgentSettings | None = None):
        self.client = client
        self.settings = settings or AgentSettings()
        # Server-side refusal fallbacks are an Anthropic feature. The client
        # declares whether it has them rather than the settings guessing.
        self._fallbacks_supported = bool(getattr(client, "supports_fallbacks", True))
        #: Set during a replay to run under a candidate strategy's lessons
        #: instead of the active ones. None means "use whatever is active".
        self.lessons_override: list[str] | None = None

    # -- API ------------------------------------------------------------

    def _create(self, **kwargs: Any) -> Any:
        """One request, with server-side fallbacks when the account supports them."""
        if self._fallbacks_supported:
            try:
                return self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - narrowed by the message check
                if "fallback" not in str(exc).lower():
                    raise
                # The beta is not enabled for this account; stop trying for the
                # rest of the process rather than paying the failure every turn.
                self._fallbacks_supported = False
        return self.client.messages.create(**kwargs)

    # -- turn -----------------------------------------------------------

    def respond(
        self,
        ctx: ToolContext,
        message: str,
        *,
        provider_message_id: str | None = None,
    ) -> AgentTurn:
        now = ctx.now()

        stored, is_duplicate = ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="inbound",
            content=message,
            now=now,
            provider_message_id=provider_message_id,
        )
        if is_duplicate:
            # A WhatsApp redelivery. Already answered — answering again would
            # double-reply and could re-run the turn's tool calls.
            return AgentTurn(reply="", duplicate=True)

        state = ctx.load_state()
        active = execute_tool(ctx, "get_active_reservation", {})
        active_reservation = active.get("reservation") if active.get("has_active_reservation") else None
        customer = execute_tool(ctx, "get_customer", {})

        # 1. Extraction — additive merge into state. It sharpens the turn but
        #    is not a prerequisite for it: the agent still has its tools and the
        #    stored state, so a failed extraction degrades rather than aborts.
        extraction_error: str | None = None
        if self.settings.extraction_enabled:
            try:
                extracted = extraction_mod.extract(
                    self.client,
                    message=message,
                    state=state,
                    now=now,
                    active_reservation=active_reservation,
                    model=self.settings.resolved_extraction_model(),
                    effort=self.settings.extraction_effort,
                    max_tokens=self.settings.extraction_max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                extraction_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                extracted = extraction_mod.Extraction()

            state = extraction_mod.merge(state, extracted, ctx.engine.tz)
            ctx.save_state(state)

            # 2. Safety signals are acted on in code. Not remembering to escalate
            #    is the most expensive mistake this system can make.
            if extracted.escalation_signal in extraction_mod.HIGH_SEVERITY_SIGNALS:
                execute_tool(
                    ctx,
                    "escalate_conversation",
                    {"reason": extracted.escalation_signal, "detail": message},
                )
                state = ctx.load_state()

        # 3. Conversational turn. A provider outage must degrade into an
        #    apology, never into a crashed turn with no reply at all.
        try:
            turn = self._run_tool_loop(ctx, state, now, customer, active_reservation)
        except ProviderUnavailable as exc:
            turn = AgentTurn(reply=PROVIDER_BUSY_REPLY, provider_error=str(exc)[:200])
        turn.extraction_error = extraction_error

        ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="outbound",
            content=turn.reply,
            now=ctx.now(),
        )
        self._record_asked_slots(ctx, turn.reply)
        return turn

    def relay(self, ctx: ToolContext, directive: str) -> AgentTurn:
        """Speak without having been spoken to.

        Used when a colleague has made a decision and the customer is waiting to
        hear it. No inbound message is recorded, because none was sent — the
        transcript stays an honest record of who said what, which matters
        because the evaluator reads it.

        The facts are already settled by the time this runs. The directive says
        what must be communicated; the model's only job is the wording.
        """
        now = ctx.now()
        state = ctx.load_state()
        active = execute_tool(ctx, "get_active_reservation", {})
        active_reservation = (
            active.get("reservation") if active.get("has_active_reservation") else None
        )
        customer = execute_tool(ctx, "get_customer", {})

        try:
            turn = self._run_tool_loop(
                ctx, state, now, customer, active_reservation, directive=directive
            )
        except ProviderUnavailable as exc:
            turn = AgentTurn(reply=PROVIDER_BUSY_REPLY, provider_error=str(exc)[:200])

        ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="outbound",
            content=turn.reply,
            now=ctx.now(),
        )
        return turn

    # -- internals ------------------------------------------------------

    def _lessons(self, ctx: ToolContext) -> list[str]:
        """What the agent has learned, injected into the per-turn state block.

        Read fresh every turn, so activating a new strategy takes effect in the
        next conversation without a restart.
        """
        if self.lessons_override is not None:
            return self.lessons_override
        if ctx.session is None:
            return []
        from ..evaluation.strategies import active_lessons

        return active_lessons(ctx)

    def _history(self, ctx: ToolContext) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for stored in ctx.messages.for_conversation(ctx.conversation_id or ""):
            role = "user" if stored.direction == "inbound" else "assistant"
            if not messages and role == "assistant":
                continue  # the first message must be from the customer
            if not stored.content:
                continue
            messages.append({"role": role, "content": stored.content})
        return messages

    def _run_tool_loop(
        self,
        ctx: ToolContext,
        state: Any,
        now: Any,
        customer: dict[str, Any],
        active_reservation: dict[str, Any] | None,
        directive: str | None = None,
    ) -> AgentTurn:
        engine = ctx.engine
        working = self._history(ctx)
        working.append(
            {
                "role": "system",
                "content": render_state(
                    state,
                    now=now,
                    customer=customer if "error" not in customer else None,
                    active_reservation=active_reservation,
                    lessons=self._lessons(ctx),
                    directive=directive,
                ),
            }
        )

        called: list[str] = []
        succeeded: list[str] = []
        iterations = 0

        while iterations < self.settings.max_tool_iterations:
            iterations += 1
            response = self._create(
                model=self.settings.resolved_model(),
                max_tokens=self.settings.max_tokens,
                system=build_system(engine.rules, engine.operator),
                tools=TOOLS,
                output_config={"effort": self.settings.effort},
                messages=working,
            )

            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "refusal":
                execute_tool(
                    ctx,
                    "escalate_conversation",
                    {"reason": "repeated_agent_failure", "detail": "model declined the request"},
                )
                return AgentTurn(
                    reply=SAFE_FALLBACK_REPLY,
                    tool_calls=called,
                    tools_succeeded=succeeded,
                    media=ctx.take_media(),
                    refusal=True,
                    escalated=True,
                    iterations=iterations,
                    stop_reason=stop_reason,
                )

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                return AgentTurn(
                    reply=self._text_of(response) or SAFE_FALLBACK_REPLY,
                    tool_calls=called,
                    tools_succeeded=succeeded,
                    media=ctx.take_media(),
                    escalated=ctx.load_state().escalated,
                    iterations=iterations,
                    stop_reason=stop_reason,
                )

            # Pass the assistant turn back whole — thinking blocks included, which
            # the API requires unchanged when continuing on the same model.
            working.append({"role": "assistant", "content": response.content})

            results = []
            for use in tool_uses:
                called.append(use.name)
                result = execute_tool(ctx, use.name, dict(use.input or {}))
                if "error" not in result:
                    succeeded.append(use.name)
                self._absorb(ctx, use.name, result)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "content": json.dumps(result, default=str),
                        "is_error": "error" in result,
                    }
                )
            working.append({"role": "user", "content": results})

        # Ran out of iterations: something is looping. Hand over rather than
        # leaving the customer waiting on a turn that will not converge.
        execute_tool(
            ctx,
            "escalate_conversation",
            {"reason": "repeated_agent_failure", "detail": "tool loop did not converge"},
        )
        return AgentTurn(
            reply=SAFE_FALLBACK_REPLY,
            tool_calls=called,
            tools_succeeded=succeeded,
            media=ctx.take_media(),
            escalated=True,
            iterations=iterations,
        )

    @staticmethod
    def _text_of(response: Any) -> str:
        return "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

    @staticmethod
    def _absorb(ctx: ToolContext, name: str, result: dict[str, Any]) -> None:
        """Fold tool output that the services layer does not already persist."""
        if "error" in result:
            return
        shown: list[str] = []
        if name == "search_available_vehicles":
            shown = [v["vehicle_id"] for v in result.get("vehicles", [])]
        elif name == "find_alternatives":
            shown = [v["vehicle_id"] for v in result.get("alternatives", [])]
        if not shown:
            return

        state = ctx.load_state()
        for vehicle_id in shown:
            if vehicle_id not in state.presented_vehicle_ids:
                state.presented_vehicle_ids.append(vehicle_id)
        if state.stage in (Stage.NEW_LEAD, Stage.QUALIFYING, Stage.CHECKING_AVAILABILITY):
            state.stage = Stage.OPTIONS_PRESENTED
        ctx.save_state(state)

    @staticmethod
    def _record_asked_slots(ctx: ToolContext, reply: str) -> None:
        """Record what the agent asked for, and whether it already knew it.

        The distinction matters: asking a second time because the customer
        never answered is right, while asking for something already supplied is
        the mistake the evaluator is looking for. Only the second can be
        detected here — by evaluation time the value is present either way.
        """
        if "?" not in reply:
            return

        asked = [slot for slot, pattern in _SLOT_QUESTIONS.items() if pattern.search(reply)]
        if not asked:
            return

        state = ctx.load_state()
        for slot in asked:
            state.asked_slots.append(slot)
            if getattr(state, slot, None) is not None:
                state.redundant_asks.append(slot)
        ctx.save_state(state)


def build_agent(settings: AgentSettings | None = None) -> Agent:
    """Build an agent on the configured provider."""
    from .providers import build_client

    settings = settings or AgentSettings()
    return Agent(build_client(settings.provider, model=settings.model), settings)
