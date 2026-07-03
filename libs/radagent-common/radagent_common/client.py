"""A2A client helper used by the orchestrator — the client-side counterpart to the server
factory in `a2a.py`. It isolates all client-side `a2a.*` plumbing so the orchestrator's
activities call ONE function and get back the agent's JSON output (already contract-shaped
by the agent). The orchestrator never builds raw JSON-RPC.

Pinned target: a2a-sdk 1.0.3 (the protobuf/gRPC rewrite). Things that bite here:
  * There is no `A2AClient` in 1.0.x — build a `Client` from `ClientFactory(ClientConfig)`.
  * The protobuf `AgentCard` has no top-level `url`; endpoints live in `supported_interfaces`.
    Our contract cards predate that, so a resolved card carries no interface. We already know
    where to send (the caller passes base_url), so we pin a JSON-RPC interface at base_url
    before creating the client. (TODO(#8 follow-up): carry the endpoint on the card contract.)
  * Payloads ride as a typed **DataPart** (`new_data_message` / `get_data_parts`), matching the
    server. Caveat: protobuf `Struct` coerces numbers to float (an int `50` returns as `50.0`);
    our Draft 2020-12 contracts accept that as `integer`.
  * `send_message` is a streaming async-iterator; a skill call is a single request/reply, so we
    run it unary (`streaming=False`). In unary mode the SDK yields either a `message` or a
    `task` reply, so we read data parts from both.
"""
from __future__ import annotations
from typing import Any
import httpx

from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import new_data_message, get_data_parts
from a2a.types import AgentInterface, Message, SendMessageRequest, StreamResponse
from a2a.utils.constants import TransportProtocol, AGENT_CARD_WELL_KNOWN_PATH as WELL_KNOWN_CARD_PATH


async def call_agent_skill(base_url: str, skill_id: str, payload: dict[str, Any], timeout: float = 30.0) -> dict:
    """Send a skill invocation to a live A2A agent and return its JSON output.

    Wire convention matches radagent_common.a2a (server side): the message carries a single
    data part `envelope(skill_id, payload)`; the reply carries the agent's output as a data part.
    """
    send_url = base_url.rstrip("/") + "/"
    hx = httpx.AsyncClient(timeout=timeout)
    try:
        card = await A2ACardResolver(hx, base_url, agent_card_path=WELL_KNOWN_CARD_PATH).get_agent_card()
        card.supported_interfaces.append(
            AgentInterface(url=send_url, protocol_binding=TransportProtocol.JSONRPC)
        )
        client = ClientFactory(ClientConfig(httpx_client=hx, streaming=False)).create(card)
        request = SendMessageRequest(message=skill_message(skill_id, payload))
        results: list[dict] = []
        async for response in client.send_message(request):
            results.extend(_response_data_parts(response))
    finally:
        await hx.aclose()  # the client's transport uses hx, so this closes it too

    if not results:
        raise ValueError(f"Empty A2A reply from {base_url} for skill {skill_id!r}")
    return results[0]


def _response_data_parts(response: StreamResponse) -> list[dict]:
    """Data-part values on a unary reply. With streaming=False the SDK yields either a `message`
    or a `task` (whose result lives in artifacts) — read data parts from both."""
    if response.HasField("message"):
        return get_data_parts(response.message.parts)
    if response.HasField("task"):
        return [d for artifact in response.task.artifacts for d in get_data_parts(artifact.parts)]
    return []


def skill_message(skill_id: str, payload: dict[str, Any]) -> Message:
    """The A2A message the client sends: one typed data part carrying {skillId, payload}."""
    return new_data_message(envelope(skill_id, payload))


def envelope(skill_id: str, payload: dict[str, Any]) -> dict:
    """The value we put on the A2A data part: {skillId, payload}. Shared by client and tests."""
    return {"skillId": skill_id, "payload": payload}
