# Etchv Python SDK

Official server-side Python client for Etchv forensic image watermarking.
Requires Python 3.10+. MIT licensed.

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


Version 0.2.0 supports original-format image results. Use the returned filename
when saving bytes; older clients that require PNG must be upgraded. Detection's
`units` field reports each frame, page or layered composite separately. The
top-level identifier is only present when all units recover the same watermark.
