"""The Desk's hosted MCP server, at https://<host>/mcp/<code> (ashigaru-3.md, "The Desk").

It needs fastmcp, the `mcp` extra. The image has it; the RPM Desk does not, and
serves no /mcp.
"""

import logging
from pathlib import Path
from typing import Any

from starlette.routing import Route
from starlette.types import Lifespan


def install(db_path: str | Path) -> tuple[Lifespan[Any] | None, Route | None]:
    """The MCP app's lifespan and route for one Desk app, or (None, None) without fastmcp."""
    try:
        from . import server
    except ModuleNotFoundError as error:
        if (error.name or "").partition(".")[0] != "fastmcp":
            raise
        logging.getLogger(__name__).info("no hosted MCP: %s (the `mcp` extra is not installed)", error)
        return None, None
    return server.install(db_path)
