from courts.model import Block, Section
from courts.paragraphs import assemble


def test_empty_source_marker_keeps_the_existing_positional_anchor():
    reasons = Section("gruende", "court", "source heading", [
        Block(" \n", label="1"),
        Block("Published text without a paragraph number."),
    ])

    paragraphs, numbering = assemble("decision", [reasons])

    assert numbering == "none"
    assert [(p.id, p.rn, p.text) for p in paragraphs] == [
        ("decision#gruende-1", None, "Published text without a paragraph number."),
    ]


def test_genuine_number_after_an_empty_marker_is_retained():
    reasons = Section("gruende", "court", "source heading", [
        Block("", label="1"),
        Block("Unnumbered introduction."),
        Block("Numbered reasons.", label="2"),
    ])

    paragraphs, numbering = assemble("decision", [reasons])

    assert numbering == "rn"
    assert [(p.id, p.rn) for p in paragraphs] == [
        ("decision#gruende-u1", None), ("decision#rn-2", "2"),
    ]
