# Changelog

## 1.7.0

- `detect_image` and `detect_document` retry a dropped connection with the
  same `Idempotency-Key`, now generated when omitted, until the client
  `timeout`. The API returns the first result without charging again. A retry
  the API reports as still running (409 `request_in_progress`) waits for
  `Retry-After` (up to 5 seconds) and is sent again; 409 `replay_expired` and
  `request_failed` raise `ConflictError`. When time runs out,
  `DeadlineExceededError` carries the key.
- `detect_image` and `detect_document` take `keep_result`. The API keeps an
  instant detection's result 24 hours so a retry gets it back; with
  `keep_result=False` it keeps nothing, and a retry runs detection again on the
  resent file at no charge. A file sent through an upload session is uploaded
  again for that, bounded like any upload; the resend after it stays within
  the `timeout` counted from the first send. A retry that finds the first copy
  still using the upload (409 `upload_in_use`) waits like `request_in_progress`.
- `EtchvError.idempotency_key` is the key a keyed call (embedding, a job or a
  detection) was sent with, including a generated one, so it can be resent. A
  network error from a detection's upload carries it as `idempotency_key` too.
- `download_asset` and `download_storage_delivery` retry a dropped connection
  within the client `timeout`.
- `download_batch_archive` and `download_batch_archive_to` start the download
  again from the beginning when the connection drops mid-download, truncating
  the `.part` file, the buffer or a seekable file object first. A file object
  that cannot seek still raises.

## 1.6.0

- `delete_link` deletes a traceable link with every recipient's opens,
  downloads and copy; `remove_link_recipient` deletes one recipient's
  records and copy and returns the link without them. Each recipient in `get_link` now has an `id`
  (`lrc_...`). Both need an owner or admin and, for an API key, the
  `assets:delete` scope as well as `links:write`.

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
