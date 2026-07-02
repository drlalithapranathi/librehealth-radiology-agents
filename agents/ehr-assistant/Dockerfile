# A2A agent image — EHR Assistant. Build from the REPO ROOT context (docker-compose sets
# `context: .` + this dockerfile), so it can COPY the shared lib, contracts, and the agent dir:
#   docker build -f agents/ehr-assistant/Dockerfile -t lhrad-ehr-assistant .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the (slow) pip install caches across agent-code changes. Agents
# serve an A2A server, so they need the [a2a] extra; + uvicorn to run it.
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common[a2a]" uvicorn

# The A2A factory reads the agent card from /contracts at runtime (load_card); LHRAD_REPO_ROOT
# points paths.repo_root() at it. The agent dir is on PYTHONPATH so `server`/`handler` import.
COPY contracts/ ./contracts/
COPY agents/ehr-assistant/ ./agents/ehr-assistant/

ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app/agents/ehr-assistant \
    PYTHONUNBUFFERED=1
WORKDIR /app/agents/ehr-assistant
EXPOSE 8102
CMD ["uvicorn", "server:asgi_app", "--host", "0.0.0.0", "--port", "8102"]
