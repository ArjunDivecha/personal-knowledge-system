"""Dream judge — Mac-side counterpart to the Worker judge queue.

Reads pending border-case ops from the Cloudflare Worker, asks Opus to
decide each one (via the Claude CLI for subscription billing, falling
back to the Anthropic API), and posts verdicts back. See run.py.
"""
