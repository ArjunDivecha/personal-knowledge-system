# Worker Test Suite

This directory contains Worker-runtime tests that run inside Cloudflare's `workerd` runtime via the Workers Vitest pool.

Current scope:

- `/health`
- OAuth and registration edges
- MCP initialize/list/call flows
- scheduled Dream trigger behavior
- operator endpoint authorization
- basic transport validation for MCP over HTTP

Design rules:

- use local Worker-runtime tests here for deterministic transport and route coverage
- use [scripts/run_e2e_staging.py](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/run_e2e_staging.py) for full staging smoke
- keep production out of this directory; production should only be touched by explicit canaries
