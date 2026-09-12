# Etchv Python SDK

Official server-side Python client for Etchv forensic image watermarking.
Requires Python 3.14.7. MIT licensed.

## Install

Install from this repository (not yet published on PyPI):

```sh
pip install "git+https://github.com/etchv-labs/python-sdk.git"
```

## Embed and detect

Create an API key in [your Etchv account](https://etchv.com).
Set `ETCHV_API_KEY` in your environment. The key needs `watermarks:embed` and
`watermarks:detect` scopes and an active plan with available credits.

```python
import os
from pathlib import Path
from etchv import Etchv

with Etchv(os.environ["ETCHV_API_KEY"]) as client:
    result = client.embed_image(
        Path("photo.jpg").read_bytes(),
        {"recipient": "customer-123"},
        filename="photo.jpg",
    )
    Path(result.filename).write_bytes(result.image)
    detection = client.detect_image(result.image)
    print(detection.watermark_id, detection.confidence)
```

`embed_image` returns native image bytes, `watermark_id`, `request_id`, `content_type`, and `filename`.
`detect_image` returns `watermarked`, `confidence`, `watermark_id` (or `None`),
and `request_id`. Read files as bytes and write the returned image without re-encoding
it to preserve the embedded identifier. Forensic data must be a non-empty JSON object; the service
embeds its SHA-256 digest. Detection recovers the digest, not the original data.
Input must be encoded image bytes (up to 20 MB); image validation happens server-side.

Optional constructor arguments: `base_url` (default `https://api.etchv.com`),
`timeout` in seconds (default 120), and an HTTPX `transport` for tests.
Both operations accept `filename` and `idempotency_key` keyword arguments.

Catch `EtchvError` for HTTP/protocol failures; inspect `status_code`, `detail`,
and `request_id`. Embedding deadlines raise status_code 0 with recovery identifiers; detection transport errors propagate separately.
401/403 indicate authentication/scopes, 402 unavailable credits or billing,
409 an idempotency conflict, and 422 invalid or unrecoverable images.
Embedding automatically retries transient transport/service failures with the same
idempotency key and polls pending jobs, returning the native image through one method call.
The client wait defaults to 120 seconds; a timeout does not cancel the durable job.
Reuse the same key and input to retrieve the saved result without another charge.
A changed input with the same key returns 409. Saved results are available for 24 hours.
Detection does not automatically retry. Neither operation follows redirects.

Keep API keys on your server. Confidence is mean decoded-bit certainty, not a
guarantee of exact recovery after cropping, compression, or editing.

## Development and contributions

```sh
pip install -e .
python -m unittest discover -s tests -v
```

This public repository is synchronized from Etchv's development monorepo.
Issues and pull requests are welcome here; maintainers incorporate accepted
changes into the source before publishing the next snapshot. The MIT license
covers this SDK only, not the hosted Etchv service.

To resume a known embedding job, call `get_embed_result(request_id)`. Supply your own stable
idempotency key when embedding if you need recovery across process restarts.


Version 0.6.0 supports native image, PDF and video results. Use the returned filename
when saving bytes; older clients that require PNG must be upgraded. Detection's
`units` field reports each frame, page or layered composite separately. The
top-level identifier is only present when all units recover the same watermark.

## PDF documents

Use `embed_document` and `detect_document` for native PDFs. The existing `image` result field contains PDF bytes. Selectable text and vector content are retained; detection reports one unit per page. See [PDF limits and preservation](https://etchv.com/docs/api/documents).

## Video

Use `embed_video` / `detect_video` for the supported H.264 MP4/MOV profile. Both methods poll durable jobs. Each successful operation costs one credit per started minute; audio is preserved but not watermarked. The `image` result field contains native video bytes. See [video requirements](https://etchv.com/docs/api/videos).

## Asset library

New successful embeddings save original and verified output assets. Files remain
downloadable for 30 days; records stay until deleted. Use `assets:read` for listing,
inspection and downloads, `assets:write` for edits, and `assets:delete` with current
owner/admin membership for deletion. Existing keys need replacement to add scopes.

```python
page = client.list_assets(kind="watermarked", limit=25)
for item in page["items"]:
    asset = client.get_asset(item["id"])
    updated = client.update_asset(asset["id"], version=asset["version"],
                                  metadata={"campaign": "spring"})
    if updated["file_available"]:
        content = client.download_asset(updated["id"])
# Pass cursor=page["next_cursor"] with the same filters for the next page.
```

Edits require the current version; reload and reconcile on HTTP 409. Metadata is
replaced, not merged, and does not change the embedded watermark. Asset operations
consume no credits. Downloads require authentication and return the original file
format. Single and bulk deletion methods are also available; batches contain at
most 50 IDs and delete atomically. Deleting an output blocks its job result replay.
See [the asset API](https://etchv.com/docs/api/assets) for the complete contract.

## Async jobs and webhooks

Submit a background job and receive a JSON receipt without polling automatically. Choose `images`, `documents`, or `videos`; every currently supported native format uses the same submission method.

```python
job = client.submit_embed("documents", pdf_bytes, {"delivery": "delivery_001"},
    filename="document.pdf", idempotency_key="delivery_001", webhook_id=webhook_id)
status = client.get_job(job["request_id"])
```

Use the corresponding submission method for detection without forensic data. For detection status, set the status method’s `detect` argument to true. Existing embed/detect methods continue waiting for results.

Create an endpoint in the [Etchv dashboard](https://etchv.com/dashboard/webhooks), then pass its ID when submitting. Persist your idempotency key before the upload so a lost receipt can be recovered safely. Download from the authenticated result URL after success, or use the existing result method. See the [async guide](https://etchv.com/docs/api/async) and [webhook verification guide](https://etchv.com/docs/api/webhooks).
