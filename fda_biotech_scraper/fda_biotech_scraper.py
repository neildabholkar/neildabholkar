#!/usr/bin/env python3
"""
FDA Biotech Approval Scraper & Daily Email Summary

Scrapes the latest FDA approval announcements from official RSS feeds,
filters for biotech-related entries, and emails a daily digest.

Usage:
    # One-off run
    python fda_biotech_scraper.py

    # Schedule via cron (e.g. every day at 8 AM):
    # 0 8 * * * /usr/bin/python3 /path/to/fda_biotech_scraper.py
"""

import os
import re
import smtplib
import logging
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (override via environment variables or .env file)
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("EMAIL_TO", "")  # comma-separated for multiple recipients
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

FDA_FEEDS = [
    # Official FDA press releases (includes approval announcements)
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    # FDA drugs-specific updates
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml",
]

# Keywords that signal a biotech-related announcement
BIOTECH_KEYWORDS = [
    # Approval actions
    r"\bapprov(?:es?|ed|al)\b",
    r"\bauthoriz(?:es?|ed|ation)\b",
    r"\bclears?\b",
    r"\bgrant(?:s|ed)?\b",
    # Drug / biologic types
    r"\bbiologic(?:s|al)?\b",
    r"\bbiosimilar\b",
    r"\bmonoclonal antibod(?:y|ies)\b",
    r"\bgene therap(?:y|ies)\b",
    r"\bcell therap(?:y|ies)\b",
    r"\bvaccine\b",
    r"\bmRNA\b",
    r"\bCAR[- ]?T\b",
    r"\brecombinant\b",
    r"\borphan drug\b",
    # Regulatory categories
    r"\bBLA\b",  # Biologics License Application
    r"\bNDA\b",  # New Drug Application
    r"\bANDA\b",
    r"\bEUA\b",  # Emergency Use Authorization
    r"\bbreakthrough therap(?:y|ies)\b",
    r"\baccelerated approval\b",
    r"\bpriority review\b",
    r"\bfast track\b",
    # Therapeutic areas common in biotech
    r"\boncology\b",
    r"\bimmunotherapy\b",
    r"\brare disease\b",
    r"\bpediatric\b",
    r"\bneuroscience\b",
    r"\bneurology\b",
    r"\bcardiology\b",
    r"\bhematolog(?:y|ical)\b",
    r"\binhibitor\b",
    # General pharma/biotech terms
    r"\bbiotech(?:nology)?\b",
    r"\bpharmaceutical\b",
    r"\bdrug\b",
    r"\btherapeutic\b",
    r"\bclinical trial\b",
]

_BIOTECH_PATTERN = re.compile("|".join(BIOTECH_KEYWORDS), re.IGNORECASE)

_USER_AGENT = "FDA-Biotech-Scraper/1.0"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class FDAItem:
    title: str
    link: str
    summary: str
    published: datetime
    matched_keywords: list[str]


# ---------------------------------------------------------------------------
# RSS parsing (stdlib xml.etree — no feedparser needed)
# ---------------------------------------------------------------------------
def _parse_rss_date(text: str | None) -> datetime | None:
    """Parse an RFC-822 date string commonly used in RSS pubDate fields."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (ValueError, TypeError):
        pass
    # Fallback: try ISO-8601 variants
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def fetch_feed_items(feed_url: str, cutoff: datetime) -> list[dict]:
    """Download an RSS/Atom feed and return entries published after *cutoff*."""
    log.info("Fetching feed: %s", feed_url)
    try:
        resp = requests.get(feed_url, timeout=30, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Failed to fetch feed %s: %s", feed_url, exc)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        log.warning("XML parse error for %s: %s", feed_url, exc)
        return []

    items: list[dict] = []

    # Handle RSS 2.0 (<rss><channel><item>)
    for item_el in root.iter("item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        summary = _clean_html(item_el.findtext("description") or "")
        published = _parse_rss_date(item_el.findtext("pubDate"))
        if published and published >= cutoff:
            items.append(
                {"title": title, "link": link, "summary": summary, "published": published}
            )

    # Handle Atom (<feed><entry>)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry_el in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = (entry_el.findtext("atom:title", namespaces=ns) or "").strip()
        link_el = entry_el.find("atom:link", namespaces=ns)
        link = (link_el.get("href", "") if link_el is not None else "").strip()
        summary = _clean_html(
            entry_el.findtext("atom:summary", namespaces=ns)
            or entry_el.findtext("atom:content", namespaces=ns)
            or ""
        )
        published = _parse_rss_date(
            entry_el.findtext("atom:updated", namespaces=ns)
            or entry_el.findtext("atom:published", namespaces=ns)
        )
        if published and published >= cutoff:
            items.append(
                {"title": title, "link": link, "summary": summary, "published": published}
            )

    log.info("  Found %d items after cutoff", len(items))
    return items


# ---------------------------------------------------------------------------
# HTML scraping fallback
# ---------------------------------------------------------------------------
def scrape_fda_approval_page() -> list[dict]:
    """Scrape the FDA 'Novel Drug Approvals' webpage as a supplemental source."""
    url = "https://www.fda.gov/drugs/development-approval-process-drugs/novel-drug-approvals-fda"
    log.info("Scraping FDA novel drug approvals page: %s", url)
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Failed to scrape FDA page: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items: list[dict] = []
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if len(cells) >= 3:
            drug_name = cells[0].get_text(strip=True)
            active_ingredient = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            approval_date_str = cells[-1].get_text(strip=True)
            link_tag = cells[0].find("a")
            link = ""
            if link_tag and link_tag.get("href"):
                href = link_tag["href"]
                link = href if href.startswith("http") else "https://www.fda.gov" + href
            items.append(
                {
                    "title": f"{drug_name} ({active_ingredient})",
                    "link": link,
                    "summary": f"Approved: {approval_date_str}",
                    "published": _try_parse_approval_date(approval_date_str),
                }
            )
    log.info("  Scraped %d rows from approvals table", len(items))
    return items


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def is_biotech_related(item: dict) -> FDAItem | None:
    """Return an FDAItem if the entry matches biotech keywords, else None."""
    text = f"{item['title']} {item['summary']}"
    matches = list({m.group().lower() for m in _BIOTECH_PATTERN.finditer(text)})
    if matches:
        return FDAItem(
            title=item["title"],
            link=item["link"],
            summary=item["summary"],
            published=item["published"],
            matched_keywords=sorted(matches),
        )
    return None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def build_email_html(items: list[FDAItem], lookback_hours: int) -> str:
    """Render the digest as an HTML email body."""
    now = datetime.now(timezone.utc).strftime("%B %d, %Y")
    rows = ""
    for it in sorted(items, key=lambda x: x.published, reverse=True):
        pub = it.published.strftime("%Y-%m-%d %H:%M UTC")
        kw = ", ".join(it.matched_keywords)
        rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee;">
            <a href="{it.link}" style="color:#1a73e8;text-decoration:none;font-weight:600;">{it.title}</a>
            <br><span style="color:#555;font-size:13px;">{it.summary[:300]}</span>
            <br><span style="color:#888;font-size:12px;">Published: {pub} &middot; Keywords: {kw}</span>
          </td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;color:#222;">
      <h2 style="color:#0d47a1;">FDA Biotech Daily Digest &mdash; {now}</h2>
      <p>Found <strong>{len(items)}</strong> biotech-related announcement(s) in the last {lookback_hours} hours.</p>
      <table style="width:100%;border-collapse:collapse;">
        {rows}
      </table>
      <hr style="margin-top:24px;">
      <p style="font-size:12px;color:#999;">
        Generated by <em>fda_biotech_scraper</em>.
        Sources: FDA Press Releases RSS, FDA Drugs RSS, FDA Novel Drug Approvals page.
      </p>
    </body>
    </html>"""


def build_email_plain(items: list[FDAItem], lookback_hours: int) -> str:
    """Render the digest as plain text."""
    now = datetime.now(timezone.utc).strftime("%B %d, %Y")
    lines = [
        f"FDA Biotech Daily Digest — {now}",
        f"Found {len(items)} biotech-related announcement(s) in the last {lookback_hours} hours.",
        "",
    ]
    for it in sorted(items, key=lambda x: x.published, reverse=True):
        pub = it.published.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"  {it.title}")
        lines.append(f"  {it.summary[:200]}")
        lines.append(f"  Link: {it.link}")
        lines.append(f"  Published: {pub} | Keywords: {', '.join(it.matched_keywords)}")
        lines.append("")
    return "\n".join(lines)


def send_email(items: list[FDAItem]) -> None:
    """Send the digest email via SMTP."""
    if not EMAIL_TO:
        log.error("EMAIL_TO is not set. Skipping email send.")
        return
    if not SMTP_USER or not SMTP_PASSWORD:
        log.error("SMTP_USER / SMTP_PASSWORD not set. Skipping email send.")
        return

    recipients = [addr.strip() for addr in EMAIL_TO.split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"FDA Biotech Digest — {datetime.now(timezone.utc).strftime('%b %d, %Y')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(build_email_plain(items, LOOKBACK_HOURS), "plain"))
    msg.attach(MIMEText(build_email_html(items, LOOKBACK_HOURS), "html"))

    log.info("Sending email to %s via %s:%d", recipients, SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())
    log.info("Email sent successfully.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _try_parse_approval_date(text: str) -> datetime:
    """Best-effort parse of a date string from the FDA approvals table."""
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _clean_html(raw: str) -> str:
    """Strip HTML tags from a string."""
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text(separator=" ", strip=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info("Cutoff: %s (%d-hour lookback)", cutoff.isoformat(), LOOKBACK_HOURS)

    # 1. Collect items from RSS feeds
    raw_items: list[dict] = []
    for feed_url in FDA_FEEDS:
        raw_items.extend(fetch_feed_items(feed_url, cutoff))

    # 2. Supplement with scraped approvals page
    for item in scrape_fda_approval_page():
        if item["published"] >= cutoff:
            raw_items.append(item)

    # 3. Deduplicate by link
    seen: set[str] = set()
    unique_items: list[dict] = []
    for item in raw_items:
        key = item["link"] or item["title"]
        if key not in seen:
            seen.add(key)
            unique_items.append(item)

    log.info("Total unique items after cutoff: %d", len(unique_items))

    # 4. Filter for biotech relevance
    biotech_items: list[FDAItem] = []
    for item in unique_items:
        result = is_biotech_related(item)
        if result:
            biotech_items.append(result)

    log.info("Biotech-related items: %d", len(biotech_items))

    if not biotech_items:
        log.info("No biotech-related announcements found. No email sent.")
        return

    # 5. Print summary to stdout (useful for cron logs)
    print(build_email_plain(biotech_items, LOOKBACK_HOURS))

    # 6. Send email
    send_email(biotech_items)


if __name__ == "__main__":
    main()
