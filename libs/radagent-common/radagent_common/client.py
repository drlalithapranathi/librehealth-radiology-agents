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
from typing import Any, Optional
import httpx

from google.protobuf import json_format

from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import new_data_message, get_data_parts
from a2a.types import (
    AgentInterface,
    Message,
    SendMessageConfiguration,
    SendMessageRequest,
    StreamResponse,
    TaskPushNotificationConfig,
    TaskState,
)
from a2a.utils.constants import TransportProtocol, AGENT_CARD_WELL_KNOWN_PATH as WELL_KNOWN_CARD_PATH

# The header BasePushNotificationSender adds to every callback POST when a token was configured.
PUSH_TOKEN_HEADER = "X-A2A-Notification-Token"


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


async def start_agent_skill(base_url: str, skill_id: str, payload: dict[str, Any],
                            callback_url: str, callback_token: str = "",
                            timeout: float = 30.0) -> str:
    """Fire a skill with a push-notification callback and return the A2A taskId (#24).

    The agent replies with a Task instead of a Message; its terminal result arrives later as a
    POST to `callback_url` (parse it with `parse_push_callback`), authenticated by
    `callback_token` in the X-A2A-Notification-Token header. Correlate on the returned taskId.
    With v1's instant stubs the callback lands almost immediately; the API is shaped for M3's
    long-running tools, where this returns as soon as the agent ACKs the work.
    """
    send_url = base_url.rstrip("/") + "/"
    hx = httpx.AsyncClient(timeout=timeout)
    try:
        card = await A2ACardResolver(hx, base_url, agent_card_path=WELL_KNOWN_CARD_PATH).get_agent_card()
        card.supported_interfaces.append(
            AgentInterface(url=send_url, protocol_binding=TransportProtocol.JSONRPC)
        )
        client = ClientFactory(ClientConfig(httpx_client=hx, streaming=False)).create(card)
        request = SendMessageRequest(
            message=skill_message(skill_id, payload),
            configuration=SendMessageConfiguration(
                task_push_notification_config=TaskPushNotificationConfig(
                    url=callback_url, token=callback_token,
                ),
                return_immediately=True,
            ),
        )
        async for response in client.send_message(request):
            if response.HasField("task"):
                return response.task.id
    finally:
        await hx.aclose()
    raise ValueError(f"No task reply from {base_url} for skill {skill_id!r} (push send)")


_TERMINAL_TASK_STATES = frozenset({
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
})


def parse_push_callback(body: dict) -> Optional[dict]:
    """Classify one push-notification POST body (the SDK's StreamResponse JSON, camelCase).

    The sender POSTs the task lifecycle as SEPARATE events — verified against the live 1.0.3
    flow: an initial `task` snapshot (SUBMITTED), a WORKING `statusUpdate`, the skill result as
    an `artifactUpdate`, then the terminal `statusUpdate`. So the result and the terminal state
    never share one POST, and the receiver buffers artifact parts until the terminal event.

    Returns:
      {"taskId", "kind": "artifact", "parts": [...]}            the result data rides here
      {"taskId", "kind": "terminal", "state", "parts": [...]}   COMPLETED/FAILED/... (parts only
                                                                in the task-snapshot form)
      None                                                      non-terminal noise — ack + ignore
    """
    response = json_format.ParseDict(body, StreamResponse(), ignore_unknown_fields=True)
    if response.HasField("artifact_update"):
        event = response.artifact_update
        return {"taskId": event.task_id, "kind": "artifact",
                "parts": get_data_parts(event.artifact.parts)}
    if response.HasField("status_update"):
        event = response.status_update
        state = TaskState.Name(event.status.state)
        if state in _TERMINAL_TASK_STATES:
            return {"taskId": event.task_id, "kind": "terminal", "state": state, "parts": []}
        return None
    if response.HasField("task"):
        state = TaskState.Name(response.task.status.state)
        if state in _TERMINAL_TASK_STATES:  # some senders emit a final full snapshot
            parts = [d for artifact in response.task.artifacts for d in get_data_parts(artifact.parts)]
            return {"taskId": response.task.id, "kind": "terminal", "state": state, "parts": parts}
        return None
    return None


def skill_message(skill_id: str, payload: dict[str, Any]) -> Message:
    """The A2A message the client sends: one typed data part carrying {skillId, payload}."""
    return new_data_message(envelope(skill_id, payload))


def envelope(skill_id: str, payload: dict[str, Any]) -> dict:
    """The value we put on the A2A data part: {skillId, payload}. Shared by client and tests."""
    return {"skillId": skill_id, "payload": payload}
