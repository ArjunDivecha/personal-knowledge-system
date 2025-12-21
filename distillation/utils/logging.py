"""
=============================================================================
LOGGING UTILITIES
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Provides logging setup and run report generation for the pipeline.

INPUT FILES:
- None

OUTPUT FILES:
- Console output with rich formatting
- Run reports (JSON)
=============================================================================
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table


# Rich console for pretty output
console = Console()


def setup_logger(name: str = "distillation", verbose: bool = False) -> logging.Logger:
    """
    Set up a logger with rich formatting.
    
    Args:
        name: Logger name
        verbose: If True, set level to DEBUG
    
    Returns:
        Configured logger
    """
    level = logging.DEBUG if verbose else logging.INFO
    
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)]
    )
    
    return logging.getLogger(name)


# -----------------------------------------------------------------------------
# RUN REPORT - Comprehensive metrics from a pipeline run
# -----------------------------------------------------------------------------
@dataclass
class RunReport:
    """
    Comprehensive report from a distillation pipeline run.
    Matches the schema in prd-distillation-v1.1.md Section 7.1.
    """
    # Identity
    run_id: str = ""
    triggered_by: str = "manual"  # "cron" or "manual"
    
    # Timing
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 0.0
    
    # Status
    status: str = "running"  # "completed", "completed_with_errors", "failed"
    
    # Input metrics
    exports_found_claude: int = 0
    exports_found_gpt: int = 0
    conversations_total: int = 0
    conversations_new: int = 0
    
    # Processing metrics
    conversations_parsed: int = 0
    parse_errors: int = 0
    conversations_filtered_in: int = 0
    conversations_filtered_out: int = 0
    filter_score_distribution: dict = field(default_factory=dict)
    
    # Extraction metrics
    knowledge_entries_extracted: int = 0
    project_entries_extracted: int = 0
    extraction_errors: int = 0
    validation_failures: int = 0
    insights_with_evidence: int = 0
    insights_without_evidence: int = 0
    
    # Merge metrics
    entries_created: int = 0
    entries_updated: int = 0
    entries_evolved: int = 0
    entries_contested: int = 0
    
    # Compression metrics
    entries_eligible_for_compression: int = 0
    entries_compressed: int = 0
    entries_archived: int = 0
    
    # Output metrics
    total_knowledge_entries: int = 0
    total_project_entries: int = 0
    active_entries: int = 0
    contested_entries: int = 0
    compressed_entries: int = 0
    thin_index_token_count: int = 0
    
    # Cost tracking
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    embedding_tokens: int = 0
    estimated_cost_usd: float = 0.0
    
    # Errors
    errors: list[dict] = field(default_factory=list)
    
    def add_error(
        self,
        stage: str,
        error_type: str,
        error_message: str,
        conversation_id: Optional[str] = None,
        recoverable: bool = True
    ):
        """Add an error to the report."""
        self.errors.append({
            "timestamp": datetime.utcnow().isoformat(),
            "stage": stage,
            "conversation_id": conversation_id,
            "error_type": error_type,
            "error_message": error_message,
            "recoverable": recoverable,
        })
    
    def calculate_cost(self):
        """
        Calculate estimated cost based on token usage.
        Uses Claude 3.5 Sonnet pricing.
        """
        # Claude 3.5 Sonnet pricing
        input_cost_per_million = 3.00
        output_cost_per_million = 15.00
        embedding_cost_per_million = 0.02
        
        input_cost = (self.llm_input_tokens / 1_000_000) * input_cost_per_million
        output_cost = (self.llm_output_tokens / 1_000_000) * output_cost_per_million
        embedding_cost = (self.embedding_tokens / 1_000_000) * embedding_cost_per_million
        
        self.estimated_cost_usd = input_cost + output_cost + embedding_cost
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    def save(self, path: Path):
        """Save report to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


def log_run_report(report: RunReport):
    """
    Display a run report in a nice table format.
    """
    console.print("\n")
    console.rule("[bold blue]Distillation Run Report")
    
    # Summary table
    summary = Table(title="Summary", show_header=False)
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    
    summary.add_row("Run ID", report.run_id)
    summary.add_row("Status", report.status)
    summary.add_row("Duration", f"{report.duration_seconds:.1f}s")
    summary.add_row("", "")
    summary.add_row("Conversations Parsed", str(report.conversations_parsed))
    summary.add_row("Conversations Filtered In", str(report.conversations_filtered_in))
    summary.add_row("Knowledge Entries Extracted", str(report.knowledge_entries_extracted))
    summary.add_row("Project Entries Extracted", str(report.project_entries_extracted))
    summary.add_row("", "")
    summary.add_row("Entries Created", str(report.entries_created))
    summary.add_row("Entries Updated", str(report.entries_updated))
    summary.add_row("Entries Contested", str(report.entries_contested))
    summary.add_row("", "")
    summary.add_row("Estimated Cost", f"${report.estimated_cost_usd:.2f}")
    
    console.print(summary)
    
    # Errors if any
    if report.errors:
        console.print("\n")
        error_table = Table(title="Errors", style="red")
        error_table.add_column("Stage")
        error_table.add_column("Type")
        error_table.add_column("Message")
        
        for error in report.errors[:10]:  # Show first 10
            error_table.add_row(
                error["stage"],
                error["error_type"],
                error["error_message"][:50] + "..." if len(error["error_message"]) > 50 else error["error_message"]
            )
        
        console.print(error_table)
        if len(report.errors) > 10:
            console.print(f"[dim]...and {len(report.errors) - 10} more errors[/dim]")
    
    console.print("\n")


def create_progress() -> Progress:
    """Create a rich progress bar for pipeline stages."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    )

