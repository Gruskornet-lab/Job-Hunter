# 🔐 Job Hunter — AI-Driven Job Matching Automation

An automated job hunting pipeline that fetches listings from multiple sources, scores them using AI (Claude API), and delivers a prioritized daily email digest.

**Built for:** A career changer transitioning from accounting into IT security — but the architecture is generic and config-driven, so it can be adapted to any job search profile.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  GitHub Actions                      │
│              (Weekdays 07:00 CET)                   │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  1. FETCH — job_fetcher.py                          │
│     ├── JobTech API (Swedish public jobs)            │
│     ├── Remotive API (Remote EU/Global)              │
│     └── WeWorkRemotely (Scraped)                     │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  2. DEDUPLICATE — deduplicator.py                   │
│     └── Filter against seen_jobs.json               │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  3. AI MATCH — ai_matcher.py                        │
│     └── Claude API scores each job 1-10             │
│         and classifies: GOLD / SILVER / STRETCH     │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  4. EMAIL — email_sender.py                         │
│     └── HTML digest via SendGrid                     │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────┐
│  5. PERSIST — deduplicator.py                       │
│     └── Update seen_jobs.json + git commit           │
└──────────────────────────────────────────────────────┘
```

---

## Project Structure

```
job-hunter/
├── .github/workflows/
│   └── job_hunter.yml        # GitHub Actions workflow (cron + manual)
├── src/
│   ├── job_fetcher.py        # Multi-source job fetcher with normalization
│   ├── ai_matcher.py         # Claude API integration for scoring/classification
│   ├── email_sender.py       # SendGrid HTML email builder and sender
│   └── deduplicator.py       # JSON-based deduplication with TTL pruning
├── config/
│   └── profile.yaml          # Candidate profile, preferences, and settings
├── data/
│   └── seen_jobs.json        # Deduplication state (auto-managed)
├── main.py                   # Pipeline orchestrator
├── requirements.txt          # Pinned Python dependencies
├── .gitignore
└── README.md
```

---

## Setup Instructions

### Prerequisites

- Python 3.11+
- A GitHub account
- An [Anthropic API key](https://console.anthropic.com/)
- A [SendGrid API key](https://sendgrid.com/) (free tier: 100 emails/day)
- A verified sender email in SendGrid

### Step 1: Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/job-hunter.git
cd job-hunter
```

### Step 2: Install Dependencies Locally (for testing)

```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### Step 3: Configure Your Profile

Edit `config/profile.yaml` to match your background, skills, and preferences. The key sections are:

- **`candidate`** — Your skills, learning platforms, and goals
- **`job_priorities`** — Keywords for each matching tier (GOLD, SILVER, BRONZE)
- **`geography`** — Cities for on-site/hybrid + remote preferences
- **`ai.matching_context`** — Special instructions for the AI matcher

### Step 4: Set Up GitHub Secrets

In your GitHub repo, go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `SENDGRID_API_KEY` | Your SendGrid API key |
| `EMAIL_FROM` | Verified sender email (e.g., `jobhunter@yourdomain.com`) |
| `EMAIL_TO` | Where to receive the daily digest |

### Step 5: Test Locally

```bash
export ANTHROPIC_API_KEY="your-key"
export SENDGRID_API_KEY="your-key"
export EMAIL_FROM="sender@example.com"
export EMAIL_TO="you@example.com"

python main.py
```

### Step 6: Deploy

Push to GitHub. The workflow runs automatically on weekdays at 07:00 CET, or trigger manually via **Actions → Job Hunter → Run workflow**.

---

## Design Decisions

### Why these three job sources?

| Source | Coverage | Method | Why |
|--------|----------|--------|-----|
| **JobTech** | Sweden | API | Official Swedish employment service — best coverage for local/hybrid roles |
| **Remotive** | EU + Global | API | Curated remote-only listings, good signal-to-noise ratio |
| **WeWorkRemotely** | Global | Scraping | Large remote job board, catches listings not on Remotive |

### Why Claude API for matching instead of keyword filtering?

Simple keyword matching produces too many false positives ("Security Guard" matches "security") and misses contextually relevant listings. Claude understands that a "Junior SOC Analyst" role is perfect for a career changer with TryHackMe experience, while a "Senior Security Architect" is a stretch but still worth flagging.

### Why SendGrid instead of a simpler notification method?

HTML emails allow rich formatting (score badges, color coding, organized sections) that plain-text notifications can't match. The daily digest format is more professional than individual push notifications and serves as a historical record in the inbox.

### Why JSON file for deduplication instead of a database?

The dataset is small (hundreds of job IDs per month), the read/write pattern is simple (load all → filter → append → save), and the file commits to Git — giving us both persistence and a version history of what was seen when.

### Why individual Claude API calls instead of batching?

Sending one job at a time costs more API tokens but produces significantly better assessments. When jobs are batched, Claude tends to give shorter, less nuanced summaries and occasionally confuses details between listings.

---

## Cost Estimate

| Resource | Usage | Cost |
|----------|-------|------|
| GitHub Actions | ~5 min/day × 22 days/month | Free (within free tier) |
| Claude API (Sonnet) | ~50 jobs × ~1K tokens each | ~$0.50–1.00/month |
| SendGrid | 1 email/day | Free (within 100/day tier) |
| **Total** | | **< $1/month** |

---

## Extending the System

### Adding a new job source

1. Create a new `fetch_<source>()` function in `job_fetcher.py`
2. Return the standard job dict format: `{id, title, company, location, url, description, source}`
3. Add it to the `fetch_all_jobs()` function
4. Add config in `profile.yaml` under `sources`

### Adding a new matching tier

1. Add the tier in `config/profile.yaml` under `job_priorities`
2. Update the `matching_context` to tell Claude about the new tier
3. Add a section entry under `email.sections`

### Changing the schedule

Edit the cron expression in `.github/workflows/job_hunter.yml`. Remember that GitHub Actions cron uses UTC.

---

## Skills Demonstrated

This project showcases:

- **Python automation** — Multi-source data fetching, normalization, and pipeline orchestration
- **AI integration** — Prompt engineering for structured JSON output from Claude API
- **CI/CD** — GitHub Actions with scheduled workflows and state persistence
- **API integration** — REST APIs (JobTech, Remotive), SendGrid email API
- **Web scraping** — BeautifulSoup HTML parsing with rate limiting
- **Configuration-driven design** — YAML-based profile that separates config from code
- **Error handling** — Graceful degradation (one source fails, others still work)
- **Documentation** — Professional README with architecture diagrams and setup guides

---

## License

MIT — See [LICENSE](LICENSE) for details.
