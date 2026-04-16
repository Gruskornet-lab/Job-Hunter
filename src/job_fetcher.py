"""
Job Fetcher — Collects job listings from multiple sources.

Sources:
1. JobTech / Arbetsförmedlingen — official Swedish public job API (extended with Swedish queries)
2. Remotive — free remote jobs API
3. We Work Remotely — scraped HTML
4. Jobicy — free remote jobs API, no key needed
5. Itjobb.se — Swedish IT job aggregator, scraped HTML
6. Teknikjobb.se — Swedish engineering/tech jobs, scraped HTML
7. Nyteknik jobb — Ny Teknik magazine jobs, scraped HTML

Excluded sources (with reason):
- LinkedIn: No public job listings API; scraping violates their terms of service.
- Indeed: Publisher API discontinued; scraping violates their terms of service.

Design decisions:
- Each source has its own fetch function that normalizes data into a common
  Job dict: {id, title, company, location, url, description, source}.
- Pre-filtering by keywords reduces jobs sent to the (paid) Claude API.
- All fetch functions return [] on failure — a broken source never blocks others.
- Swedish-language queries are added to JobTech because many Swedish employers
  write ads in Swedish, and keyword search is case/language-sensitive.
"""

import logging
import os
import re
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "JobHunter/1.0 (career-automation-project; open-source)"
})

TIMEOUT = 30


def load_config() -> dict[str, Any]:
    """Load profile configuration from YAML."""
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

    Used to pre-filter scraped results before sending to Claude.
    """
    keywords = set()
    for tier in ["gold", "silver", "bronze"]:
        tier_config = config.get("job_priorities", {}).get(tier, {})
        for kw in tier_config.get("keywords", []):
            keywords.add(kw.lower())
    return list(keywords)


def _scrape_generic_jobs(
    base_url: str,
    search_paths: list[str],
    keywords: list[str],
    source_label: str,
    id_prefix: str,
) -> list[dict[str, Any]]:
    """
    Reusable scraper for sites that use standard job-card HTML patterns.

    Tries multiple CSS selector patterns for job cards, titles, companies,
    and locations so it degrades gracefully if the site updates its markup.
    Returns [] on any failure.
    """
    jobs = []
    seen_urls: set[str] = set()

    for path in search_paths:
        try:
            url = f"{base_url}{path}"
            response = SESSION.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            # Try progressively looser selectors for job cards
            job_cards = (
                soup.select("article.job-ad")
                or soup.select("article.job")
                or soup.select(".job-listing")
                or soup.select(".job-item")
                or soup.select(".job-ad")
                or soup.select("[class*='job-card']")
                or soup.select("li.job")
            )

            for card in job_cards[:25]:
                link_el = card.select_one("a[href]")
                if not link_el:
                    continue

                job_url = urljoin(base_url, link_el.get("href", ""))
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)

                title_el = (
                    card.select_one("h2")
                    or card.select_one("h3")
                    or card.select_one(".job-title")
                    or card.select_one("[class*='title']")
                    or link_el
                )
                title = title_el.get_text(strip=True)

                if not any(kw in title.lower() for kw in keywords):
                    continue

                company_el = (
                    card.select_one(".company")
                    or card.select_one(".employer")
                    or card.select_one("[class*='company']")
                    or card.select_one("[class*='employer']")
                )
                company = company_el.get_text(strip=True) if company_el else "Unknown"

                location_el = (
                    card.select_one(".location")
                    or card.select_one("[class*='location']")
                    or card.select_one("[class*='city']")
                    or card.select_one("[class*='place']")
                )
                location = location_el.get_text(strip=True) if location_el else "Sverige"

                # Build a stable ID from the URL
                job_id = f"{id_prefix}_{re.sub(r'[^a-z0-9]', '_', job_url.lower())[-50:]}"

                jobs.append({
                    "id": job_id,
                    "title": title,
                    "company": company,
                    "location": location,
                    "url": job_url,
                    "description": "",
                    "source": source_label,
                })

            logger.info(f"{source_label} '{path}': {len(job_cards)} cards, {len(jobs)} kept so far")

        except requests.RequestException as e:
            logger.error(f"{source_label} failed for '{path}': {e}")
            continue

    logger.info(f"{source_label} total: {len(jobs)} relevant jobs")
    return jobs


# ═══════════════════════════════════════════════════════
# Source 1: JobTech / Arbetsförmedlingen / Platsbanken
# ═══════════════════════════════════════════════════════

def fetch_jobtech(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Fetch jobs from the Swedish JobTech API (jobsearch.api.jobtechdev.se).

    This is the official Arbetsförmedlingen/Platsbanken API — free, no key,
    covers essentially all publicly advertised Swedish jobs.

    We query in both English and Swedish because many Swedish employers write
    their ads in Swedish, and the keyword search is language-sensitive.
    """
    source_config = config.get("sources", {}).get("jobtech", {})
    if not source_config.get("enabled", False):
        logger.info("JobTech disabled, skipping.")
        return []

    base_url = source_config["base_url"]
    jobs = []
    seen_ids: set[str] = set()

    search_terms = [
        # ── English ──────────────────────────────────────────────
        "cybersecurity OR SOC OR security analyst",
        "penetration test OR pentest OR red team",
        "devops OR sysadmin OR automation engineer",
        "IT support OR cloud engineer OR infrastructure",
        "backend developer OR software engineer",
        # ── Swedish ──────────────────────────────────────────────
        "IT-säkerhet OR cybersäkerhet OR informationssäkerhet",
        "SOC-analytiker OR säkerhetsanalytiker OR nätverkssäkerhet",
        "penetrationstest OR etisk hacker OR incidenthantering",
        "systemadministratör OR nätverksadministratör OR drifttekniker",
        "nätverkstekniker OR cloud OR infrastruktur",
        "systemutvecklare OR mjukvaruutvecklare OR backend",
        "IT-tekniker OR IT-support OR servicedesk",
    ]

    for query in search_terms:
        try:
            params = {"q": query, "limit": 100, "offset": 0}
            response = SESSION.get(
                f"{base_url}/search", params=params, timeout=TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            for hit in data.get("hits", []):
                job_id = f"jobtech_{hit.get('id', '')}"
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                workplace = hit.get("workplace_address", {}) or {}
                location = (
                    workplace.get("municipality")
                    or workplace.get("region")
                    or "Sverige"
                )

                jobs.append({
                    "id": job_id,
                    "title": hit.get("headline", "Unknown"),
                    "company": hit.get("employer", {}).get("name", "Unknown"),
                    "location": location,
                    "url": (
                        hit.get("webpage_url")
                        or hit.get("application_details", {}).get("url", "")
                    ),
                    "description": hit.get("description", {}).get("text", "")[:2000],
                    "source": "Arbetsförmedlingen",
                })

            logger.info(
                f"JobTech '{query[:45]}...': {len(data.get('hits', []))} hits"
            )

        except requests.RequestException as e:
            logger.error(f"JobTech query failed: {e}")
            continue

    logger.info(f"JobTech/Arbetsförmedlingen total: {len(jobs)} unique jobs")
    return jobs


# ═══════════════════════════════════════
# Source 2: Remotive (Remote jobs API)
# ═══════════════════════════════════════

def fetch_remotive(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch remote jobs from Remotive.com. Free API, no key."""
    source_config = config.get("sources", {}).get("remotive", {})
    if not source_config.get("enabled", False):
        logger.info("Remotive disabled, skipping.")
        return []

    base_url = source_config["base_url"]
    keywords = get_search_keywords(config)
    jobs = []

    try:
        response = SESSION.get(base_url, timeout=TIMEOUT)
        response.raise_for_status()
        data = response.json()

        for listing in data.get("jobs", []):
            title_lower = listing.get("title", "").lower()
            tags = [t.lower() for t in listing.get("tags", [])]
            if not any(kw in f"{title_lower} {' '.join(tags)}" for kw in keywords):
                continue

            raw_desc = listing.get("description", "")
            clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ")[:2000]
            location = listing.get("candidate_required_location", "Remote") or "Remote"

            jobs.append({
                "id": f"remotive_{listing.get('id', '')}",
                "title": listing.get("title", "Unknown"),
                "company": listing.get("company_name", "Unknown"),
                "location": location,
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

    Fetches category listing pages and individual job pages.
    Limited to 10 detail-page fetches per category to be respectful.
    """
    source_config = config.get("sources", {}).get("weworkremotely", {})
    if not source_config.get("enabled", False):
        logger.info("WeWorkRemotely disabled, skipping.")
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

                if not any(kw in title.lower() for kw in keywords):
                    continue

                job_url = f"{base_url}{href}" if href.startswith("/") else href
                job_id = f"wwr_{href.strip('/').split('/')[-1]}"

                description = ""
                try:
                    detail_resp = SESSION.get(job_url, timeout=TIMEOUT)
                    detail_resp.raise_for_status()
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    container = detail_soup.select_one(".listing-container")
                    if container:
                        description = container.get_text(separator=" ", strip=True)[:2000]
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
# Source 4: Jobicy (Free remote jobs API)
# ═══════════════════════════════════════

def fetch_jobicy(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Fetch remote jobs from Jobicy's open API.

    Free, no API key required. Queries by tag to target relevant roles.
    API docs: https://jobicy.com/jobs-rss-feed
    """
    source_config = config.get("sources", {}).get("jobicy", {})
    if not source_config.get("enabled", False):
        logger.info("Jobicy disabled, skipping.")
        return []

    base_url = source_config.get("base_url", "https://jobicy.com/api/v2/remote-jobs")
    keywords = get_search_keywords(config)
    jobs = []
    seen_ids: set[str] = set()

    tag_groups = [
        "cybersecurity,infosec,security",
        "devops,sysadmin,linux",
        "networking,cloud,infrastructure",
        "python,backend,software-engineer",
    ]

    for tags in tag_groups:
        try:
            params = {"count": 50, "tag": tags}
            response = SESSION.get(base_url, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()

            for listing in data.get("jobs", []):
                job_id = f"jobicy_{listing.get('id', '')}"
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                title = listing.get("jobTitle", "Unknown")
                combined = f"{title} {listing.get('jobIndustry', '')}".lower()
                if not any(kw in combined for kw in keywords):
                    continue

                raw_desc = listing.get("jobDescription", listing.get("jobExcerpt", ""))
                clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ")[:2000]
                geo = listing.get("jobGeo", "Remote") or "Remote"

                jobs.append({
                    "id": job_id,
                    "title": title,
                    "company": listing.get("companyName", "Unknown"),
                    "location": f"Remote — {geo}" if geo not in ("Remote", "Anywhere") else "Remote",
                    "url": listing.get("url", ""),
                    "description": clean_desc,
                    "source": "Jobicy",
                })

            logger.info(f"Jobicy tags '{tags}': {len(data.get('jobs', []))} listings")

        except requests.RequestException as e:
            logger.error(f"Jobicy fetch failed for tags '{tags}': {e}")
            continue

    logger.info(f"Jobicy total: {len(jobs)} relevant jobs")
    return jobs


# ═══════════════════════════════════════
# Source 5: Itjobb.se (Swedish IT jobs)
# ═══════════════════════════════════════

def fetch_itjobb(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Scrape Swedish IT job listings from Itjobb.se.

    Itjobb.se aggregates IT job ads from Swedish employers and boards.
    Uses defensive multi-selector scraping — returns [] if the site's
    HTML structure does not match any known pattern.
    """
    source_config = config.get("sources", {}).get("itjobb", {})
    if not source_config.get("enabled", False):
        logger.info("Itjobb.se disabled, skipping.")
        return []

    base_url = source_config.get("base_url", "https://www.itjobb.se")
    keywords = get_search_keywords(config)

    search_paths = [
        "/lediga-jobb/?q=security",
        "/lediga-jobb/?q=devops",
        "/lediga-jobb/?q=nätverkstekniker",
        "/lediga-jobb/?q=sysadmin",
        "/lediga-jobb/?q=systemadministratör",
    ]

    return _scrape_generic_jobs(base_url, search_paths, keywords, "Itjobb.se", "itjobb")


# ═══════════════════════════════════════
# Source 6: Teknikjobb.se (Swedish tech)
# ═══════════════════════════════════════

def fetch_teknikjobb(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Scrape Swedish engineering/tech job listings from Teknikjobb.se.

    Run by Teknikföretagen (Swedish Association of Engineering Industries).
    Uses defensive multi-selector scraping — returns [] if the HTML does
    not match any known pattern.
    """
    source_config = config.get("sources", {}).get("teknikjobb", {})
    if not source_config.get("enabled", False):
        logger.info("Teknikjobb.se disabled, skipping.")
        return []

    base_url = source_config.get("base_url", "https://www.teknikjobb.se")
    keywords = get_search_keywords(config)

    search_paths = [
        "/lediga-jobb/?q=it+s%C3%A4kerhet",
        "/lediga-jobb/?q=devops",
        "/lediga-jobb/?q=systemutvecklare",
        "/lediga-jobb/?q=nätverkstekniker",
        "/lediga-jobb/?q=drifttekniker",
    ]

    return _scrape_generic_jobs(base_url, search_paths, keywords, "Teknikjobb.se", "teknikjobb")


# ═══════════════════════════════════════
# Source 7: Nyteknik jobb (Swedish tech)
# ═══════════════════════════════════════

def fetch_nyteknik(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Scrape job listings from Ny Teknik's job board (nyteknik.se/jobb).

    Ny Teknik is Sweden's leading engineering/technology magazine and
    its job board targets tech/engineering professionals.
    Uses defensive multi-selector scraping — returns [] if HTML does
    not match any known pattern.
    """
    source_config = config.get("sources", {}).get("nyteknik", {})
    if not source_config.get("enabled", False):
        logger.info("Nyteknik disabled, skipping.")
        return []

    base_url = source_config.get("base_url", "https://www.nyteknik.se")
    keywords = get_search_keywords(config)

    search_paths = [
        "/jobb/?q=it+s%C3%A4kerhet",
        "/jobb/?q=devops",
        "/jobb/?q=systemutvecklare",
        "/jobb/?q=nätverkstekniker",
    ]

    return _scrape_generic_jobs(base_url, search_paths, keywords, "Nyteknik Jobb", "nyteknik")


# ═══════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════

def fetch_all_jobs() -> list[dict[str, Any]]:
    """
    Fetch jobs from all enabled sources.

    Returns a combined, normalized list of job dicts.
    Each source is independent — a failure in one never blocks others.
    """
    config = load_config()
    all_jobs = []

    for fetcher, name in [
        (fetch_jobtech,       "Arbetsförmedlingen"),
        (fetch_remotive,      "Remotive"),
        (fetch_weworkremotely,"WeWorkRemotely"),
        (fetch_jobicy,        "Jobicy"),
        (fetch_itjobb,        "Itjobb.se"),
        (fetch_teknikjobb,    "Teknikjobb.se"),
        (fetch_nyteknik,      "Nyteknik Jobb"),
    ]:
        try:
            jobs = fetcher(config)
            all_jobs.extend(jobs)
            logger.info(f"  {name}: {len(jobs)} jobs")
        except Exception as e:
            logger.error(f"Unexpected error in {name} fetcher: {e}")
            continue

    logger.info(f"Total jobs fetched from all sources: {len(all_jobs)}")
    return all_jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    jobs = fetch_all_jobs()
    for job in jobs[:10]:
        print(f"[{job['source']}] {job['title']} — {job['company']} ({job['location']})")
    print(f"\nTotal: {len(jobs)} jobs")
