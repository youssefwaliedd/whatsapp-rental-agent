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
from concurrent.futures import ThreadPoolExecutor
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

#: Sent once the outage has cost the customer a second turn. At that point
#: asking them to try again is just a slower way of losing them.
PROVIDER_DOWN_REPLY = (
    "Sorry — I'm still having trouble on my end. I've asked a colleague to pick "
    "this up with you directly so you're not left waiting."
)

#: How many consecutive unreachable turns before a person is pulled in. One is
#: bad luck and worth an apology; two is an outage the customer should not be
#: made to sit through.
PROVIDER_FAILURES_BEFORE_HANDOVER = 2


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


#: Extraction runs here while the first conversation call is in flight. Small
#: and shared: one worker per concurrent turn is plenty, since each turn submits
#: exactly one job and joins it a few seconds later.
_EXTRACTION_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="extraction")


@dataclass
class _Pending:
    """An extraction that may still be running.

    Carries its own failure rather than raising, because extraction is an
    enhancement: a turn that loses it still has tools, stored state and the
    customer's words, and must not be lost with it.
    """

    future: Any = None
    result: Any = None
    error: str | None = None
    joined: bool = False

    def run_now(self, call: Any) -> None:
        """Sequential mode — used when parallelism is switched off."""
        try:
            self.result = call()
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            self.error = f"{type(exc).__name__}: {str(exc)[:200]}"

    def join(self) -> Any:
        """Wait for the extraction and hand back whatever it managed.

        Returns an empty Extraction on failure, so every caller gets the same
        shape and none of them has to branch on whether it worked.
        """
        if not self.joined:
            self.joined = True
            if self.future is not None:
                try:
                    self.result = self.future.result()
                except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                    self.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return self.result if self.result is not None else extraction_mod.Extraction()

    def cancel(self) -> None:
        if self.future is not None and not self.joined:
            self.future.cancel()
            self.joined = True

    @property
    def outstanding(self) -> bool:
        return self.future is not None and not self.joined


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
        media: list[str] | None = None,
    ) -> AgentTurn:
        now = ctx.now()

        stored, is_duplicate = ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="inbound",
            content=message,
            now=now,
            provider_message_id=provider_message_id,
            media=media,
        )
        if is_duplicate:
            # A WhatsApp redelivery. Already answered — answering again would
            # double-reply and could re-run the turn's tool calls.
            return AgentTurn(reply="", duplicate=True)

        state = ctx.load_state()
        active = execute_tool(ctx, "get_active_reservation", {})
        active_reservation = active.get("reservation") if active.get("has_active_reservation") else None
        customer = execute_tool(ctx, "get_customer", {})

        # 1. Extraction — an additive merge into state. It sharpens the turn but
        #    is not a prerequisite for it: the agent still has its tools, the
        #    stored state and the customer's actual words, so a failed
        #    extraction degrades rather than aborts.
        #
        #    It is also a whole model call, and running it before the
        #    conversation makes a turn three sequential round trips. It takes no
        #    session and touches no database, so it can run *alongside* the
        #    first conversation call and be joined before the second — which
        #    takes it off the critical path entirely.
        pending = _Pending()
        if self.settings.extraction_enabled:
            call = lambda: extraction_mod.extract(  # noqa: E731
                self.client,
                message=message,
                state=state,
                now=now,
                active_reservation=active_reservation,
                model=self.settings.resolved_extraction_model(),
                effort=self.settings.extraction_effort,
                max_tokens=self.settings.extraction_max_tokens,
            )
            if self.settings.parallel_extraction:
                pending.future = _EXTRACTION_POOL.submit(call)
            else:
                pending.run_now(call)
                state = self._absorb_extraction(ctx, pending, message, state)

        # 2. Conversational turn. A provider outage must degrade into an
        #    apology, never into a crashed turn with no reply at all.
        try:
            turn = self._run_tool_loop(
                ctx, state, now, customer, active_reservation, pending=pending, message=message
            )
            self._clear_provider_failures(ctx)
        except ProviderUnavailable as exc:
            pending.cancel()
            turn = self._provider_unavailable(ctx, exc, message)
        turn.extraction_error = pending.error

        ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="outbound",
            content=turn.reply,
            now=ctx.now(),
        )
        self._record_asked_slots(ctx, turn.reply)
        return turn

    def warm_up(self) -> float | None:
        """Pay the first-call cost before a customer is waiting on it.

        The first request of a process is far slower than the rest — TLS,
        connection setup and the provider's own cold path all land on it. It was
        measured at 42s against 4-6s for every message after, which means the
        first customer of the day gets by far the worst experience of anyone.

        Called at server startup. Failure is returned as None rather than
        raised: a warm-up that cannot reach the provider is not a reason to
        refuse to start, since the next real turn will surface the problem with
        a proper error anyway.
        """
        import time

        started = time.time()
        try:
            self.client.messages.create(
                model=self.settings.resolved_model(),
                max_tokens=8,
                system=[{"type": "text", "text": "Reply with OK."}],
                messages=[{"role": "user", "content": "ok"}],
            )
        except Exception:  # noqa: BLE001 - never block startup on this
            return None
        return time.time() - started

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

    @staticmethod
    def _live_quote(ctx: ToolContext, state: Any) -> dict[str, Any] | None:
        """The quote the customer has already been shown, if it still stands.

        Read straight from the store rather than recalculated, so what the state
        block says and what the customer was told cannot drift apart.
        """
        if not state.quote_id or ctx.session is None:
            return None
        quote = ctx.quotes.get(state.quote_id)
        if quote is None or quote.status != "active":
            return None
        try:
            vehicle = ctx.engine.get_vehicle(quote.vehicle_id).display_name
        except Exception:  # noqa: BLE001 - a stale vehicle id must not kill the turn
            vehicle = quote.vehicle_id
        return {
            "quote_id": quote.quote_id,
            "vehicle": vehicle,
            "currency": ctx.engine.operator.currency,
            "total_charge": quote.total_charge,
            "deposit": quote.deposit,
            "expires_at": quote.expires_at.strftime("%a %d %b, %-I:%M %p"),
        }

    def _provider_unavailable(
        self, ctx: ToolContext, exc: Exception, message: str
    ) -> AgentTurn:
        """The model could not be reached. Do not leave the customer with it.

        A single failure gets an apology and an invitation to resend — outages
        are usually brief. A second consecutive one means the customer is stuck
        behind something that is not clearing, and repeating the apology is a
        slower way of losing them. So a person is pulled in, through the same
        escalation path everything else uses, and somebody at the company finds
        out a live customer is stranded.
        """
        state = ctx.load_state()
        state.consecutive_provider_failures += 1
        failures = state.consecutive_provider_failures
        ctx.save_state(state)

        if failures < PROVIDER_FAILURES_BEFORE_HANDOVER:
            return AgentTurn(reply=PROVIDER_BUSY_REPLY, provider_error=str(exc)[:200])

        execute_tool(
            ctx,
            "escalate_conversation",
            {
                "reason": "repeated_agent_failure",
                "detail": (
                    f"The model has been unreachable for {failures} turns. "
                    f'Their last message: "{message[:200]}"'
                ),
            },
        )
        return AgentTurn(
            reply=PROVIDER_DOWN_REPLY,
            provider_error=str(exc)[:200],
            escalated=True,
        )

    @staticmethod
    def _clear_provider_failures(ctx: ToolContext) -> None:
        """A turn got through. Reset the counter so an outage next week does not
        inherit a count from this one."""
        state = ctx.load_state()
        if state.consecutive_provider_failures:
            state.consecutive_provider_failures = 0
            ctx.save_state(state)

    def _absorb_extraction(
        self, ctx: ToolContext, pending: _Pending, message: str, state: Any
    ) -> Any:
        """Merge a finished extraction into state, acting on safety in code.

        Not remembering to escalate is the most expensive mistake this system
        can make, so the signal is executed here rather than left to the model
        to notice.
        """
        extracted = pending.join()
        state = extraction_mod.merge(state, extracted, ctx.engine.tz)
        ctx.save_state(state)

        if extracted.escalation_signal in extraction_mod.HIGH_SEVERITY_SIGNALS:
            execute_tool(
                ctx,
                "escalate_conversation",
                {"reason": extracted.escalation_signal, "detail": message},
            )
            state = ctx.load_state()
        return state

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
        pending: "_Pending | None" = None,
        message: str = "",
    ) -> AgentTurn:
        engine = ctx.engine
        pending = pending or _Pending()
        working = self._history(ctx)

        def state_block(current: Any) -> dict[str, Any]:
            return {
                "role": "system",
                "content": render_state(
                    current,
                    now=now,
                    customer=customer if "error" not in customer else None,
                    active_reservation=active_reservation,
                    lessons=self._lessons(ctx),
                    directive=directive,
                    live_quote=self._live_quote(ctx, current),
                ),
            }

        #: The state block is rewritten in place once extraction lands, so the
        #: reply is written against current knowledge even though the call that
        #: produced it started before extraction finished.
        state_index = len(working)
        working.append(state_block(state))

        called: list[str] = []
        succeeded: list[str] = []
        iterations = 0
        escalated_late = False

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

            # Join the extraction that has been running alongside this call. It
            # cost nothing in wall-clock time, and everything after this point
            # sees the merged state.
            if pending.outstanding:
                was_escalated = state.escalated
                state = self._absorb_extraction(ctx, pending, message, state)
                working[state_index] = state_block(state)

                if state.escalated and not was_escalated:
                    # The reply in hand was composed without knowing the
                    # customer had just reported an accident. Throw it away and
                    # answer again, now that the state block says so. One extra
                    # call, on the rarest and highest-stakes turn there is.
                    escalated_late = True
                    continue

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
