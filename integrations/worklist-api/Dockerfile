# Worklist API image — serves OHIF a sorted reading worklist (joins Orthanc + priority + assignment).
# Build from the REPO ROOT context (docker-compose sets `context: .` + this dockerfile), so it can
# COPY the shared lib alongside the API dir:
#   docker build -f integrations/worklist-api/Dockerfile -t lhrad-worklist-api .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the (slow) pip install caches across API code changes. Worklist API
# is a plain FastAPI service (not an A2A agent), so it does NOT need the [a2a] extra — just fastapi
# + uvicorn on top of the shared radagent_common client stubs.
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common" fastapi "pydantic>=2" uvicorn

COPY integrations/worklist-api/ ./integrations/worklist-api/

# store.py + assignment.py sit next to main.py; PYTHONPATH lets `from store import ...` resolve.
ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app/integrations/worklist-api \
    PYTHONUNBUFFERED=1 \
    WORKLIST_STORE_PATH=/var/lib/lhrad/worklist.sqlite
WORKDIR /app/integrations/worklist-api
EXPOSE 8107
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8107"]
