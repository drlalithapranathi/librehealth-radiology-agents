# A2A agent image — Interpretation Assistant. Build from the REPO ROOT context (docker-compose
# sets `context: .` + this dockerfile), so it can COPY the shared lib, contracts, and agent dir:
#   docker build -f agents/interpretation-assistant/Dockerfile -t lhrad-interpretation-assistant .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the (slow) pip install caches across agent-code changes. Agents
# serve an A2A server, so they need the [a2a] extra; + uvicorn to run it.
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common[a2a]" uvicorn

# The A2A factory reads the agent card from /contracts at runtime (load_card); LHRAD_REPO_ROOT
# points paths.repo_root() at it. The agent dir is on PYTHONPATH so `server`/`handler`/`registry`
# import.
COPY contracts/ ./contracts/
COPY agents/interpretation-assistant/ ./agents/interpretation-assistant/

ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app/agents/interpretation-assistant \
    PYTHONUNBUFFERED=1
WORKDIR /app/agents/interpretation-assistant
EXPOSE 8103
CMD ["uvicorn", "server:asgi_app", "--host", "0.0.0.0", "--port", "8103"]
