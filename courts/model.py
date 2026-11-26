"""Source-neutral decision model produced by every adapter."""
from __future__ import annotations

from dataclasses import dataclass, field

FOOTNOTE_SECTIONS = ("fussnoten", "redaktionelle_fussnoten", "fussnoten_unklar")

# Canonical section names.  ``authorship`` separates court-authored text from
# editorial additions (documentation titles, Orientierungssätze).
SECTION_ORDER = (
    "titel", "leitsatz", "orientierungssatz", "tenor", "tatbestand",
    "entscheidungsgruende", "gruende", "abweichende_meinung", "sonstiger_langtext", "rechtsmittelbelehrung", *FOOTNOTE_SECTIONS,
)
HEADNOTE_SECTIONS = frozenset({"titel", "leitsatz", "orientierungssatz"})
AUTHORSHIP = frozenset({"court", "editorial", "unknown"})


@dataclass
class Block:
    """One source text block.  ``label`` is an explicit Randnummer, if any."""
    text: str
    label: str | None = None
    kind: str = "text"          # text | heading | pagebreak
    locator: str = ""


@dataclass
class Section:
    name: str
    authorship: str
    authorship_basis: str
    blocks: list[Block] = field(default_factory=list)
    heading: str | None = None

    def __post_init__(self) -> None:
        if self.authorship not in AUTHORSHIP:
            raise ValueError(f"invalid authorship {self.authorship!r}")


@dataclass
class ParsedDecision:
    source_id: str
    source_doc_id: str
    source_url: str
    court_name: str
    decision_date: str               # ISO yyyy-mm-dd
    case_numbers_raw: str            # verbatim published docket string
    case_numbers: list[str]
    decision_type: str | None
    sections: list[Section]
    ecli: str | None = None
    court_id: str | None = None      # filled by courtids.resolve if None
    court_type: str | None = None
    jurisdiction: str | None = None  # branch: verfassung|ordentlich|verwaltung|sozial|finanz|arbeit|patent
    land: str | None = None          # de | bw | by | ... (federal courts: "de")
    chamber: str | None = None
    title: str | None = None
    metadata: dict = field(default_factory=dict)
    procedural: list[dict] = field(default_factory=list)  # Vorinstanz etc.


@dataclass
class Paragraph:
    id: str
    decision_id: str
    section: str
    ordinal: int
    rn: str | None
    numbering: str                   # rn | none
    blocks: list[Block]

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)
