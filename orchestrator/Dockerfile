# Orchestrator image — Temporal worker + FastAPI ingress in one container. Build from the REPO
# ROOT context (docker-compose sets `context: .` + this dockerfile), so it can COPY the shared
# lib, contracts, and the orchestrator package:
#   docker build -f orchestrator/Dockerfile -t lhrad-orchestrator .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the pip install caches across orchestrator-code changes. The
# orchestrator is the A2A client (radagent_common.client -> [a2a]) and runs on Temporal + FastAPI.
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common[a2a,otel]" "temporalio==1.29.0" fastapi uvicorn

# Contracts are read at runtime (event validation, agent cards); LHRAD_REPO_ROOT points at them.
COPY contracts/ ./contracts/
COPY orchestrator/ ./orchestrator/

ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1
EXPOSE 8090
# One container runs the durable worker AND the ingress API (compose overrides via `command`).
# `wait -n` exits as soon as EITHER process dies so a restart policy can recover the container
# instead of it looking healthy with a dead worker.
CMD ["bash", "-c", "python -m orchestrator.worker & uvicorn orchestrator.ingress:app --host 0.0.0.0 --port 8090 & wait -n"]
