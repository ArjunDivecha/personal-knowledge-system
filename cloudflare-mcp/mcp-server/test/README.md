# Worker Test Suite

This directory is reserved for Worker runtime tests.

The intended scope is:

- `/health`
- OAuth and registration edges
- MCP initialize/list/call flows
- search filtering and tier behavior
- operator endpoint authorization
- Dream dry-run operator calls in staging

These tests should target isolated staging or local Worker environments, not production by default.
