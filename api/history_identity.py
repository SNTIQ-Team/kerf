"""Shared norm-text identity for historical law versions (phase 4).

Every source of a historical norm text (GII daily states, Wayback captures of
GII pages, NeuRIS LegalDocML expressions, BAYERN.RECHT, reconstructions) is
de-duplicated with :func:`norm_identity`.  Two texts are "the same version"
exactly when their designator, title and whitespace-free body agree.

The body is compared without any whitespace (and without soft hyphens / with
no-break spaces folded) because different renderings of the same official
text (GII XML flattening, GII HTML, LegalDocML) disagree only in where line
breaks and blanks fall between block elements.  Designator and title are
compared with whitespace runs collapsed, not removed, so "§ 1a" and "§ 1 a"
stay distinct labels.

Keep the function bodies byte-identical across workstreams; other modules
import from here instead of copying.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")


def normalize_text_for_identity(value: str | None) -> str:
    """Norm body: NFC, nbsp folded, soft hyphens dropped, all whitespace removed."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\xa0", " ").replace("\xad", "")
    return _WS.sub("", text)


def normalize_label_for_identity(value: str | None) -> str:
    """Designator/title: NFC, nbsp as space, whitespace runs collapsed."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\xa0", " ").replace("\xad", "")
    return _WS.sub(" ", text).strip()


def norm_identity(enbez: str | None, titel: str | None, text: str | None) -> str:
    """sha256 identity of one norm version (hex)."""
    payload = "\x1f".join((normalize_label_for_identity(enbez),
                           normalize_label_for_identity(titel),
                           normalize_text_for_identity(text)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
