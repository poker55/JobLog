#!/usr/bin/env python3
"""
autolog.py — log job applications from either:
1) a pasted job-post URL, or
2) pasted raw job description text

What it stores:
- company (required after confirmation)
- role/title (optional)
- job_url (optional)
- location (optional)
- salary_range (optional)
- hr_emails (optional, comma-separated)
- status (sent/interview/offer/rejected/ghosted/draft)
- created_at_utc (auto)
- raw_text (optional, configurable)

Usage:
  python autolog.py
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

import requests
from bs4 import BeautifulSoup

DB_PATH = Path("/Users/Sheldon/Desktop/Career/applylog/applylog.sqlite3")

STORE_RAW_TEXT = True
REQUEST_TIMEOUT = 15

VALID_STATUS = {"sent", "interview", "offer", "rejected", "ghosted", "draft"}

URL_RE = re.compile(r"https?://[^\s)\]}>\"']+", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

ROLE_MWD_RE = re.compile(
    r"\b([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9 /\-+,&]{3,100}?)\s*\((?:m|f|w|d|x)\/(?:m|f|w|d|x)\/?(?:d|x)?\)",
    re.IGNORECASE,
)

POSTCODE_LOCATION_RE = re.compile(
    r"\b(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-\s]+)\b"
)

ROLE_LABEL_PATTERNS = [
    r"(?im)^\s*(job title|title|position|role|stellenbezeichnung|stelle|funktion)\s*[:\-]\s*(.+?)\s*$",
    r"(?im)^\s*(we are looking for|wir suchen)\s*(.+?)\s*$",
]

COMPANY_LABEL_PATTERNS = [
    r"(?im)^\s*(company|firma|unternehmen)\s*[:\-]\s*(.+?)\s*$",
    r"(?im)^\s*about\s+(.+?)\s*$",
    r"(?im)^\s*über\s+(.+?)\s*$",
]

LOCATION_LABEL_PATTERNS = [
    r"(?im)^\s*(location|standort|arbeitsort)\s*[:\-]\s*(.+?)\s*$",
]

SALARY_LABEL_PATTERNS = [
    r"(?im)^\s*(salary|gehalt|vergütung|jahresgehalt)\s*[:\-]\s*(.+?)\s*$",
]

LOCATION_HINT_RE = re.compile(
    r"\b(Berlin|Hamburg|München|Munich|Frankfurt|Köln|Cologne|Stuttgart|Düsseldorf|Darmstadt|Aachen|Leipzig|Dresden|"
    r"Remote|Hybrid|Deutschland|Germany|DE)\b",
    re.IGNORECASE,
)

LEGAL_SUFFIX_RE = re.compile(
    r"\b(GmbH|AG|SE|KG|UG|GmbH\s*&\s*Co\.\s*KG|Ltd\.|Limited|Inc\.|Corporation|S\.?r\.?l\.?|S\.?p\.?A\.?)\b",
    re.IGNORECASE,
)

SALARY_RE = re.compile(
    r"(?i)\b("
    r"(?:€|\$)?\s?\d[\d\.\, ]{2,15}\s?(?:€|\$)?\s*(?:pro\s*(?:jahr|monat|stunde)|per\s*(?:year|month|hour)|/year|/month|/hour)?"
    r"(?:\s*[-–—]\s*(?:€|\$)?\s?\d[\d\.\, ]{2,15}\s?(?:€|\$)?)?"
    r")\b"
)

HR_HINT_RE = re.compile(
    r"(?i)\b(hr|human resources|recruiting|recruiter|talent|bewerbung|karriere|jobs)\b"
)


@dataclass
class Extracted:
    company: Optional[str] = None
    role: Optional[str] = None
    job_url: Optional[str] = None
    location: Optional[str] = None
    salary_range: Optional[str] = None
    hr_emails: List[str] = None


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at_utc TEXT NOT NULL,
          company TEXT NOT NULL,
          role TEXT,
          job_url TEXT,
          location TEXT,
          salary_range TEXT,
          hr_emails TEXT,
          status TEXT NOT NULL,
          raw_text TEXT
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company ON applications(company);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON applications(created_at_utc);")
    conn.commit()
    return conn


def migrate_schema(conn: sqlite3.Connection) -> None:
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(applications);").fetchall()
    }
    if "salary_range" not in existing:
        conn.execute("ALTER TABLE applications ADD COLUMN salary_range TEXT;")
    if "hr_emails" not in existing:
        conn.execute("ALTER TABLE applications ADD COLUMN hr_emails TEXT;")
    conn.commit()


def read_multiline() -> str:
    print("Paste job description. End with a line containing only: <<<END>>>")
    lines: List[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "<<<END>>>":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def ask_mode() -> str:
    print("Choose input mode:")
    print("1) Paste job URL")
    print("2) Paste raw job description")
    choice = input("Mode [1]: ").strip()
    return "url" if choice in {"", "1"} else "text"


def fetch_url_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }

    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _first_label_match(patterns: List[str], text: str) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        val = m.groups()[-1].strip()
        val = re.sub(r"^[\s:–—\-]+|[\s:–—\-]+$", "", val)
        if 2 <= len(val) <= 140:
            return val
    return None


def _guess_company_from_legal_name(text: str) -> Optional[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = []
    for ln in lines[:120]:
        if LEGAL_SUFFIX_RE.search(ln) and len(ln) <= 120:
            candidates.append(ln)

    for c in candidates:
        if len(c.split()) <= 10:
            return c
    return candidates[0] if candidates else None


def _extract_hr_emails(text: str) -> List[str]:
    emails = EMAIL_RE.findall(text)
    if not emails:
        return []

    scored = []
    for email in emails:
        score = 0
        if HR_HINT_RE.search(email):
            score += 2
        local = email.split("@")[0].lower()
        if any(k in local for k in ["hr", "jobs", "career", "careers", "recruit", "talent", "bewerbung", "karriere"]):
            score += 2
        scored.append((score, email))

    scored.sort(key=lambda x: (-x[0], x[1].lower()))

    seen = set()
    result = []
    for _, email in scored:
        key = email.lower()
        if key not in seen:
            seen.add(key)
            result.append(email)

    return result[:5]


def _extract_salary(text: str) -> Optional[str]:
    label_val = _first_label_match(SALARY_LABEL_PATTERNS, text)
    if label_val:
        return label_val

    matches = SALARY_RE.findall(text)
    if matches:
        return matches[0].strip()

    return None


def extract(text: str, source_url: Optional[str] = None) -> Extracted:
    ex = Extracted(hr_emails=[])

    urls = URL_RE.findall(text)
    ex.job_url = source_url or (urls[0] if urls else None)

    m_role = ROLE_MWD_RE.search(text)
    if m_role:
        ex.role = m_role.group(1).strip()
    else:
        ex.role = _first_label_match(ROLE_LABEL_PATTERNS, text)

    ex.company = _first_label_match(COMPANY_LABEL_PATTERNS, text)
    if not ex.company:
        ex.company = _guess_company_from_legal_name(text)

    m_loc = POSTCODE_LOCATION_RE.search(text)
    if m_loc:
        ex.location = f"{m_loc.group(1)} {m_loc.group(2).strip()}"
    else:
        ex.location = _first_label_match(LOCATION_LABEL_PATTERNS, text)
        if not ex.location:
            m = LOCATION_HINT_RE.search(text)
            ex.location = m.group(0) if m else None

    ex.salary_range = _extract_salary(text)
    ex.hr_emails = _extract_hr_emails(text)

    if ex.company:
        ex.company = re.sub(r"(?i)^(about|über)\s+", "", ex.company).strip()

    return ex


def confirm_field(label: str, current: Optional[str], required: bool = False) -> str:
    while True:
        shown = current if current else ""
        val = input(f"{label} [{shown}]: ").strip()
        if val:
            return val
        if current:
            return current
        if not required:
            return ""
        print(f"{label} is required. Please type it.")


def confirm_status() -> str:
    val = input(f"Status {sorted(VALID_STATUS)} [sent]: ").strip().lower()
    if not val:
        return "sent"
    if val not in VALID_STATUS:
        print("Invalid status; using 'sent'.")
        return "sent"
    return val


def insert(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    job_url: str,
    location: str,
    salary_range: str,
    hr_emails: str,
    status: str,
    raw_text: Optional[str],
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO applications
        (created_at_utc, company, role, job_url, location, salary_range, hr_emails, status, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_utc_iso(),
            company,
            role or None,
            job_url or None,
            location or None,
            salary_range or None,
            hr_emails or None,
            status,
            raw_text,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def main() -> None:
    conn = connect()
    migrate_schema(conn)

    print(f"DB: {DB_PATH}\n")

    mode = ask_mode()

    raw = ""
    source_url = None

    if mode == "url":
        source_url = input("Paste job URL: ").strip()
        if not source_url:
            print("No URL provided. Exiting.")
            return

        try:
            raw = fetch_url_text(source_url)
            if not raw:
                print("Fetched page but no readable text was extracted.")
                return
        except requests.RequestException as e:
            print(f"Could not fetch URL: {e}")
            print("Tip: some sites block requests. In that case, use paste-text mode.")
            return
    else:
        raw = read_multiline()
        if not raw:
            print("No text pasted. Exiting.")
            return

    ex = extract(raw, source_url=source_url)

    print("\nExtracted (edit if needed, press Enter to accept):")
    company = confirm_field("Company", ex.company, required=True)
    role = confirm_field("Role/Title", ex.role, required=False)
    job_url = confirm_field("Job URL", ex.job_url, required=False)
    location = confirm_field("Location", ex.location, required=False)
    salary_range = confirm_field("Salary Range", ex.salary_range, required=False)
    hr_emails = confirm_field("HR Emails (comma-separated)", ", ".join(ex.hr_emails), required=False)
    status = confirm_status()

    raw_to_store = raw if STORE_RAW_TEXT else None

    app_id = insert(
        conn=conn,
        company=company,
        role=role,
        job_url=job_url,
        location=location,
        salary_range=salary_range,
        hr_emails=hr_emails,
        status=status,
        raw_text=raw_to_store,
    )

    print(f"\nSaved ✅  ID #{app_id}")
    print(f"{company} | {role or '-'} | {status} | {location or '-'}")
    if salary_range:
        print(f"Salary: {salary_range}")
    if hr_emails:
        print(f"HR Emails: {hr_emails}")
    if job_url:
        print(f"URL: {job_url}")


if __name__ == "__main__":
    main()