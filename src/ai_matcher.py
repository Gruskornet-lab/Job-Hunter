"""
AI Matcher — Uses Claude API to score and classify job listings.

Design decisions:
- Each job is sent individually to Claude for precise scoring.
  Batching would be cheaper but risks lower-quality assessments.
- The system prompt includes the full candidate profile context
  from profile.yaml, so Claude understands the career-changer angle.
- Rate limiting: 2-second delay between calls to stay well within
  Anthropic's rate limits on the Sonnet tier.
- Response validation: strict JSON parsing with fallback handling
  for malformed responses.
"""

import json
import logging
import os
import time
from typing import Any

import anthropic
import yaml

logger = logging.getLogger(__name__)

# Delay between API calls (seconds) — respects rate limits
API_CALL_DELAY = 2


def load_config() -> dict[str, Any]:
    """Load profile configuration from YAML."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "profile.yaml",
    )
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_system_prompt(config: dict[str, Any]) -> str:
    """
    Build the system prompt for Claude's matching assessment.

    Injects the full candidate profile so Claude has context about
    the career change, practical experience, and job tier priorities.
    """
    candidate = config.get("candidate", {})
    priorities = config.get("job_priorities", {})
    matching_context = config.get("ai", {}).get("matching_context", "")

    # Format learning platforms and skills
    learning_lines = []
    for platform in candidate.get("current_learning", []):
        skills = ", ".join(platform.get("skills", []))
        ptype = platform.get("type", "Learning")
        learning_lines.append(f"  - {platform['platform']} ({ptype}): {skills}")

    # Format automation projects
    project_lines = []
    for project in candidate.get("automation_projects", []):
        project_lines.append(f"  - {project['name']} ({project['stack']})")

    system_prompt = f"""You are a job matching AI for a career changer.

CANDIDATE PROFILE:
- Name: {candidate.get('name', 'Unknown')}
- Background: {candidate.get('background', '')}
- Short-term goal: {candidate.get('goals', {}).get('short_term', '')}
- Long-term goal: {candidate.get('goals', {}).get('long_term', '')}
- Technical skills: {', '.join(candidate.get('technical_skills', []))}
- Current learning:
{chr(10).join(learning_lines)}
- Automation projects built:
{chr(10).join(project_lines)}

MATCHING CONTEXT:
{matching_context}

JOB CLASSIFICATION TIERS:
- GOLD: {priorities.get('gold', {}).get('label', 'IT Security')} roles
- SILVER: {priorities.get('silver', {}).get('label', 'IT General')} roles
- STRETCH: {priorities.get('bronze', {}).get('label', 'Pentesting')} roles (include even if requirements are high)
- SKIP: Non-IT roles (do not include)

RESPONSE FORMAT:
You MUST respond with ONLY a valid JSON object, no markdown, no explanation:
{{
  "score": <1-10 integer>,
  "summary": "<2-3 sentences explaining why this job matches or doesn't>",
  "flag": "GOLD" | "SILVER" | "STRETCH" | "SKIP",
  "tough_match": <true if formal degree/certification is strictly required, false otherwise>
}}

SCORING GUIDE:
- 8-10: Strong match, candidate should apply
- 6-7: Decent match, worth considering
- 4-5: Partial match, some relevant aspects
- 1-3: Poor match or non-IT role
"""
    return system_prompt


def match_single_job(
    client: anthropic.Anthropic,
    job: dict[str, Any],
    system_prompt: str,
    model: str,
) -> dict[str, Any] | None:
    """
    Send a single job to Claude for matching assessment.

    Returns the parsed JSON response or None if the call fails.
    """
    # Build the job description for Claude
    job_text = f"""EVALUATE THIS JOB LISTING:

Title: {job.get('title', 'Unknown')}
Company: {job.get('company', 'Unknown')}
Location: {job.get('location', 'Unknown')}
Source: {job.get('source', 'Unknown')}

Description:
{job.get('description', 'No description available')[:3000]}
"""

    # FIX: declare raw_text before the try block so it is always defined
    # if the except json.JSONDecodeError branch tries to log it.
    raw_text = "<not yet received>"

    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": job_text}],
        )

        # Extract text response
        raw_text = response.content[0].text.strip()

        # Clean markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1]
            raw_text = raw_text.rsplit("```", 1)[0].strip()

        result = json.loads(raw_text)

        # Validate required fields
        required_fields = {"score", "summary", "flag"}
        if not required_fields.issubset(result.keys()):
            logger.warning(f"Missing fields in response for '{job['title']}': {result}")
            return None

        # FIX: coerce score to int — Claude may return a string or float
        try:
            result["score"] = int(result["score"])
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid score type for '{job['title']}': {result['score']!r} — defaulting to 0"
            )
            result["score"] = 0

        # Clamp score to valid range
        result["score"] = max(1, min(10, result["score"]))

        # Normalize flag value
        result["flag"] = result["flag"].upper()
        if result["flag"] not in {"GOLD", "SILVER", "STRETCH", "SKIP"}:
            result["flag"] = "SILVER"  # Default to SILVER for ambiguous flags

        # Ensure tough_match exists
        result.setdefault("tough_match", False)

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON for '{job['title']}': {e}")
        logger.debug(f"Raw response: {raw_text!r}")
        return None
    except anthropic.APIError as e:
        logger.error(f"Claude API error for '{job['title']}': {e}")
        return None


def match_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Score and classify all jobs using Claude API.

    Args:
        jobs: List of normalized job dicts from job_fetcher.

    Returns:
        List of jobs enriched with AI matching data (score, flag, summary).
        Jobs flagged as SKIP are excluded from the result.
    """
    config = load_config()
    ai_config = config.get("ai", {})
    model = ai_config.get("model", "claude-sonnet-4-6")
    score_threshold = ai_config.get("score_threshold", 4)
    max_jobs = ai_config.get("max_jobs_per_run", 50)

    # Initialize Anthropic client (uses ANTHROPIC_API_KEY env var)
    client = anthropic.Anthropic()

    system_prompt = build_system_prompt(config)

    matched_jobs = []
    processed = 0

    for job in jobs[:max_jobs]:
        logger.info(f"Matching [{processed + 1}/{min(len(jobs), max_jobs)}]: {job['title']}")

        result = match_single_job(client, job, system_prompt, model)

        if result is None:
            logger.warning(f"Skipping '{job['title']}' — no valid AI response")
            processed += 1
            time.sleep(API_CALL_DELAY)
            continue

        # Skip non-IT jobs
        if result["flag"] == "SKIP":
            logger.info(f"  → SKIP (score: {result['score']})")
            processed += 1
            time.sleep(API_CALL_DELAY)
            continue

        # Skip jobs below threshold (unless STRETCH — always include)
        if result["score"] < score_threshold and result["flag"] != "STRETCH":
            logger.info(f"  → Below threshold (score: {result['score']})")
            processed += 1
            time.sleep(API_CALL_DELAY)
            continue

        # Enrich job with AI data
        job["ai_score"] = result["score"]
        job["ai_summary"] = result["summary"]
        job["ai_flag"] = result["flag"]
        job["ai_tough_match"] = result.get("tough_match", False)

        logger.info(f"  → {result['flag']} (score: {result['score']})")
        matched_jobs.append(job)

        processed += 1
        time.sleep(API_CALL_DELAY)

    # Sort by score descending within each flag category
    matched_jobs.sort(key=lambda j: j.get("ai_score", 0), reverse=True)

    logger.info(
        f"Matching complete: {len(matched_jobs)} jobs passed "
        f"(from {processed} evaluated)"
    )
    return matched_jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Test with a sample job
    test_job = {
        "id": "test_001",
        "title": "Junior SOC Analyst",
        "company": "SecureCorp AB",
        "location": "Göteborg",
        "url": "https://example.com/job/123",
        "description": (
            "We are looking for a Junior SOC Analyst to join our security "
            "operations center. You will monitor SIEM alerts, investigate "
            "incidents, and help improve our detection capabilities. "
            "Experience with Nmap, Wireshark, or similar tools is a plus. "
            "No formal degree required — we value practical skills."
        ),
        "source": "Test",
    }
    results = match_jobs([test_job])
    for job in results:
        print(f"[{job['ai_flag']}] {job['ai_score']}/10 — {job['title']}")
        print(f"  {job['ai_summary']}")
