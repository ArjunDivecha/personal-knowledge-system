"""
=============================================================================
KNOWLEDGE DISTILLATION PIPELINE - MAIN ENTRY POINT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Main CLI for running the complete distillation pipeline.
Processes Claude and GPT conversation exports into structured knowledge
entries stored in Upstash Redis/Vector.

INPUT FILES:
- Claude exports: /Users/macbook2024/Library/CloudStorage/Dropbox/Identity and 
  Important Papers/Arjun Digital Identity/Anthropic/conversations.json
- GPT exports: /Users/macbook2024/Library/CloudStorage/Dropbox/Identity and 
  Important Papers/Arjun Digital Identity/ChatGPT/conversations.json

OUTPUT FILES:
- Entries in Upstash Redis
- Embeddings in Upstash Vector
- Thin index at Redis key "index:current"
- Run report in knowledge-system/distillation/runs/

USAGE:
    cd knowledge-system/distillation
    python main.py --run           # Full pipeline run
    python main.py --run --verbose # With detailed output
    python main.py --dry-run       # Show what would be processed
    python main.py --status        # Show current state
=============================================================================
"""

import sys
import uuid
import argparse
from pathlib import Path
from datetime import datetime

# Add distillation to path
sys.path.insert(0, str(Path(__file__).parent))

from config import validate_config, ARCHIVE_PATH, CLAUDE_EXPORT_PATH, GPT_EXPORT_PATH
from storage.redis_client import RedisClient
from storage.vector_client import VectorClient
from pipeline.parse import parse_all_exports
from pipeline.filter import filter_conversations, get_score_distribution
from pipeline.extract import extract_entries
from pipeline.merge import merge_knowledge_entries, merge_project_entries
from pipeline.compress import compress_eligible_entries
from pipeline.index import update_index
from utils.logging import RunReport, log_run_report, console, create_progress


# -----------------------------------------------------------------------------
# STATUS COMMAND
# -----------------------------------------------------------------------------

def show_status():
    """Show current state of the knowledge system."""
    print("\n" + "=" * 60)
    print("KNOWLEDGE SYSTEM STATUS")
    print("=" * 60 + "\n")
    
    # Check configuration
    errors = validate_config()
    if errors:
        print("⚠️  Configuration Issues:")
        for error in errors:
            print(f"   - {error}")
        print()
        return
    
    print("✓ Configuration OK\n")
    
    # Connect to storage
    try:
        redis = RedisClient()
        vector = VectorClient()
        
        redis_ok, redis_msg = redis.test_connection()
        vector_ok, vector_msg = vector.test_connection()
        
        print(f"Redis: {'✓' if redis_ok else '✗'} {redis_msg}")
        print(f"Vector: {'✓' if vector_ok else '✗'} {vector_msg}")
        print()
        
        if redis_ok:
            knowledge = redis.get_all_knowledge_entries()
            projects = redis.get_all_project_entries()
            thin_index = redis.get_thin_index()
            
            print(f"Knowledge Entries: {len(knowledge)}")
            print(f"Project Entries: {len(projects)}")
            
            if thin_index:
                print(f"Thin Index: {thin_index.token_count} tokens, generated at {thin_index.generated_at}")
            else:
                print("Thin Index: Not generated yet")
            
            # Count states
            active = sum(1 for e in knowledge if e.state == "active")
            contested = sum(1 for e in knowledge if e.state == "contested")
            compressed = sum(1 for e in knowledge if e.detail_level == "compressed")
            
            print(f"  - Active: {active}")
            print(f"  - Contested: {contested}")
            print(f"  - Compressed: {compressed}")
    
    except Exception as e:
        print(f"✗ Error connecting to storage: {e}")
    
    print("\n" + "=" * 60 + "\n")


# -----------------------------------------------------------------------------
# DRY RUN COMMAND
# -----------------------------------------------------------------------------

def dry_run():
    """Show what would be processed without making changes."""
    print("\n" + "=" * 60)
    print("DRY RUN - Processing Preview")
    print("=" * 60 + "\n")
    
    # Check source paths
    print("Source Paths:")
    print(f"  Claude: {CLAUDE_EXPORT_PATH} {'✓ exists' if CLAUDE_EXPORT_PATH.exists() else '✗ not found'}")
    print(f"  GPT: {GPT_EXPORT_PATH} {'✓ exists' if GPT_EXPORT_PATH.exists() else '✗ not found'}")
    print()
    
    # Parse
    print("Parsing exports...")
    conversations = parse_all_exports()
    print(f"  Found {len(conversations)} conversations")
    print()
    
    # Filter
    print("Filtering...")
    filtered = filter_conversations(conversations)
    kept = [f for f in filtered if f.should_keep]
    skipped = [f for f in filtered if not f.should_keep]
    
    print(f"  Would keep: {len(kept)}")
    print(f"  Would skip: {len(skipped)}")
    print()
    
    # Score distribution
    print("Score Distribution:")
    dist = get_score_distribution(filtered)
    for score, count in list(dist.items())[-10:]:  # Last 10 scores
        print(f"  Score {score:3d}: {count} conversations")
    print()
    
    # Check for already processed
    try:
        redis = RedisClient()
        redis_ok, _ = redis.test_connection()
        
        if redis_ok:
            processed_ids = redis.get_processed_conversation_ids()
            new_conversations = [c for c in conversations if c.id not in processed_ids]
            print(f"Already Processed: {len(conversations) - len(new_conversations)}")
            print(f"New Conversations: {len(new_conversations)}")
    except:
        print("Could not check processed status (Redis not configured)")
    
    print("\n" + "=" * 60 + "\n")


# -----------------------------------------------------------------------------
# RUN COMMAND
# -----------------------------------------------------------------------------

def run_pipeline(verbose: bool = False, limit: int = None):
    """Execute the full distillation pipeline."""
    run_id = f"run_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    
    report = RunReport(
        run_id=run_id,
        triggered_by="manual",
        started_at=datetime.utcnow().isoformat(),
        status="running",
    )
    
    console.print("\n" + "=" * 60)
    console.print(f"[bold blue]DISTILLATION PIPELINE RUN[/bold blue] - {run_id}")
    console.print("=" * 60 + "\n")
    
    try:
        # Check configuration
        console.print("[bold]Step 1: Checking configuration...[/bold]")
        errors = validate_config()
        if errors:
            for error in errors:
                console.print(f"  [red]✗ {error}[/red]")
            raise Exception("Configuration errors - cannot proceed")
        console.print("  [green]✓ Configuration OK[/green]\n")
        
        # Connect to storage
        console.print("[bold]Step 2: Connecting to storage...[/bold]")
        redis = RedisClient()
        vector = VectorClient()
        
        redis_ok, redis_msg = redis.test_connection()
        vector_ok, vector_msg = vector.test_connection()
        
        if not redis_ok or not vector_ok:
            if not redis_ok:
                console.print(f"  [red]✗ Redis: {redis_msg}[/red]")
            if not vector_ok:
                console.print(f"  [red]✗ Vector: {vector_msg}[/red]")
            raise Exception("Storage connection failed")
        
        console.print(f"  [green]✓ {redis_msg}[/green]")
        console.print(f"  [green]✓ {vector_msg}[/green]\n")
        
        # Stage 1: Parse
        console.print("[bold]Step 3: Parsing exports...[/bold]")
        conversations = parse_all_exports()
        report.conversations_total = len(conversations)
        
        # Filter out already processed
        processed_ids = redis.get_processed_conversation_ids()
        new_conversations = [c for c in conversations if c.id not in processed_ids]
        report.conversations_new = len(new_conversations)
        report.conversations_parsed = len(conversations)
        
        console.print(f"  Parsed: {len(conversations)} total, {len(new_conversations)} new\n")
        
        # Stage 2: Filter
        console.print("[bold]Step 4: Filtering by value...[/bold]")
        filtered = filter_conversations(new_conversations)
        kept = [f for f in filtered if f.should_keep]
        skipped = [f for f in filtered if not f.should_keep]
        
        report.conversations_filtered_in = len(kept)
        report.conversations_filtered_out = len(skipped)
        report.filter_score_distribution = get_score_distribution(filtered)
        
        console.print(f"  Kept: {len(kept)}, Skipped: {len(skipped)}\n")
        
        # Apply limit if specified
        if limit and len(kept) > limit:
            console.print(f"  [yellow]Limiting to {limit} conversations for testing[/yellow]\n")
            kept = kept[:limit]
        
        # Stage 3: Extract
        if kept:
            console.print("[bold]Step 5: Extracting knowledge...[/bold]")
            
            extraction_results = []
            with create_progress() as progress:
                task = progress.add_task("Extracting", total=len(kept))
                
                for fc in kept:
                    from pipeline.extract import extract_from_conversation
                    result = extract_from_conversation(fc.conversation)
                    extraction_results.append(result)
                    progress.update(task, advance=1)
                    
                    report.llm_input_tokens += result.input_tokens
                    report.llm_output_tokens += result.output_tokens
            
            # Aggregate extraction results
            all_knowledge = []
            all_projects = []
            
            for result in extraction_results:
                if result.success:
                    all_knowledge.extend(result.knowledge_entries)
                    all_projects.extend(result.project_entries)
                    report.insights_with_evidence += sum(
                        len(e.key_insights) for e in result.knowledge_entries
                    )
                    for error in result.validation_errors:
                        report.add_error("extract", "validation", error, result.conversation_id)
                else:
                    report.extraction_errors += 1
                    report.add_error("extract", "api", result.error or "Unknown", result.conversation_id)
            
            report.knowledge_entries_extracted = len(all_knowledge)
            report.project_entries_extracted = len(all_projects)
            
            console.print(f"  Extracted: {len(all_knowledge)} knowledge, {len(all_projects)} project entries\n")
            
            # Stage 4: Merge
            console.print("[bold]Step 6: Merging with existing...[/bold]")
            
            if all_knowledge:
                knowledge_results = merge_knowledge_entries(all_knowledge, redis, vector)
                for r in knowledge_results:
                    if r.action == "create":
                        report.entries_created += 1
                    elif r.action == "update":
                        report.entries_updated += 1
                    elif r.action == "evolve":
                        report.entries_evolved += 1
                    elif r.action == "contest":
                        report.entries_contested += 1
            
            if all_projects:
                project_results = merge_project_entries(all_projects, redis)
                for r in project_results:
                    if r.action == "create":
                        report.entries_created += 1
                    elif r.action == "update":
                        report.entries_updated += 1
            
            console.print(f"  Created: {report.entries_created}, Updated: {report.entries_updated}")
            console.print(f"  Evolved: {report.entries_evolved}, Contested: {report.entries_contested}\n")
            
            # Mark conversations as processed
            for fc in kept:
                redis.mark_conversation_processed(fc.conversation.id)
        else:
            console.print("[dim]  No new conversations to process[/dim]\n")
        
        # Stage 5: Compress (optional, runs on eligible old entries)
        console.print("[bold]Step 7: Checking for compression...[/bold]")
        compression_results = compress_eligible_entries(redis)
        
        compressed = [r for r in compression_results if r.action == "compressed"]
        report.entries_compressed = len(compressed)
        
        console.print(f"  Compressed: {len(compressed)} entries\n")
        
        # Stage 6: Update index
        console.print("[bold]Step 8: Updating index...[/bold]")
        index_result = update_index(redis, vector)
        
        report.embedding_tokens = index_result.embedding_tokens
        report.thin_index_token_count = index_result.thin_index_token_count
        
        console.print(f"  Entries indexed: {index_result.entries_indexed}")
        console.print(f"  Vectors upserted: {index_result.vectors_upserted}")
        console.print(f"  Thin index tokens: {index_result.thin_index_token_count}\n")
        
        # Final counts
        final_knowledge = redis.get_all_knowledge_entries()
        final_projects = redis.get_all_project_entries()
        
        report.total_knowledge_entries = len(final_knowledge)
        report.total_project_entries = len(final_projects)
        report.active_entries = sum(1 for e in final_knowledge if e.state == "active")
        report.contested_entries = sum(1 for e in final_knowledge if e.state == "contested")
        report.compressed_entries = sum(1 for e in final_knowledge if e.detail_level == "compressed")
        
        # Complete
        report.completed_at = datetime.utcnow().isoformat()
        report.status = "completed_with_errors" if report.errors else "completed"
        report.duration_seconds = (
            datetime.fromisoformat(report.completed_at) -
            datetime.fromisoformat(report.started_at)
        ).total_seconds()
        report.calculate_cost()
        
    except Exception as e:
        report.completed_at = datetime.utcnow().isoformat()
        report.status = "failed"
        report.add_error("pipeline", "fatal", str(e), recoverable=False)
        console.print(f"\n[red]✗ Pipeline failed: {e}[/red]\n")
    
    # Save and display report
    runs_dir = Path(__file__).parent / "runs"
    runs_dir.mkdir(exist_ok=True)
    report_path = runs_dir / f"{run_id}.json"
    report.save(report_path)
    
    log_run_report(report)
    console.print(f"[dim]Report saved to: {report_path}[/dim]\n")


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Knowledge Distillation Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --run              Run the full pipeline
  python main.py --run --verbose    Run with detailed output
  python main.py --run --limit 5    Run on max 5 conversations (testing)
  python main.py --dry-run          Preview what would be processed
  python main.py --status           Show current system status
        """
    )
    
    parser.add_argument("--run", action="store_true", help="Execute the full pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--status", action="store_true", help="Show current status")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--limit", type=int, help="Limit conversations to process")
    
    args = parser.parse_args()
    
    if not any([args.run, args.dry_run, args.status]):
        parser.print_help()
        return
    
    if args.status:
        show_status()
    elif args.dry_run:
        dry_run()
    elif args.run:
        run_pipeline(verbose=args.verbose, limit=args.limit)


if __name__ == "__main__":
    main()

