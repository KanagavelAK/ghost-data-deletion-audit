"""PDF business documents (support attachments, statements, registers, journal extracts).

Text is extracted locally (pypdf, falling back to the pdftotext CLI). Every
resource id the document names becomes an observation linked to the
identifiers printed in the same document.
"""

import re
import shutil
import subprocess

from ..dataset import EvidenceFile
from .model import IDENTIFIABLE, KIND_TO_PII, PRESENT, RESOURCE_RE, Coverage, Observation, default_category, emails_in_text, system_for, token

USER_IN_TEXT = re.compile(r"\buser_\d+\b")
PHONE_IN_TEXT = re.compile(r"\+\d[\d ()-]{7,}\d")


def pdf_text(path) -> str:
    try:
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except ImportError:
        pass
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, encoding="utf-8").stdout
    raise RuntimeError("no PDF text extractor available (pip install pypdf)")


def text_identifiers(text: str) -> set[str]:
    ids = emails_in_text(text)
    ids |= {token("user", u) for u in USER_IN_TEXT.findall(text)}
    ids |= {token("phone", re.sub(r"\D", "", p)) for p in PHONE_IN_TEXT.findall(text)}
    return ids


def parse_pdf(evidence: EvidenceFile) -> tuple[list[Observation], list[Coverage], list[str], str]:
    try:
        text = pdf_text(evidence.path)
    except Exception as exc:  # noqa: BLE001 - report and continue with other evidence
        return [], [], [f"{evidence.file_id}: PDF text extraction failed: {exc}"], ""
    identifiers = text_identifiers(text)
    direct = {t for t in identifiers if not t.startswith("user:")}
    lowered = text.lower()
    observations = []
    for resource_id in sorted({m.group(0) for m in RESOURCE_RE.finditer(text)}):
        category = default_category(resource_id)
        if resource_id.startswith("support_"):
            category = "SUPPORT_ATTACHMENT" if "attachment" in lowered else "SUPPORT_MESSAGE"
        observations.append(
            Observation(
                file_id=evidence.file_id,
                observed_at=evidence.captured_at,
                system_id=system_for(resource_id),
                resource_id=resource_id,
                presence=PRESENT,
                condition=IDENTIFIABLE if direct and category != "SECURITY_AUDIT_LOG" else None,
                category=category,
                identifiers=set(identifiers),
                pii_fields={KIND_TO_PII[t.split(":", 1)[0]] for t in identifiers},
                source="pdf:text",
                attributes={"document": evidence.path.name},
            )
        )
    return observations, [], [], text
