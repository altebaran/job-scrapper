# 🎯 Daily Healthcare Job Scraper

Fully automated job scraper that runs daily on **GitHub Actions** (free), scrapes 15+ job boards and 40+ company career pages, scores relevance, and publishes a beautiful report to **GitHub Pages** — plus optional email delivery.

**Zero infrastructure. Zero cost. Zero daily effort.**

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions (runs daily at 07:00 CET)                   │
│                                                             │
│  6 Source Categories        Relevance Engine    Output       │
│  ├─ LinkedIn           →   ├─ Title match  →   ├─ GitHub    │
│  ├─ Indeed DE               ├─ Keywords          Pages      │
│  ├─ StepStone               ├─ Location        ├─ Email     │
│  ├─ 40+ Career Pages        ├─ Company         └─ Artifact  │
│  ├─ Startup Boards          ├─ Salary signal                │
│  └─ Remote Boards           └─ Dedup + new-only filter      │
└─────────────────────────────────────────────────────────────┘
```

## ⚡ 5-Minute Setup

### Step 1: Create the GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `job-scraper` (private repo recommended)
3. **Do NOT** initialize with README (we'll push our own files)
4. Click **Create repository**

### Step 2: Push This Code

```bash
# In your terminal, navigate to this folder:
cd job-scraper

# Initialize git and push:
git init
git add .
git commit -m "🚀 Initial setup"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/job-scraper.git
git push -u origin main
```

### Step 3: Enable GitHub Pages

1. Go to your repo → **Settings** → **Pages** (left sidebar)
2. Under **Source**, select **GitHub Actions**
3. Click **Save**

Your dashboard will be live at: `https://YOUR_USERNAME.github.io/job-scraper/`

### Step 4: Set Up Email Notifications (Optional but Recommended)

To receive daily reports in your inbox:

1. **Create a Gmail App Password** (if using Gmail):
   - Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   - Select "Mail" → "Other" → name it "Job Scraper"
   - Copy the 16-character password

2. **Add GitHub Secrets**:
   Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

   Add these 5 secrets:

   | Secret Name      | Value                          |
   |-----------------|--------------------------------|
   | `SMTP_SERVER`   | `smtp.gmail.com`               |
   | `SMTP_PORT`     | `587`                          |
   | `EMAIL_USERNAME`| `your.email@gmail.com`         |
   | `EMAIL_PASSWORD`| Your 16-char app password      |
   | `EMAIL_TO`      | `your.email@gmail.com`         |

   > **Not using Gmail?** Use your provider's SMTP settings:
   > - Outlook: `smtp-mail.outlook.com` / port `587`
   > - Yahoo: `smtp.mail.yahoo.com` / port `587`
   > - ProtonMail Bridge: `127.0.0.1` / port `1025`

### Step 5: Run It!

1. Go to your repo → **Actions** tab
2. Click **🎯 Daily Job Scrape** in the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Watch it run (takes ~5–10 minutes)

**That's it!** From now on, the scraper runs automatically every day at 07:00 CET.

---

## 📊 Accessing Your Reports

You have three ways to see results:

### 1. GitHub Pages Dashboard (Recommended)
Visit `https://YOUR_USERNAME.github.io/job-scraper/`
- Dashboard with all past reports
- Click any date to see that day's matches
- Bookmark it or add to your phone's home screen

### 2. Email
If configured, you'll receive the full HTML report in your inbox every morning.

### 3. GitHub Actions Artifacts
Go to **Actions** → click any run → scroll down to **Artifacts** → download the report.

---

## 🛠 Customizing

### Adjust Relevance Sensitivity

In `config.yaml`:

```yaml
profile:
  min_relevance_score: 40  # Lower = more results, higher = stricter
```

- **30**: Cast a wide net (more noise)
- **40**: Balanced (default)
- **50**: Focused (fewer but more relevant)
- **60+**: Very strict (only near-perfect matches)

### Add Target Companies

```yaml
target_companies:
  scale_ups:
    - name: "NewCompany"
      careers_url: "https://newcompany.com/careers/"
      hq: "Berlin"
```

### Add Search Queries

```yaml
search_queries:
  - "Your Custom Query Here"
```

### Change Schedule

In `.github/workflows/daily-scrape.yml`, edit the cron:

```yaml
schedule:
  - cron: "0 6 * * *"  # 06:00 UTC = 07:00 CET / 08:00 CEST
```

Some useful schedules:
- `"0 6 * * 1-5"` — Weekdays only
- `"0 6,18 * * *"` — Twice daily (7 AM and 7 PM CET)
- `"0 5 * * *"` — Earlier, at 6 AM CET

### Scoring Weights

| Factor | Max Points | What It Matches |
|--------|-----------|-----------------|
| Title match | 35 | Exact target title (Head of Strategy, Director BD...) |
| Seniority fallback | 15 | Seniority indicator in title (VP, Director, Lead...) |
| Keywords | 30 | Healthcare/digital health/pharma + function keywords |
| Location | 20 | Germany, Berlin, Munich, Remote, DACH, EMEA... |
| Target company | 15 | One of the 40+ pre-configured companies |
| Salary signal | +10/-20 | Salary ≥100k or <60k detected |
| Negative keyword | -40 | "junior", "intern", "trainee" in title |

---

## 🔧 Manual Operations

### Trigger a Manual Run
Actions → 🎯 Daily Job Scrape → Run workflow

### Reset Seen Jobs (Show Everything as New)
Actions → 🎯 Daily Job Scrape → Run workflow → Set "Reset seen jobs" to `true`

### Run Locally
```bash
pip install -r requirements.txt
python scraper.py              # Full run
python scraper.py --dry-run    # Test config
python scraper.py --source linkedin  # Single source
python scraper.py --reset      # Clear history
```

---

## 📁 Project Structure

```
job-scraper/
├── .github/
│   └── workflows/
│       └── daily-scrape.yml    ← GitHub Actions workflow
├── docs/                        ← GitHub Pages (auto-updated)
│   ├── index.html               ← Dashboard
│   └── report-YYYY-MM-DD.html  ← Daily reports
├── data/
│   └── seen_jobs.json           ← Persisted via git commits
├── reports/
│   ├── latest-report.html       ← Used for email
│   └── latest-report.md
├── scraper.py                   ← Main scraper engine
├── config.yaml                  ← All settings
├── requirements.txt
└── README.md
```

---

## 💡 Tips

- **LinkedIn is unreliable** for automated scraping — it aggressively blocks bots. The scraper handles this gracefully but don't rely on LinkedIn alone. Your network-based approach (DayOne, DMEA connections) will catch roles that no scraper can.

- **Career page structures change** — if a target company redesigns their site, the scraper will silently return 0 results from that source. Check the Actions logs periodically.

- **GitHub Actions free tier** gives you 2,000 minutes/month for private repos. Each scrape takes ~5–10 min, so daily runs use ~150–300 min/month — well within the limit.

- **GitHub Pages** is free for public and private repos on paid plans. For free accounts, the repo must be public for Pages to work. If you want a private repo without Pages, email delivery and artifact downloads still work.

---

## ⚠️ Important Notes

- This is for **personal job search use only**
- The scraper includes polite delays (1.5–3.5s) between requests
- Respect each site's terms of service and robots.txt
- If a site blocks you, the scraper handles it gracefully and moves on
