"""
Pipeline stages for knowledge distillation.

Stages:
1. parse - Parse Claude and GPT exports into normalized conversations
2. filter - Score and filter conversations by value
3. extract - Use LLM to extract knowledge entries with evidence
4. merge - Merge new entries with existing, handle conflicts
5. compress - Compress old entries, archive full content
6. index - Write to Upstash Redis/Vector, generate thin index
"""

