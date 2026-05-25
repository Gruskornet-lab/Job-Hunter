"""
Job Fetcher — Collects job listings from multiple sources.

Sources:
1. JobTech API (Swedish public employment service) — structured API
2. Remotive.com API — remote jobs, structured API
3. We Work Remotely — scraped HTML
4. Jobindex.se — Swedish job aggregator, scraped HTML

Design decisions:
- Each source has its own fetch function that normalizes data into
  a common Job dict format: {id, title, company, location, url, description, source}.
- Pre-filtering by keywords reduces the number of jobs sent to the
  (rate-limited, paid) Claude API for matching.
- Location filtering removes jobs outside the candidate's commuting range
  before AI evaluation, saving API cost.
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
# Location filter
# ═══════════════════════════════════════

def is_location_allowed(location: str, config: dict[str, Any]) -> bool:
    """
    Returns True if a job's location is within commuting range or is remote.

    Remote-keyword match → always allowed.
    On-site/hybrid → must match one of the allowed cities from geography config.
    Empty/unknown location → passed through so the AI can decide.
    """
    if not location or location.strip() == "":
        return True

    loc_lower = location.lower()
    geo = config.get("geography", {})

    for term in geo.get("remote", []):
        if term.lower() in loc_lower:
            return True

    for city in geo.get("onsite_hybrid", []):
        if city.lower() in loc_lower:
            return True

    return False


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
                # FIX: strip trailing slash before splitting so URLs like
                # "/remote-jobs/foo/" don't produce an empty ID suffix.
                job_id = f"wwr_{href.strip('/').split('/')[-1]}"

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
# Source 4: Jobindex.se (Swedish aggregator)
# ═══════════════════════════════════════

def fetch_jobindex(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Scrape job listings from Jobindex.se for Swedish IT/security roles.

    Jobindex aggregates job ads from Swedish employers and job boards.
    We search for each configured query term and parse the listing cards.
    """
    source_config = config.get("sources", {}).get("jobindex", {})
    if not source_config.get("enabled", False):
        logger.info("Jobindex source is disabled, skipping.")
        return []

    base_url = source_config["base_url"]
    search_queries = source_config.get("search_queries", [])
    jobs = []
    seen_ids: set[str] = set()

    for query in search_queries:
        try:
            params = {
                "q": query,
                "jobnr": 0,
                "hits": 30,
            }
            url = f"{base_url}/tjob"
            response = SESSION.get(url, params=params, timeout=TIMEOUT)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Jobindex listing cards — class names may vary; we try multiple selectors
            job_cards = (
                soup.select("article.jix_robotjob")
                or soup.select("article.jix_job")
                or soup.select("div.PaidJob")
                or soup.select("div.jix_job_result")
            )

            found = 0
            for card in job_cards:
                # Title
                title_el = card.select_one("h4 a, h3 a, .jix-toolbar-small a, a.jobtitle")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                if not href:
                    continue

                job_url = href if href.startswith("http") else f"{base_url}{href}"
                job_id = f"jobindex_{re.sub(r'[^a-zA-Z0-9]', '_', href)[-60:]}"

                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Company
                company_el = card.select_one(".jix_robotjob--company, .company, em")
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                # Location
                location_el = card.select_one(".jix_robotjob--area, .area, .location")
                location = location_el.get_text(strip=True) if location_el else "Sweden"

                # Description snippet from card
                desc_el = card.select_one(".jix_robotjob--teaser, .teaser, p")
                description = desc_el.get_text(strip=True)[:2000] if desc_el else ""

                jobs.append({
                    "id": job_id,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": job_url,
                    "description": description,
                    "source": "Jobindex",
                })
                found += 1

            logger.info(f"Jobindex query '{query}': {found} listings found")

        except requests.RequestException as e:
            logger.error(f"Jobindex query '{query}' failed: {e}")
            continue

    logger.info(f"Jobindex total: {len(jobs)} unique jobs")
    return jobs


# ═══════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════

def fetch_all_jobs() -> list[dict[str, Any]]:
    """
    Fetch jobs from all enabled sources, then filter by location.

    Returns a combined list of normalized job dicts where each job is
    either remote or within the candidate's commuting range.
    """
    config = load_config()

    all_jobs = []

    # Fetch from each source independently
    for fetcher, name in [
        (fetch_jobtech, "JobTech"),
        (fetch_remotive, "Remotive"),
        (fetch_weworkremotely, "WeWorkRemotely"),
        (fetch_jobindex, "Jobindex"),
    ]:
        try:
            jobs = fetcher(config)
            all_jobs.extend(jobs)
        except Exception as e:
            logger.error(f"Unexpected error in {name} fetcher: {e}")
            continue

    logger.info(f"Total jobs fetched from all sources: {len(all_jobs)}")

    # Filter by location: keep remote jobs and jobs within commuting range
    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_location_allowed(j.get("location", ""), config)]
    removed = before - len(all_jobs)
    logger.info(f"Location filter: removed {removed} out-of-range jobs, {len(all_jobs)} remain")

    return all_jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    jobs = fetch_all_jobs()
    for job in jobs[:5]:
        print(f"[{job['source']}] {job['title']} — {job['company']} ({job['location']})")
    print(f"\nTotal: {len(jobs)} jobs")
