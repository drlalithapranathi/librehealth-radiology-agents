"""A2A factory — the ONLY module that imports `a2a.*`.

Why this exists
---------------
The official `a2a-sdk` is young and moved class names + the well-known path between
0.3 and 1.0. To keep four developers productive and the blast radius of an SDK bump
to ONE file, all protocol specifics live here. An agent author writes:

    async def handle(skill_id: str, payload: dict) -> dict: ...

and calls `build_agent_app("worklist-triage", handle)`. They never touch a2a types.

Pinned target: a2a-sdk 1.0 (AgentExecutor / DefaultRequestHandler / A2AStarletteApplication).
TODO(M1): once the version is pinned in a real venv, replace the defensive text-part
payload handling below with typed DataPart in/out and verify the well-known path
(`/.well-known/agent-card.json` in 1.0 vs `/.well-known/agent.json` in 0.3).
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable

from . import paths
from .validation import validate_skill_output, ContractError

# --- SDK imports kept in one place ------------------------------------------------
from a2a.types import AgentCard  # type: ignore
from a2a.server.agent_execution import AgentExecutor, RequestContext  # type: ignore
from a2a.server.events import EventQueue  # type: ignore
from a2a.server.request_handlers import DefaultRequestHandler  # type: ignore
from a2a.server.tasks import InMemoryTaskStore  # type: ignore
from a2a.server.apps import A2AStarletteApplication  # type: ignore
from a2a.utils import new_agent_text_message  # type: ignore

SkillHandler = Callable[[str, dict], Awaitable[dict]]


def load_card(agent_dir_name: str) -> AgentCard:
    """Load the canonical Agent Card JSON from /contracts/cards and build an AgentCard."""
    with paths.card_path(agent_dir_name).open() as f:
        data = json.load(f)
    # AgentCard is a pydantic model in the SDK; construct from the contract JSON.
    return AgentCard.model_validate(data)


def _extract_payload(context: RequestContext) -> tuple[str, dict]:
    """Pull {skillId, payload} out of the incoming message.

    Convention: the orchestrator sends a single part whose text is JSON
    `{"skillId": "...", "payload": {...}}`. We read text defensively so this keeps
    working across SDK part-type changes. TODO(M1): prefer typed DataPart.
    """
    message = getattr(context, "message", None)
    raw = None
    for part in getattr(message, "parts", []) or []:
        # part may expose .root.text (1.0) or .text (0.3); try both.
        text = getattr(getattr(part, "root", part), "text", None)
        if text:
            raw = text
            break
    if raw is None:
        raise ContractError("No JSON payload found on incoming A2A message.")
    obj = json.loads(raw)
    return obj.get("skillId", ""), obj.get("payload", {})


class _SkillExecutor(AgentExecutor):
    """Generic executor: decode -> call handler -> validate output -> emit JSON."""

    def __init__(self, handler: SkillHandler):
        self._handler = handler

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        skill_id, payload = _extract_payload(context)
        result = await self._handler(skill_id, payload)
        # Enforce the inter-agent contract before it ever leaves this process.
        validate_skill_output(skill_id, result)
        await event_queue.enqueue_event(new_agent_text_message(json.dumps(result)))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # No long-running work in v1 stubs. TODO(M1): cooperative cancel for real tools.
        raise NotImplementedError("cancel not supported in v1")


def build_agent_app(agent_dir_name: str, handler: SkillHandler):
    """Return a runnable ASGI app for an agent. Run with: uvicorn ... or app.build()."""
    card = load_card(agent_dir_name)
    request_handler = DefaultRequestHandler(
        agent_executor=_SkillExecutor(handler),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(agent_card=card, http_handler=request_handler)
