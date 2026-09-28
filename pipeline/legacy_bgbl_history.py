"""Capture a bounded, reviewed amendment from the official pre-2023 BGBl.

The modern archive starts with publications in 2023. The SGB XII recast
effective on 2023-01-01 was published in December 2022 and needs its older
official source. This supplement admits only reviewed command/date pairs;
it does not infer a complete historical consolidated act.

Run ``python3 pipeline/legacy_bgbl_history.py --capture`` to recapture and
check the same text. Normal builds validate the pinned reviewed provenance
offline; a recapture never silently replaces it.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = ROOT / "data" / "legacy_bgbl_reviews.json"
HTML_URL = (
    "https://www.bgbl.de/xaver/bgbl/start.xav?"
    "start=%2F%2F*%5B%40attr_id%3D%27bgbl122s2328.pdf%27%5D"
    "&startbk=Bundesanzeiger_BGBl"
)
PDF_VIEWER_URL = (
    "https://www.bgbl.de/xaver/bgbl/text.xav?"
    "start=%2F%2F*%5B%40attr_id%3D%27bgbl122s2328.pdf%27%5D"
    "&bk=Bundesanzeiger_BGBl&skin=pdf"
)
COMMAND_SHA256 = "a553c6cbdaaf5fc904d407af4b47614a010cc2aa2f126937520f4b81ea952164"
COMMENCEMENT_SHA256 = "58def0020c41bb313ec65223f207a9a1678864d1234d8a9999c1e8270c0c11d6"
REVIEWED_PDF_SHA256 = "1b47be92c98922373695171d7156d5105efa0f8808ede9a530e2fa8dd6c1943a"
REVIEWED_TEXT_SHA256 = "b531e04cda92cd43553cc7ed9e1543e6a81016f2ddf1876a6fe35d0feb6d761e"


def normalize_excerpt(text: str) -> str:
    text = re.sub(r"(?<=\w)[-\u00ad]\s*\n\s*(?=\w)", "", text)
    return " ".join(text.replace("\u00ad", "").split())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reviewed_candidate(capture: dict, *, fresh_capture: bool = False) -> dict:
    """Fail closed if either reviewed source passage or its identity changed."""
    command = normalize_excerpt(str(capture.get("command_text") or ""))
    commencement = normalize_excerpt(str(capture.get("commencement_text") or ""))
    if _sha(command) != COMMAND_SHA256 or _sha(commencement) != COMMENCEMENT_SHA256:
        raise ValueError("legacy BGBl command/commencement differs from reviewed source")
    if capture.get("official_html_url") != HTML_URL or \
            capture.get("official_pdf_url") != PDF_VIEWER_URL or \
            capture.get("document_id") != "bgbl-1-2022-s2328":
        raise ValueError("legacy BGBl source identity differs from review")
    digest = str(capture.get("pdf_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("legacy BGBl capture has no valid PDF digest")
    if not fresh_capture and digest != REVIEWED_PDF_SHA256:
        raise ValueError("legacy BGBl PDF digest differs from reviewed capture")
    if capture.get("text_sha256") != REVIEWED_TEXT_SHA256:
        raise ValueError("legacy BGBl text digest differs from reviewed capture")
    # Art. 13(1) gives the default date. The full reviewed exception list
    # includes Art. 5 Nr. 12, but not this joint recast in Art. 5 Nr. 2.
    return {
        "id": "fed-bgbl-reviewed:sgb12-2022-s2328-art5-nr2",
        "act_id": "fed_sgb_12", "jurabk": "SGB 12", "slug": "sgb_12",
        "long_title": "Sozialgesetzbuch (SGB) Zwölftes Buch (XII) - Sozialhilfe",
        "candidate_only": True, "historical_text_reconstructed": False,
        "document_id": capture["document_id"],
        "procedure_id": None,
        "procedure_title": (
            "Zwölftes Gesetz zur Änderung des Zweiten Buches Sozialgesetzbuch "
            "und anderer Gesetze - Einführung eines Bürgergeldes (Bürgergeld-Gesetz)"),
        "amending_article": "5",
        "article_heading": "Änderung des Zwölften Buches Sozialgesetzbuch",
        "command_scope_status": "reviewed_joint_command_5_2_only",
        "publication_date": "2022-12-20", "execution_date": "2022-12-16",
        "effective_at": "2023-01-01",
        "effective_date_status": "reviewed_final_bgbl_command_commencement",
        "effective_date_basis": "official_bgbl_reviewed_command_commencement",
        "commencement_evidence": [{
            "article": "13", "raw": commencement, "source_page": 2351,
            "sha256": COMMENCEMENT_SHA256,
        }],
        "official_html_url": HTML_URL, "official_pdf_url": PDF_VIEWER_URL,
        "pdf_sha256": digest, "text_sha256": capture.get("text_sha256"),
        "article_text_sha256": None,
        "source": "official_bgbl_legacy_review",
        "legacy_review_id": "sgb12-2022-s2328-art5-nr2",
        "integrity_verified": True,
        "generated_at": capture["captured_at"],
        "affected_norms": ["§ 11", "§ 12"], "command_count": 1,
        "commands": [{
            "item": 2, "operation": "replace",
            "ref": {"para": "11", "paras": ["11", "12"]},
            "raw": command, "raw_sha256": COMMAND_SHA256,
            "old_text_constraint": None,
            "new_text": command.split("„", 1)[1].removesuffix("“"),
            "source_page": 2341,
        }],
    }


def load_reviewed_candidates(path: Path = REVIEW_PATH) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported legacy BGBl review schema")
    candidates = [reviewed_candidate(row) for row in payload["captures"]]
    # Raw captures ship separately from Git. When present, verify the source
    # bytes too; a clean checkout still has the pinned reviewed excerpts.
    for folder, digest, suffix in (
        ("objects", REVIEWED_PDF_SHA256, ".pdf"),
        ("texts", REVIEWED_TEXT_SHA256, ".txt"),
    ):
        source = ROOT / "data" / "bgbl_documents" / folder / (digest + suffix)
        if source.exists() and hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError("legacy BGBl cached source digest mismatch")
    return candidates


def validate_reviewed_candidate(candidate: dict) -> None:
    """Admit the legacy origin only for the exact reviewed command and dates."""
    try:
        expected = reviewed_candidate({
            "command_text": candidate["commands"][0]["raw"],
            "commencement_text": candidate["commencement_evidence"][0]["raw"],
            "official_html_url": candidate.get("official_html_url"),
            "official_pdf_url": candidate.get("official_pdf_url"),
            "document_id": candidate.get("document_id"),
            "pdf_sha256": candidate.get("pdf_sha256"),
            "text_sha256": candidate.get("text_sha256"),
            "captured_at": candidate.get("generated_at"),
        })
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("legacy BGBl reviewed evidence is missing") from exc
    if expected != candidate:
        raise ValueError("legacy BGBl candidate differs from reviewed scope")


def capture_review() -> dict:
    with requests.Session() as session:
        response = session.get(PDF_VIEWER_URL, timeout=30)
        response.raise_for_status()
        image = BeautifulSoup(response.text, "html.parser").select_one(".content span img")
        if image is None:
            raise ValueError("official BGBl PDF viewer contains no PDF")
        media_url = urljoin(PDF_VIEWER_URL, str(image.get("src") or ""))
        parsed = urlparse(media_url)
        if parsed.scheme != "https" or parsed.netloc != "www.bgbl.de" or \
                parsed.path != "/xaver/bgbl/media.xav/bgbl122s2328.pdf":
            raise ValueError("unexpected official BGBl media origin")
        pdf = session.get(media_url, timeout=60)
        pdf.raise_for_status()
    if not pdf.content.startswith(b"%PDF-"):
        raise ValueError("official BGBl response is not PDF")
    reader = PdfReader(io.BytesIO(pdf.content))
    if len(reader.pages) != 24:
        raise ValueError("reviewed BGBl document must have 24 pages")
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    if "20. Dezember 2022" not in pages[0] or "2328" not in pages[0]:
        raise ValueError("BGBl publication header does not match review")
    command = pages[13].split("2. Die §§ 11 und 12", 1)[1].split("3. § 23", 1)[0]
    command = "2. Die §§ 11 und 12" + command.strip("\n")
    commencement = pages[23].split("Das vorstehende Gesetz", 1)[0].strip()
    text = "\n\f\n".join(pages) + "\n"
    pdf_sha = hashlib.sha256(pdf.content).hexdigest()
    row = {
        "document_id": "bgbl-1-2022-s2328",
        "official_html_url": HTML_URL, "official_pdf_url": PDF_VIEWER_URL,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pdf_sha256": pdf_sha, "text_sha256": _sha(text),
        "command_text": command, "commencement_text": commencement,
        "review": "BGBl I 2022 p2341 Art5 Nr2; p2351 Art13(1)-(4); visually checked 2026-09-21",
    }
    # The legacy viewer generates new PDF metadata on each request, while
    # the complete extracted text and reviewed passages must stay identical.
    reviewed_candidate(row, fresh_capture=True)
    for folder, name, content in (
        ("objects", pdf_sha + ".pdf", pdf.content),
        ("texts", _sha(text) + ".txt", text.encode("utf-8")),
    ):
        target = ROOT / "data" / "bgbl_documents" / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content)
    return row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    if args.capture:
        row = capture_review()
        print(json.dumps({"recaptured_pdf_sha256": row["pdf_sha256"],
                          "reviewed_text_verified": True}))
    print(json.dumps([{"id": row["id"], "norms": row["affected_norms"],
                       "published": row["publication_date"], "effective": row["effective_at"]}
                      for row in load_reviewed_candidates()], ensure_ascii=False))
