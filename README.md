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
    Path("watermarked.png").write_bytes(result.image)
    detection = client.detect_image(result.image)
    print(detection.watermark_id, detection.confidence)
```

`embed_image` returns PNG bytes, `watermark_id`, and `request_id`.
`detect_image` returns `watermarked`, `confidence`, `watermark_id` (or `None`),
and `request_id`. Read files as bytes and write the returned PNG without re-encoding
it to preserve the embedded identifier. Forensic data must be a non-empty JSON object; the service
embeds its SHA-256 digest. Detection recovers the digest, not the original data.
Input must be encoded image bytes (up to 20 MB); image validation happens server-side.

Optional constructor arguments: `base_url` (default `https://api.etchv.com`),
`timeout` in seconds (default 120), and an HTTPX `transport` for tests.
Both operations accept `filename` and `idempotency_key` keyword arguments.

Catch `EtchvError` for HTTP/protocol failures; inspect `status_code`, `detail`,
and `request_id`. HTTPX transport and timeout errors propagate separately.
401/403 indicate authentication/scopes, 402 unavailable credits or billing,
409 an idempotency conflict, and 422 invalid or unrecoverable images.
No automatic retries or redirects are performed. Reusing an idempotency key
may return 409; it does not replay the previous image. Save successful outputs.

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
