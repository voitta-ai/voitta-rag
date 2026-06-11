# Google Drive document indexing

How Google Workspace files (Docs, Sheets, Slides) become searchable content in
voitta-rag. There are **two distinct paths** with very different results. Code
references are anchored to function/symbol names (stable across line drift);
parenthetical line numbers are accurate as of the `master` at time of writing.

## TL;DR

| Path | Trigger | What gets indexed | Where content comes from |
|------|---------|-------------------|--------------------------|
| **A. Server sync connector** | A configured Google Drive sync source | **Full document body** | Drive API `export_media` → OOXML → existing parsers |
| **B. Local-mount stub** (`.gdoc`/`.gsheet`/`.gslides` on a Google Drive Desktop mount) | Indexing a locally-mounted Drive folder | **Filename/title only** | Stub JSON pointer; body is never fetched |

If you need the body searchable, you need **Path A** (a configured sync source).
A bare Desktop-app mount yields only the title plus a `source_url`, retrievable
on demand through the `resolve_url` MCP tool.

---

## Path A — server sync connector (full body)

This is the path that actually indexes document content. Conversion from a
native Google type to a parseable office format happens at **download** time,
not at index time.

### Flow

```
sync source (OAuth / service account)
  -> _run_sync                         api/routes/sync.py            (_run_sync)
     -> connector.sync(source, fs)     services/sync/base.py         (BaseSyncConnector.sync)
        -> download_file(...)          services/sync/google_drive.py (GoogleDriveConnector.download_file)
           -> export_media(fileId, mimeType=OOXML)
              writes  name.gdoc.docx / name.gsheet.xlsx / name.gslides.pptx  to disk
     -> sync_folder(...)               services/indexing.py          (IndexingService.sync_folder)
        -> index_file -> _index_file_standard
           -> parse_file(abs_path)     services/parsers/registry.py  (routes by FINAL suffix)
              -> DocxParser / XlsxParser / PptxParser   (full body text)
           -> chunk -> embed -> Qdrant
```

### Native -> OOXML mapping

`_GOOGLE_EXPORT_MAP` in `services/sync/google_drive.py` (~line 21):

| Google type | Virtual suffix on disk | Export mimeType |
|-------------|------------------------|-----------------|
| `application/vnd.google-apps.document` | `.gdoc.docx` | `...wordprocessingml.document` |
| `application/vnd.google-apps.spreadsheet` | `.gsheet.xlsx` | `...spreadsheetml.sheet` |
| `application/vnd.google-apps.presentation` | `.gslides.pptx` | `...presentationml.presentation` |

`GoogleDriveConnector.download_file` (~line 300) calls
`service.files().export_media(fileId=..., mimeType=export_mime)` and streams the
converted bytes to the local path.

### Why the title-only parser is bypassed here

The parser registry (`services/parsers/registry.py`, `get_parser`) routes by
`Path(file_path).suffix` — the **last** suffix only. A downloaded
`report.gdoc.docx` has suffix `.docx`, so `DocxParser` handles it and extracts
the full body. `GdocParser` only matches a bare `.gdoc`, so it never sees the
synced file. This is intentional: the sync path produces real office documents,
not stubs.

### What triggers indexing of newly-synced files

Two independent triggers reach `_index_file_standard`, both automatic once a
sync source **and** an indexed-folder status row exist:

1. **Post-sync reconcile.** `_run_sync` runs `IndexingService.sync_folder`
   immediately after `connector.sync()`
   (`api/routes/sync.py`, the reconcile loop ~line 1117). `sync_folder` walks
   the folder and indexes any on-disk file that has zero chunks — i.e. the
   freshly-downloaded OOXML files. (The call site logs only `removed`, but the
   `added` indexing runs as a side effect.)
2. **Indexing worker.** `IndexingWorker._process_pending_folders`
   (`services/indexing_worker.py`, ~line 62) polls
   `FolderIndexStatus.status == "pending"` and runs
   `IndexingService.index_folder`, which walks the folder and calls the same
   `_index_file_standard`. `"pending"` is set by the add-folder / reindex
   endpoints (`api/routes/settings.py`) and by the MCP index tool
   (`mcp_server.py`).

### Gating caveat

Post-sync reconcile only runs for folders that **already** have a
`FolderIndexStatus` row (status `indexed` or `pending`) under the synced
`folder_path` (`api/routes/sync.py`, ~line 1110). First-time setup therefore is:
add the folder as an indexed folder (sets `pending` → worker indexes it), then
sync keeps the body fresh on subsequent runs. With no sync source and no indexed
folder, none of this fires.

---

## Path B — local-mount stub (`.gdoc` / `.gsheet` / `.gslides`)

When a Google Drive Desktop mount is volume-mounted into voitta-rag (see
[Mounting local directories](../README.md#mounting-local-directories-mapped-paths)),
the Workspace files appear as **stub JSON pointers**, e.g.:

```json
{ "doc_id": "1AbC...", "email": "user@example.com" }
```

These carry **no document body** and **no API credentials** — just a `doc_id`.

`GdocParser` (`services/parsers/gdoc_parser.py`) handles these:

- reads `doc_id` from the JSON,
- builds a `source_url` (`https://docs.google.com/.../{doc_id}/edit`),
- returns `content = filename stem` (the **title**, nothing more),
- stores `source_url` + `google_doc_id` in chunk metadata.

So a mounted `.gsheet` contributes only its filename to the index. The body is
not fetched, because the stub has no credentials and the parser has no Drive
client.

### `resolve_url` is a lookup, not a fetch

The `resolve_url` MCP tool (`mcp_server.py`, `resolve_url`) normalizes a Google
Docs/Sheets/Slides URL and calls
`VectorStore.find_by_source_url` — a **payload lookup over already-indexed
chunks**. It does not call Google. For a Path B stub, that means it returns the
title chunk, not the document contents.

---

## Source URL tagging (both paths)

`source_url` is attached to chunks so search results and `resolve_url` can point
back to the original document:

- **Path A**: `GoogleDriveConnector` computes `source_url` from mimeType +
  file id (`_GOOGLE_URL_MAP`), stores it on `RemoteFile`, and
  `BaseSyncConnector.sync` writes a `.voitta_sources.json` sidecar.
  `_load_source_url` (`services/indexing.py`) reads that sidecar at index time.
- **Path B**: `GdocParser` puts `source_url` directly in parser metadata;
  `_index_file_standard` reads it from `parse_result.metadata`.

---

## Code reference

| Concern | File | Symbol |
|---------|------|--------|
| Sync background task | `api/routes/sync.py` | `_run_sync` |
| Full mirror sync | `services/sync/base.py` | `BaseSyncConnector.sync` |
| Native -> OOXML export | `services/sync/google_drive.py` | `GoogleDriveConnector.download_file`, `_GOOGLE_EXPORT_MAP` |
| Post-sync index reconcile | `services/indexing.py` | `IndexingService.sync_folder` |
| Worker (pending folders) | `services/indexing_worker.py` | `IndexingWorker._process_pending_folders` |
| Standard file indexing | `services/indexing.py` | `IndexingService._index_file_standard` |
| Parser routing (by suffix) | `services/parsers/registry.py` | `ParserRegistry.get_parser` |
| Stub parser (title only) | `services/parsers/gdoc_parser.py` | `GdocParser` |
| On-demand URL lookup | `mcp_server.py` | `resolve_url` |

## Related

- Issue [#28](https://github.com/voitta-ai/voitta-rag/issues/28) — proposal to
  extract Google Drive ingestion into its own plugin/repo.
- PR [#22](https://github.com/voitta-ai/voitta-rag/pull/22) — added `source_url`
  tagging, the `.gdoc`/`.gsheet`/`.gslides` stub parser, and the `resolve_url`
  MCP tool.
