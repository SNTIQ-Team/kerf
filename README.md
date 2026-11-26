# Kerf

Kerf tracks German federal law, Bavarian law and EU instruments. A reader can open a provision on a given date, compare its wording across editions and inspect the source for a change. Each record distinguishes the date of publication, the date a provision takes effect and the date Kerf observed it.

[Open the reader](https://sntiq.com/dt/kerf) · [Explore the API](https://api.sntiq.com/kerf/docs) · [Download the dataset](https://huggingface.co/datasets/SNTIQ-Team/kerf)

## Coverage and sources

The federal catalogue identifies acts by their official GII metadata. Selected acts have full text, norm-level search and a version history. The EU index records instruments and German transpositions. Selected instruments also have full text. The current build reports its coverage by jurisdiction at [`/stats`](https://api.sntiq.com/kerf/stats).

For German federal law, the pipeline joins GII texts to Bundestag DIP procedures, final BGBl publications and NeuRIS captures. Bavarian records use BAYERN.RECHT, GVBl and Landtag sources. EU records use official CELLAR and EUR-Lex identifiers. Each procedure keeps its observations, published documents and references to the affected acts. [Source inventory](docs/SOURCES.md)

## Working with dates

A complete archived official text can support an exact historical reading. Kerf records the observation time and source hash. A reviewed reconstruction starts with a later official text, reverses the changes in the final amending act, then replays them against that text. Its record carries `body_complete: true` and `source_exact: false`.

Publication and commencement can fall on different dates. If an event has an official publication date and insufficient evidence of commencement, the effective date stays empty. `/official-transition-reviews` links the command, the affected provision and the commencement evidence. `/verified-reconstructions` lists accepted reconstructions. [API contract](docs/API.md)

## Court citations

Kerf records each citation with its source passage, text hash, locator and character span. The retained source can be retrieved at `/decisions/{id}/source`. An official text without paragraph numbers has `paragraph_label: null`. Use `/decision-passages` to find passages and `/decisions/{id}/relations` to inspect the citations in either direction.

## Run locally

Install the Python dependencies from `requirements.txt`. Run the build before starting the reader or the API. Start each long-running server in a separate terminal.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./refresh.sh                       # fetch sources and build the data
python3 -m http.server -d web 8777 # serve web/ at http://localhost:8777
uvicorn api.server:server --port 8010   # REST API over web/data (see docs/API.md)
curl 'localhost:8010/acts/fed_aufenthg_2004/markdown?norm=%C2%A7%2024'
curl 'localhost:8010/acts/fed_asylvfg_1992/markdown?at=2026-07-06&norm=%C2%A7%2029a'
curl 'localhost:8010/official-transition-reviews?act=fed_asylvfg_1992'
curl 'localhost:8010/verified-reconstructions?act=SGB%20VIII'
curl 'localhost:8010/acts/fed_asylblg/history'
curl 'localhost:8010/changes?act=fed_aufenthg_2004&norm=%C2%A7%2024'
curl 'localhost:8010/amending-acts/bgbl-1-2024-323'
curl 'localhost:8010/changes.atom'
curl 'localhost:8010/acts/fed_asylvfg_1992/markdown?at=2026-07-10'
curl -OJ 'localhost:8010/retrospective-history.sqlite'
python3 tools/kerf_log.py AsylbLG   # chronology for one act
curl 'localhost:8010/federal-history?act=AsylbLG&tier=current_text_correspondence'
```

## Repository

| Path | Contents |
| --- | --- |
| `api/` | Read-only HTTP API, source provenance and historical queries |
| `pipeline/` | Official-source fetchers and amendment extraction |
| `tools/` | Data builds, export, chronology and source verification |
| `courts/` | Court identity, decision parsing and citation records |
| `web/` | The standalone reader and its generated data |
| [conformance/](conformance/README.md) | Shared fixtures and expected results |
| [deploy/](deploy/README.md) | Publishing, refresh jobs and signed archives |

## Rights

SNTIQ publishes its code under Apache-2.0 and its original database arrangement and annotations under CC BY 4.0. Both licenses permit commercial reuse. Legal texts, source databases and other third-party material retain their source terms. [Attribution](LICENSING.md) · [File-level rights](docs/RIGHTS.md)
