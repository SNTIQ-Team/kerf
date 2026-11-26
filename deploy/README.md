# Operating Kerf

## Refresh and publish

Run `deploy/refresh-server.sh` for a full corpus and history refresh. For watched DIP and EUR-Lex procedures, use `deploy/refresh-procedure-watch.sh`. Both jobs acquire the same lock. Repeated checks of an unchanged procedure update its observation time without creating a new history event.

`deploy/publish-web-data.sh` stages a complete generation, parses its JSON, checks its SQLite databases and verifies the act files before switching the active release. A failed validation leaves the previous release available.

The service and timer files in this directory are examples for systemd. Review their paths, resource limits and schedules for your installation. Inspect a run with `systemctl status kerf-procedure-watch.service` or `journalctl -u kerf-procedure-watch.service`.

## Archive and verify sources

Set `KERF_ARCHIVE` to the archive directory and `KERF_ARCHIVE_KEY` to the path of your archive signing key. Configure `KERF_REMOTE` for transfers. A signed receipt records which relative paths and SHA-256 digests were verified. Pruning requires a receipt for the exact bytes and an expired retention window.

Use `deploy/legacy-rebuild-push.sh --dry-run` to rebuild and verify the pre-2023 BGBl amendment candidates. After a parser change, run the same script without `--dry-run` to publish the signed rebuild. The publisher verifies it before admitting candidate rows.

## Law history

`tools/build_law_history.py` builds `law_history.sqlite` from official observations, amendment records and an optional signed history bundle. The bundle contains parsed captures, sourced amendment dates and Bavarian version records. The capture date records when a text was observed. Dates of legal effect come from the cited legal source.

Set `KERF_HISTORY_ARCHIVE` to the capture directory. To publish a verified history bundle with date resolutions and Bavarian records, run:

```sh
deploy/history-bundle-push.sh --change-dates "$dates_file" --bavarian "$bavarian_records"
```

## Court index

Build the compact court index from a complete web-data release and the corresponding immutable dataset commit:

```sh
python3 tools/compact_court_index.py "$web_data" "$output_dir" "$hub_commit"
```

The tool checks the catalogue against the retained originals. Use the four generated files together with the remaining web-data from the same release. The search index supports local lookups. The source endpoint retrieves the requested byte range and verifies its digests before serving the file.
