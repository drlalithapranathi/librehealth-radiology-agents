"""A2A server factory — isolates the *server-side* `a2a.*` plumbing. Together with its
client-side counterpart `client.py`, these are the only two modules that import `a2a.*`;
agent handlers never do.

Why this exists
---------------
The official `a2a-sdk` is young and churns hard: it went from a pydantic + Starlette API
(0.2/0.3) to a **protobuf/gRPC-based** rewrite in 1.0. To keep four developers productive
and the blast radius of an SDK bump to these two isolated modules, all server-side protocol
specifics live here. An agent author writes:

    async def handle(skill_id: str, payload: dict) -> dict: ...

and calls `build_agent_app("worklist-triage", handle).build()` to get a runnable ASGI app.
They never touch a2a types, and this file's public surface (`build_agent_app(...).build()`)
stays stable across SDK bumps.

Pinned target: a2a-sdk[http-server] 1.0.3 (verified in a real venv). In 1.0.x:
  * `AgentCard` is a protobuf message (module `a2a_pb2`) — build it from the contract JSON
    with `google.protobuf.json_format.ParseDict`, NOT `AgentCard.model_validate`.
  * There is no `A2AStarletteApplication`. Serving is DIY: mount the route lists returned by
    `create_agent_card_routes` / `create_jsonrpc_routes` on a hand-rolled Starlette app.
  * `DefaultRequestHandler` now also requires the `agent_card`.
  * Payloads ride as a typed **DataPart** (`a2a.helpers.new_data_message` / `get_data_parts`),
    not JSON stuffed in a text part. Caveat: protobuf `Struct` coerces every number to float
    (an int `50` arrives as `50.0`); our Draft 2020-12 contracts accept that as `integer`. The
    integer fields are `priorityScore` (triage output) and `numberOfInstances` (in StudyContext,
    hence every skill's input); both arrive as integral floats and re-coerce to `int` when parsed
    through the typed `StudyContext` model (`radagent_common.context`).
  * The well-known card path is pinned explicitly to the SDK's canonical
    `AGENT_CARD_WELL_KNOWN_PATH` (see `WELL_KNOWN_CARD_PATH`) rather than relying on a default.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable

from google.protobuf import json_format

from . import paths
from .validation import validate_skill_output, ContractError

# --- SDK imports kept in one place ------------------------------------------------
from a2a.types import AgentCard  # protobuf message class in 1.0.x  # type: ignore
from a2a.server.agent_execution import AgentExecutor, RequestContext  # type: ignore
from a2a.server.events import EventQueue  # type: ignore
from a2a.server.request_handlers import DefaultRequestHandler  # type: ignore
from a2a.server.tasks import InMemoryTaskStore  # type: ignore
from a2a.server.routes.agent_card_routes import create_agent_card_routes  # type: ignore
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes  # type: ignore
from a2a.helpers import new_data_message, get_data_parts  # type: ignore
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH  # type: ignore
from starlette.applications import Starlette  # provided by a2a-sdk[http-server]

SkillHandler = Callable[[str, dict], Awaitable[dict]]

# Well-known Agent Card path, pinned to a2a-sdk 1.0.3's canonical value. Drift is caught by the
# exact `==1.0.3` SDK pin plus a literal-lock assertion in the tests; client.py resolves cards
# from the same SDK constant, and build_agent_app serves the card here explicitly.
WELL_KNOWN_CARD_PATH = AGENT_CARD_WELL_KNOWN_PATH  # "/.well-known/agent-card.json"


def load_card(agent_dir_name: str) -> AgentCard:
    """Load the canonical Agent Card JSON from /contracts/cards and build an AgentCard.

    In a2a-sdk 1.0.x `AgentCard` is a protobuf message, so we parse the contract JSON with
    ParseDict (contract keys are camelCase, matching the proto json_name fields).
    """
    with paths.card_path(agent_dir_name).open() as f:
        data = json.load(f)
    return json_format.ParseDict(data, AgentCard(), ignore_unknown_fields=True)


def _extract_payload(context: RequestContext) -> tuple[str, dict]:
    """Pull {skillId, payload} out of the incoming message's typed DataPart.

    Convention: the orchestrator (radagent_common.client) sends a single data part whose value
    is `{"skillId": "...", "payload": {...}}`.
    """
    parts = getattr(getattr(context, "message", None), "parts", None) or []
    data = get_data_parts(parts)
    if not data or not isinstance(data[0], dict):
        raise ContractError("No dict DataPart payload found on incoming A2A message.")
    obj = data[0]
    return obj.get("skillId", ""), obj.get("payload", {})


class _SkillExecutor(AgentExecutor):
    """Generic executor: decode -> call handler -> validate output -> emit DataPart."""

    def __init__(self, handler: SkillHandler):
        self._handler = handler

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        skill_id, payload = _extract_payload(context)
        result = await self._handler(skill_id, payload)
        # Enforce the inter-agent contract before it ever leaves this process.
        validate_skill_output(skill_id, result)
        await event_queue.enqueue_event(new_data_message(result))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # No long-running work in v1 stubs. TODO(M1): cooperative cancel for real tools.
        raise NotImplementedError("cancel not supported in v1")


class _AgentApp:
    """Thin handle exposing `.build() -> ASGI app`.

    Preserves the factory's public surface so agent `server.py` files (`build_agent_app(...).build()`)
    never change when the SDK does — the whole reason this module exists.
    """

    def __init__(self, app: Starlette):
        self._app = app

    def build(self) -> Starlette:
        return self._app


def build_agent_app(agent_dir_name: str, handler: SkillHandler) -> _AgentApp:
    """Return an agent app handle. `build_agent_app(name, handle).build()` is the ASGI app."""
    card = load_card(agent_dir_name)
    request_handler = DefaultRequestHandler(
        agent_executor=_SkillExecutor(handler),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    # 1.0.x has no A2AStarletteApplication: assemble the app from route lists ourselves.
    routes = create_agent_card_routes(
        agent_card=card, card_url=WELL_KNOWN_CARD_PATH
    ) + create_jsonrpc_routes(request_handler=request_handler, rpc_url="/")
    return _AgentApp(Starlette(routes=routes))
