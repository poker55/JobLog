#!/usr/bin/env python3
"""
autolog.py — paste job description -> LLM-extract fields -> confirm -> save to SQLite

What it stores:
- company (required after confirmation)
- role/title (optional)
- job_url (optional)
- location (optional)
- status (sent/interview/offer/rejected/ghosted/draft)
- created_at_utc (auto)
- raw_text (optional, configurable)

Usage:
  pip install anthropic
  export ANTHROPIC_API_KEY=sk-...
  python autolog.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

DB_PATH = Path.home() / "applylog.sqlite3"

# Set to False if you don't want to store the raw pasted text
STORE_RAW_TEXT = True

# Model to use for extraction. Haiku is fast and cheap (~$0.001 per job post).
# Falls back to regex if API key is missing or call fails.
LLM_MODEL = "claude-haiku-4-5-20251001"

VALID_STATUS = {"sent", "interview", "offer", "rejected", "ghosted", "draft"}

URL_RE = re.compile(r"https?://[^\s)\]}>\"']+", re.IGNORECASE)

# ── Regex fallback patterns (used when LLM is unavailable) ──────────────────

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

LOCATION_HINT_RE = re.compile(
    r"\b(Berlin|Hamburg|München|Munich|Frankfurt|Köln|Cologne|Stuttgart|Düsseldorf|Darmstadt|Aachen|Leipzig|Dresden|"
    r"Remote|Hybrid|Deutschland|Germany|DE)\b",
    re.IGNORECASE,
)

LEGAL_SUFFIX_RE = re.compile(
    r"\b(GmbH|AG|SE|KG|UG|GmbH\s*&\s*Co\.\s*KG|Ltd\.|Limited|Inc\.|Corporation|S\.?r\.?l\.?|S\.?p\.?A\.?)\b",
    re.IGNORECASE,
)

# ── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class Extracted:
    company: Optional[str] = None
    role: Optional[str] = None
    job_url: Optional[str] = None
    location: Optional[str] = None
    extraction_method: str = "regex"  # "llm" or "regex"


# ── DB ────────────────────────────────────────────────────────────────────────

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


def insert(conn: sqlite3.Connection, company: str, role: str, job_url: str,
           location: str, status: str, raw_text: Optional[str]) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO applications (created_at_utc, company, role, job_url, location, status, raw_text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_utc_iso(), company, role or None, job_url or None,
         location or None, status, raw_text),
    )
    conn.commit()
    return cur.lastrowid


# ── Input ─────────────────────────────────────────────────────────────────────

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


# ── LLM extraction ────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """\
You are a precise data extractor for job applications. Given a job description, extract the following fields and return ONLY a valid JSON object — no explanation, no markdown, no extra text.

Fields to extract:
- "company": The hiring company's name. If not explicitly stated, infer from context or legal name (e.g. "Acme GmbH"). Return null if truly unknown.
- "role": The job title or role being advertised (e.g. "Senior Backend Engineer"). Return null if not found.
- "job_url": The first application or job listing URL found in the text. Return null if none.
- "location": City, region, or work arrangement (e.g. "Berlin", "Remote", "Hybrid – Munich"). Return null if not found.

Rules:
- Return exactly these 4 keys, nothing else.
- Values must be strings or null.
- Keep values concise — no full sentences.
- If a company name contains a legal suffix (GmbH, AG, Inc., etc.), include it.

Job description:
\"\"\"
{text}
\"\"\"\
"""


def extract_with_llm(text: str) -> Optional[Extracted]:
    """Call the Anthropic API to extract fields. Returns None if unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic  # type: ignore
    except ImportError:
        print("  [!] anthropic package not installed. Run: pip install anthropic")
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        # Trim very long postings to avoid burning tokens needlessly
        trimmed = text[:8000] if len(text) > 8000 else text

        message = client.messages.create(
            model=LLM_MODEL,
            max_tokens=256,
            messages=[
                {"role": "user", "content": EXTRACTION_PROMPT.format(text=trimmed)}
            ],
        )

        raw_json = message.content[0].text.strip()

        # Strip markdown fences if the model wraps anyway
        raw_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_json, flags=re.DOTALL).strip()

        data = json.loads(raw_json)

        return Extracted(
            company=data.get("company") or None,
            role=data.get("role") or None,
            job_url=data.get("job_url") or None,
            location=data.get("location") or None,
            extraction_method="llm",
        )

    except Exception as e:
        print(f"  [!] LLM extraction failed ({e}), falling back to regex.")
        return None


# ── Regex fallback extraction ─────────────────────────────────────────────────

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


def extract_with_regex(text: str) -> Extracted:
    ex = Extracted(extraction_method="regex")

    urls = URL_RE.findall(text)
    ex.job_url = urls[0] if urls else None
    ex.role = _first_label_match(ROLE_LABEL_PATTERNS, text)
    ex.company = _first_label_match(COMPANY_LABEL_PATTERNS, text)
    if not ex.company:
        ex.company = _guess_company_from_legal_name(text)
    ex.location = _first_label_match(LOCATION_LABEL_PATTERNS, text)
    if not ex.location:
        m = LOCATION_HINT_RE.search(text)
        ex.location = m.group(0) if m else None
    if ex.company:
        ex.company = re.sub(r"(?i)^(about|über)\s+", "", ex.company).strip()

    return ex


# ── Main extraction entry point ───────────────────────────────────────────────

def extract(text: str) -> Extracted:
    """Try LLM first; fall back to regex."""
    result = extract_with_llm(text)
    if result is not None:
        # LLM doesn't always pick up URLs well — patch with regex if missing
        if not result.job_url:
            urls = URL_RE.findall(text)
            result.job_url = urls[0] if urls else None
        return result
    return extract_with_regex(text)


# ── Confirmation UI ───────────────────────────────────────────────────────────

def confirm_field(label: str, current: Optional[str], required: bool = False) -> str:
    while True:
        shown = current if current else ""
        val = input(f"  {label} [{shown}]: ").strip()
        result = val if val else current
        if result:
            return result
        if not required:
            return ""
        print(f"  {label} is required. Please type it.")


def confirm_status() -> str:
    val = input(f"  Status {sorted(VALID_STATUS)} [sent]: ").strip().lower()
    if not val:
        return "sent"
    if val not in VALID_STATUS:
        print("  Invalid status; using 'sent'.")
        return "sent"
    return val


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    conn = connect()
    print(f"DB: {DB_PATH}\n")

    raw = read_multiline()
    if not raw:
        print("No text pasted. Exiting.")
        return

    print("\nExtracting fields...", end=" ", flush=True)
    ex = extract(raw)
    print(f"done (via {ex.extraction_method}).\n")

    print("Review extracted fields (press Enter to accept, or type a correction):")
    company  = confirm_field("Company",   ex.company,  required=True)
    role     = confirm_field("Role/Title", ex.role,    required=False)
    job_url  = confirm_field("Job URL",   ex.job_url,  required=False)
    location = confirm_field("Location",  ex.location, required=False)
    status   = confirm_status()

    raw_to_store = raw if STORE_RAW_TEXT else None

    app_id = insert(conn, company, role, job_url, location, status, raw_to_store)
    print(f"\nSaved ✅  ID #{app_id}")
    print(f"  {company} | {role or '-'} | {status} | {location or '-'}")
    if job_url:
        print(f"  URL: {job_url}")


if __name__ == "__main__":
    main()