"""A2A server for the communications agent. All protocol plumbing lives in radagent_common.a2a."""
import uvicorn
from radagent_common.a2a import build_agent_app
from handler import handle

# build_agent_app(...).build() returns the Starlette ASGI app (card at /.well-known + JSON-RPC at /).
asgi_app = build_agent_app("communications", handle).build()

if __name__ == "__main__":
    uvicorn.run(asgi_app, host="0.0.0.0", port=8106)
