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
`detect_video` for H.264 MP4/MOV. Uploads are limited to 20 MB. Detection
recovers a SHA-256 digest of your data, not the data itself.

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
(quote it to support). Messages never include your API key.

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
