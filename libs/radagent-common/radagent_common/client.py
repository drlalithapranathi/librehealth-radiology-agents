"""A2A client helper used by the orchestrator — the client-side counterpart to the server
factory in `a2a.py`. It isolates all client-side `a2a.*` plumbing so the orchestrator's
activities call ONE function and get back the agent's JSON output (already contract-shaped
by the agent). The orchestrator never builds raw JSON-RPC.

Pinned target: a2a-sdk 1.0.3 (the protobuf/gRPC rewrite). Things that bite here:
  * There is no `A2AClient` in 1.0.x — build a `Client` from `ClientFactory(ClientConfig)`.
  * The protobuf `AgentCard` has no top-level `url`; endpoints live in `supported_interfaces`.
    Our contract cards predate that, so a resolved card carries no interface. We already know
    where to send (the caller passes base_url), so we pin a JSON-RPC interface at base_url
    before creating the client. (TODO(#8): carry the endpoint on the card contract itself.)
  * `send_message` is a streaming async-iterator; a skill call is a single request/reply, so
    we run it unary (`streaming=False`) and concatenate the text part(s) of the reply.
"""
from __future__ import annotations
import json
from typing import Any
import httpx

from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import new_text_message, get_stream_response_text
from a2a.types import AgentInterface, SendMessageRequest
from a2a.utils.constants import TransportProtocol


async def call_agent_skill(base_url: str, skill_id: str, payload: dict[str, Any], timeout: float = 30.0) -> dict:
    """Send a skill invocation to a live A2A agent and return its JSON output.

    Wire convention matches radagent_common.a2a (server side): the message is a single text
    part carrying `envelope(skill_id, payload)`; the reply is a text part carrying the agent's
    JSON output.
    """
    send_url = base_url.rstrip("/") + "/"
    hx = httpx.AsyncClient(timeout=timeout)
    try:
        card = await A2ACardResolver(hx, base_url).get_agent_card()
        card.supported_interfaces.append(
            AgentInterface(url=send_url, protocol_binding=TransportProtocol.JSONRPC)
        )
        client = ClientFactory(ClientConfig(httpx_client=hx, streaming=False)).create(card)
        request = SendMessageRequest(message=new_text_message(envelope(skill_id, payload)))
        parts = [get_stream_response_text(resp) async for resp in client.send_message(request)]
    finally:
        await hx.aclose()  # the client's transport uses hx, so this closes it too

    raw = "".join(p for p in parts if p)
    if not raw:
        raise ValueError(f"Empty A2A reply from {base_url} for skill {skill_id!r}")
    return json.loads(raw)


def envelope(skill_id: str, payload: dict[str, Any]) -> str:
    """The exact text we put on the A2A message part. Shared by client, mocks and tests."""
    return json.dumps({"skillId": skill_id, "payload": payload})
