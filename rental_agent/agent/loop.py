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
from dataclasses import dataclass, field
from typing import Any

from ..context import ToolContext
from ..domain.enums import Stage
from ..tools.registry import execute_tool
from . import extraction as extraction_mod
from .prompt import build_system, render_state
from .schemas import TOOLS
from .settings import FALLBACK_BETA, AgentSettings

#: Sent when the model declines or the turn fails outright. The customer must
#: never be left with silence.
SAFE_FALLBACK_REPLY = (
    "Sorry — I'm having trouble with that one. Let me get a colleague to pick it up "
    "with you."
)


@dataclass
class AgentTurn:
    reply: str
    tool_calls: list[str] = field(default_factory=list)
    duplicate: bool = False
    escalated: bool = False
    refusal: bool = False
    iterations: int = 0
    stop_reason: str | None = None


class Agent:
    def __init__(self, client: Any, settings: AgentSettings | None = None):
        self.client = client
        self.settings = settings or AgentSettings()
        self._fallbacks_supported = self.settings.use_fallbacks

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

        # 1. Extraction — additive merge into state.
        if self.settings.extraction_enabled:
            extracted = extraction_mod.extract(
                self.client,
                message=message,
                state=state,
                now=now,
                active_reservation=active_reservation,
                model=self.settings.extraction_model,
                effort=self.settings.extraction_effort,
                max_tokens=self.settings.extraction_max_tokens,
            )
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

        # 3. Conversational turn.
        turn = self._run_tool_loop(ctx, state, now, customer, active_reservation)

        ctx.messages.record(
            conversation_id=ctx.conversation_id or "",
            direction="outbound",
            content=turn.reply,
            now=ctx.now(),
        )
        self._record_asked_slots(ctx, turn.reply)
        return turn

    # -- internals ------------------------------------------------------

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
                ),
            }
        )

        called: list[str] = []
        iterations = 0

        while iterations < self.settings.max_tool_iterations:
            iterations += 1
            response = self._create(
                model=self.settings.model,
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
        """Heuristic record of what the agent asked for, for the evaluator.

        Only meaningful when the reply actually contains a question; the
        evaluator uses it to detect the same slot being asked for twice.
        """
        if "?" not in reply:
            return
        state = ctx.load_state()
        for slot in state.missing_requirements():
            state.asked_slots.append(slot)
        ctx.save_state(state)


def build_client() -> Any:
    """Construct the Anthropic client.

    Credentials resolve from the environment (`ANTHROPIC_API_KEY`) or an
    `ant auth login` profile — the zero-argument constructor handles both.
    """
    import anthropic

    return anthropic.Anthropic()
