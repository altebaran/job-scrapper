#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  DAILY JOB SCRAPER — GitHub Actions Edition                 ║
║  Healthcare / Digital Health / Pharma Scale-ups & Corp.     ║
║  Innovation Centers — Germany & Remote-from-DE              ║
╚══════════════════════════════════════════════════════════════╝

Designed to run via GitHub Actions on a daily cron schedule.
Reports are deployed to GitHub Pages for easy access.

Local usage:
    python scraper.py                    # Run full scrape + report
    python scraper.py --dry-run          # Test config, no scraping
    python scraper.py --source linkedin  # Scrape single source
    python scraper.py --reset            # Clear seen jobs DB
"""

import os
import sys
import json
import re
import hashlib
import logging
import argparse
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlencode, quote_plus

import yaml
import requests
from bs4 import BeautifulSoup

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jobscraper")

IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"

def set_github_env(key, value):
    """Set environment variable for subsequent GitHub Actions steps."""
    if IS_CI:
        env_file = os.environ.get("GITHUB_ENV", "")
        if env_file:
            with open(env_file, "a") as f:
                f.write(f"{key}={value}\n")


# ── Data Model ───────────────────────────────────────────────
@dataclass
class JobPosting:
    title: str
    company: str
    location: str
    url: str
    source: str
    description: str = ""
    salary_info: str = ""
    date_posted: str = ""
    relevance_score: float = 0.0
    match_reasons: list = field(default_factory=list)
    job_id: str = ""

    def __post_init__(self):
        if not self.job_id:
            raw = f"{self.title}|{self.company}|{self.url}"
            self.job_id = hashlib.md5(raw.encode()).hexdigest()


# ── Configuration ────────────────────────────────────────────
class Config:
    def __init__(self, path="config.yaml"):
        with open(path) as f:
            self._cfg = yaml.safe_load(f)

    def __getattr__(self, key):
        return self._cfg.get(key, {})

    @property
    def target_titles(self):
        return [t.lower() for t in self._cfg["profile"]["target_titles"]]

    @property
    def positive_keywords(self):
        return [k.lower() for k in self._cfg["profile"]["positive_keywords"]]

    @property
    def negative_keywords(self):
        return [k.lower() for k in self._cfg["profile"]["negative_keywords"]]

    @property
    def min_score(self):
        return self._cfg["profile"]["min_relevance_score"]

    @property
    def search_queries(self):
        return self._cfg["search_queries"]

    @property
    def location_include(self):
        return [l.lower() for l in self._cfg["locations"]["include"]]

    @property
    def location_exclude(self):
        return [l.lower() for l in self._cfg["locations"]["exclude"]]

    @property
    def seniority_indicators(self):
        return [s.lower() for s in self._cfg["salary"]["seniority_indicators"]]

    @property
    def target_companies(self):
        companies = []
        for category in self._cfg.get("target_companies", {}).values():
            companies.extend(category)
        return companies


# ── Seen Jobs Database ───────────────────────────────────────
class SeenJobsDB:
    """Track previously seen jobs. State persists via git commits in CI."""

    def __init__(self, path="./data/seen_jobs.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, KeyError):
                log.warning("Corrupted seen_jobs.json, starting fresh")
        return {"seen": {}, "last_run": None, "stats": {"total_seen": 0, "total_reported": 0}}

    def save(self):
        self.data["last_run"] = datetime.now().isoformat()
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def is_new(self, job: JobPosting) -> bool:
        return job.job_id not in self.data["seen"]

    def mark_seen(self, job: JobPosting):
        self.data["seen"][job.job_id] = {
            "title": job.title,
            "company": job.company,
            "first_seen": datetime.now().isoformat(),
            "score": job.relevance_score,
        }

    def cleanup(self, days=30):
        cutoff = datetime.now() - timedelta(days=days)
        before = len(self.data["seen"])
        self.data["seen"] = {
            k: v for k, v in self.data["seen"].items()
            if datetime.fromisoformat(v["first_seen"]) > cutoff
        }
        removed = before - len(self.data["seen"])
        if removed:
            log.info(f"Cleaned up {removed} entries older than {days} days")

    def reset(self):
        self.data = {"seen": {}, "last_run": None, "stats": {"total_seen": 0, "total_reported": 0}}
        self.save()
        log.info("Seen jobs database reset.")

    def update_stats(self, new_count):
        if "stats" not in self.data:
            self.data["stats"] = {"total_seen": 0, "total_reported": 0}
        self.data["stats"]["total_seen"] = len(self.data["seen"])
        self.data["stats"]["total_reported"] += new_count


# ── Relevance Scorer ─────────────────────────────────────────
class RelevanceScorer:
    def __init__(self, config: Config):
        self.cfg = config

    def score(self, job: JobPosting) -> JobPosting:
        score = 0.0
        reasons = []
        text = f"{job.title} {job.company} {job.description} {job.location}".lower()

        # Title match (0–35)
        title_lower = job.title.lower()
        for target in self.cfg.target_titles:
            if target in title_lower:
                score += 35
                reasons.append(f"Title match: '{target}'")
                break
        else:
            for indicator in self.cfg.seniority_indicators:
                if indicator in title_lower:
                    score += 15
                    reasons.append(f"Seniority match: '{indicator}'")
                    break

        # Industry/function keywords (0–30)
        kw_hits = [kw for kw in self.cfg.positive_keywords if kw in text]
        if kw_hits:
            kw_score = min(30, len(kw_hits) * 5)
            score += kw_score
            reasons.append(f"Keywords ({len(kw_hits)}): {', '.join(kw_hits[:5])}")

        # Location match (0–20)
        loc_lower = f"{job.location} {job.description[:500]}".lower()
        for loc in self.cfg.location_include:
            if loc in loc_lower:
                score += 20
                reasons.append(f"Location: '{loc}'")
                break

        # Location exclusion
        for exc in self.cfg.location_exclude:
            if exc in loc_lower:
                score -= 50
                reasons.append(f"Location excluded: '{exc}'")

        # Company match (0–15)
        company_lower = job.company.lower()
        for tc in self.cfg.target_companies:
            if tc["name"].lower() in company_lower or company_lower in tc["name"].lower():
                score += 15
                reasons.append(f"Target company: {tc['name']}")
                break

        # Salary indicators
        salary_text = f"{job.salary_info} {job.description[:1000]}".lower()
        salary_match = re.search(r'(\d{2,3})[.,]?(\d{3})?\s*(?:€|eur|euro)', salary_text)
        if salary_match:
            try:
                amount = int(salary_match.group(1)) * (1000 if not salary_match.group(2) else 1)
                if salary_match.group(2):
                    amount = int(salary_match.group(1) + salary_match.group(2))
                if amount >= 100000:
                    score += 10
                    reasons.append(f"Salary: ~€{amount:,}")
                elif amount < 60000:
                    score -= 20
                    reasons.append(f"Low salary: ~€{amount:,}")
            except (ValueError, TypeError):
                pass

        # Negative keyword penalty
        for neg in self.cfg.negative_keywords:
            if neg in title_lower:
                score -= 40
                reasons.append(f"Negative: '{neg}'")

        job.relevance_score = max(0, min(100, score))
        job.match_reasons = reasons
        return job


# ── HTTP Session ─────────────────────────────────────────────
def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    })
    return session


def polite_delay(min_s=1.5, max_s=3.5):
    time.sleep(random.uniform(min_s, max_s))


# ══════════════════════════════════════════════════════════════
#  SCRAPERS
# ══════════════════════════════════════════════════════════════

class LinkedInScraper:
    SOURCE = "LinkedIn"

    def __init__(self, session, config):
        self.session = session
        self.cfg = config

    def scrape(self) -> list[JobPosting]:
        jobs = []
        queries = self.cfg.search_queries[:8]

        for query in queries:
            try:
                params = {
                    "keywords": query,
                    "location": "Germany",
                    "geoId": "101282230",
                    "f_TPR": "r86400",
                    "f_E": "4,5",
                    "sortBy": "DD",
                    "position": "1",
                    "pageNum": "0",
                }
                url = f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"
                log.info(f"  LinkedIn: '{query[:50]}'")

                resp = self.session.get(url, timeout=15)
                if resp.status_code != 200:
                    log.warning(f"  LinkedIn returned {resp.status_code}")
                    polite_delay(3, 6)
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select(".base-card, .job-search-card, .result-card")

                for card in cards[:10]:
                    try:
                        title_el = card.select_one(".base-search-card__title, .result-card__title, h3")
                        company_el = card.select_one(".base-search-card__subtitle, .result-card__subtitle, h4")
                        loc_el = card.select_one(".job-search-card__location, .job-result-card__location")
                        link_el = card.select_one("a.base-card__full-link, a")
                        date_el = card.select_one("time")

                        if title_el and company_el:
                            jobs.append(JobPosting(
                                title=title_el.get_text(strip=True),
                                company=company_el.get_text(strip=True),
                                location=loc_el.get_text(strip=True) if loc_el else "Germany",
                                url=link_el["href"].split("?")[0] if link_el and link_el.get("href") else "",
                                source=self.SOURCE,
                                date_posted=date_el.get("datetime", "") if date_el else "",
                            ))
                    except Exception as e:
                        log.debug(f"  Card parse error: {e}")

                polite_delay()

            except Exception as e:
                log.warning(f"  LinkedIn query failed: {e}")

        log.info(f"  LinkedIn: {len(jobs)} raw results")
        return jobs


class IndeedScraper:
    SOURCE = "Indeed"

    def __init__(self, session, config):
        self.session = session
        self.cfg = config

    def scrape(self) -> list[JobPosting]:
        jobs = []
        queries = self.cfg.search_queries[:6]

        for query in queries:
            try:
                params = {"q": query, "l": "Deutschland", "fromage": "1", "sort": "date"}
                url = f"https://de.indeed.com/jobs?{urlencode(params)}"
                log.info(f"  Indeed: '{query[:50]}'")

                resp = self.session.get(url, timeout=15)
                if resp.status_code != 200:
                    polite_delay(3, 6)
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select(".job_seen_beacon, .jobsearch-ResultsList > li, .result")

                for card in cards[:10]:
                    try:
                        title_el = card.select_one(".jobTitle span, h2.jobTitle, .jcs-JobTitle")
                        company_el = card.select_one(".companyName, [data-testid='company-name'], .company")
                        loc_el = card.select_one(".companyLocation, [data-testid='text-location']")
                        link_el = card.select_one("a[href*='/rc/clk'], a[href*='viewjob'], h2 a")
                        salary_el = card.select_one(".salary-snippet-container, .estimated-salary")

                        if title_el and company_el:
                            href = ""
                            if link_el and link_el.get("href"):
                                href = link_el["href"]
                                if href.startswith("/"):
                                    href = f"https://de.indeed.com{href}"

                            jobs.append(JobPosting(
                                title=title_el.get_text(strip=True),
                                company=company_el.get_text(strip=True),
                                location=loc_el.get_text(strip=True) if loc_el else "Germany",
                                url=href,
                                source=self.SOURCE,
                                salary_info=salary_el.get_text(strip=True) if salary_el else "",
                            ))
                    except Exception as e:
                        log.debug(f"  Card parse error: {e}")

                polite_delay()

            except Exception as e:
                log.warning(f"  Indeed query failed: {e}")

        log.info(f"  Indeed: {len(jobs)} raw results")
        return jobs


class StepStoneScraper:
    SOURCE = "StepStone"

    def __init__(self, session, config):
        self.session = session
        self.cfg = config

    def scrape(self) -> list[JobPosting]:
        jobs = []
        queries = self.cfg.search_queries[:5]

        for query in queries:
            try:
                url = f"https://www.stepstone.de/jobs/{quote_plus(query)}?age=1&sort=date"
                log.info(f"  StepStone: '{query[:50]}'")

                resp = self.session.get(url, timeout=15)
                if resp.status_code != 200:
                    polite_delay(3, 6)
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("[data-testid='job-item'], .res-1p8ewa0, article")

                for card in cards[:10]:
                    try:
                        title_el = card.select_one("[data-testid='job-item-title'], h2, .res-nehv70")
                        company_el = card.select_one("[data-testid='job-item-company'], .res-1r68bfv")
                        loc_el = card.select_one("[data-testid='job-item-location'], .res-1w6cxnr")
                        link_el = card.select_one("a[href*='stellenangebote'], a[href*='/jobs/']")

                        if title_el:
                            href = ""
                            if link_el and link_el.get("href"):
                                href = link_el["href"]
                                if href.startswith("/"):
                                    href = f"https://www.stepstone.de{href}"

                            jobs.append(JobPosting(
                                title=title_el.get_text(strip=True),
                                company=company_el.get_text(strip=True) if company_el else "",
                                location=loc_el.get_text(strip=True) if loc_el else "Germany",
                                url=href,
                                source=self.SOURCE,
                            ))
                    except Exception as e:
                        log.debug(f"  Card parse error: {e}")

                polite_delay()

            except Exception as e:
                log.warning(f"  StepStone query failed: {e}")

        log.info(f"  StepStone: {len(jobs)} raw results")
        return jobs


class CompanyCareersScraper:
    SOURCE = "Direct"

    def __init__(self, session, config):
        self.session = session
        self.cfg = config

    def scrape(self) -> list[JobPosting]:
        jobs = []
        companies = self.cfg.target_companies

        for company in companies:
            try:
                url = company["careers_url"]
                name = company["name"]
                log.info(f"  Careers: {name}")

                resp = self.session.get(url, timeout=12)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                selectors = [
                    "a[href*='job'], a[href*='position'], a[href*='career']",
                    ".job-listing a, .opening a, .position a",
                    "[class*='job'] a, [class*='opening'] a, [class*='position'] a",
                    "li a[href*='lever.co'], li a[href*='greenhouse.io'], li a[href*='workable.com']",
                    "li a[href*='smartrecruiters'], li a[href*='recruitee']",
                    "a[href*='ashbyhq.com'], a[href*='personio']",
                ]

                found_links = set()
                for selector in selectors:
                    for el in soup.select(selector):
                        href = el.get("href", "")
                        text = el.get_text(strip=True)
                        if text and len(text) > 5 and href not in found_links:
                            found_links.add(href)
                            if not href.startswith("http"):
                                href = url.rstrip("/") + "/" + href.lstrip("/")
                            jobs.append(JobPosting(
                                title=text,
                                company=name,
                                location=company.get("hq", "Germany"),
                                url=href,
                                source=f"{self.SOURCE} ({name})",
                            ))

                polite_delay(1, 2)

            except Exception as e:
                log.debug(f"  {company['name']} career page error: {e}")

        log.info(f"  Direct careers: {len(jobs)} raw results")
        return jobs


class StartupJobBoardScraper:
    SOURCE = "StartupBoard"

    def __init__(self, session, config):
        self.session = session
        self.cfg = config

    def _scrape_berlin_startup_jobs(self) -> list[JobPosting]:
        jobs = []
        try:
            for category in ["business", "management", "product"]:
                url = f"https://berlinstartupjobs.com/skill-areas/{category}/"
                log.info(f"  BerlinStartupJobs: {category}")
                resp = self.session.get(url, timeout=12)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                for card in soup.select(".bsj-jb, .job-listing, article")[:15]:
                    title_el = card.select_one("h4 a, .bsj-jb__title a, h3 a")
                    company_el = card.select_one(".bsj-jb__company, .company-name")
                    if title_el:
                        jobs.append(JobPosting(
                            title=title_el.get_text(strip=True),
                            company=company_el.get_text(strip=True) if company_el else "",
                            location="Berlin, Germany",
                            url=title_el.get("href", ""),
                            source="BerlinStartupJobs",
                        ))
                polite_delay(1, 2)
        except Exception as e:
            log.warning(f"  BerlinStartupJobs error: {e}")
        return jobs

    def _scrape_german_tech_jobs(self) -> list[JobPosting]:
        jobs = []
        try:
            for query in ["healthcare", "health", "pharma", "medical", "strategy", "business development"]:
                url = f"https://germantechjobs.de/jobs?search={quote_plus(query)}"
                log.info(f"  GermanTechJobs: {query}")
                resp = self.session.get(url, timeout=12)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                for card in soup.select(".card, .job-card, article, .job-listing")[:10]:
                    title_el = card.select_one("h2 a, h3 a, .card-title a, a.job-title")
                    company_el = card.select_one(".company, .card-subtitle, .employer")
                    loc_el = card.select_one(".location, .city")
                    if title_el:
                        href = title_el.get("href", "")
                        if href and not href.startswith("http"):
                            href = f"https://germantechjobs.de{href}"
                        jobs.append(JobPosting(
                            title=title_el.get_text(strip=True),
                            company=company_el.get_text(strip=True) if company_el else "",
                            location=loc_el.get_text(strip=True) if loc_el else "Germany",
                            url=href,
                            source="GermanTechJobs",
                        ))
                polite_delay(1, 2)
        except Exception as e:
            log.warning(f"  GermanTechJobs error: {e}")
        return jobs

    def scrape(self) -> list[JobPosting]:
        jobs = []
        jobs.extend(self._scrape_berlin_startup_jobs())
        jobs.extend(self._scrape_german_tech_jobs())
        log.info(f"  Startup boards: {len(jobs)} raw results")
        return jobs


class RemoteJobScraper:
    SOURCE = "Remote"

    def __init__(self, session, config):
        self.session = session
        self.cfg = config

    def scrape(self) -> list[JobPosting]:
        jobs = []
        try:
            url = "https://remoteok.com/remote-healthcare-jobs"
            log.info(f"  RemoteOK: healthcare")
            resp = self.session.get(url, timeout=12)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for row in soup.select("tr.job, .job")[:15]:
                    title_el = row.select_one("h2, .company_and_position h2")
                    company_el = row.select_one("h3, .companyLink h3")
                    link_el = row.select_one("a[href*='/remote-jobs/']")
                    if title_el:
                        href = ""
                        if link_el:
                            href = link_el.get("href", "")
                            if href.startswith("/"):
                                href = f"https://remoteok.com{href}"
                        jobs.append(JobPosting(
                            title=title_el.get_text(strip=True),
                            company=company_el.get_text(strip=True) if company_el else "",
                            location="Remote",
                            url=href,
                            source="RemoteOK",
                        ))
            polite_delay()
        except Exception as e:
            log.warning(f"  RemoteOK error: {e}")

        log.info(f"  Remote boards: {len(jobs)} raw results")
        return jobs


# ══════════════════════════════════════════════════════════════
#  REPORT GENERATOR
# ══════════════════════════════════════════════════════════════

class ReportGenerator:
    def __init__(self, config: Config):
        self.cfg = config
        self.report_dir = Path("reports")
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir = Path("docs")
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, jobs: list[JobPosting], seen_db: SeenJobsDB) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H:%M")

        jobs.sort(key=lambda j: j.relevance_score, reverse=True)

        # ── Individual HTML Report ──
        html = self._build_html(jobs, today, time_str)

        # Save dated report
        dated_path = self.report_dir / f"job-report-{today}.html"
        with open(dated_path, "w", encoding="utf-8") as f:
            f.write(html)

        # Save as "latest" for email + artifact
        latest_path = self.report_dir / "latest-report.html"
        with open(latest_path, "w", encoding="utf-8") as f:
            f.write(html)

        # ── Markdown ──
        md = self._build_markdown(jobs, today, time_str)
        md_path = self.report_dir / "latest-report.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)

        # ── GitHub Pages: copy to docs/ + update index ──
        docs_report = self.docs_dir / f"report-{today}.html"
        with open(docs_report, "w", encoding="utf-8") as f:
            f.write(html)

        self._update_pages_index(jobs, today, seen_db)

        log.info(f"Reports saved: {dated_path}, {docs_report}")
        return str(latest_path)

    def _update_pages_index(self, jobs, today, seen_db):
        """Build a dashboard index.html for GitHub Pages listing all reports."""
        # Find all existing reports in docs/
        reports = sorted(self.docs_dir.glob("report-*.html"), reverse=True)

        report_links = ""
        for rp in reports[:60]:
            date_str = rp.stem.replace("report-", "")
            is_today = date_str == today
            badge = f'<span class="badge new">TODAY — {len(jobs)} jobs</span>' if is_today else ""
            report_links += f'''
            <a href="{rp.name}" class="report-link {'today' if is_today else ''}">
                <span class="date">{date_str}</span>
                {badge}
            </a>'''

        stats = seen_db.data.get("stats", {})

        index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🎯 Job Scraper Dashboard</title>
<style>
    :root {{ --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --green: #22c55e; }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Inter', -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
    .container {{ max-width: 720px; margin: 0 auto; padding: 2rem; }}
    header {{ text-align: center; margin-bottom: 2.5rem; }}
    header h1 {{ font-size: 2rem; margin-bottom: 0.5rem; background: linear-gradient(135deg, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    header p {{ color: var(--muted); font-size: 1rem; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin: 1.5rem 0; }}
    .stat-card {{ background: var(--card); padding: 1.2rem; border-radius: 12px; text-align: center; border: 1px solid #334155; }}
    .stat-card .number {{ font-size: 1.8rem; font-weight: 700; color: var(--accent); }}
    .stat-card .label {{ font-size: 0.8rem; color: var(--muted); margin-top: 0.3rem; }}
    h2 {{ margin: 2rem 0 1rem; color: var(--muted); font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em; }}
    .report-link {{ display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.2rem; background: var(--card); border-radius: 10px; margin-bottom: 0.5rem; text-decoration: none; color: var(--text); border: 1px solid #334155; transition: all 0.2s; }}
    .report-link:hover {{ border-color: var(--accent); background: #334155; }}
    .report-link.today {{ border-color: var(--green); background: #1a2e1a; }}
    .report-link .date {{ font-weight: 600; font-size: 1rem; }}
    .badge {{ font-size: 0.75rem; padding: 0.25rem 0.6rem; border-radius: 20px; font-weight: 600; }}
    .badge.new {{ background: var(--green); color: #0f172a; }}
    footer {{ text-align: center; margin-top: 3rem; padding: 1rem; color: var(--muted); font-size: 0.8rem; }}
    footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>🎯 Job Scraper Dashboard</h1>
        <p>Healthcare · Digital Health · Pharma — Germany & Remote</p>
    </header>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="number">{len(jobs)}</div>
            <div class="label">Today's Matches</div>
        </div>
        <div class="stat-card">
            <div class="number">{stats.get('total_reported', 0)}</div>
            <div class="label">Total Reported</div>
        </div>
        <div class="stat-card">
            <div class="number">{len(reports)}</div>
            <div class="label">Reports Generated</div>
        </div>
    </div>

    <h2>📋 Daily Reports</h2>
    {report_links}

    <footer>
        <p>Auto-updated daily at 07:00 CET via <a href="https://github.com/features/actions">GitHub Actions</a></p>
    </footer>
</div>
</body>
</html>"""

        with open(self.docs_dir / "index.html", "w", encoding="utf-8") as f:
            f.write(index_html)

    def _build_html(self, jobs, today, time_str):
        rows = ""
        for i, job in enumerate(jobs, 1):
            score_color = (
                "#16a34a" if job.relevance_score >= 70
                else "#ca8a04" if job.relevance_score >= 50
                else "#94a3b8"
            )
            reasons_html = "<br>".join(f"• {r}" for r in job.match_reasons)
            rows += f"""
            <tr class="job-row" onclick="this.classList.toggle('expanded')">
                <td class="rank">{i}</td>
                <td>
                    <div class="title"><a href="{job.url}" target="_blank" rel="noopener">{job.title}</a></div>
                    <div class="company">{job.company}</div>
                    <div class="meta">
                        <span class="location">📍 {job.location}</span>
                        <span class="source">via {job.source}</span>
                        {f'<span class="salary">💰 {job.salary_info}</span>' if job.salary_info else ''}
                        {f'<span class="date">📅 {job.date_posted[:10]}</span>' if job.date_posted else ''}
                    </div>
                    <div class="reasons">{reasons_html}</div>
                </td>
                <td>
                    <div class="score" style="background-color: {score_color}">
                        {job.relevance_score:.0f}
                    </div>
                </td>
            </tr>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Report — {today}</title>
<style>
    :root {{ --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Inter', -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 2rem; }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    header {{ text-align: center; margin-bottom: 2rem; padding: 2rem; background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border-radius: 16px; border: 1px solid #334155; }}
    header h1 {{ font-size: 1.8rem; margin-bottom: 0.5rem; background: linear-gradient(135deg, #38bdf8, #818cf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
    header p {{ color: var(--muted); font-size: 0.95rem; }}
    .stats {{ display: flex; gap: 1rem; justify-content: center; margin-top: 1rem; flex-wrap: wrap; }}
    .stat {{ background: #334155; padding: 0.5rem 1rem; border-radius: 8px; font-size: 0.85rem; }}
    .stat strong {{ color: var(--accent); }}
    .back-link {{ display: inline-block; margin-bottom: 1.5rem; color: var(--accent); text-decoration: none; font-size: 0.9rem; }}
    .back-link:hover {{ text-decoration: underline; }}
    table {{ width: 100%; border-collapse: collapse; }}
    .job-row {{ background: var(--card); cursor: pointer; transition: background 0.2s; }}
    .job-row:hover {{ background: #334155; }}
    .job-row td {{ padding: 1rem; border-bottom: 1px solid #334155; vertical-align: top; }}
    .rank {{ width: 40px; text-align: center; color: var(--muted); font-weight: 600; }}
    .title a {{ color: var(--accent); text-decoration: none; font-weight: 600; font-size: 1.05rem; }}
    .title a:hover {{ text-decoration: underline; }}
    .company {{ color: #f1f5f9; margin: 0.25rem 0; font-weight: 500; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 0.75rem; color: var(--muted); font-size: 0.8rem; margin-top: 0.4rem; }}
    .score {{ width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; color: white; font-weight: 700; font-size: 0.85rem; }}
    .reasons {{ display: none; margin-top: 0.6rem; font-size: 0.8rem; color: var(--muted); line-height: 1.6; }}
    .expanded .reasons {{ display: block; }}
    .empty {{ text-align: center; padding: 3rem; color: var(--muted); }}
    footer {{ text-align: center; margin-top: 2rem; color: var(--muted); font-size: 0.8rem; }}
</style>
</head>
<body>
<div class="container">
    <a href="index.html" class="back-link">← Dashboard</a>
    <header>
        <h1>🎯 Daily Job Report</h1>
        <p>Healthcare · Digital Health · Pharma — Scale-ups & Innovation Centers</p>
        <div class="stats">
            <div class="stat"><strong>{len(jobs)}</strong> new matches</div>
            <div class="stat"><strong>{today}</strong> {time_str}</div>
            <div class="stat"><strong>{len([j for j in jobs if j.relevance_score >= 70])}</strong> high relevance</div>
        </div>
    </header>
    <table>
        <tbody>
            {rows if jobs else '<tr><td colspan="3" class="empty">No new relevant jobs found today. Check back tomorrow!</td></tr>'}
        </tbody>
    </table>
    <footer>
        <p>Click any row to see match details · Auto-generated by JobScraper</p>
    </footer>
</div>
</body>
</html>"""

    def _build_markdown(self, jobs, today, time_str):
        lines = [
            f"# 🎯 Daily Job Report — {today}",
            f"",
            f"**{len(jobs)}** new matches | **{len([j for j in jobs if j.relevance_score >= 70])}** high relevance",
            f"",
            f"---",
            f"",
        ]

        for i, job in enumerate(jobs, 1):
            emoji = "🟢" if job.relevance_score >= 70 else "🟡" if job.relevance_score >= 50 else "⚪"
            lines.append(f"### {i}. {emoji} {job.title}")
            lines.append(f"**{job.company}** · 📍 {job.location} · Score: {job.relevance_score:.0f}/100")
            if job.salary_info:
                lines.append(f"💰 {job.salary_info}")
            lines.append(f"🔗 [{job.source}]({job.url})")
            if job.match_reasons:
                for reason in job.match_reasons:
                    lines.append(f"  - {reason}")
            lines.append("")

        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════

def run(args):
    log.info("=" * 60)
    log.info("  JOB SCRAPER — Starting daily run")
    log.info(f"  Environment: {'GitHub Actions' if IS_CI else 'Local'}")
    log.info("=" * 60)

    cfg = Config(args.config)
    log.info(f"Config loaded: {len(cfg.search_queries)} queries, {len(cfg.target_companies)} companies")

    if args.dry_run:
        log.info("DRY RUN — Config valid. Exiting.")
        return

    session = create_session()
    seen_db = SeenJobsDB("./data/seen_jobs.json")
    scorer = RelevanceScorer(cfg)
    reporter = ReportGenerator(cfg)

    if args.reset:
        seen_db.reset()
        return

    seen_db.cleanup(days=30)

    # ── Run scrapers ──
    all_jobs = []

    scrapers = {
        "linkedin": LinkedInScraper,
        "indeed": IndeedScraper,
        "stepstone": StepStoneScraper,
        "careers": CompanyCareersScraper,
        "startups": StartupJobBoardScraper,
        "remote": RemoteJobScraper,
    }

    active_scrapers = scrapers
    if args.source:
        if args.source in scrapers:
            active_scrapers = {args.source: scrapers[args.source]}
        else:
            log.error(f"Unknown source: {args.source}")
            return

    for name, scraper_cls in active_scrapers.items():
        log.info(f"\n{'─' * 40}")
        log.info(f"Scraping: {name.upper()}")
        log.info(f"{'─' * 40}")
        try:
            scraper = scraper_cls(session, cfg)
            jobs = scraper.scrape()
            all_jobs.extend(jobs)
        except Exception as e:
            log.error(f"Scraper {name} failed: {e}")

    log.info(f"\nTotal raw results: {len(all_jobs)}")

    # ── Deduplicate ──
    seen_urls = set()
    unique_jobs = []
    for job in all_jobs:
        key = job.url if job.url else f"{job.title}|{job.company}"
        if key not in seen_urls:
            seen_urls.add(key)
            unique_jobs.append(job)

    log.info(f"After deduplication: {len(unique_jobs)}")

    # ── Score ──
    scored_jobs = [scorer.score(job) for job in unique_jobs]

    # ── Filter ──
    relevant_jobs = [j for j in scored_jobs if j.relevance_score >= cfg.min_score]
    log.info(f"Above threshold ({cfg.min_score}): {len(relevant_jobs)}")

    new_jobs = [j for j in relevant_jobs if seen_db.is_new(j)]
    log.info(f"New (not seen before): {len(new_jobs)}")

    for job in new_jobs:
        seen_db.mark_seen(job)
    seen_db.update_stats(len(new_jobs))
    seen_db.save()

    # ── Set CI env vars for downstream steps ──
    today = datetime.now().strftime("%Y-%m-%d")
    set_github_env("REPORT_DATE", today)
    set_github_env("JOB_COUNT", str(len(new_jobs)))

    # ── Generate report (even if empty, so Pages stays updated) ──
    max_results = cfg.output.get("max_results_per_report", 50)
    report_jobs = new_jobs[:max_results]
    report_path = reporter.generate(report_jobs, seen_db)

    log.info(f"\n{'═' * 40}")
    log.info(f"✅ Done! {len(report_jobs)} jobs in today's report.")
    log.info(f"Report: {report_path}")
    log.info(f"{'═' * 40}")


def main():
    parser = argparse.ArgumentParser(description="Daily Job Scraper")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Validate config only")
    parser.add_argument("--source", type=str, help="Scrape single source")
    parser.add_argument("--reset", action="store_true", help="Reset seen jobs database")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
