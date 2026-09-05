from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class EtchvError(RuntimeError):
    def __init__(self, status_code: int, detail: Any, request_id: str | None = None):
        super().__init__(f"Etchv request failed (HTTP {status_code})")
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id


@dataclass(frozen=True)
class EmbedResult:
    image: bytes
    watermark_id: str
    request_id: str | None


@dataclass(frozen=True)
class DetectionResult:
    watermarked: bool
    confidence: float
    watermark_id: str | None
    request_id: str | None


class Etchv:
    def __init__(self, api_key: str, *, base_url: str = "https://pilot.api.etchv.com",
                 timeout: float = 120, transport: httpx.BaseTransport | None = None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        url = urlsplit(base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("base_url must use HTTPS (HTTP is allowed for localhost)")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self._client = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=timeout,
                                    headers={"X-API-Key": api_key}, transport=transport,
                                    follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Etchv:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _post(self, path: str, image: bytes, filename: str,
              data: dict[str, str] | None, idempotency_key: str | None) -> httpx.Response:
        if not isinstance(image, bytes) or not image or len(image) > 20 * 1024 * 1024:
            raise ValueError("image must contain 1 byte to 20 MB of encoded image bytes")
        headers = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.post(path, files={"file": (filename, image, "application/octet-stream")},
                                     data=data, headers=headers)
        if response.status_code != 200:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:1000]
            raise EtchvError(response.status_code, detail, response.headers.get("x-request-id"))
        return response

    def embed_image(self, image: bytes, data: dict[str, Any], *, filename: str = "image.png",
                    idempotency_key: str | None = None) -> EmbedResult:
        if not isinstance(data, dict) or not data:
            raise ValueError("data must be a non-empty JSON object")
        encoded = json.dumps(data, allow_nan=False)
        response = self._post("watermarks/images", image, filename, {"data": encoded}, idempotency_key)
        watermark_id = response.headers.get("x-watermark-id", "")
        if (response.headers.get("content-type", "").split(";")[0] != "image/png"
                or not response.content.startswith(b"\x89PNG\r\n\x1a\n") or not _valid_id(watermark_id)):
            raise EtchvError(200, "Invalid embedding response", response.headers.get("x-request-id"))
        return EmbedResult(response.content, watermark_id, response.headers.get("x-request-id"))

    def detect_image(self, image: bytes, *, filename: str = "image.png",
                     idempotency_key: str | None = None) -> DetectionResult:
        response = self._post("watermarks/images/detect", image, filename, None, idempotency_key)
        try:
            result = response.json()
            confidence = result["confidence"]
            detected = result["watermarked"]
            identifier = result["watermark_id"]
            if (type(detected) is not bool or type(confidence) not in (float, int)
                    or not 0 <= confidence <= 1
                    or (detected and not _valid_id(identifier))
                    or (not detected and identifier is not None)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise EtchvError(200, "Invalid detection response", response.headers.get("x-request-id")) from None
        return DetectionResult(detected, confidence, identifier, response.headers.get("x-request-id"))


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None
