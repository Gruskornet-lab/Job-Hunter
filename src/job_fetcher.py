"""
Job Fetcher — Collects job listings from multiple sources.

Sources:
1. JobTech API (Swedish public employment service) — structured API
2. Remotive.com API — remote jobs, structured API
3. We Work Remotely — scraped HTML

Design decisions:
- Each source has its own fetch function that normalizes data into
  a common Job dict format: {id, title, company, location, url, description, source}.
- Pre-filtering by keywords reduces the number of jobs sent to the
  (rate-limited, paid) Claude API for matching.
- Errors in one source don't block the others — each is wrapped in
  try/except so partial results are still delivered.
"""

import logging
import re
from typing import Any
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Shared HTTP session for connection pooling
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "JobHunter/1.0 (career-automation-project)"
})

# Request timeout in seconds
TIMEOUT = 30


def load_config() -> dict[str, Any]:
    """Load profile configuration from YAML."""
    import os
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "profile.yaml",
    )
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_search_keywords(config: dict[str, Any]) -> list[str]:
    """
    Extract all unique keywords from job priority tiers.

    These are used to query APIs that support keyword search,
    reducing irrelevant results before AI matching.
    """
    keywords = set()
    for tier in ["gold", "silver", "bronze"]:
        tier_config = config.get("job_priorities", {}).get(tier, {})
        for kw in tier_config.get("keywords", []):
            keywords.add(kw.lower())
    return list(keywords)


# ═══════════════════════════════════════
# Source 1: JobTech API (Swedish jobs)
# ═══════════════════════════════════════

def fetch_jobtech(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Fetch jobs from the Swedish JobTech (Arbetsförmedlingen) API.

    The API supports free-text search and geographic filtering.
    We run one query per keyword group to maximize coverage.
    """
    source_config = config.get("sources", {}).get("jobtech", {})
    if not source_config.get("enabled", False):
        logger.info("JobTech source is disabled, skipping.")
        return []

    base_url = source_config["base_url"]
    jobs = []
    seen_ids = set()

    # Group keywords into broader search queries to reduce API calls
    search_terms = [
        "cybersecurity OR SOC OR security analyst",
        "penetration test OR pentest OR red team",
        "devops OR sysadmin OR automation engineer",
        "IT support OR cloud engineer OR infrastructure",
    ]

    for query in search_terms:
        try:
            params = {
                "q": query,
                "limit": 50,
                "offset": 0,
            }

            response = SESSION.get(
                f"{base_url}/search",
                params=params,
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            for hit in data.get("hits", []):
                job_id = f"jobtech_{hit.get('id', '')}"
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Extract location from workplace address
                workplace = hit.get("workplace_address", {}) or {}
                location = workplace.get("municipality", "Sweden")

                jobs.append({
                    "id": job_id,
                    "title": hit.get("headline", "Unknown"),
                    "company": hit.get("employer", {}).get("name", "Unknown"),
                    "location": location,
                    "url": hit.get("webpage_url", hit.get("application_details", {}).get("url", "")),
                    "description": hit.get("description", {}).get("text", "")[:2000],
                    "source": "JobTech",
                })

            logger.info(f"JobTech query '{query}': found {len(data.get('hits', []))} hits")

        except requests.RequestException as e:
            logger.error(f"JobTech query '{query}' failed: {e}")
            continue

    logger.info(f"JobTech total: {len(jobs)} unique jobs")
    return jobs


# ═══════════════════════════════════════
# Source 2: Remotive API (Remote jobs)
# ═══════════════════════════════════════

def fetch_remotive(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Fetch remote jobs from the Remotive.com API.

    The API returns all jobs in one call; we filter client-side
    using our keyword list since there's no search parameter.
    """
    source_config = config.get("sources", {}).get("remotive", {})
    if not source_config.get("enabled", False):
        logger.info("Remotive source is disabled, skipping.")
        return []

    base_url = source_config["base_url"]
    jobs = []
    keywords = get_search_keywords(config)

    try:
        response = SESSION.get(base_url, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()

        for listing in data.get("jobs", []):
            # Pre-filter: check if title or tags contain any of our keywords
            title_lower = listing.get("title", "").lower()
            tags = [t.lower() for t in listing.get("tags", [])]
            combined_text = f"{title_lower} {' '.join(tags)}"

            if not any(kw in combined_text for kw in keywords):
                continue

            # Strip HTML from description
            raw_desc = listing.get("description", "")
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ")[:2000]

            job_id = f"remotive_{listing.get('id', '')}"
            location = listing.get("candidate_required_location", "Remote")

            jobs.append({
                "id": job_id,
                "title": listing.get("title", "Unknown"),
                "company": listing.get("company_name", "Unknown"),
                "location": location if location else "Remote",
                "url": listing.get("url", ""),
                "description": clean_desc,
                "source": "Remotive",
            })

        logger.info(f"Remotive: {len(jobs)} relevant jobs after keyword filter")

    except requests.RequestException as e:
        logger.error(f"Remotive fetch failed: {e}")

    return jobs


# ═══════════════════════════════════════
# Source 3: We Work Remotely (Scraped)
# ═══════════════════════════════════════

def fetch_weworkremotely(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Scrape job listings from We Work Remotely.

    We scrape category listing pages and extract job cards.
    Descriptions are fetched from individual job pages.
    Limited to 10 detail-page fetches per category to be respectful.
    """
    source_config = config.get("sources", {}).get("weworkremotely", {})
    if not source_config.get("enabled", False):
        logger.info("WeWorkRemotely source is disabled, skipping.")
        return []

    base_url = source_config["base_url"]
    categories = source_config.get("scrape_categories", [])
    keywords = get_search_keywords(config)
    jobs = []

    for category in categories:
        try:
            url = f"{base_url}/categories/{category}"
            response = SESSION.get(url, timeout=TIMEOUT)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            job_links = soup.select("li.feature a, li:not(.ad) a[href*='/remote-jobs/']")

            fetched_count = 0
            for link in job_links:
                if fetched_count >= 10:
                    break

                href = link.get("href", "")
                if not href or "/remote-jobs/" not in href:
                    continue

                title_el = link.select_one(".title")
                company_el = link.select_one(".company")

                if not title_el:
                    continue

                title = title_el.get_text(strip=True)
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                # Pre-filter by keywords
                if not any(kw in title.lower() for kw in keywords):
                    continue

                job_url = f"{base_url}{href}" if href.startswith("/") else href
                job_id = f"wwr_{href.split('/')[-1]}"

                # Fetch job detail page for description
                description = ""
                try:
                    detail_resp = SESSION.get(job_url, timeout=TIMEOUT)
                    detail_resp.raise_for_status()
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    listing_container = detail_soup.select_one(".listing-container")
                    if listing_container:
                        description = listing_container.get_text(separator=" ", strip=True)[:2000]
                    fetched_count += 1
                except requests.RequestException:
                    pass

                jobs.append({
                    "id": job_id,
                    "title": title,
                    "company": company,
                    "location": "Remote",
                    "url": job_url,
                    "description": description,
                    "source": "WeWorkRemotely",
                })

            logger.info(f"WWR category '{category}': {fetched_count} jobs fetched")

        except requests.RequestException as e:
            logger.error(f"WWR category '{category}' failed: {e}")
            continue

    logger.info(f"WeWorkRemotely total: {len(jobs)} jobs")
    return jobs


# ═══════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════

def fetch_all_jobs() -> list[dict[str, Any]]:
    """
    Fetch jobs from all enabled sources.

    Returns a combined list of normalized job dicts.
    Each source is independent — if one fails, the others
    still contribute results.
    """
    config = load_config()

    all_jobs = []

    # Fetch from each source independently
    for fetcher, name in [
        (fetch_jobtech, "JobTech"),
        (fetch_remotive, "Remotive"),
        (fetch_weworkremotely, "WeWorkRemotely"),
    ]:
        try:
            jobs = fetcher(config)
            all_jobs.extend(jobs)
        except Exception as e:
            logger.error(f"Unexpected error in {name} fetcher: {e}")
            continue

    logger.info(f"Total jobs fetched from all sources: {len(all_jobs)}")
    return all_jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    jobs = fetch_all_jobs()
    for job in jobs[:5]:
        print(f"[{job['source']}] {job['title']} — {job['company']} ({job['location']})")
    print(f"\nTotal: {len(jobs)} jobs")
