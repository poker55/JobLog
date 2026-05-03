#!/usr/bin/env python3
"""
applylog.py — paste job description -> auto-extract fields -> confirm -> save to SQLite

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
  python applylog.py
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

DB_PATH = Path("/Users/Sheldon/Desktop/Career/applylog/applylog.sqlite3")

STORE_RAW_TEXT = True

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"

VALID_STATUS = {"sent", "interview", "offer", "rejected", "ghosted", "draft"}


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


def _clean_optional(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, list):
        value = ", ".join(str(v).strip() for v in value if str(v).strip())
    value = str(value).strip()
    return value or None


def _clean_emails(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = value
    else:
        return []

    seen = set()
    result = []
    for part in parts:
        email = str(part).strip()
        key = email.lower()
        if email and key not in seen:
            seen.add(key)
            result.append(email)
    return result[:5]


def _extract_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def extract(text: str) -> Extracted:
    prompt = f"""
Extract job application fields from the job description below.

Return only one valid JSON object with exactly these keys:
company, role, job_url, location, salary_range, hr_emails

Rules:
- Use null for unknown string fields.
- hr_emails must be an array of strings.
- Do not guess if the text does not say it.
- Do not include markdown, comments, or extra text.

Job description:
\"\"\"{text}\"\"\"
""".strip()

    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }

    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            ollama_result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"Ollama extraction failed: {exc}. Continuing with blank fields.")
        return Extracted(hr_emails=[])

    try:
        data = _extract_json_object(ollama_result.get("response", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        print(f"Ollama returned invalid JSON: {exc}. Continuing with blank fields.")
        return Extracted(hr_emails=[])

    return Extracted(
        company=_clean_optional(data.get("company")),
        role=_clean_optional(data.get("role")),
        job_url=_clean_optional(data.get("job_url")),
        location=_clean_optional(data.get("location")),
        salary_range=_clean_optional(data.get("salary_range")),
        hr_emails=_clean_emails(data.get("hr_emails")),
    )


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
