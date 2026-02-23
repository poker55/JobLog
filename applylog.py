#!/usr/bin/env python3
"""
autolog.py — paste job description -> auto-extract fields -> confirm -> save to SQLite

What it stores:
- company (required after confirmation)
- role/title (optional)
- job_url (optional)
- location (optional)
- status (sent/interview/offer/rejected/ghosted)
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

DB_PATH = Path("/Users/Sheldon/Desktop/Career/applylog/applylog.sqlite3")

# If you *really* don't want to store the pasted text, set this to False.
STORE_RAW_TEXT = True

VALID_STATUS = {"sent", "interview", "offer", "rejected", "ghosted", "draft"}

URL_RE = re.compile(r"https?://[^\s)\]}>\"']+", re.IGNORECASE)

# Role like: "IT Systembetreuer (m/w/d)", "Software Engineer (f/m/d)", etc.
ROLE_MWD_RE = re.compile(
    r"\b([A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9 /\-+,&]{3,100}?)\s*\((?:m|f|w|d|x)\/(?:m|f|w|d|x)\/?(?:d|x)?\)",
    re.IGNORECASE,
)

# German postcode + city, e.g. "64295 Darmstadt", "70173 Stuttgart"
POSTCODE_LOCATION_RE = re.compile(
    r"\b(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-\s]+)\b"
)

# Common “label: value” patterns found in job posts
ROLE_LABEL_PATTERNS = [
    r"(?im)^\s*(job title|title|position|role|stellenbezeichnung|stelle|funktion)\s*[:\-]\s*(.+?)\s*$",
    r"(?im)^\s*(we are looking for|wir suchen)\s*(.+?)\s*$",
]

COMPANY_LABEL_PATTERNS = [
    r"(?im)^\s*(company|firma|unternehmen)\s*[:\-]\s*(.+?)\s*$",
    r"(?im)^\s*about\s+(.+?)\s*$",           # "About X"
    r"(?im)^\s*über\s+(.+?)\s*$",            # "Über X"
]

LOCATION_LABEL_PATTERNS = [
    r"(?im)^\s*(location|standort|arbeitsort)\s*[:\-]\s*(.+?)\s*$",
]

# Light location hints (expand as you like)
LOCATION_HINT_RE = re.compile(
    r"\b(Berlin|Hamburg|München|Munich|Frankfurt|Köln|Cologne|Stuttgart|Düsseldorf|Darmstadt|Aachen|Leipzig|Dresden|"
    r"Remote|Hybrid|Deutschland|Germany|DE)\b",
    re.IGNORECASE,
)

# Company “suffixes” that often appear in German/EU legal names
LEGAL_SUFFIX_RE = re.compile(
    r"\b(GmbH|AG|SE|KG|UG|GmbH\s*&\s*Co\.\s*KG|Ltd\.|Limited|Inc\.|Corporation|S\.?r\.?l\.?|S\.?p\.?A\.?)\b",
    re.IGNORECASE,
)


@dataclass
class Extracted:
    company: Optional[str] = None
    role: Optional[str] = None
    job_url: Optional[str] = None
    location: Optional[str] = None


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
          status TEXT NOT NULL,
          raw_text TEXT
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company ON applications(company);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON applications(created_at_utc);")
    conn.commit()
    return conn


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


def _first_label_match(patterns: List[str], text: str) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        # Some patterns have (label, value) groups; take the last group
        val = m.groups()[-1].strip()
        val = re.sub(r"^[\s:–—\-]+|[\s:–—\-]+$", "", val)
        # avoid huge captures
        if 2 <= len(val) <= 140:
            return val
    return None


def _guess_company_from_legal_name(text: str) -> Optional[str]:
    """
    Try to find something that looks like 'X GmbH' / 'X AG' etc.
    This is a heuristic, but often works with German postings.
    """
    # Grab lines that contain legal suffixes, prefer short-ish lines.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = []
    for ln in lines[:120]:  # only scan the top part (usually has header)
        if LEGAL_SUFFIX_RE.search(ln) and len(ln) <= 120:
            candidates.append(ln)

    # Choose the first candidate that isn't obviously a sentence
    for c in candidates:
        # avoid lines that are clearly “benefits” etc.
        if len(c.split()) <= 10:
            return c
    return candidates[0] if candidates else None


def extract(text: str) -> Extracted:
    ex = Extracted()

    # URL: first URL found
    urls = URL_RE.findall(text)
    ex.job_url = urls[0] if urls else None

    # Role: first try (m/w/d)-style titles
    m_role = ROLE_MWD_RE.search(text)
    if m_role:
        ex.role = m_role.group(1).strip()
    else:
        ex.role = _first_label_match(ROLE_LABEL_PATTERNS, text)

    # Company: from labels, else legal-name heuristic
    ex.company = _first_label_match(COMPANY_LABEL_PATTERNS, text)
    if not ex.company:
        ex.company = _guess_company_from_legal_name(text)

    # Location: first try postcode + city
    m_loc = POSTCODE_LOCATION_RE.search(text)
    if m_loc:
        ex.location = f"{m_loc.group(1)} {m_loc.group(2).strip()}"
    else:
        ex.location = _first_label_match(LOCATION_LABEL_PATTERNS, text)
        if not ex.location:
            m = LOCATION_HINT_RE.search(text)
            ex.location = m.group(0) if m else None

    # Clean up company if it looks like “About us”
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


def insert(conn: sqlite3.Connection, company: str, role: str, job_url: str, location: str, status: str, raw_text: Optional[str]) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO applications (created_at_utc, company, role, job_url, location, status, raw_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_utc_iso(), company, role or None, job_url or None, location or None, status, raw_text),
    )
    conn.commit()
    return int(cur.lastrowid)


def main() -> None:
    conn = connect()
    print(f"DB: {DB_PATH}\n")

    raw = read_multiline()
    if not raw:
        print("No text pasted. Exiting.")
        return

    ex = extract(raw)

    print("\nExtracted (edit if needed, press Enter to accept):")
    company = confirm_field("Company", ex.company, required=True)
    role = confirm_field("Role/Title", ex.role, required=False)
    job_url = confirm_field("Job URL", ex.job_url, required=False)
    location = confirm_field("Location", ex.location, required=False)
    status = confirm_status()

    raw_to_store = raw if STORE_RAW_TEXT else None

    app_id = insert(conn, company, role, job_url, location, status, raw_to_store)
    print(f"\nSaved ✅  ID #{app_id}")
    print(f"{company} | {role or '-'} | {status} | {location or '-'}")
    if job_url:
        print(f"URL: {job_url}")


if __name__ == "__main__":
    main()