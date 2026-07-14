# A2A agent image — Interpretation Assistant. Build from the REPO ROOT context (docker-compose
# sets `context: .` + this dockerfile), so it can COPY the shared lib, contracts, and agent dir:
#   docker build -f agents/interpretation-assistant/Dockerfile -t lhrad-interpretation-assistant .
FROM python:3.11-slim

WORKDIR /app

# Shared lib in its own layer so the (slow) pip install caches across agent-code changes. Agents
# serve an A2A server, so they need the [a2a] extra; + uvicorn to run it. [imaging] adds
# pydicom/numpy for radagent_common.imaging -- this is the one agent that reads pixels (#27).
COPY libs/ ./libs/
RUN pip install --no-cache-dir -e "libs/radagent-common[a2a,otel,imaging]" uvicorn

# The CXR screening model (#27). CPU-only torch: this agent classifies one 224x224 frame per study
# in well under a second, and a CUDA build would add gigabytes for nothing. Pulling from the CPU
# index rather than PyPI is what keeps the image ~1.5GB instead of ~4GB.
#
# Its own layer, before the agent code is copied, so that editing handler.py does not re-download
# torch on every rebuild.
# torch AND torchvision must come from the SAME index, in one resolve. torchxrayvision depends on
# torchvision, so installing only torch here lets pip satisfy that dep from PyPI -- and a torchvision
# wheel built against a different torch fails at IMPORT with
#   RuntimeError: operator torchvision::nms does not exist
# which is an ABI mismatch, not a missing package, and no amount of reinstalling torch fixes it.
# Pinning both to the cpu index up front means the later torchxrayvision install finds torchvision
# already satisfied and leaves it alone.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir torchxrayvision

# BAKE THE WEIGHTS. torchxrayvision lazily downloads to ~/.torchxrayvision on first use -- which
# would mean the first study of a demo (or of a deployment behind a firewall) blocks on a GitHub
# release download, and fails outright if there is no egress. Fetch them at BUILD time so the
# running agent never needs the network to score an image.
RUN python -c "import torchxrayvision as xrv; xrv.models.DenseNet(weights='densenet121-res224-all')" \
 && echo "cxr weights baked"

# The A2A factory reads the agent card from /contracts at runtime (load_card); LHRAD_REPO_ROOT
# points paths.repo_root() at it. The agent dir is on PYTHONPATH so `server`/`handler`/`registry`
# import.
COPY contracts/ ./contracts/
COPY agents/interpretation-assistant/ ./agents/interpretation-assistant/

# HOME is pinned because torchxrayvision resolves its weights cache against it -- the layer above
# baked them into /root/.torchxrayvision, and the running agent must find them there.
ENV LHRAD_REPO_ROOT=/app \
    PYTHONPATH=/app/agents/interpretation-assistant \
    PYTHONUNBUFFERED=1 \
    HOME=/root
WORKDIR /app/agents/interpretation-assistant
EXPOSE 8103
CMD ["uvicorn", "server:asgi_app", "--host", "0.0.0.0", "--port", "8103"]
