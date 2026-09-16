"""Крок 2 — пошта.

Компанії зі списку Drive-папки -> пошук Gmail (3 незалежні умови, вже
об'єднані в один query в google_services/gmail.py) -> hr@/company-domain
розпізнаються детерміновано регексом (не Claude) -> Claude лише
підтверджує релевантність (не job-board спам) і класифікує статус.
"""
from __future__ import annotations

import logging
import re

from claude_orchestrator.client import call_json
from claude_orchestrator.prompts import build_email_classify_prompt
from config import DEDUP_LOG_TITLE, FILENAME_NOISE_TOKENS, GMAIL_LABEL_NAME, METRICS_SHEET_TITLE, RESUME_FOLDER_ID
from google_services import gmail
from google_services.drive import list_folder_files
from models import EmailFinding

logger = logging.getLogger(__name__)

SERVICE_FILE_TITLES = {DEDUP_LOG_TITLE, METRICS_SHEET_TITLE}


def _extract_company_names() -> list[str]:
    files = list_folder_files(RESUME_FOLDER_ID)
    companies: set[str] = set()
    for f in files:
        name = f["name"]
        if name in SERVICE_FILE_TITLES:
            continue
        stem = re.sub(r"\.(pdf|docx?|xlsx?)$", "", name, flags=re.I)
        tokens = re.split(r"[_\-\s]+", stem)
        cleaned = [
            t for t in tokens
            if t.lower() not in FILENAME_NOISE_TOKENS and not re.fullmatch(r"\d{1,2}", t)
        ]
        if cleaned:
            companies.add(" ".join(cleaned).strip())
    return sorted(companies)


def _sender_domain(sender: str) -> str:
    match = re.search(r"@([\w.-]+)", sender)
    return match.group(1).lower() if match else ""


def run_step2(company_names: list[str] | None = None) -> dict:
    company_names = company_names if company_names is not None else _extract_company_names()

    label_id = gmail.get_or_create_label(GMAIL_LABEL_NAME)
    threads = gmail.search_threads(company_names, label_id_to_exclude=GMAIL_LABEL_NAME)

    raw_emails: list[dict] = []
    thread_message_ids: list[tuple[str, str]] = []  # (thread_id, message_id) для першого листа треду

    for t in threads:
        thread = gmail.get_thread(t["id"])
        messages = thread.get("messages", [])
        if not messages:
            continue
        message = messages[-1]  # останнє повідомлення в треді — найактуальніше
        sender = gmail.header_value(message, "From")
        subject = gmail.header_value(message, "Subject")
        date_str = gmail.header_value(message, "Date")
        body = gmail.extract_plain_text(message)
        raw_emails.append(
            {"sender": sender, "subject": subject, "date": date_str, "body_text": body}
        )
        thread_message_ids.append((t["id"], message["id"]))

    if not raw_emails:
        return {"findings": [], "total_emails_found": 0, "hr_domain_emails": 0, "known_company_emails": 0}

    prompt = build_email_classify_prompt(raw_emails)
    result = call_json(prompt)
    eval_by_index = {ev["index"]: ev for ev in result.get("evaluations", [])}

    company_names_lower = {c.lower() for c in company_names}
    findings: list[EmailFinding] = []

    for i, raw in enumerate(raw_emails):
        ev = eval_by_index.get(i)
        if not ev or not ev.get("is_relevant"):
            continue
        thread_id, message_id = thread_message_ids[i]
        domain = _sender_domain(raw["sender"])
        from_hr = domain.startswith("hr.") or "hr@" in raw["sender"].lower()
        from_known_company = any(c in domain or c in raw["sender"].lower() for c in company_names_lower)

        findings.append(
            EmailFinding(
                thread_id=thread_id,
                message_id=message_id,
                sender=raw["sender"],
                subject=raw["subject"],
                date=raw["date"],
                summary=ev.get("summary", ""),
                status=ev.get("status"),
                from_hr_domain=from_hr,
                from_known_company=from_known_company,
            )
        )
        gmail.apply_label(message_id, label_id)

    return {
        "findings": findings,
        "total_emails_found": len(findings),
        "hr_domain_emails": sum(1 for f in findings if f.from_hr_domain),
        "known_company_emails": sum(1 for f in findings if f.from_known_company),
        "company_names": company_names,
    }
