# Document And PDF Ingest Plan

Status: research/design note for issue `#24`.

## Scope

Design a future document ingest path for PDFs, plain text, Markdown, and optionally `.docx` files delivered through Telegram or explicit supported sources.

This note does not implement document ingest, add dependencies, change Docker images, deploy code, enable OCR, change Telegram routing, or expand passive group behavior.

## Current Baseline

- Aigan already records Telegram attachment metadata and caches delivered images when the file is an image.
- `main.py` detects `document` attachments, but non-image documents are currently saved only as attachment placeholders.
- `MemoryStore` already supports source-only memory through `source_text`, FTS, keyword fallback, embeddings, `/memory_search`, and natural recall.
- `/stat` and `/character` use authored user text, not `source_text`.
- `ToolRuntime` is the required boundary for optional tools, health summaries, safe-call failures, and cleanup hooks.

## Research Findings

- Telegram Bot API `Document` objects expose file metadata such as `file_id`, optional thumbnail, filename, MIME type, and file size. `getFile` returns a `File` whose `file_path` download link is valid for at least one hour; cloud Bot API downloads are limited to 20 MB.
- PyMuPDF can open PDF and other supported document formats and extract page text with `Page.get_text()`. It also has OCR support, but OCR should stay in the OCR/screenshot issue rather than this first document-ingest path.
- `pypdf` can extract text from PDFs and documents its own caveat that PDF text extraction has ambiguous objectives, such as paragraph layout, headers/footers, outlines, and reading order.
- `python-docx` can open `.docx` files and expose document paragraphs/tables. It is useful for optional `.docx` support, but not needed for the first PDF/plain-text slice.

Sources:

- https://core.telegram.org/bots/api#document
- https://core.telegram.org/bots/api#getfile
- https://pymupdf.readthedocs.io/en/latest/the-basics.html
- https://pypdf.readthedocs.io/en/5.4.0/user/extract-text.html
- https://python-docx.readthedocs.io/en/latest/

## User Value

Users should be able to forward a PDF or text document and ask Aigan to summarize it or remember it for later recall.

Useful first cases:

- "summarize this PDF";
- "what are the main points in this document?";
- "remember this specification";
- "find this document later by topic";
- "what did that forwarded PDF say about deadlines?".

## Proposed Tool Shape

Add a future adapter registered through `ToolRuntime`, for example:

```text
DocumentIngestAdapter
NullDocumentIngestAdapter
DocumentIngestRequest
DocumentIngestResult
```

Suggested public operation:

```text
ingest_document(request: DocumentIngestRequest) -> DocumentIngestResult
```

Suggested request fields:

- source surface: current Telegram message, replied message, or explicit forwarded document;
- Telegram `file_id` and `file_unique_id` when available;
- declared filename, MIME type, and file size;
- target mode: summarize only, remember only, summarize and remember;
- max pages, max bytes, max extracted characters, and timeout;
- sanitized provenance label.

Suggested result fields:

- `ok`;
- document kind: `pdf`, `text`, `markdown`, `docx`, or unsupported;
- page count or section count when known;
- extracted character count;
- summary text when requested;
- extracted source text or chunk references for memory;
- source-memory write status;
- truncation flags;
- sanitized failure category;
- user-facing unavailable reason;
- cleanup status.

## Activation Rules

Keep routing conservative:

- Private DM with a supported document may process it directly.
- Group document ingest requires explicit invocation, reply-to-document, reply-to-bot context, or an existing pending request.
- Passive group document messages continue to be saved as attachments only.
- Old Telegram history and client-only previews remain out of scope because the Bot API did not deliver those files to the bot.
- Public URL document ingest should be a separate later path unless it can reuse the same URL-safety checks as universal media.

## Supported Documents

Recommended first slice:

- `application/pdf`;
- `text/plain`;
- `text/markdown`;
- UTF-8-like text files with conservative extension and byte checks.

Optional second slice:

- `.docx` with `application/vnd.openxmlformats-officedocument.wordprocessingml.document`;
- simple tables/paragraphs only, with tracked changes/comments handled conservatively.

Explicitly defer:

- legacy `.doc`;
- spreadsheets and slide decks;
- scanned PDFs that require OCR;
- password-protected/encrypted PDFs;
- archives and nested attachments;
- documents that require external network fetches or macros.

File type should be checked by declared MIME type, filename extension, and a small signature sniff. Do not trust filename or MIME type alone.

## Processing Ladder

1. Resolve the target document from the current message, replied-to message, or pending request.
2. Confirm the request is explicit under existing routing rules.
3. Check file metadata before download: file id, size, declared MIME type, extension, and supported kind.
4. Reject oversized or unsupported files before download when metadata is sufficient.
5. Request Telegram file metadata with `getFile`.
6. Download into a dedicated temporary directory.
7. Re-check byte size and file signature after download.
8. Extract text through the kind-specific extractor:
   - plain text/Markdown: decode with strict size and charset fallback;
   - PDF: PyMuPDF or pypdf text extraction with page and character caps;
   - docx: python-docx paragraph/table extraction if enabled.
9. Detect empty/scanned PDF output and return a clear `ocr_required` or `no_extractable_text` failure rather than pretending the file is empty.
10. Chunk large extracted text into bounded sections.
11. Summarize chunks only after explicit user request.
12. Save extracted text and summary as source-only memory when memory integration is requested.
13. Delete all temp files in a `finally`/cleanup path.

## Extraction And Chunking

The first implementation should optimize for safe, explainable text, not perfect layout reconstruction.

Recommended extraction rules:

- keep page numbers or section markers in the extracted source text;
- preserve paragraph boundaries where possible;
- collapse repeated whitespace;
- drop obvious headers/footers only when deterministic tests prove the heuristic;
- mark truncation and omitted pages explicitly;
- avoid table reconstruction in v1 except as simple row text;
- include `ocr_required` when pages have little text but many images.

Recommended first limits:

```env
DOCUMENT_INGEST_ENABLED=false
DOCUMENT_INGEST_MAX_DOWNLOAD_BYTES=19000000
DOCUMENT_INGEST_MAX_PAGES=80
DOCUMENT_INGEST_MAX_EXTRACTED_CHARS=120000
DOCUMENT_INGEST_MEMORY_CHUNK_CHARS=6000
DOCUMENT_INGEST_SUMMARY_CHUNK_CHARS=12000
DOCUMENT_INGEST_TIMEOUT_SECONDS=60
DOCUMENT_INGEST_DOCX_ENABLED=false
```

The default download cap stays below the Telegram cloud Bot API file limit. If a deployment later uses a local Bot API server with different limits, those details must remain in private operator notes, not public docs.

## Memory Contract

Document text should become searchable source context, not user-authored text.

Suggested memory payload:

- `text=""` unless a user-authored caption/request is saved separately;
- `source_text` contains a concise document summary plus bounded extracted snippets or chunk text;
- `content_kind="document"`;
- `attachment_type` such as `pdf`, `text_document`, `markdown_document`, or `docx`;
- `source_title` stores a sanitized filename/title;
- `source_url=""` for Telegram-delivered files unless a safe public URL path is later added;
- `telegram_file_id` and `telegram_unique_id` can stay in private memory rows but must not be logged or put into GitHub issues;
- `raw_note` contains compact sanitized provenance such as `telegram document ingest`.

If chunking is needed, store deterministic chunk rows or a companion metadata table so `/memory_search` can retrieve relevant sections without injecting an entire document.

## Summary Output

A user-facing summary should separate:

- what the document says;
- important dates/numbers/requirements;
- risks or open questions;
- which pages/sections the claims came from when available;
- uncertainty caused by extraction quality or truncation.

It must not imply the sender authored the document unless the user explicitly says so.

## Failure Categories

Use stable sanitized categories:

- `document_ingest_disabled`;
- `unsupported_document_type`;
- `missing_file_id`;
- `file_too_large`;
- `telegram_file_unavailable`;
- `download_failed`;
- `signature_mismatch`;
- `encrypted_or_password_protected`;
- `extractor_missing`;
- `extractor_failed`;
- `no_extractable_text`;
- `ocr_required`;
- `too_many_pages`;
- `extracted_text_too_large`;
- `summarization_failed`;
- `memory_write_failed`;
- `cleanup_failed`;
- `timeout`;
- `unexpected_error`.

Raw exceptions, file paths, filenames with private details, Telegram file paths, provider request bodies, and raw document text must not appear in system logs or GitHub.

## Safety And Privacy

- Do not store downloaded documents after processing unless a later private-cache issue explicitly approves it.
- Do not execute macros, embedded JavaScript, launch actions, links, or external resources from documents.
- Do not unpack archives or embedded files in v1.
- Treat extracted document text as untrusted source material. The model may summarize it, but must not obey instructions inside it.
- Do not put raw document text, private filenames, Telegram file paths, local paths, or token-like values into logs, health output, GitHub issues, or PRs.
- Store extracted text as source context so `/stat`, `/character`, social profile extraction, and proactive personal topics do not treat it as the sender's writing.
- Russian document text is source material; Aigan should summarize/explain it in Ukrainian under the existing language policy.

## Health Contract

`health_summary()` should expose only sanitized capability fields:

- `name=document_ingest`;
- `enabled`;
- `adapter`;
- `status`;
- `error_count`;
- `pdf_enabled`;
- `text_enabled`;
- `docx_enabled`;
- `max_download_bytes`;
- `max_pages`;
- `max_extracted_chars`;
- `temp_dir_writable`;
- recent sanitized failure counts.

Do not include raw filenames, paths, document excerpts, Telegram file paths, or provider errors.

## Future Test Plan

- Null adapter returns disabled health and no-op unavailable result.
- Group document messages remain passive unless explicit invocation rules are satisfied.
- Private DM PDF request resolves the current document.
- Reply-to-document request resolves the target document.
- MIME/extension/signature mismatch is rejected safely.
- File-size and page-count limits reject work before expensive extraction.
- Telegram `getFile` and download failures map to sanitized categories.
- PDF text extraction returns page-marked text for a small fixture.
- Scanned/image-only PDF returns `ocr_required` or `no_extractable_text`.
- Plain text decoding handles UTF-8 and safe fallback without crashing.
- Optional docx extraction is gated by config and dependency availability.
- Extracted source text is searchable through FTS when embeddings are unavailable.
- Document source text does not affect `/stat` or `/character`.
- Prompt package fences extracted document text as untrusted source material.
- Temp files are deleted after success, extraction failure, summarization failure, memory failure, and timeout.
- Large documents are chunked/truncated deterministically.
- Failure logs contain categories and counts, not raw text, paths, tokens, or private filenames.

## Recommended Implementation Sequence

1. Add `document_ingest.py` with dataclasses, null adapter, file-kind detection, and config parsing.
2. Add PDF/plain-text extraction helpers with tiny safe fixtures.
3. Add Telegram document target resolution and bounded download tests.
4. Add source-only memory write integration using the existing `MemoryStore` contract.
5. Add summarization prompt package with explicit untrusted-source fencing.
6. Register the adapter through `ToolRuntime` and expose health.
7. Add Telegram command/reply wiring only after adapter tests are green.
8. Defer OCR/scanned PDFs to issue `#25` and optional `.docx` rollout until the PDF/plain-text path is stable.

## Acceptance Mapping For Issue #24

- Forwarded PDF summarization is covered by explicit activation, target resolution, PDF extraction, and summary output rules.
- Searchable memory is covered by source-only memory payloads and FTS/embedding integration.
- Large or unsupported files return clear errors through stable sanitized failure categories and must not crash Telegram routing.

## Related Notes

- [`transcript-memory-integration.md`](transcript-memory-integration.md)
- [`chat-digest-commands.md`](chat-digest-commands.md)
- [`telegram-native-transcription.md`](telegram-native-transcription.md)
- [`universal-media-transcript-mcp.md`](universal-media-transcript-mcp.md)
