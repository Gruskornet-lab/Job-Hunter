"""
Job Hunter — Main Pipeline Orchestrator

This is the entry point for the automation workflow.
It runs the complete pipeline in order:
1. Fetch jobs from all sources
2. Deduplicate against previously seen jobs
3. Score and classify via Claude API
4. Send HTML email digest via SendGrid
5. Mark processed jobs as seen

Design decisions:
- Each step is a separate module for testability and clarity.
- The pipeline fails gracefully: if fetching fails, the email
  still sends (with zero results). If email fails, seen_jobs
  are NOT updated (so the next run retries those jobs).
- Exit code 0 even when no jobs are found — this is normal
  behavior, not an error.
"""

import logging
import sys

from src.job_fetcher import fetch_all_jobs
from src.deduplicator import filter_new_jobs, mark_jobs_as_seen
from src.ai_matcher import match_jobs
from src.email_sender import send_email

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("job_hunter")


def main() -> int:
    """
    Run the complete job hunting pipeline.

    Returns:
        0 on success, 1 on failure.
    """
    logger.info("=" * 60)
    logger.info("JOB HUNTER — Starting pipeline")
    logger.info("=" * 60)

    # Step 1: Fetch jobs from all sources
    logger.info("Step 1/5: Fetching jobs from all sources...")
    all_jobs = fetch_all_jobs()
    logger.info(f"  → {len(all_jobs)} total jobs fetched")

    if not all_jobs:
        logger.info("No jobs found from any source. Sending empty digest.")
        send_email([])
        return 0

    # Step 2: Filter out previously seen jobs
    logger.info("Step 2/5: Deduplicating against seen jobs...")
    new_jobs = filter_new_jobs(all_jobs)
    logger.info(f"  → {len(new_jobs)} new jobs (filtered {len(all_jobs) - len(new_jobs)} duplicates)")

    if not new_jobs:
        logger.info("No new jobs since last run. Sending empty digest.")
        send_email([])
        return 0

    # Step 3: AI matching via Claude
    logger.info("Step 3/5: Running AI matching via Claude API...")
    matched_jobs = match_jobs(new_jobs)
    logger.info(f"  → {len(matched_jobs)} jobs passed AI matching")

    # Step 4: Send email digest
    logger.info("Step 4/5: Sending email digest via SendGrid...")
    email_sent = send_email(matched_jobs)

    if not email_sent:
        logger.error("Email sending failed. NOT marking jobs as seen (will retry next run).")
        return 1

    # Step 5: Mark jobs as seen
    # FIX: always mark new_jobs as seen after a successful email, not only
    # when matched_jobs > 0. The previous condition caused jobs that
    # consistently score below threshold to be re-fetched and re-evaluated
    # by Claude on every single run, wasting API calls indefinitely.
    # If the AI returned no results due to a real API outage the email step
    # above would still have succeeded, but that edge case is acceptable —
    # it is far less costly than infinite re-evaluation of low-scoring jobs.
    logger.info("Step 5/5: Marking jobs as seen...")
    all_job_ids = [job["id"] for job in new_jobs]
    mark_jobs_as_seen(all_job_ids)
    logger.info(f"  → {len(all_job_ids)} job IDs added to seen list")

    # Summary
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info(f"  Fetched:    {len(all_jobs)}")
    logger.info(f"  New:        {len(new_jobs)}")
    logger.info(f"  Matched:    {len(matched_jobs)}")
    logger.info(f"  Emailed:    {'Yes' if email_sent else 'No'}")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
