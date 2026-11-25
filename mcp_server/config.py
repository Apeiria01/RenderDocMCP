"""Configuration for RenderDoc MCP Server"""

import os


class Settings:
    """Server settings"""

    def __init__(self):
        self.renderdoc_host = os.environ.get("RENDERDOC_MCP_HOST", "127.0.0.1")
        self.renderdoc_port = int(os.environ.get("RENDERDOC_MCP_PORT", "19876"))
        # Claude's stdio client disconnects on any JSON-RPC line over 16 MB, so
        # refuse tool results whose estimated message exceeds this budget.
        self.max_response_bytes = int(
            os.environ.get("RENDERDOC_MCP_MAX_RESPONSE_BYTES", "15000000"))


settings = Settings()
