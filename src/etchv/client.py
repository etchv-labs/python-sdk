from __future__ import annotations

import json
import time
from uuid import uuid4
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
    content_type: str = "image/png"
    filename: str = "image-watermarked.png"


@dataclass(frozen=True)
class DetectionUnit:
    index: int
    watermarked: bool
    confidence: float
    watermark_id: str | None


@dataclass(frozen=True)
class DetectionResult:
    watermarked: bool
    confidence: float
    watermark_id: str | None
    request_id: str | None
    units: tuple[DetectionUnit, ...] = ()


class Etchv:
    def __init__(self, api_key: str, *, base_url: str = "https://api.etchv.com",
                 timeout: float = 120, transport: httpx.BaseTransport | None = None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        url = urlsplit(base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("base_url must use HTTPS (HTTP is allowed for localhost)")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self._timeout = timeout
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
        durable = path == "watermarks/images"
        if durable and not idempotency_key:
            headers["Idempotency-Key"] = uuid4().hex
        return self._request(path, "POST", durable, headers=headers,
                             files={"file": (filename, image, "application/octet-stream")}, data=data)

    def _request(self, path, method, durable, **kwargs):
        deadline = time.monotonic() + self._timeout
        match = re.search(r"watermarks/jobs/(req_[a-f0-9]{64})", path)
        request_id = match.group(1) if match else None
        idempotency_key = kwargs.get("headers", {}).get("Idempotency-Key")
        def pause(seconds=1):
            time.sleep(min(seconds, max(0, deadline - time.monotonic())))
        while time.monotonic() < deadline:
            try:
                response = self._client.request(method, path, timeout=max(.001, deadline - time.monotonic()), **kwargs)
            except httpx.TransportError:
                if not durable:
                    raise
                pause()
                continue
            request_id = response.headers.get("x-request-id") or request_id
            if response.status_code == 200:
                return response
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:1000]
            if durable and response.status_code == 202:
                if not isinstance(detail, dict) or not re.fullmatch(r"req_[a-f0-9]{64}", str(detail.get("request_id", ""))):
                    raise EtchvError(202, "Invalid job response", request_id)
                request_id = detail["request_id"]
                path, method, kwargs = f"watermarks/jobs/{request_id}/result", "GET", {}
                try:
                    delay = float(response.headers.get("retry-after", "1"))
                    delay = min(5, max(.01, delay)) if math.isfinite(delay) else 1
                except ValueError:
                    delay = 1
                pause(delay)
                continue
            if durable and response.status_code in (429, 502, 503, 504) and not (isinstance(detail, dict) and detail.get("status") == "failed"):
                pause()
                continue
            raise EtchvError(response.status_code, detail, request_id)
        raise EtchvError(0, {"message": "Client deadline exceeded; the job may still complete", "idempotency_key": idempotency_key}, request_id)

    def get_embed_result(self, request_id: str) -> EmbedResult:
        if not re.fullmatch(r"req_[a-f0-9]{64}", request_id):
            raise ValueError("Invalid request ID")
        return self._embedding_result(self._request(f"watermarks/jobs/{request_id}/result", "GET", True))

    def embed_image(self, image: bytes, data: dict[str, Any], *, filename: str = "image.png",
                    idempotency_key: str | None = None) -> EmbedResult:
        if not isinstance(data, dict) or not data:
            raise ValueError("data must be a non-empty JSON object")
        encoded = json.dumps(data, allow_nan=False)
        response = self._post("watermarks/images", image, filename, {"data": encoded}, idempotency_key)
        return self._embedding_result(response)

    def _embedding_result(self, response: httpx.Response) -> EmbedResult:
        watermark_id = response.headers.get("x-watermark-id", "")
        content_type = response.headers.get("content-type", "").split(";")[0]
        extension = _image_extension(response.content, content_type)
        if not extension or not _valid_id(watermark_id):
            raise EtchvError(200, "Invalid embedding response", response.headers.get("x-request-id"))
        match = re.search(r'filename="([A-Za-z0-9._-]+)"', response.headers.get("content-disposition", ""))
        filename = match.group(1) if match else f"image-watermarked.{extension}"
        return EmbedResult(response.content, watermark_id, response.headers.get("x-request-id"), content_type, filename)

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
        raw_units = result.get("units", [{"index": 0, **result}])
        if not isinstance(raw_units, list) or not raw_units:
            raise EtchvError(200, "Invalid detection units", response.headers.get("x-request-id"))
        units = []
        for index, unit in enumerate(raw_units):
            if (not isinstance(unit, dict) or type(unit.get("index")) is not int or unit.get("index") != index
                    or type(unit.get("watermarked")) is not bool
                    or type(unit.get("confidence")) not in (int, float)
                    or not 0 <= unit["confidence"] <= 1
                    or (unit["watermarked"] and not _valid_id(unit.get("watermark_id")))
                    or (not unit["watermarked"] and unit.get("watermark_id") is not None)):
                raise EtchvError(200, "Invalid detection units", response.headers.get("x-request-id"))
            units.append(DetectionUnit(index, unit["watermarked"], unit["confidence"], unit.get("watermark_id")))
        return DetectionResult(detected, confidence, identifier, response.headers.get("x-request-id"), tuple(units))


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _image_extension(data: bytes, mime: str) -> str | None:
    signatures = {
        "image/png": ((b"\x89PNG\r\n\x1a\n",), "png"),
        "image/jpeg": ((b"\xff\xd8\xff",), "jpg"),
        "image/gif": ((b"GIF87a", b"GIF89a"), "gif"),
        "image/tiff": ((b"II*\0", b"MM\0*"), "tiff"),
        "image/bmp": ((b"BM",), "bmp"),
        "image/x-portable-pixmap": ((b"P6", b"P3"), "ppm"),
    }
    if mime == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if mime == "image/vnd.adobe.photoshop" and data[:4] == b"8BPS":
        return {b"\0\1": "psd", b"\0\2": "psb"}.get(data[4:6])
    if mime in signatures:
        prefixes, extension = signatures[mime]
        if data.startswith(prefixes): return extension
    return None
