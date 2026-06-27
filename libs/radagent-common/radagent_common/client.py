"""A2A client helper used by the orchestrator. Isolated like the server factory.

The orchestrator never builds raw JSON-RPC: it calls `call_agent_skill(base_url, skill, payload)`
and gets back the agent's JSON output (already contract-shaped by the agent).

TODO(M1): implement against the pinned a2a-sdk client (a2a.client.A2AClient +
A2ACardResolver). For M0 this raises clearly so nothing silently no-ops; the walking
skeleton (mocks/) exercises handlers in-process instead.
"""
from __future__ import annotations
import json
from typing import Any
import httpx


async def call_agent_skill(base_url: str, skill_id: str, payload: dict[str, Any], timeout: float = 30.0) -> dict:
    """Send a skill invocation to an A2A agent and return its JSON output.

    Wire convention matches radagent_common.a2a._extract_payload:
        message part text = json.dumps({"skillId": skill_id, "payload": payload})
    """
    # TODO(M1): replace this stub with the official a2a-sdk client flow:
    #   resolver = A2ACardResolver(httpx_client, base_url)
    #   card = await resolver.get_agent_card()
    #   client = A2AClient(httpx_client, agent_card=card)
    #   resp = await client.send_message(SendMessageRequest(... DataPart(payload) ...))
    #   return _parse_first_json_part(resp)
    raise NotImplementedError(
        "call_agent_skill is stubbed for M0. Implement with a2a-sdk client in M1. "
        f"(base_url={base_url}, skill_id={skill_id})"
    )


def envelope(skill_id: str, payload: dict[str, Any]) -> str:
    """The exact text we put on the A2A message part. Shared by client, mocks and tests."""
    return json.dumps({"skillId": skill_id, "payload": payload})
