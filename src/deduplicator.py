"""
Deduplicator — Tracks previously seen job IDs across runs.

Design decisions:
- Uses a flat JSON file (data/seen_jobs.json) for simplicity.
  No database needed since the dataset stays small (~100s of IDs).
- The file is committed to the repo so GitHub Actions preserves
  state between workflow runs.
- IDs older than 30 days are pruned to prevent unbounded growth.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

# Path relative to project root
SEEN_JOBS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "seen_jobs.json",
)

# Jobs older than this are pruned from the seen list
RETENTION_DAYS = 30


def load_seen_jobs() -> list[dict[str, str]]:
    """
    Load the list of previously seen job entries.

    Each entry is a dict with 'id' and 'seen_at' (ISO timestamp).
    Returns an empty list if the file doesn't exist or is invalid.
    """
    if not os.path.exists(SEEN_JOBS_PATH):
        return []

    try:
        with open(SEEN_JOBS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Handle legacy format: plain list of ID strings
            if data and isinstance(data[0], str):
                return [{"id": job_id, "seen_at": datetime.now(timezone.utc).isoformat()} for job_id in data]
            return data
    except (json.JSONDecodeError, IndexError):
        return []


def save_seen_jobs(seen_jobs: list[dict[str, str]]) -> None:
    """
    Persist the seen jobs list to disk.

    Prunes entries older than RETENTION_DAYS before saving
    to prevent the file from growing indefinitely.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    pruned = []

    for entry in seen_jobs:
        try:
            seen_at = datetime.fromisoformat(entry["seen_at"])
            if seen_at > cutoff:
                pruned.append(entry)
        except (KeyError, ValueError):
            # Keep entries with missing/invalid timestamps — safer than dropping
            pruned.append(entry)

    with open(SEEN_JOBS_PATH, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2, ensure_ascii=False)


def filter_new_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Filter out jobs that have already been seen.

    Args:
        jobs: List of job dicts, each must have an 'id' field.

    Returns:
        Only the jobs whose IDs are not in the seen list.
    """
    seen_entries = load_seen_jobs()
    seen_ids = {entry["id"] for entry in seen_entries}

    new_jobs = [job for job in jobs if job.get("id") not in seen_ids]
    return new_jobs


def mark_jobs_as_seen(job_ids: list[str]) -> None:
    """
    Add job IDs to the seen list with the current timestamp.

    Called after jobs have been successfully processed and emailed,
    so a failed run doesn't mark jobs as seen prematurely.
    """
    seen_entries = load_seen_jobs()
    seen_ids = {entry["id"] for entry in seen_entries}
    now = datetime.now(timezone.utc).isoformat()

    for job_id in job_ids:
        if job_id not in seen_ids:
            seen_entries.append({"id": job_id, "seen_at": now})

    save_seen_jobs(seen_entries)
