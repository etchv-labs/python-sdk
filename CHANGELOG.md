# Changelog

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
