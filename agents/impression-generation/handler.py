"""Impression Generation handler — owner: Chaitra.

Timing-agnostic (the timing note in impression-generation/CLAUDE.md): same skill whether invoked post-sign (v1 safety-net)
or pre-sign (M2 assist).
Input  : { studyContext, report?, ehrContext?, aiFindings? }
Output : contracts/skills/impression.schema.json
"""
from __future__ import annotations
import json
import os
import google.genai as genai
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "impression.generate":
        raise ValueError(f"unexpected skill {skill_id}")

    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    ehr = payload.get("ehrContext") or {}

    modality = ctx["study"].get("modality", "unknown")
    description = ctx["study"].get("studyDescription", "")
    findings_text = report.get("findingsText", "No findings provided.")
    ehr_summary = ehr.get("summary", "No prior history available.")

    prompt = f"""You are a radiology AI assistant. Based on the information below, generate a structured radiology impression.

Study: {modality} — {description}
Radiologist findings: {findings_text}
Patient history: {ehr_summary}

Respond ONLY with a JSON object in this exact format, no markdown, no extra text:
{{
  "impressionText": "one paragraph impression summary",
  "recommendations": [
    {{"text": "follow-up recommendation"}}
  ],
  "criticalFlags": [
    {{"label": "critical finding name", "severity": "critical"}}
  ]
}}

If there are no recommendations, return an empty list.
If there are no critical findings, return an empty list for criticalFlags.
Only include criticalFlags for urgent/life-threatening findings."""

    response = client.models.generate_content(
        model="gemini-2.0-flash-lite",
        contents=prompt
    )
    raw = response.text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    parsed = json.loads(raw)

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "impressionText": parsed.get("impressionText", ""),
        "structuredFindings": [],
        "recommendations": parsed.get("recommendations", []),
        "criticalFlags": parsed.get("criticalFlags", []),
        "agentVersion": AGENT_VERSION,
        "generatedAt": now_iso(),
    }