"""The instrument catalogue keeps the date of a work separate from its OJ issue."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import fetch_eu_layer as layer


def test_publication_dates_come_from_the_official_journal(monkeypatch):
    def cellar_rows(_http, query):
        assert "cdm:work_date_document ?documentDate" in query
        assert "cdm:official-journal-act_date_publication ?actPublication" in query
        assert "cdm:resource_legal_published_in_official-journal/cdm:publication_general_date_publication ?issuePublication" in query
        assert "COALESCE(?actPublication, ?issuePublication) AS ?pub" in query
        return [
            {"u": layer.celex_uri("32001L0055"), "documentDate": "2001-07-20", "pub": "2001-08-07"},
            {"u": layer.celex_uri("32024R1689"), "documentDate": "2024-06-13", "pub": "2024-07-12"},
            {"u": layer.celex_uri("32000R0001"), "documentDate": "2000-05-02"},
        ]

    monkeypatch.setattr(layer, "sparql", cellar_rows)
    acts = layer.fetch_metadata(None, ["32001L0055", "32024R1689", "32000R0001"])
    assert acts["32001L0055"] == {"document_date": "2001-07-20", "pub_date": "2001-08-07"}
    assert acts["32024R1689"] == {"document_date": "2024-06-13", "pub_date": "2024-07-12"}
    assert acts["32000R0001"] == {"document_date": "2000-05-02"}


def test_typed_dates_keep_partial_application_and_expiry_out_of_whole_act():
    from fetch_eu_texts import work_metadata

    class CellarExample:
        def sparql(self, query):
            if "SELECT ?p ?o" not in query:
                return []
            assert "owl:annotatedTarget ?o" in query and "ann:comment_on_date" in query
            assert "cdm:date_creation_legacy" not in query
            entry = "http://publications.europa.eu/ontology/cdm#resource_legal_date_entry-into-force"
            end = "http://publications.europa.eu/ontology/cdm#resource_legal_date_end-of-validity"
            return [
                {"p": entry, "o": "2028-11-02", "dateRole": "{MA|u}", "dateComment": "{MA/PART|u} {ART|u} 91.2"},
                {"p": entry, "o": "2004-05-20", "dateRole": "{EV|u}"},
                {"p": entry, "o": "2010-05-01", "dateRole": "{MA|u}", "dateComment": "{DATEFF|u} {ART|u} 91"},
                {"p": end, "o": "2031-11-02", "dateComment": "{FIN/VAL/PART|u}"},
                {"p": end, "o": "9999-12-31"},
            ]

    act = work_metadata(CellarExample(), "32004R0883")
    assert act["in_force"] == "2004-05-20"
    assert act["applies_from"] == "2010-05-01"
    assert act["end_of_validity"] is None
    assert act["published"] is None
    assert act["entry_into_force_dates"] == ["2004-05-20", "2010-05-01", "2028-11-02"]


def test_untyped_and_ambiguous_dates():
    from eu_dates import general_date

    # The Charter uses one unannotated assertion, and the expiry sentinel is open-ended.
    assert general_date([{"date": "2009-12-01"}], "EV") == "2009-12-01"
    assert general_date([{"date": "2009-12-01"}], "MA") is None
    assert general_date([{"date": "9999-12-31"}], None) is None
    assert general_date([{"date": "2026-06-11"}], None) == "2026-06-11"
    assert general_date([{"date": "2010-01-01"}, {"date": "2010-12-01"}], "EV") is None
    assert general_date([{"date": "2025-01-01", "dateRole": "{MA/PART|u}"}], "MA") is None


def test_cellar_corrigenda_have_encoded_aliases_and_keep_the_language_scope():
    import fetch_eu_texts as texts

    german = "32003L0008R(01)"
    dutch = "32003L0008R(02)"
    encoded = texts.CELEX_URI + "32003L0008R%2801%29"

    class OfficialAliases:
        def sparql(self, query):
            assert encoded in query
            assert (texts.CELEX_URI + "32003L0008R%2802%29") in query
            return [{"s": encoded, "m": "http://example.org/german", "t": "pdf"}]

    assert texts.celex_uri(german) == "<" + encoded + ">"
    assert texts.celex_uri("12007L/TXT") == "<" + texts.CELEX_URI + "12007L%2FTXT>"
    assert texts.manifestations(OfficialAliases(), [german, dutch]) == {
        german: {"pdf": "http://example.org/german"}}
