"""MCP endpoint for Kuzya: tools auto-generated from OpenAPI.

Public surface: :func:`attach_mcp_endpoint` mounts the Streamable HTTP MCP
server on ``/mcp`` of an existing FastAPI application and returns the
attachment whose lifecycle the app lifespan must drive.
"""

from __future__ import annotations

from kuzya.api.mcp.endpoint import McpEndpoint, attach_mcp_endpoint

__all__ = ["McpEndpoint", "attach_mcp_endpoint"]
