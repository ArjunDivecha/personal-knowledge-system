"""
=============================================================================
EXTRACTION PROMPT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
LLM prompt template for extracting knowledge and project entries
from conversations. Requires evidence for every insight.
=============================================================================
"""

from models import NormalizedConversation


EXTRACTION_PROMPT = '''You are extracting knowledge entries from a conversation between a user and an AI assistant. Your extractions must include evidence linking back to specific messages.

## User Context
Investment analyst focused on systematic trading strategies. Building AI tools for trading and research workflows. Areas of interest include volatility trading, MLX fine-tuning, agentic coding, and personal knowledge systems.

## Task
Analyze this conversation and extract structured knowledge. For EVERY insight, decision, or finding, you MUST provide evidence pointing to the specific message(s) that support it.

## Output Format
Return valid JSON matching this exact schema:

```json
{
  "knowledge_entries": [
    {
      "domain": "specific topic area (e.g., 'MLX layer selection' not 'machine learning')",
      "current_view": "1-3 sentences describing what the user now thinks/knows",
      "confidence": "high|medium|low",
      "key_insights": [
        {
          "insight": "specific learning or conclusion",
          "evidence": {
            "message_ids": ["msg_id1", "msg_id2"],
            "snippet": "key quote from the message, max 200 chars"
          }
        }
      ],
      "knows_how_to": [
        {
          "capability": "practical skill demonstrated",
          "evidence": {
            "message_ids": ["msg_id"],
            "snippet": "optional supporting quote"
          }
        }
      ],
      "open_questions": [
        {
          "question": "unresolved question from the conversation",
          "evidence": {
            "message_ids": ["msg_id"]
          }
        }
      ],
      "repo_mentions": ["any GitHub repos or code paths mentioned"]
    }
  ],
  "project_entries": [
    {
      "name": "project name (use explicit name if mentioned)",
      "goal": "what the user is trying to achieve",
      "current_phase": "where they are in the work",
      "decisions_made": [
        {
          "decision": "specific choice made",
          "rationale": "why, if stated",
          "evidence": {
            "message_ids": ["msg_id"],
            "snippet": "key quote"
          }
        }
      ],
      "blocked_on": "what's stopping progress, or null",
      "tech_stack": ["technologies involved"],
      "repo_mentions": ["any GitHub repos mentioned"]
    }
  ]
}
```

## Critical Rules
1. EVERY insight and decision MUST have evidence with message_ids that exist in this conversation
2. Snippets should be DIRECT QUOTES from messages, max 200 characters
3. If you cannot find evidence for a claim, DO NOT include that claim
4. Be SPECIFIC in domain naming - prefer "React state management" over "React"
5. Return empty arrays if no extractable knowledge exists
6. Only extract substantive knowledge - skip trivial facts or obvious information
7. For projects, only create an entry if there's a clear ongoing project discussed

## Conversation
Messages are formatted as:
[MESSAGE_ID] ROLE: CONTENT

{conversation}

## Response
Return ONLY the JSON object, no additional text.'''


def format_conversation_for_extraction(conversation: NormalizedConversation) -> str:
    """
    Format a conversation for the extraction prompt.
    
    Args:
        conversation: The normalized conversation
    
    Returns:
        Formatted string with [message_id] role: content format
    """
    lines = []
    
    for msg in conversation.messages:
        # Truncate very long messages to avoid token explosion
        content = msg.content
        if len(content) > 3000:
            content = content[:2900] + "\n... [truncated] ..."
        
        lines.append(f"[{msg.message_id}] {msg.role.upper()}: {content}")
    
    return "\n\n".join(lines)


def build_extraction_prompt(conversation: NormalizedConversation) -> str:
    """
    Build the complete extraction prompt for a conversation.
    
    Args:
        conversation: The normalized conversation
    
    Returns:
        Complete prompt string
    """
    formatted_conv = format_conversation_for_extraction(conversation)
    return EXTRACTION_PROMPT.replace("{conversation}", formatted_conv)

