"""
=============================================================================
GMAIL MBOX PARSER
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Parse Gmail mbox export files and extract email content.
Filters out automated/transactional emails.

INPUT FILES:
- Gmail sent messages mbox file

OUTPUT FILES:
- Parsed email data for extraction
=============================================================================
"""

import mailbox
import email
import re
import hashlib
from typing import Optional, Iterator
from pathlib import Path
from datetime import datetime
from email.utils import parsedate_to_datetime
import html
from html.parser import HTMLParser

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import (
    GMAIL_MBOX_PATH,
    GMAIL_MIN_CONTENT_LENGTH,
    GMAIL_SINCE_YEAR,
    GMAIL_SKIP_DOMAINS,
)


class HTMLTextExtractor(HTMLParser):
    """Extract text from HTML, stripping tags."""
    
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.skip_tags = {"script", "style", "head", "meta"}
        self.current_skip = False
    
    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.skip_tags:
            self.current_skip = True
        if tag.lower() in {"br", "p", "div", "li", "tr"}:
            self.text_parts.append("\n")
    
    def handle_endtag(self, tag):
        if tag.lower() in self.skip_tags:
            self.current_skip = False
    
    def handle_data(self, data):
        if not self.current_skip:
            self.text_parts.append(data)
    
    def get_text(self) -> str:
        return "".join(self.text_parts)


def html_to_text(html_content: str) -> str:
    """Convert HTML to plain text."""
    try:
        parser = HTMLTextExtractor()
        parser.feed(html_content)
        text = parser.get_text()
        
        # Clean up whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = html.unescape(text)
        
        return text.strip()
    except Exception:
        # Fallback: simple tag stripping
        text = re.sub(r"<[^>]+>", " ", html_content)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()


class MboxParser:
    """
    Parser for Gmail mbox export files.
    
    Handles:
    - Parsing mbox format
    - Extracting email headers and body
    - Converting HTML to text
    - Filtering out automated emails
    """
    
    def __init__(self, mbox_path: str = None):
        """Initialize with mbox file path."""
        self.mbox_path = Path(mbox_path or GMAIL_MBOX_PATH)
        
        if not self.mbox_path.exists():
            raise FileNotFoundError(f"Mbox file not found: {self.mbox_path}")
    
    def _get_email_body(self, msg: email.message.Message) -> str:
        """Extract the text body from an email message."""
        if msg.is_multipart():
            # Try to find text/plain first, then text/html
            text_part = None
            html_part = None
            
            for part in msg.walk():
                content_type = part.get_content_type()
                
                if content_type == "text/plain":
                    try:
                        charset = part.get_content_charset() or "utf-8"
                        payload = part.get_payload(decode=True)
                        if payload:
                            text_part = payload.decode(charset, errors="replace")
                    except Exception:
                        pass
                
                elif content_type == "text/html":
                    try:
                        charset = part.get_content_charset() or "utf-8"
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_part = payload.decode(charset, errors="replace")
                    except Exception:
                        pass
            
            # Prefer plain text
            if text_part:
                return text_part
            elif html_part:
                return html_to_text(html_part)
            
            return ""
        
        else:
            # Single part message
            content_type = msg.get_content_type()
            
            try:
                charset = msg.get_content_charset() or "utf-8"
                payload = msg.get_payload(decode=True)
                
                if not payload:
                    return ""
                
                text = payload.decode(charset, errors="replace")
                
                if content_type == "text/html":
                    return html_to_text(text)
                
                return text
                
            except Exception:
                return ""
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse email date header."""
        if not date_str:
            return None
        
        try:
            return parsedate_to_datetime(date_str)
        except Exception:
            return None
    
    def _is_automated_email(self, from_addr: str, to_addrs: list[str], subject: str) -> bool:
        """Check if email is automated/transactional."""
        # Check from address
        from_lower = from_addr.lower() if from_addr else ""
        
        for skip_domain in GMAIL_SKIP_DOMAINS:
            if skip_domain in from_lower:
                return True
        
        # Check for common automated patterns
        automated_patterns = [
            r"noreply",
            r"no-reply",
            r"donotreply",
            r"notifications?@",
            r"alert@",
            r"mailer-daemon",
            r"postmaster",
        ]
        
        for pattern in automated_patterns:
            if re.search(pattern, from_lower):
                return True
        
        # Check subject for common automated patterns
        subject_lower = subject.lower() if subject else ""
        automated_subjects = [
            "your order",
            "order confirmation",
            "shipping confirmation",
            "receipt for",
            "payment received",
            "password reset",
            "verify your email",
            "subscription",
            "unsubscribe",
            "newsletter",
        ]
        
        for pattern in automated_subjects:
            if pattern in subject_lower:
                return True
        
        return False
    
    def _clean_content(self, content: str) -> str:
        """Clean email content - remove signatures, forwards, etc."""
        lines = content.split("\n")
        cleaned_lines = []
        
        # Stop at common signature/forward markers
        stop_markers = [
            "get outlook for",
            "sent from my",
            "---------- forwarded message",
            "on .* wrote:",
            "from:.*sent:.*to:.*subject:",
            "_+" * 5,  # Long underscores
        ]
        
        for line in lines:
            line_lower = line.lower().strip()
            
            # Check for stop markers
            should_stop = False
            for marker in stop_markers:
                if re.search(marker, line_lower):
                    should_stop = True
                    break
            
            if should_stop:
                break
            
            cleaned_lines.append(line)
        
        text = "\n".join(cleaned_lines)
        
        # Remove quoted text (lines starting with >)
        text = re.sub(r"^>.*$", "", text, flags=re.MULTILINE)
        
        # Clean up whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        
        return text.strip()
    
    def _generate_email_id(self, msg: email.message.Message) -> str:
        """Generate a unique ID for an email."""
        message_id = msg.get("Message-ID", "")
        date = msg.get("Date", "")
        subject = msg.get("Subject", "")
        
        hash_input = f"{message_id}:{date}:{subject}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    
    def parse_emails(
        self,
        since_year: int = None,
        max_emails: int = None,
        min_length: int = None,
    ) -> Iterator[dict]:
        """
        Parse emails from the mbox file.
        
        Args:
            since_year: Only include emails from this year onwards
            max_emails: Maximum number of emails to return
            min_length: Minimum content length
        
        Yields:
            dict with {id, date, subject, from, to, content}
        """
        since_year = since_year or GMAIL_SINCE_YEAR
        min_length = min_length or GMAIL_MIN_CONTENT_LENGTH
        
        mbox = mailbox.mbox(str(self.mbox_path))
        count = 0
        
        for msg in mbox:
            try:
                # Parse date
                date = self._parse_date(msg.get("Date", ""))
                
                if not date:
                    continue
                
                # Filter by year
                if date.year < since_year:
                    continue
                
                # Get headers
                from_addr = msg.get("From", "")
                to_addrs = msg.get("To", "").split(",")
                subject = msg.get("Subject", "")
                
                # Skip automated emails
                if self._is_automated_email(from_addr, to_addrs, subject):
                    continue
                
                # Get content
                content = self._get_email_body(msg)
                content = self._clean_content(content)
                
                # Skip short emails
                if len(content) < min_length:
                    continue
                
                email_id = self._generate_email_id(msg)
                
                yield {
                    "id": email_id,
                    "date": date.isoformat(),
                    "date_obj": date,
                    "subject": subject,
                    "from": from_addr,
                    "to": [addr.strip() for addr in to_addrs if addr.strip()],
                    "content": content,
                }
                
                count += 1
                
                if max_emails and count >= max_emails:
                    break
                
            except Exception as e:
                # Skip problematic emails
                continue
        
        mbox.close()
    
    def count_emails(self, since_year: int = None) -> dict:
        """
        Count emails by year.
        
        Returns dict with {year: count} and total.
        """
        since_year = since_year or 2000  # Count all
        
        mbox = mailbox.mbox(str(self.mbox_path))
        counts = {}
        total = 0
        
        for msg in mbox:
            try:
                date = self._parse_date(msg.get("Date", ""))
                if date:
                    year = date.year
                    if year >= since_year:
                        counts[year] = counts.get(year, 0) + 1
                        total += 1
            except Exception:
                continue
        
        mbox.close()
        
        return {
            "by_year": dict(sorted(counts.items())),
            "total": total,
        }


if __name__ == "__main__":
    # Quick test when run directly
    parser = MboxParser()
    
    print("=== Gmail Mbox Parser Test ===\n")
    print(f"Mbox file: {parser.mbox_path}")
    
    print("\nCounting emails...")
    counts = parser.count_emails(since_year=2020)
    print(f"Total since 2020: {counts['total']}")
    print("By year:")
    for year, count in counts["by_year"].items():
        print(f"  {year}: {count}")
    
    print("\nParsing first 5 substantive emails...")
    for i, email_data in enumerate(parser.parse_emails(max_emails=5), 1):
        print(f"\n[{i}] {email_data['date'][:10]}")
        print(f"    Subject: {email_data['subject'][:60]}...")
        print(f"    To: {', '.join(email_data['to'][:2])}")
        print(f"    Content: {len(email_data['content'])} chars")

