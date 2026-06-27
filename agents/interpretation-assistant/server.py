"""A2A server for the interpretation-assistant agent. All protocol plumbing lives in radagent_common.a2a."""
import uvicorn
from radagent_common.a2a import build_agent_app
from handler import handle

# A2AStarletteApplication; .build() returns the Starlette ASGI app.
asgi_app = build_agent_app("interpretation-assistant", handle).build()

if __name__ == "__main__":
    uvicorn.run(asgi_app, host="0.0.0.0", port=8103)
