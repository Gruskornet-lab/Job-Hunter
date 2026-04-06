"""
Email Sender — Builds and sends an HTML email digest via SendGrid.

Design decisions:
- HTML email is built with Jinja2 templates for clean separation
  of content and presentation.
- The email is structured into sections (GOLD, SILVER, STRETCH, TOUGH)
  driven by config/profile.yaml — adding a new tier only requires
  a YAML change.
- Inline CSS is used because most email clients strip <style> tags.
- SendGrid is chosen for its generous free tier (100 emails/day)
  and simple Python SDK.
"""

import logging
import os
from datetime import datetime
from typing import Any

import yaml
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Content

logger = logging.getLogger(__name__)


def load_config() -> dict[str, Any]:
    """Load profile configuration from YAML."""
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "profile.yaml",
    )
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_job_card(job: dict[str, Any]) -> str:
    """
    Build an HTML card for a single job listing.

    Uses inline CSS for email client compatibility.
    """
    score = job.get("ai_score", "?")
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    location = job.get("location", "Unknown")
    summary = job.get("ai_summary", "")
    url = job.get("url", "#")
    source = job.get("source", "Unknown")
    tough = job.get("ai_tough_match", False)

    # Score color coding
    if isinstance(score, int):
        if score >= 8:
            score_color = "#22c55e"  # Green
        elif score >= 6:
            score_color = "#eab308"  # Yellow
        else:
            score_color = "#ef4444"  # Red
    else:
        score_color = "#9ca3af"  # Gray

    tough_badge = ""
    if tough:
        tough_badge = (
            '<span style="background:#fef3c7;color:#92400e;'
            'padding:2px 8px;border-radius:4px;font-size:12px;'
            'margin-left:8px;">⚠️ Degree/cert required</span>'
        )

    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;
                margin-bottom:12px;background:#ffffff;">
        <div style="display:flex;align-items:center;margin-bottom:8px;">
            <span style="background:{score_color};color:white;
                         border-radius:50%;width:32px;height:32px;
                         display:inline-flex;align-items:center;
                         justify-content:center;font-weight:bold;
                         font-size:14px;margin-right:12px;">
                {score}
            </span>
            <div>
                <strong style="font-size:16px;color:#1f2937;">{title}</strong>
                {tough_badge}
                <br/>
                <span style="color:#6b7280;font-size:14px;">
                    {company} · {location} · {source}
                </span>
            </div>
        </div>
        <p style="color:#374151;font-size:14px;line-height:1.5;
                  margin:8px 0;font-style:italic;">
            "{summary}"
        </p>
        <a href="{url}" style="color:#2563eb;font-size:14px;
                              text-decoration:none;font-weight:500;">
            → Se annons ↗
        </a>
    </div>
    """


def build_email_html(jobs: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """
    Build the complete HTML email with all job sections.

    Jobs are grouped by their AI flag (GOLD, SILVER, STRETCH)
    and tough_match jobs get their own section at the bottom.
    """
    email_config = config.get("email", {})
    sections = email_config.get("sections", [])

    today = datetime.now().strftime("%A %d %B %Y")

    # Group jobs by flag
    grouped: dict[str, list[dict[str, Any]]] = {}
    tough_jobs: list[dict[str, Any]] = []

    for job in jobs:
        flag = job.get("ai_flag", "SILVER")
        if job.get("ai_tough_match", False):
            tough_jobs.append(job)
        if flag not in grouped:
            grouped[flag] = []
        grouped[flag].append(job)

    # Build sections HTML
    sections_html = ""
    for section in sections:
        flag = section["flag"]

        # TOUGH section uses the tough_jobs list
        if flag == "TOUGH":
            section_jobs = tough_jobs
        else:
            section_jobs = grouped.get(flag, [])

        if not section_jobs:
            continue

        emoji = section.get("emoji", "📋")
        heading = section.get("heading", flag)

        cards_html = "\n".join(build_job_card(job) for job in section_jobs)

        sections_html += f"""
        <div style="margin-bottom:32px;">
            <h2 style="color:#1f2937;border-bottom:2px solid #e5e7eb;
                       padding-bottom:8px;font-size:20px;">
                {emoji} {heading}
            </h2>
            {cards_html}
        </div>
        """

    # If no jobs matched at all
    if not sections_html:
        sections_html = """
        <div style="text-align:center;padding:40px;color:#6b7280;">
            <p style="font-size:18px;">Inga nya matchande jobb idag.</p>
            <p>Systemet kollade alla tre källor men hittade inga
               nya relevanta annonser sedan senaste körningen.</p>
        </div>
        """

    # Complete email HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
                 Roboto,sans-serif;background:#f3f4f6;margin:0;padding:0;">
        <div style="max-width:640px;margin:0 auto;padding:20px;">
            <!-- Header -->
            <div style="background:linear-gradient(135deg,#1e3a5f,#2563eb);
                        color:white;padding:24px;border-radius:12px 12px 0 0;
                        text-align:center;">
                <h1 style="margin:0;font-size:24px;">🔐 Jobbmatch</h1>
                <p style="margin:8px 0 0;opacity:0.9;font-size:14px;">
                    {today}
                </p>
                <p style="margin:4px 0 0;opacity:0.7;font-size:12px;">
                    {len(jobs)} matchande jobb hittade
                </p>
            </div>

            <!-- Content -->
            <div style="background:white;padding:24px;
                        border-radius:0 0 12px 12px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
                {sections_html}
            </div>

            <!-- Footer -->
            <div style="text-align:center;padding:16px;color:#9ca3af;font-size:12px;">
                <p>Genererat av Job Hunter — AI-driven jobbmatchning</p>
                <p>Powered by Claude API + GitHub Actions</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_email(jobs: list[dict[str, Any]]) -> bool:
    """
    Build and send the job digest email via SendGrid.

    Returns True if the email was sent successfully, False otherwise.
    Environment variables required:
    - SENDGRID_API_KEY: SendGrid API key
    - EMAIL_FROM: Sender email address (must be verified in SendGrid)
    - EMAIL_TO: Recipient email address
    """
    config = load_config()

    # Get credentials from environment
    api_key = os.environ.get("SENDGRID_API_KEY")
    email_from = os.environ.get("EMAIL_FROM")
    email_to = os.environ.get("EMAIL_TO")

    if not all([api_key, email_from, email_to]):
        logger.error(
            "Missing email configuration. Ensure SENDGRID_API_KEY, "
            "EMAIL_FROM, and EMAIL_TO are set."
        )
        return False

    # Build email content
    today = datetime.now().strftime("%a %d %b")
    subject_prefix = config.get("email", {}).get("subject_prefix", "🔐 Jobbmatch")
    subject = f"{subject_prefix} — {today}"

    html_content = build_email_html(jobs, config)

    # Send via SendGrid
    try:
        message = Mail(
            from_email=email_from,
            to_emails=email_to,
            subject=subject,
            html_content=Content("text/html", html_content),
        )

        sg = SendGridAPIClient(api_key)
        response = sg.send(message)

        logger.info(
            f"Email sent successfully (status: {response.status_code}) "
            f"to {email_to} with {len(jobs)} jobs"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Test with sample data — renders HTML to file for preview
    sample_jobs = [
        {
            "id": "test_1",
            "title": "Junior SOC Analyst",
            "company": "SecureCorp AB",
            "location": "Göteborg",
            "url": "https://example.com/job/1",
            "description": "Monitor security events...",
            "source": "JobTech",
            "ai_score": 9,
            "ai_summary": "Excellent match — junior security role in Gothenburg, no degree required.",
            "ai_flag": "GOLD",
            "ai_tough_match": False,
        },
        {
            "id": "test_2",
            "title": "DevOps Engineer",
            "company": "TechStart AB",
            "location": "Remote, Sweden",
            "url": "https://example.com/job/2",
            "description": "Automate infrastructure...",
            "source": "Remotive",
            "ai_score": 7,
            "ai_summary": "Good match — automation focus aligns with Python/GitHub Actions skills.",
            "ai_flag": "SILVER",
            "ai_tough_match": False,
        },
        {
            "id": "test_3",
            "title": "Senior Penetration Tester",
            "company": "HackDefend",
            "location": "Remote, Europe",
            "url": "https://example.com/job/3",
            "description": "Lead penetration testing...",
            "source": "WeWorkRemotely",
            "ai_score": 5,
            "ai_summary": "Stretch goal — requires 3+ years but aligns with long-term career target.",
            "ai_flag": "STRETCH",
            "ai_tough_match": True,
        },
    ]

    config = load_config()
    html = build_email_html(sample_jobs, config)

    preview_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "email_preview.html",
    )
    with open(preview_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Email preview saved to {preview_path}")
