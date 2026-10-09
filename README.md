# Etchv Python SDK

Official server-side Python client for [Etchv](https://etchv.com): embed and
detect invisible forensic watermarks in images, PDFs and videos.

## Install

```sh
pip install etchv-sdk
```

Requires Python 3.14. Import it as `etchv`. Fully typed.

## Quickstart

Create an API key in the [Etchv dashboard](https://etchv.com/dashboard/api-keys)
and set `ETCHV_API_KEY` on your server. Embedding needs the `watermarks:embed`
scope, detection `watermarks:detect`, and both use credits.

```python
import os
from pathlib import Path
from etchv import Etchv

with Etchv(os.environ["ETCHV_API_KEY"]) as client:
    result = client.embed_image(Path("photo.jpg").read_bytes(),
                                {"recipient": "customer-123"}, filename="photo.jpg")
    Path(result.filename).write_bytes(result.image)  # write bytes as-is

    detection = client.detect_image(result.image)
    print(detection.watermarked, detection.watermark_id, detection.confidence)
```

Use `embed_document` / `detect_document` for PDFs and `embed_video` /
`detect_video` for H.264 MP4/MOV. Image uploads are limited to 50 MB; PDF and
video uploads to 20 MB. Detection takes the files Etchv delivered: up to 192 MB
for images, 64 MB for PDFs and 100 MB for video. Detection recovers a SHA-256
digest of your data, not the data itself.

## Large files

Files over 40 MB are uploaded once to a signed URL and then referenced by ID,
so the request never carries the file. This is automatic in every embed,
detect and submit method; retries reuse the same upload. Image and PDF
detection above 95 MB runs as a background job and the call waits for it.
Change the threshold with `Etchv(..., large_file_threshold=...)`, or upload
explicitly:

```python
upload = client.upload_file("detect", delivered, filename="delivered.tiff")
upload["upload_id"]  # send as the upload_id form field instead of file
```

## GPU processing

Business and Enterprise plans can pass `accelerator="gpu"` to any embed,
detect or submit method (other plans get `PermissionDeniedError`). GPU
operations use 3× credits. If no GPU is ready, the job runs on CPU at normal
credits instead. `result.accelerator` reports the hardware actually used
(`"gpu"` or `"cpu"`), and job receipts include `accelerator_requested` and
`accelerator`.

```python
result = client.embed_video(video_bytes, {"recipient": "customer-123"}, accelerator="gpu")
print(result.accelerator)  # "gpu", or "cpu" after a fallback
```

## Async jobs

```python
job = client.submit_embed("documents", pdf_bytes, {"recipient": "customer-123"},
                          filename="report.pdf", idempotency_key="report-001")
status = client.get_job(job["request_id"])  # ["status"]: queued, running, retrying, succeeded, failed
result = client.get_embed_result(job["request_id"])  # waits for the file
```

`submit_detection`, `get_job(..., detect=True)` and `get_detection_result` work
the same way. Reusing an `idempotency_key` with the same input returns the saved
result (kept 24 hours) without another charge.

## Many files at once

A batch watermarks up to 100 images, PDFs and videos with one call.
`submit_batch` creates the batch, uploads every file to its own signed URL
(four at a time, never with your API key) and starts it. Each item has its own
forensic data; the filename's extension sets the media type.

```python
from pathlib import Path

batch = client.submit_batch(
    [{"filename": p.name, "file": p, "data": {"recipient": p.stem}} for p in Path("in").glob("*.pdf")],
    archive=True,  # also zip every result into one download
)
client.wait_for_batch(batch.batch_id, timeout=1800)  # honors Retry-After between polls

for item in client.iter_batch_results(batch.batch_id):
    if item.ok:
        Path("out", item.result.filename).write_bytes(item.result.image)
    else:
        print(item.filename, item.error_code)  # not charged, or refunded

client.download_batch_archive_to(batch.batch_id, "out.zip")  # streams to disk
```

`file` is bytes or a path. A batch costs the same credits per file as single
requests. Files that never arrive or fail their checks are rejected
(`upload_not_received`, `invalid_input`, ...) without a charge, and failed
files are refunded, so one bad file never stops the rest. Results are kept 24
hours; the archive (with `archive=True`) holds every successful result plus
`manifest.json` and is limited to 1 GB. `download_batch_archive_to` streams it
to a path or file object; `download_batch_archive` returns it as bytes.

More than 100 items raise `ValueError` before any request; split larger sets
into several batches. Pass `webhook_id` to get one `watermark.batch.completed`,
`watermark.batch.failed` or `watermark.batch.cancelled` event when the batch
ends, and `accelerator` or `storage_destination_id` as for single files.
Retries reuse the same `idempotency_key` (generated when omitted). If an upload
or the start still fails, `BatchSubmitError` carries `batch_id` and
`idempotency_key`; calling `submit_batch` again with that key and the same items
uploads only the files that have not arrived and starts the batch. A batch not
started within 24 hours expires, and resuming it raises `GoneError`.

Files already in one zip (up to 55 MB) can go in one request; list every
member:

```python
batch = client.submit_batch_zip(Path("in.zip"), [{"filename": "in/a.png", "data": {"recipient": "a"}}])
```

Also: `get_batch`, `cancel_batch` (files still waiting are canceled and
refunded; queued and running files finish) and
`list_batches(limit=20, before=...)`.

## Also included

- API key check: `check_api_key()` (no credits used).
- Assets: `list_assets`, `get_asset`, `update_asset`, `download_asset`,
  `delete_asset`, `delete_assets`.
- Webhooks: `create_webhook`, `list_webhooks`, `update_webhook`,
  `delete_webhook`, `list_webhook_deliveries`, `redeliver_webhook`; verify
  deliveries with `verify_webhook(raw_body, headers, signing_secret)`, which
  raises `WebhookVerificationError`.
- Customer storage (S3, GCS, Azure): `create_storage_destination`,
  `list_storage_destinations`, `update_storage_destination`,
  `verify_storage_destination`, `delete_storage_destination`,
  `create_storage_delivery`, `list_storage_deliveries`, `get_storage_delivery`,
  `retry_storage_delivery`, `download_storage_delivery`.

## Errors

API failures raise `EtchvError` or a subclass such as `AuthenticationError`,
`PermissionDeniedError`, `ConflictError`, `GoneError`, `RateLimitError` or
`DeadlineExceededError`. Each carries `status_code`, `detail` and `request_id`
(quote it to support). Messages never include your API key. Structured API
errors also set `code` (for example `rate_limited` or `concurrency_limited`),
`message` (also shown in the error text) and `limit`, and `retry_after` holds
the `Retry-After` seconds (`None` when not sent).
Embedding, video detection and job calls retry HTTP 429, 502, 503 and 504
within `timeout`, waiting for `Retry-After` (up to 5 seconds per wait) on 429.

```python
from etchv import EtchvError

try:
    client.detect_image(image)
except EtchvError as error:
    print(error.status_code, error.request_id)
```

## Links

- Full guide: https://etchv.com/docs/sdks/python
- API reference: https://etchv.com/docs
- Support: hello@etchv.com

License: MIT (covers this SDK, not the hosted Etchv service).

Questions or bug reports: open an issue here or email hello@etchv.com.
