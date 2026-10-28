# Changelog

## 1.5.0

- Traceable links: `create_link` shares a source asset so each recipient
  downloads their own watermarked copy, with `recipients` or
  `allowed_domains`, `expires_at`, `prepare`, `download_limit` and
  `webhook_id`. Retries reuse one `Idempotency-Key`. Also `estimate_link`,
  `list_links`, `get_link`, `list_link_events` and `revoke_link`, and the
  `LinkPrepare` type.
- `DetectionResult.copy` names the traceable link copy a watermark is on (link,
  recipient, first open) and `DetectionResult.asset` another watermarked asset
  of yours, when your key may read them; both are `None` otherwise.

## 1.4.0

- `embed_document`, `submit_embed("documents", ...)`, `submit_batch` and
  `submit_batch_zip` take `active_content` (`"keep"`, the default, or
  `"remove_auto_run"`) to remove what a PDF viewer runs by itself. The new
  `ActiveContent` type names the values, and `Batch.active_content` reports a
  batch's choice. Other media raise `ValueError`.
- The README states the PDF limits: 1 to 2,000 pages and 50 MB to embed, 95 MB
  for the watermarked copy (48 KB per page) and 95 MB to detect.

## 1.3.0

- `embed_video` and `detect_video` results can now be WebM, Matroska (`.mkv`),
  MPEG program stream (`.mpg`), MPEG transport stream (`.ts`) or AVI, besides
  MP4 and MOV. Each result's content type is checked against the file's bytes.
- `embed_document` and `detect_document` take DOCX and PPTX, besides PDF.
- `DetectionUnit.part` names the DOCX/PPTX package part that carries a unit;
  it is `None` for other files.

## 1.2.0

- Batches: watermark up to 100 files in one call. `submit_batch(items, ...)`
  creates the batch, uploads every file to its signed URL (`upload_concurrency`
  at a time, never with the API key) and starts it; `wait_for_batch` polls
  until the batch is final, honoring `Retry-After`; `iter_batch_results` yields
  each item's verified file or its `error_code`; `download_batch_archive` waits
  for and downloads the zip of a batch created with `archive=True`, and
  `download_batch_archive_to` streams it to a path or file object.
- `submit_batch_zip` sends files already in one zip (up to 55 MB) with a
  manifest. Also `get_batch`, `cancel_batch` and `list_batches`.
- New typed models: `Batch`, `BatchEntry`, `BatchItemResult`, `BatchPage` and the
  `BatchItem` / `ZipBatchItem` input types.
- More than 100 items raise `ValueError` before any request. Batch creation
  retries reuse one `Idempotency-Key`. Any failed upload or start raises
  `BatchSubmitError` with the `batch_id` and `idempotency_key` to resume with;
  resuming skips files that already arrived and raises `GoneError` for an
  expired batch.
- Signed uploads (batches and `upload_file`) use the client `timeout` per
  network read or write, and retry for as long as the file needs at 128 KiB/s
  on top of it.
- Batch archives above `ARCHIVE_MAX_BYTES` (1 GiB + 64 MiB) are refused with
  `code` `archive_too_large` and not retried.

## 1.1.0

- Files above 40 MB (`large_file_threshold`) are uploaded once to a signed URL,
  without the API key, and referenced by `upload_id`; retries reuse the same
  upload. `upload_file` is public.
- Detection accepts delivered files up to 192 MB; image and PDF detection above
  95 MB runs as a job that the call waits for.
