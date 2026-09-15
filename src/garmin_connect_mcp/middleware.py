"""Middleware for Garmin Connect MCP server.

This module provides middleware components that run before tool execution.
"""

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .auth import load_config, validate_credentials
from .client import (
    GarminAuthenticationError,
    GarminClientWrapper,
    clear_cached_client,
    init_garmin_client,
)


class ConfigMiddleware(Middleware):
    """Middleware that initializes Garmin client for all tool calls.

    This middleware:
    1. Loads the Garmin config from environment variables
    2. Validates that credentials are properly configured
    3. Initializes the Garmin client
    4. Injects the client into the context state for tools to access via ctx.get_state("client")
    5. Raises ToolError if authentication fails
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]):
        """Initialize Garmin client before every tool call."""
        # Load and validate configuration
        config = load_config()

        if not validate_credentials(config):
            raise ToolError(
                "Garmin credentials not configured. "
                "Please run 'garmin-connect-mcp-auth' to set up authentication."
            )

        # Get the cached Garmin client (authenticated once, reused across calls).
        client = init_garmin_client(config)
        if client is None:
            raise ToolError(
                "Garmin client unavailable. This is usually a transient error "
                "(rate limit or network) — wait a moment and retry. If it persists, "
                "run 'garmin-connect-mcp-auth' in a terminal to re-authenticate."
            )

        client_wrapper = GarminClientWrapper(client)

        # Inject client into context state for tools to access
        if context.fastmcp_context:
            context.fastmcp_context.set_state("client", client_wrapper)

        # Continue to the tool execution. If the session went genuinely stale
        # mid-call, drop the cache so the next call rebuilds it from disk.
        try:
            return await call_next(context)
        except GarminAuthenticationError:
            clear_cached_client()
            raise
