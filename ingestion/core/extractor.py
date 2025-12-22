"""
=============================================================================
INGESTION PIPELINE - LLM EXTRACTOR
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Extract structured knowledge entries from raw content using Claude.
Used by GitHub and Gmail ingestion pipelines.

INPUT FILES:
- Raw content (README, code, emails, commits)

OUTPUT FILES:
- Structured knowledge entry dicts ready for storage
=============================================================================
"""

import json
import hashlib
from typing import Optional
from datetime import datetime

import anthropic

from .config import ANTHROPIC_API_KEY, EXTRACTION_MODEL


class Extractor:
    """
    LLM-based knowledge extractor.
    
    Takes raw content (code, emails, commits) and extracts structured
    knowledge entries that match the schema.
    """
    
    def __init__(self):
        """Initialize Anthropic client."""
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    def _generate_id(self, content: str, source_type: str) -> str:
        """Generate a unique ID for a knowledge entry."""
        hash_input = f"{source_type}:{content[:500]}"
        hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
        return f"ke_{hash_value}"
    
    # -------------------------------------------------------------------------
    # GITHUB EXTRACTION
    # -------------------------------------------------------------------------
    def extract_from_readme(
        self,
        readme_content: str,
        repo_name: str,
        repo_url: str,
    ) -> list[dict]:
        """
        Extract knowledge from a README file.
        
        Returns list of knowledge entry dicts.
        """
        if not readme_content or len(readme_content) < 100:
            return []
        
        prompt = f"""Analyze this README from the repository "{repo_name}" and extract knowledge entries.

README CONTENT:
{readme_content[:8000]}

Extract the following types of knowledge:
1. **Technical decisions** - Architecture choices, library selections, design patterns
2. **Capabilities demonstrated** - What the project shows the author knows how to do
3. **Domain knowledge** - Subject matter expertise shown in the project
4. **Preferences** - Coding style, tooling choices, workflow preferences

For each piece of knowledge, provide:
- domain: A specific topic name (e.g., "MLX fine-tuning workflow", not just "machine learning")
- current_view: A 1-3 sentence summary of the knowledge/position
- confidence: high/medium/low based on how definitively stated
- key_insights: 1-3 specific insights with evidence snippets

Return a JSON array of knowledge entries. Return empty array [] if no substantive knowledge.

IMPORTANT: Focus on knowledge that would be useful for future reference - skip trivial details.

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "summary of knowledge/position",
    "confidence": "high|medium|low",
    "key_insights": [
      {{"insight": "...", "evidence_snippet": "quote from readme"}}
    ],
    "capabilities": ["what the author knows how to do"]
  }}
]"""
        
        try:
            response = self.client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}]
            )
            
            # Extract JSON from response
            text = response.content[0].text
            
            # Find JSON array in response
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = json.loads(text[start:end])
            
            # Convert to proper entry format
            entries = []
            now = datetime.utcnow().isoformat()
            
            for raw in raw_entries:
                entry_id = self._generate_id(raw.get("domain", "") + raw.get("current_view", ""), "github")
                
                entry = {
                    "id": entry_id,
                    "type": "knowledge",
                    "domain": raw.get("domain", ""),
                    "subdomain": None,
                    "state": "active",
                    "detail_level": "full",
                    "current_view": raw.get("current_view", ""),
                    "confidence": raw.get("confidence", "medium"),
                    "positions": [],
                    "key_insights": [
                        {
                            "insight": i.get("insight", ""),
                            "evidence": {
                                "conversation_id": f"github:{repo_name}:readme",
                                "message_ids": [],
                                "snippet": i.get("evidence_snippet", "")[:200],
                            }
                        }
                        for i in raw.get("key_insights", [])
                    ],
                    "knows_how_to": [
                        {
                            "capability": cap,
                            "evidence": {
                                "conversation_id": f"github:{repo_name}:readme",
                                "message_ids": [],
                                "snippet": f"Demonstrated in {repo_name}",
                            }
                        }
                        for cap in raw.get("capabilities", [])
                    ],
                    "open_questions": [],
                    "related_repos": [
                        {
                            "repo": repo_name,
                            "path": "README.md",
                            "link_type": "explicit",
                            "confidence": 1.0,
                            "evidence": "Source of extraction",
                        }
                    ],
                    "related_knowledge": [],
                    "evolution": [],
                    "metadata": {
                        "created_at": now,
                        "updated_at": now,
                        "source_conversations": [f"github:{repo_name}:readme"],
                        "source_messages": [],
                        "access_count": 0,
                        "last_accessed": None,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            print(f"  Error extracting from README: {e}")
            return []
    
    def extract_from_commits(
        self,
        commits: list[dict],
        repo_name: str,
    ) -> list[dict]:
        """
        Extract knowledge from commit messages.
        
        Args:
            commits: List of {sha, message, date, files_changed} dicts
        
        Returns list of knowledge entry dicts.
        """
        # Filter to substantive commits (long messages, not just "fix" or "update")
        substantive_commits = [
            c for c in commits
            if len(c.get("message", "")) > 50 and not c.get("message", "").lower().startswith(("merge", "update", "fix typo", "bump"))
        ]
        
        if not substantive_commits:
            return []
        
        # Prepare commit summary for LLM
        commit_text = "\n\n".join([
            f"[{c['date'][:10]}] {c['message'][:500]}"
            for c in substantive_commits[:30]  # Limit to recent 30
        ])
        
        prompt = f"""Analyze these commit messages from the repository "{repo_name}" and extract knowledge about the author's development practices, technical decisions, and problem-solving approaches.

COMMIT MESSAGES:
{commit_text}

Extract knowledge entries for:
1. **Development practices** - Testing approaches, code review patterns, deployment strategies
2. **Technical decisions** - Why certain approaches were chosen (look for "because", "to enable", etc.)
3. **Problem-solving patterns** - How the author debugs, refactors, or handles issues
4. **Architecture evolution** - How the project changed over time

Return a JSON array. Return empty [] if commits are too trivial.

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "summary of insight",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from commit"
  }}
]"""
        
        try:
            response = self.client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = json.loads(text[start:end])
            
            entries = []
            now = datetime.utcnow().isoformat()
            
            for raw in raw_entries:
                entry_id = self._generate_id(raw.get("domain", "") + raw.get("current_view", ""), "github_commits")
                
                entry = {
                    "id": entry_id,
                    "type": "knowledge",
                    "domain": raw.get("domain", ""),
                    "subdomain": None,
                    "state": "active",
                    "detail_level": "full",
                    "current_view": raw.get("current_view", ""),
                    "confidence": raw.get("confidence", "medium"),
                    "positions": [],
                    "key_insights": [
                        {
                            "insight": raw.get("current_view", ""),
                            "evidence": {
                                "conversation_id": f"github:{repo_name}:commits",
                                "message_ids": [],
                                "snippet": raw.get("evidence_snippet", "")[:200],
                            }
                        }
                    ],
                    "knows_how_to": [],
                    "open_questions": [],
                    "related_repos": [
                        {
                            "repo": repo_name,
                            "path": None,
                            "link_type": "explicit",
                            "confidence": 1.0,
                            "evidence": "Extracted from commit history",
                        }
                    ],
                    "related_knowledge": [],
                    "evolution": [],
                    "metadata": {
                        "created_at": now,
                        "updated_at": now,
                        "source_conversations": [f"github:{repo_name}:commits"],
                        "source_messages": [],
                        "access_count": 0,
                        "last_accessed": None,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            print(f"  Error extracting from commits: {e}")
            return []
    
    # -------------------------------------------------------------------------
    # GMAIL EXTRACTION
    # -------------------------------------------------------------------------
    def extract_from_email(
        self,
        email_content: str,
        email_subject: str,
        email_date: str,
        recipients: list[str],
    ) -> list[dict]:
        """
        Extract knowledge from an email.
        
        Returns list of knowledge entry dicts.
        """
        if not email_content or len(email_content) < 150:
            return []
        
        prompt = f"""Analyze this sent email and extract substantive knowledge about the author's positions, expertise, or commitments.

EMAIL:
Subject: {email_subject}
Date: {email_date}
To: {', '.join(recipients[:5])}

Content:
{email_content[:6000]}

Extract knowledge for:
1. **Stated positions** - Opinions, recommendations, or advice given
2. **Expertise demonstrated** - Technical or domain knowledge shown
3. **Commitments made** - Promises or plans stated
4. **Relationships** - Professional connections or collaborations mentioned

IMPORTANT:
- Skip trivial/logistical emails (scheduling, confirmations)
- Focus on substantive intellectual content
- Capture the author's actual positions, not just topics discussed
- Return empty [] if email has no extractable knowledge

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "the author's position or knowledge",
    "confidence": "high|medium|low",
    "evidence_snippet": "key quote from email",
    "as_of": "{email_date}"
  }}
]"""
        
        try:
            response = self.client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = json.loads(text[start:end])
            
            entries = []
            now = datetime.utcnow().isoformat()
            
            for raw in raw_entries:
                entry_id = self._generate_id(
                    raw.get("domain", "") + raw.get("current_view", ""),
                    "gmail"
                )
                
                entry = {
                    "id": entry_id,
                    "type": "knowledge",
                    "domain": raw.get("domain", ""),
                    "subdomain": None,
                    "state": "active",
                    "detail_level": "full",
                    "current_view": raw.get("current_view", ""),
                    "confidence": raw.get("confidence", "medium"),
                    "positions": [
                        {
                            "view": raw.get("current_view", ""),
                            "confidence": raw.get("confidence", "medium"),
                            "as_of": raw.get("as_of", email_date),
                            "evidence": {
                                "conversation_id": f"gmail:{email_date}:{email_subject[:50]}",
                                "message_ids": [],
                                "snippet": raw.get("evidence_snippet", "")[:200],
                            }
                        }
                    ],
                    "key_insights": [
                        {
                            "insight": raw.get("current_view", ""),
                            "evidence": {
                                "conversation_id": f"gmail:{email_date}:{email_subject[:50]}",
                                "message_ids": [],
                                "snippet": raw.get("evidence_snippet", "")[:200],
                            }
                        }
                    ],
                    "knows_how_to": [],
                    "open_questions": [],
                    "related_repos": [],
                    "related_knowledge": [],
                    "evolution": [],
                    "metadata": {
                        "created_at": now,
                        "updated_at": now,
                        "source_conversations": [f"gmail:{email_date}:{email_subject[:50]}"],
                        "source_messages": [],
                        "access_count": 0,
                        "last_accessed": None,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            print(f"  Error extracting from email: {e}")
            return []
    
    # -------------------------------------------------------------------------
    # BATCH EXTRACTION (for efficiency)
    # -------------------------------------------------------------------------
    def extract_from_code_comments(
        self,
        files: list[dict],
        repo_name: str,
    ) -> list[dict]:
        """
        Extract knowledge from code comments that explain "why".
        
        Args:
            files: List of {path, content} dicts
        
        Returns list of knowledge entry dicts.
        """
        # Extract comments that explain rationale
        rationale_comments = []
        
        for file in files[:30]:  # Limit files
            content = file.get("content", "")
            path = file.get("path", "")
            
            # Look for comments with reasoning keywords
            lines = content.split("\n")
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if any(kw in line_lower for kw in ["because", "reason:", "why:", "note:", "important:", "todo:", "hack:", "workaround"]):
                    # Capture comment and surrounding context
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    context = "\n".join(lines[start:end])
                    rationale_comments.append({
                        "path": path,
                        "comment": context[:300]
                    })
        
        if not rationale_comments:
            return []
        
        # Deduplicate similar comments
        rationale_comments = rationale_comments[:20]
        
        comments_text = "\n\n".join([
            f"[{c['path']}]\n{c['comment']}"
            for c in rationale_comments
        ])
        
        prompt = f"""Analyze these code comments from the repository "{repo_name}" and extract development knowledge.

CODE COMMENTS:
{comments_text}

Extract knowledge about:
1. **Technical decisions** - Why certain approaches were chosen
2. **Gotchas/lessons learned** - Issues discovered and how they were handled
3. **Best practices** - Patterns the author follows
4. **Workarounds** - Hacks or temporary solutions with context

Return a JSON array. Skip trivial TODOs or obvious comments.

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "the insight or practice",
    "confidence": "high|medium|low",
    "source_file": "path/to/file.py",
    "evidence_snippet": "key comment"
  }}
]"""
        
        try:
            response = self.client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            
            text = response.content[0].text
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = json.loads(text[start:end])
            
            entries = []
            now = datetime.utcnow().isoformat()
            
            for raw in raw_entries:
                entry_id = self._generate_id(
                    raw.get("domain", "") + raw.get("current_view", ""),
                    "github_code"
                )
                
                entry = {
                    "id": entry_id,
                    "type": "knowledge",
                    "domain": raw.get("domain", ""),
                    "subdomain": None,
                    "state": "active",
                    "detail_level": "full",
                    "current_view": raw.get("current_view", ""),
                    "confidence": raw.get("confidence", "medium"),
                    "positions": [],
                    "key_insights": [
                        {
                            "insight": raw.get("current_view", ""),
                            "evidence": {
                                "conversation_id": f"github:{repo_name}:code",
                                "message_ids": [],
                                "snippet": raw.get("evidence_snippet", "")[:200],
                            }
                        }
                    ],
                    "knows_how_to": [],
                    "open_questions": [],
                    "related_repos": [
                        {
                            "repo": repo_name,
                            "path": raw.get("source_file"),
                            "link_type": "explicit",
                            "confidence": 1.0,
                            "evidence": "Source of extraction",
                        }
                    ],
                    "related_knowledge": [],
                    "evolution": [],
                    "metadata": {
                        "created_at": now,
                        "updated_at": now,
                        "source_conversations": [f"github:{repo_name}:code"],
                        "source_messages": [],
                        "access_count": 0,
                        "last_accessed": None,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            print(f"  Error extracting from code: {e}")
            return []

