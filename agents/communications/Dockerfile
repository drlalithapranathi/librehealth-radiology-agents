# A2A agent image — Communications Agent (CritCom, #52/#69). Build from the REPO ROOT context
# (docker-compose sets `context: .` + this dockerfile), so it can COPY the shared lib, contracts,
# and agent dir:
#   docker build -f agents/communications/Dockerfile -t lhrad-communications .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the (slow) pip install caches across agent-code changes. Agents
# serve an A2A server, so they need the [a2a] extra; + uvicorn to run it. Both stores are reached
# through the shared lib (fhir2 read-only via fhir_client, the comms ledger via comms_ledger), so
# no extra dependencies beyond the sibling agents'.
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common[a2a,otel]" uvicorn

# The A2A factory reads the agent card from /contracts at runtime (load_card); LHRAD_REPO_ROOT
# points paths.repo_root() at it. The agent dir is on PYTHONPATH so `server`/`handler`/`tools`/
# `classifier` import.
COPY contracts/ ./contracts/
COPY agents/communications/ ./agents/communications/

ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app/agents/communications \
    PYTHONUNBUFFERED=1
WORKDIR /app/agents/communications
EXPOSE 8106
CMD ["uvicorn", "server:asgi_app", "--host", "0.0.0.0", "--port", "8106"]
