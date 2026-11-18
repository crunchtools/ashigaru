"""The REST side of hosted MCP: generate, list and revoke an identity's MCP URLs.

Same authority as API keys, through the same functions: an admin mints, an admin
or the owner lists and revokes. The URL is in the mint response and nowhere else.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .. import store
from ..api import Admin, Conn, Who, _found, errors, self_or_admin
from . import codes


class McpCode(BaseModel):
    code_id: str
    identity: str
    label: str | None
    created_at: str
    revoked_at: str | None
    last_used_at: str | None


class McpCodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None


class MintedMcpCode(McpCode):
    url: str = Field(
        description="Shown once. The MCP server URL, ending in the secret code; it is the credential."
    )


def url_for(request: Request, code: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/mcp/{code}"


router = APIRouter(tags=["mcp"])


@router.get("/v1/identities/{name}/mcp", responses=errors(401, 403))
def list_codes(conn: Conn, who: Who, name: str) -> list[McpCode]:
    """Code metadata only; codes are never stored."""
    self_or_admin(who, name)
    return [McpCode.model_validate(row) for row in codes.list_codes(conn, name)]


@router.post(
    "/v1/identities/{name}/mcp", status_code=status.HTTP_201_CREATED, responses=errors(401, 403, 404, 409)
)
def mint_code(conn: Conn, who: Admin, request: Request, name: str, body: McpCodeCreate) -> MintedMcpCode:
    """Admin only. Whoever holds the URL acts as `name`, through the same authority as its API key.
    409 where the Desk serves no MCP (installed without the `mcp` extra, as the RPM is)."""
    if not request.app.state.mcp:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this Desk serves no MCP: it was installed without the `mcp` extra"
        )
    _found(store.get_identity(conn, name), "identity")
    code_id, code = codes.mint(conn, name, body.label)
    row: dict[str, Any] = _found(codes.get(conn, code_id), "code")
    return MintedMcpCode.model_validate({**row, "url": url_for(request, code)})


@router.delete("/v1/identities/{name}/mcp/{code_id}", responses=errors(401, 403, 404))
def revoke_code(conn: Conn, who: Who, name: str, code_id: str) -> McpCode:
    """An admin revokes any code; an identity its own. Takes effect on the next MCP request."""
    self_or_admin(who, name)
    row = codes.get(conn, code_id)
    if row is None or row["identity"] != name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such code")
    codes.revoke(conn, code_id)
    return McpCode.model_validate(codes.get(conn, code_id))
