"""Whitespace-free identity of one norm text across sources (see SPEC).

GII XML, GII HTML, NeuRIS LegalDocML and BAYERN.RECHT render the same
wording with different whitespace (``1.das`` vs ``1. das``, line breaks,
non-breaking spaces, soft hyphens).  De-duplication therefore compares the
NFC-normalised designator, title and body with every whitespace character
and soft hyphen removed.  Two wordings that differ only there are the same
legal text.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")


def normalize_text_for_identity(value: str | None) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\xa0", " ").replace("\u00ad", "")
    return _WS.sub("", text)


def normalize_label_for_identity(value: str | None) -> str:
    """Designator/title: NFC, nbsp as space, whitespace runs collapsed."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\xa0", " ").replace("\u00ad", "")
    return _WS.sub(" ", text).strip()


def norm_identity(enbez: str | None, titel: str | None,
                  text: str | None) -> str:
    payload = "\x1f".join((
        normalize_label_for_identity(enbez),
        normalize_label_for_identity(titel),
        normalize_text_for_identity(text)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
