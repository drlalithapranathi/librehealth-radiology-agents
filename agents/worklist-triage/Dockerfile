# A2A agent image — Worklist Triage. Build from the REPO ROOT context (docker-compose sets
# `context: .` + this dockerfile), so it can COPY the shared lib, contracts, and the agent dir:
#   docker build -f agents/worklist-triage/Dockerfile -t lhrad-worklist-triage .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the (slow) pip install caches across agent-code changes. Agents
# serve an A2A server, so they need the [a2a] extra; + uvicorn to run it.
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common[a2a]" uvicorn

# The A2A factory reads the agent card from /contracts at runtime (load_card); LHRAD_REPO_ROOT
# points paths.repo_root() at it. The agent dir is on PYTHONPATH so `server`/`handler` import.
COPY contracts/ ./contracts/
COPY agents/worklist-triage/ ./agents/worklist-triage/

ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app/agents/worklist-triage \
    PYTHONUNBUFFERED=1
WORKDIR /app/agents/worklist-triage
EXPOSE 8101
CMD ["uvicorn", "server:asgi_app", "--host", "0.0.0.0", "--port", "8101"]
