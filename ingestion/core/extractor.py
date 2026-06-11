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
import re
from typing import Optional
from datetime import datetime

from .config import EXTRACTION_MODEL  # kept for reference; not passed to SDK
from .sdk_client import sdk_query


class Extractor:
    """
    LLM-based knowledge extractor.
    
    Takes raw content (code, emails, commits) and extracts structured
    knowledge entries that match the schema.
    """
    
    def __init__(self):
        """Auth handled via macOS Keychain through sdk_client; no client object needed."""
        self.last_error: Optional[str] = None

    def _query(self, prompt: str, *, max_tokens: int) -> str:
        """Query Claude, preserving the test seam used by older unit tests."""
        client = getattr(self, "client", None)
        messages = getattr(client, "messages", None)
        create = getattr(messages, "create", None)
        if callable(create):
            response = create(
                model=EXTRACTION_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            content = getattr(response, "content", None) or []
            if content:
                return str(getattr(content[0], "text", ""))
            return ""
        self.last_error = None
        try:
            return sdk_query(prompt, max_tokens=max_tokens)
        except Exception as error:
            self.last_error = str(error)
            raise

    def _record_error(self, label: str, error: Exception) -> None:
        self.last_error = str(error)
        print(f"  Error extracting from {label}: {error}")
    
    def _generate_id(self, content: str, source_type: str) -> str:
        """Generate a unique ID for a knowledge entry."""
        hash_input = f"{source_type}:{content[:500]}"
        hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
        return f"ke_{hash_value}"

    def _generate_repo_scoped_id(self, repo_full_name: str, domain: str, source_type: str) -> str:
        """Generate a stable repo-scoped ID so repeated repo updates merge."""
        hash_input = f"{source_type}:{repo_full_name.lower()}:{domain.strip().lower()}"
        hash_value = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
        return f"ke_{hash_value}"

    def _extract_json_array(self, text: str) -> list[dict]:
        """
        Robustly extract a JSON array of objects from a model response.

        LLM JSON output is frequently slightly malformed — a trailing comma, a
        missing comma between objects, code fences, or prose wrapped around the
        array. Historically a single bad character (e.g. "Expecting ',' delimiter")
        raised, which aborted the whole repo and, via the nightly wrapper, the
        whole night. This parser is deliberately tolerant of recoverable
        corruption while still failing loudly on genuinely unparseable output:

          1. strip ```json ... ``` fences and locate the outermost [...] slice,
          2. try strict json.loads first (the common, clean case),
          3. fall back to json_repair (logged, not silent) for recoverable
             corruption,
          4. raise only if even repair cannot yield a list — callers treat that
             as a per-item soft failure (skip + retry), never an all-or-nothing
             abort of the run.

        Every return path goes through _normalize_entry_list, which guarantees a
        flat list of dicts. Repair of concatenated arrays ("Extra data") can yield
        a list of lists; callers do raw.get(...) and crashed on that shape
        ('list' object has no attribute 'get' — the News-repo nightly failure).
        """
        if not text:
            return []
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z0-9_]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

        start = cleaned.find("[")
        end = cleaned.rfind("]") + 1
        if start == -1 or end == 0:
            return []
        candidate = cleaned[start:end]

        try:
            parsed = json.loads(candidate)
            return self._normalize_entry_list(parsed) if isinstance(parsed, list) else []
        except json.JSONDecodeError as strict_error:
            try:
                from json_repair import repair_json

                repaired = repair_json(candidate, return_objects=True)
            except Exception as repair_error:  # json_repair missing or hard failure
                raise ValueError(
                    f"Unparseable JSON array from model "
                    f"(strict={strict_error}; repair={repair_error})"
                ) from strict_error

            if isinstance(repaired, list):
                print(
                    f"  ⚠ Repaired malformed model JSON ({strict_error}); "
                    f"recovered {len(repaired)} item(s).",
                    flush=True,
                )
                return self._normalize_entry_list(repaired)
            if isinstance(repaired, dict) and repaired:
                print(
                    f"  ⚠ Repaired malformed model JSON ({strict_error}); "
                    "recovered a single object.",
                    flush=True,
                )
                return [repaired]
            raise ValueError(
                f"Model JSON repair did not yield a usable array (strict={strict_error})"
            ) from strict_error

    def _normalize_entry_list(self, items: list) -> list[dict]:
        """Flatten one level of nested lists and keep only dicts.

        Guarantees the _extract_json_array contract (list of dicts) regardless
        of what strict parsing or json_repair produced. Dropped items are logged
        loudly so malformed model output is visible, never silently discarded.
        """
        flat: list = []
        for item in items:
            if isinstance(item, list):
                flat.extend(item)
            else:
                flat.append(item)

        entries = [item for item in flat if isinstance(item, dict)]
        dropped = len(flat) - len(entries)
        if dropped:
            print(
                f"  ⚠ Dropped {dropped} non-object item(s) from model JSON "
                f"(kept {len(entries)} dict entries).",
                flush=True,
            )
        return entries

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """Parse a minimal YAML-style frontmatter block from markdown."""
        if not content.startswith("---\n"):
            return {}, content

        match = re.match(r"^---\n(.*?)\n---\n?(.*)$", content, flags=re.DOTALL)
        if not match:
            return {}, content

        raw_frontmatter, body = match.groups()
        metadata: dict[str, str] = {}
        for line in raw_frontmatter.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip('"').strip("'")
        return metadata, body
    
    # -------------------------------------------------------------------------
    # GITHUB EXTRACTION
    # -------------------------------------------------------------------------
    def extract_from_readme(
        self,
        readme_content: str,
        repo_name: str,
        repo_url: str,
        repo_full_name: Optional[str] = None,
    ) -> list[dict]:
        """
        Extract knowledge from a README file.
        
        Returns list of knowledge entry dicts.
        """
        if not readme_content or len(readme_content) < 100:
            return []
        repo_full_name = repo_full_name or repo_name
        
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
        
        self.last_error = None
        try:
            text = self._query(prompt, max_tokens=4000)

            # Find JSON array in response
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []

            raw_entries = self._extract_json_array(text)
            
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
                            "repo": repo_full_name,
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
                        "project": repo_name,
                        "source_type": "github_readme",
                        "context_type": "active_project",
                        "github_repo": repo_full_name,
                        "github_url": repo_url,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            self._record_error("README", e)
            return []
    
    def extract_from_commits(
        self,
        commits: list[dict],
        repo_name: str,
        repo_url: Optional[str] = None,
        repo_full_name: Optional[str] = None,
    ) -> list[dict]:
        """
        Extract knowledge from commit messages.
        
        Args:
            commits: List of {sha, message, date, files_changed} dicts
        
        Returns list of knowledge entry dicts.
        """
        repo_full_name = repo_full_name or repo_name
        repo_url = repo_url or f"https://github.com/{repo_full_name}"

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
        
        self.last_error = None
        try:
            text = self._query(prompt, max_tokens=2000)
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = self._extract_json_array(text)
            
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
                            "repo": repo_full_name,
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
                        "project": repo_name,
                        "source_type": "github_commits",
                        "context_type": "active_project",
                        "github_repo": repo_full_name,
                        "github_url": repo_url,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            self._record_error("commits", e)
            return []

    def extract_from_markdown_files(
        self,
        files: list[dict],
        repo_name: str,
        repo_url: Optional[str] = None,
        repo_full_name: Optional[str] = None,
    ) -> list[dict]:
        """
        Extract knowledge from markdown documentation files (non-README).

        Each file is sent individually so Claude can interpret it in context
        of the file's purpose (AGENTS.md vs CONTRIBUTING.md vs ADR vs docs).

        Args:
            files: List of {path, content} dicts
        Returns:
            List of knowledge entry dicts.
        """
        repo_full_name = repo_full_name or repo_name
        repo_url = repo_url or f"https://github.com/{repo_full_name}"

        all_entries: list[dict] = []
        now = datetime.utcnow().isoformat()

        self.last_error = None
        for file in files:
            path = file.get("path", "")
            content = file.get("content", "")
            if not content or len(content) < 80:
                continue

            prompt = f"""Analyze this markdown file from the repository "{repo_name}" and extract durable knowledge.

File path: {path}
Repository: {repo_full_name}

CONTENT:
{content[:8000]}

Extract knowledge entries that would be valuable to remember for future work:
1. **Technical decisions or design choices** — why something was built this way
2. **Workflow or process knowledge** — how the author works, contributes, or deploys
3. **Architecture or system understanding** — how the system is structured
4. **Constraints, gotchas, or lessons learned** — things that would surprise a newcomer
5. **Active context or open questions** — TODOs, known issues, next steps with rationale

IMPORTANT:
- Focus on insights that are specific to this repo, not generic best practices
- Skip boilerplate or obvious information (e.g. "run npm install to set up")
- Return empty [] if the file has no extractable knowledge
- Confidence: "high" if explicitly stated, "medium" if clearly implied, "low" if inferred

JSON format:
[
  {{
    "domain": "specific topic",
    "current_view": "1-3 sentence summary of the durable knowledge",
    "confidence": "high|medium|low",
    "key_insights": [
      {{"insight": "...", "evidence_snippet": "quote from file"}}
    ],
    "capabilities": ["optional: what this shows the author knows how to do"],
    "open_questions": ["optional: unresolved question or risk mentioned"]
  }}
]"""

            try:
                raw_entries = self._extract_json_array(self._query(prompt, max_tokens=3000))
                conversation_id = f"github:{repo_full_name}:docs:{path}"

                for raw in raw_entries:
                    domain = raw.get("domain", "").strip()
                    current_view = raw.get("current_view", "").strip()
                    if not domain or not current_view:
                        continue

                    entry_id = self._generate_repo_scoped_id(
                        repo_full_name=repo_full_name,
                        domain=domain,
                        source_type=f"github_markdown:{path}",
                    )

                    entry = {
                        "id": entry_id,
                        "type": "knowledge",
                        "domain": domain,
                        "subdomain": None,
                        "state": "active",
                        "detail_level": "full",
                        "current_view": current_view,
                        "confidence": raw.get("confidence", "medium"),
                        "positions": [],
                        "key_insights": [
                            {
                                "insight": item.get("insight", ""),
                                "evidence": {
                                    "conversation_id": conversation_id,
                                    "message_ids": [],
                                    "snippet": item.get("evidence_snippet", "")[:240],
                                },
                            }
                            for item in raw.get("key_insights", [])
                            if isinstance(item, dict) and item.get("insight")
                        ],
                        "knows_how_to": [
                            {
                                "capability": cap,
                                "evidence": {
                                    "conversation_id": conversation_id,
                                    "message_ids": [],
                                    "snippet": f"Documented in {path}",
                                },
                            }
                            for cap in raw.get("capabilities", [])
                            if cap
                        ],
                        "open_questions": [
                            {"question": q, "status": "open"}
                            for q in raw.get("open_questions", [])
                            if q
                        ],
                        "related_repos": [
                            {
                                "repo": repo_full_name,
                                "path": path,
                                "link_type": "explicit",
                                "confidence": 1.0,
                                "evidence": f"Extracted from {path}",
                            }
                        ],
                        "related_knowledge": [],
                        "evolution": [],
                        "metadata": {
                            "created_at": now,
                            "updated_at": now,
                            "source_conversations": [conversation_id],
                            "source_messages": [],
                            "access_count": 0,
                            "last_accessed": None,
                            "project": repo_name,
                            "source_type": "github_markdown",
                            "context_type": "active_project",
                            "github_repo": repo_full_name,
                            "github_url": repo_url,
                            "markdown_path": path,
                        },
                        "full_content_ref": f"{repo_url}/blob/HEAD/{path}",
                    }

                    all_entries.append(entry)

            except Exception as e:
                self._record_error(path, e)
                continue

        return all_entries

    def extract_from_agent_context_artifact(
        self,
        artifact_content: str,
        repo_name: str,
        repo_full_name: str,
        repo_url: str,
        artifact_path: str,
        artifact_sha: Optional[str] = None,
    ) -> list[dict]:
        """
        Extract repo-specific knowledge from a committed AI agent artifact.

        The artifact belongs to the repository itself, so entries should merge
        by repo + domain rather than by individual session.
        """
        if not artifact_content or len(artifact_content) < 80:
            return []

        artifact_meta, artifact_body = self._parse_frontmatter(artifact_content)
        artifact_body = artifact_body.strip()
        if len(artifact_body) < 80:
            return []

        surface = artifact_meta.get("surface", "unknown")
        session_id = artifact_meta.get("session_id", "")
        exported_at = artifact_meta.get("exported_at", "")
        export_base_commit_sha = (
            artifact_meta.get("export_base_commit_sha", "")
            or artifact_meta.get("commit_sha", "")
        )

        prompt = f"""Analyze this repo-attached AI coding session artifact for the repository "{repo_full_name}".

This file was committed into the repository itself and should be treated as evidence about how this specific repo evolved, not as a separate standalone chat.

Artifact metadata:
- Repository: {repo_full_name}
- Surface: {surface}
- Artifact path: {artifact_path}
- Session id: {session_id or "unknown"}
- Exported at: {exported_at or "unknown"}
- Export base commit sha: {export_base_commit_sha or "unknown"}

ARTIFACT CONTENT:
{artifact_body[:8000]}

Extract durable repo-specific knowledge entries for:
1. Technical decisions made in this repo
2. Architecture or workflow changes for this repo
3. Debugging lessons or implementation constraints discovered while working in this repo
4. TODOs, risks, or active project context that future work on this repo should remember

Return a JSON array. Return empty [] if the artifact has no substantive repo-specific knowledge.

JSON format:
[
  {{
    "domain": "specific repo topic",
    "current_view": "1-3 sentence summary of the durable repo-specific knowledge",
    "confidence": "high|medium|low",
    "key_insights": [
      {{"insight": "...", "evidence_snippet": "quote from artifact"}}
    ],
    "capabilities": ["optional repo capability or workflow shown"],
    "open_questions": ["optional unresolved repo question or risk"]
  }}
]"""

        self.last_error = None
        try:
            raw_entries = self._extract_json_array(self._query(prompt, max_tokens=3000))
            entries = []
            now = datetime.utcnow().isoformat()
            conversation_id = f"github:{repo_full_name}:agent-context:{artifact_path}"

            for raw in raw_entries:
                domain = raw.get("domain", "").strip()
                current_view = raw.get("current_view", "").strip()
                if not domain or not current_view:
                    continue

                entry_id = self._generate_repo_scoped_id(
                    repo_full_name=repo_full_name,
                    domain=domain,
                    source_type="github_agent_context",
                )

                entry = {
                    "id": entry_id,
                    "type": "knowledge",
                    "domain": domain,
                    "subdomain": None,
                    "state": "active",
                    "detail_level": "full",
                    "current_view": current_view,
                    "confidence": raw.get("confidence", "medium"),
                    "positions": [],
                    "key_insights": [
                        {
                            "insight": item.get("insight", ""),
                            "evidence": {
                                "conversation_id": conversation_id,
                                "message_ids": [],
                                "snippet": item.get("evidence_snippet", "")[:240],
                            },
                        }
                        for item in raw.get("key_insights", [])
                        if isinstance(item, dict) and item.get("insight")
                    ],
                    "knows_how_to": [
                        {
                            "capability": capability,
                            "evidence": {
                                "conversation_id": conversation_id,
                                "message_ids": [],
                                "snippet": f"Demonstrated in repo-attached {surface} context",
                            },
                        }
                        for capability in raw.get("capabilities", [])
                        if capability
                    ],
                    "open_questions": [
                        {
                            "question": question,
                            "status": "open",
                        }
                        for question in raw.get("open_questions", [])
                        if question
                    ],
                    "related_repos": [
                        {
                            "repo": repo_full_name,
                            "path": artifact_path,
                            "link_type": "explicit",
                            "confidence": 1.0,
                            "evidence": f"Committed {surface} artifact attached to repo",
                        }
                    ],
                    "related_knowledge": [],
                    "evolution": [],
                    "metadata": {
                        "created_at": now,
                        "updated_at": now,
                        "source_conversations": [conversation_id],
                        "source_messages": [],
                        "access_count": 0,
                        "last_accessed": None,
                        "project": repo_name,
                        "source_type": "github_agent_context",
                        "context_type": "active_project",
                        "github_repo": repo_full_name,
                        "github_url": repo_url,
                        "artifact_path": artifact_path,
                        "artifact_sha": artifact_sha,
                        "artifact_surface": surface,
                        "session_id": session_id or None,
                        "export_base_commit_sha": export_base_commit_sha or None,
                    },
                    "full_content_ref": f"{repo_url}/blob/HEAD/{artifact_path}",
                }

                entries.append(entry)

            return entries

        except Exception as e:
            self._record_error("agent context artifact", e)
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
        
        self.last_error = None
        try:
            text = self._query(prompt, max_tokens=2000)
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = self._extract_json_array(text)
            
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
            self._record_error("email", e)
            return []
    
    # -------------------------------------------------------------------------
    # BATCH EXTRACTION (for efficiency)
    # -------------------------------------------------------------------------
    def extract_from_code_comments(
        self,
        files: list[dict],
        repo_name: str,
        repo_url: Optional[str] = None,
        repo_full_name: Optional[str] = None,
    ) -> list[dict]:
        """
        Extract knowledge from code comments that explain "why".
        
        Args:
            files: List of {path, content} dicts
        
        Returns list of knowledge entry dicts.
        """
        repo_full_name = repo_full_name or repo_name
        repo_url = repo_url or f"https://github.com/{repo_full_name}"

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
        
        self.last_error = None
        try:
            text = self._query(prompt, max_tokens=2000)
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                return []
            
            raw_entries = self._extract_json_array(text)
            
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
                            "repo": repo_full_name,
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
                        "project": repo_name,
                        "source_type": "github_code",
                        "context_type": "active_project",
                        "github_repo": repo_full_name,
                        "github_url": repo_url,
                    },
                    "full_content_ref": None,
                }
                
                if entry["domain"] and entry["current_view"]:
                    entries.append(entry)
            
            return entries
            
        except Exception as e:
            self._record_error("code", e)
            return []
