from __future__ import annotations

import hashlib
import re
import unicodedata

from api.history_identity import (
    norm_identity, normalize_label_for_identity, normalize_text_for_identity)


def _spec_identity(enbez, titel, text):
    """SPEC formula, with the agreed label whitespace collapse."""
    def label(value):
        value = unicodedata.normalize("NFC", value).replace("\xa0", " ")
        return re.sub(r"\s+", " ", value.replace("\xad", "")).strip()
    body = re.sub(r"\s+", "", unicodedata.normalize("NFC", text)
                  .replace("\xa0", " ").replace("\xad", ""))
    payload = "\x1f".join((label(enbez), label(titel), body))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_identity_ignores_all_whitespace_in_body():
    xml_text = "(1) Leistungen erhalten Personen, die 1.das 15. Lebensjahr"
    html_text = "(1) Leistungen erhalten Personen, die\n1.\xa0das 15.  Lebensjahr"
    assert norm_identity("§ 7", "Leistungsberechtigte", xml_text) == \
        norm_identity("§ 7", "Leistungsberechtigte", html_text)


def test_identity_drops_soft_hyphen_and_normalizes_nfc():
    composed = "Bedarfsgemeinschaft für Ärzte"
    decomposed = unicodedata.normalize("NFD", "Bedarfs\xadgemeinschaft für Ärzte")
    assert normalize_text_for_identity(composed) == \
        normalize_text_for_identity(decomposed)
    assert norm_identity("§ 1", "", composed) == norm_identity("§ 1", "", decomposed)


def test_labels_collapse_but_keep_word_boundaries():
    assert normalize_label_for_identity(" §\xa0 1a ") == "§ 1a"
    assert norm_identity("§ 1a", "", "x") != norm_identity("§ 1 a", "", "x")
    assert norm_identity("§ 1", "Titel", "x") != norm_identity("§ 1", "Titel2", "x")
    # designator/title/body are separated: moving text between them differs
    assert norm_identity("§ 1", "ab", "c") != norm_identity("§ 1", "a", "bc")


def test_identity_matches_spec_formula_and_handles_none():
    for enbez, titel, text in [("§ 7", "Leistungsberechtigte", "(1) Text."),
                               ("Art 1", "", "(1) Die Würde"),
                               ("Inhaltsübersicht", "", "")]:
        assert norm_identity(enbez, titel, text) == _spec_identity(enbez, titel, text)
    assert norm_identity(None, None, None) == _spec_identity("", "", "")
