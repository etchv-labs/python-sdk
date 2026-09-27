from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from uuid import uuid4
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlencode

import httpx

__version__ = "1.0.0"
USER_AGENT = f"etchv-python/{__version__}"


class EtchvError(RuntimeError):
    """An HTTP or protocol failure returned by the Etchv API.

    ``status_code`` is the HTTP status (``0`` when the client deadline passed
    before a durable job finished), ``detail`` is the parsed error body and
    ``request_id`` is the ``X-Request-ID`` (or job ID) to quote to support.
    The message never contains the API key or the response body.
    """

    def __init__(self, status_code: int, detail: Any, request_id: str | None = None):
        message = f"Etchv request failed (HTTP {status_code})"
        if request_id:
            message += f"; request ID {request_id}"
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id


class AuthenticationError(EtchvError):
    """HTTP 401: the API key is missing, invalid, revoked or expired."""


class PaymentRequiredError(EtchvError):
    """HTTP 402: billing or credits are unavailable."""


class PermissionDeniedError(EtchvError):
    """HTTP 403: the key lacks a scope, membership role or plan access."""


class NotFoundError(EtchvError):
    """HTTP 404: the resource does not exist in the key's organization."""


class ConflictError(EtchvError):
    """HTTP 409: idempotency conflict, stale version or invalid state."""


class GoneError(EtchvError):
    """HTTP 410: a saved result or file expired or its asset was deleted.

    Resubmitting the same idempotency key does not charge again.
    """


class RateLimitError(EtchvError):
    """HTTP 429: too many requests."""


class DeadlineExceededError(EtchvError):
    """The client ``timeout`` passed before a durable job finished (status 0).

    The job may still complete. ``detail`` contains the ``idempotency_key``
    and ``request_id`` (when known) for recovery.
    """


_ERRORS: dict[int, type[EtchvError]] = {
    401: AuthenticationError, 402: PaymentRequiredError, 403: PermissionDeniedError,
    404: NotFoundError, 409: ConflictError, 410: GoneError, 429: RateLimitError,
}


def _error(status_code: int, detail: Any, request_id: str | None) -> EtchvError:
    return _ERRORS.get(status_code, EtchvError)(status_code, detail, request_id)


class WebhookVerificationError(ValueError):
    """A webhook delivery failed signature, timestamp or payload verification."""


def verify_webhook(payload: bytes, headers: Mapping[str, str], signing_secret: str, *,
                   tolerance: int = 300, now: float | None = None) -> dict[str, Any]:
    """Verify an Etchv webhook delivery and return the parsed event.

    ``payload`` must be the raw request body bytes, before any JSON parsing.
    ``signing_secret`` is the endpoint's full ``whsec_…`` secret. The signature
    is ``v1=`` + hex HMAC-SHA256 of ``timestamp + "." + payload``. Deliveries
    whose ``X-Etchv-Timestamp`` is more than ``tolerance`` seconds from ``now``
    are rejected, and the body ``id`` must match ``X-Etchv-Event-ID``.

    Raises ``WebhookVerificationError`` on any failure.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be the raw request body bytes")
    if not isinstance(signing_secret, str) or not signing_secret:
        raise ValueError("signing_secret is required")
    lowered = {str(k).lower(): v for k, v in headers.items()}
    timestamp = lowered.get("x-etchv-timestamp", "")
    signature = lowered.get("x-etchv-signature", "")
    if not isinstance(timestamp, str) or not re.fullmatch(r"\d{1,12}", timestamp):
        raise WebhookVerificationError("Missing or invalid webhook timestamp")
    if abs((time.time() if now is None else now) - int(timestamp)) > tolerance:
        raise WebhookVerificationError("Webhook timestamp is outside the tolerance window")
    expected = "v1=" + hmac.new(signing_secret.encode(), timestamp.encode() + b"." + bytes(payload),
                                hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(expected.encode(), signature.encode()):
        raise WebhookVerificationError("Invalid webhook signature")
    try:
        event = json.loads(payload)
    except ValueError:
        raise WebhookVerificationError("Webhook body is not valid JSON") from None
    if not isinstance(event, dict) or event.get("id") != lowered.get("x-etchv-event-id"):
        raise WebhookVerificationError("Webhook event ID does not match X-Etchv-Event-ID")
    return event


@dataclass(frozen=True)
class EmbedResult:
    """A verified watermarked file. ``image`` holds native image, PDF or video bytes."""
    image: bytes
    watermark_id: str
    request_id: str | None
    content_type: str = "image/png"
    filename: str = "image-watermarked.png"
    asset_id: str | None = None
    source_asset_id: str | None = None
    storage_delivery_id: str | None = None


@dataclass(frozen=True)
class DetectionUnit:
    """Detection for one frame, page or layered composite."""
    index: int
    watermarked: bool
    confidence: float
    watermark_id: str | None


@dataclass(frozen=True)
class DetectionResult:
    """Detection outcome. ``watermark_id`` is set only when every unit agrees."""
    watermarked: bool
    confidence: float
    watermark_id: str | None
    request_id: str | None
    units: tuple[DetectionUnit, ...] = ()


class Etchv:
    """Synchronous Etchv API client authenticated with an ``X-API-Key``.

    ``timeout`` (seconds) bounds each call, including automatic polling of
    durable jobs. Use as a context manager or call ``close()``.
    """

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
                                    headers={"X-API-Key": api_key, "User-Agent": USER_AGENT}, transport=transport,
                                    follow_redirects=False)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> Etchv:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _post(self, path: str, image: bytes, filename: str,
              data: dict[str, str] | None, idempotency_key: str | None) -> httpx.Response:
        if not isinstance(image, bytes) or not image or len(image) > 50 * 1024 * 1024:
            raise ValueError("image must contain 1 byte to 50 MB of encoded image bytes")
        headers = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        durable = path.split("?")[0].endswith("/async") or path.split("?")[0] in ("watermarks/images", "watermarks/documents", "watermarks/videos", "watermarks/videos/detect")
        if durable and not idempotency_key:
            headers["Idempotency-Key"] = uuid4().hex
        return self._request(path, "POST", durable, headers=headers,
                             files={"file": (filename, image, "application/octet-stream")}, data=data)

    def _request(self, path: str, method: str, durable: bool, *, accept: tuple[int, ...] = (),
                 **kwargs: Any) -> httpx.Response:
        async_submission = path.split("?")[0].endswith("/async")
        detection_job = path == "watermarks/videos/detect" or "detection-jobs/" in path
        deadline = time.monotonic() + self._timeout
        match = re.search(r"watermarks/(?:detection-)?jobs/(req_[a-f0-9]{64})", path)
        request_id = match.group(1) if match else None
        idempotency_key = kwargs.get("headers", {}).get("Idempotency-Key")
        def pause(seconds: float = 1) -> None:
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
            if (response.status_code in (200, 201, 204) or response.status_code in accept
                    or (async_submission and response.status_code == 202)):
                return response
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:1000]
            if durable and response.status_code == 202:
                if not isinstance(detail, dict) or not re.fullmatch(r"req_[a-f0-9]{64}", str(detail.get("request_id", ""))):
                    raise EtchvError(202, "Invalid job response", request_id)
                request_id = detail["request_id"]
                path, method, kwargs = f"watermarks/{'detection-jobs' if detection_job else 'jobs'}/{request_id}/result", "GET", {}
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
            raise _error(response.status_code, detail, request_id)
        raise DeadlineExceededError(0, {"message": "Client deadline exceeded; the job may still complete", "idempotency_key": idempotency_key, "request_id": request_id}, request_id)

    def _json(self, path: str, method: str = "GET", *, accept: tuple[int, ...] = (), **kwargs: Any) -> Any:
        response = self._request(path, method, False, accept=accept, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise EtchvError(response.status_code, "Invalid JSON response", response.headers.get("x-request-id")) from None

    # Account

    def check_api_key(self) -> dict[str, Any]:
        """Non-billable connection check (``GET /auth/api-key``).

        Returns ``organization_id``, ``key_id`` and ``scopes``. Requires an
        active key but no particular scope.
        """
        return self._json("auth/api-key")

    # Async jobs

    @staticmethod
    def _async_path(media: str, detect: bool, webhook_id: str | None) -> str:
        if media not in ("images", "documents", "videos"):
            raise ValueError("media must be images, documents or videos")
        if webhook_id is not None:
            _check_id(webhook_id, r"wh_[a-f0-9]{32}", "webhook ID")
        return f"watermarks/{media}{'/detect' if detect else ''}/async" + (f"?webhook_id={webhook_id}" if webhook_id else "")

    def submit_embed(self, media: str, file: bytes, data: dict[str, Any], *, filename: str = "file",
                     idempotency_key: str | None = None, webhook_id: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None) -> dict[str, Any]:
        """Submit a background embedding job and return its 202 JSON receipt without polling.

        ``media`` is ``"images"``, ``"documents"`` or ``"videos"``. Transient
        failures are retried with the same idempotency key (generated when omitted).
        """
        if not isinstance(data, dict) or not data:
            raise ValueError("data must be a non-empty JSON object")
        return self._post(self._storage_path(self._async_path(media, False, webhook_id), storage_destination_id, storage_key), file, filename,
                          {"data": json.dumps(data, allow_nan=False)}, idempotency_key).json()

    def submit_detection(self, media: str, file: bytes, *, filename: str = "file",
                         idempotency_key: str | None = None, webhook_id: str | None = None) -> dict[str, Any]:
        """Submit a background detection job and return its 202 JSON receipt without polling."""
        return self._post(self._async_path(media, True, webhook_id), file, filename, None, idempotency_key).json()

    def get_job(self, request_id: str, *, detect: bool = False) -> dict[str, Any]:
        """Read a job receipt. Set ``detect=True`` for detection jobs."""
        _check_id(request_id, r"req_[a-f0-9]{64}", "request ID")
        return self._json(f"watermarks/{'detection-jobs' if detect else 'jobs'}/{request_id}")

    def get_embed_result(self, request_id: str) -> EmbedResult:
        """Wait for and download an embedding job's verified file.

        Polls while the job is pending (HTTP 202). Raises ``GoneError`` (410)
        when the saved result expired or its asset was deleted.
        """
        _check_id(request_id, r"req_[a-f0-9]{64}", "request ID")
        return self._embedding_result(self._request(f"watermarks/jobs/{request_id}/result", "GET", True))

    def get_detection_result(self, request_id: str) -> DetectionResult:
        """Wait for and return a detection job's result, polling while it is pending (HTTP 202)."""
        _check_id(request_id, r"req_[a-f0-9]{64}", "request ID")
        return self._detection_result(self._request(f"watermarks/detection-jobs/{request_id}/result", "GET", True))

    # Assets

    @staticmethod
    def _asset_path(asset_id: str) -> str:
        _check_id(asset_id, r"ast_[a-f0-9]{64}", "asset ID")
        return f"assets/{asset_id}"

    def list_assets(self, *, limit: int = 25, cursor: str | None = None,
                    kind: str | None = None, media_type: str | None = None,
                    watermark_id: str | None = None) -> dict[str, Any]:
        """List assets newest first. Returns ``items`` and ``next_cursor``."""
        params = {k: v for k, v in dict(limit=limit, cursor=cursor, kind=kind,
                  media_type=media_type, watermark_id=watermark_id).items() if v is not None}
        return self._json("assets", params=params)

    def get_asset(self, asset_id: str, *, include_metadata: bool = True) -> dict[str, Any]:
        """Read an asset record."""
        params = {} if include_metadata else {"include_metadata": "false"}
        return self._json(self._asset_path(asset_id), params=params)

    def update_asset(self, asset_id: str, *, version: int, **changes: Any) -> dict[str, Any]:
        """Rename an asset or replace its ``metadata``. ``version`` must be current (409 otherwise)."""
        return self._json(self._asset_path(asset_id), "PATCH", json={**changes, "version": version})

    def delete_asset(self, asset_id: str) -> None:
        """Delete an asset (requires ``assets:delete`` and owner/admin membership)."""
        self._request(self._asset_path(asset_id), "DELETE", False)

    def delete_assets(self, asset_ids: list[str]) -> None:
        """Atomically delete 1–50 assets."""
        if isinstance(asset_ids, str) or not 1 <= len(asset_ids) <= 50:
            raise ValueError("Provide 1–50 asset IDs")
        for identifier in asset_ids:
            self._asset_path(identifier)
        self._request("assets/bulk-delete", "POST", False, json={"asset_ids": list(asset_ids)})

    def download_asset(self, asset_id: str) -> bytes:
        """Download an asset file in its original format. Raises ``GoneError`` when expired."""
        return self._request(self._asset_path(asset_id) + "/content", "GET", False).content

    # Webhooks

    @staticmethod
    def _webhook_path(webhook_id: str) -> str:
        _check_id(webhook_id, r"wh_[a-f0-9]{32}", "webhook ID")
        return f"webhooks/{webhook_id}"

    def list_webhooks(self) -> list[dict[str, Any]]:
        """List webhook endpoints (``webhooks:read``)."""
        return self._json("webhooks")

    def create_webhook(self, url: str) -> dict[str, Any]:
        """Create a webhook endpoint for a public HTTPS URL (``webhooks:write``, owner/admin).

        The response includes a one-time ``signing_secret``; store it securely.
        """
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("url must be a public HTTPS URL")
        return self._json("webhooks", "POST", json={"url": url})

    def update_webhook(self, webhook_id: str, *, enabled: bool) -> dict[str, Any]:
        """Enable or disable a webhook endpoint."""
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return self._json(self._webhook_path(webhook_id), "PATCH", json={"enabled": enabled})

    def delete_webhook(self, webhook_id: str) -> None:
        """Permanently delete a webhook endpoint."""
        self._request(self._webhook_path(webhook_id), "DELETE", False)

    def list_webhook_deliveries(self, webhook_id: str, *, after: str | None = None) -> dict[str, Any]:
        """List up to 50 deliveries. Returns ``data`` and ``next_cursor`` (pass it as ``after``)."""
        params = {}
        if after is not None:
            _check_id(after, r"evt_[a-f0-9]{64}", "delivery cursor")
            params["after"] = after
        return self._json(self._webhook_path(webhook_id) + "/deliveries", params=params)

    def redeliver_webhook(self, webhook_id: str, event_id: str) -> dict[str, Any]:
        """Queue a delivered, exhausted or cancelled event for redelivery."""
        _check_id(event_id, r"evt_[a-f0-9]{64}", "event ID")
        return self._json(f"{self._webhook_path(webhook_id)}/deliveries/{event_id}/redeliver", "POST", accept=(202,))

    # Storage

    @staticmethod
    def _storage_path(path: str, destination: str | None, key: str | None) -> str:
        if key is not None and destination is None: raise ValueError("storage_key requires storage_destination_id")
        if destination is None: return path
        _check_id(destination, r"dst_[a-f0-9]{32}", "storage destination ID")
        values = {"storage_destination_id": destination}
        if key is not None: values["storage_key"] = key
        return path + ("&" if "?" in path else "?") + urlencode(values)

    @staticmethod
    def _destination_path(destination_id: str) -> str:
        _check_id(destination_id, r"dst_[a-f0-9]{32}", "storage destination ID")
        return f"storage/destinations/{destination_id}"

    @staticmethod
    def _delivery_path(delivery_id: str) -> str:
        _check_id(delivery_id, r"std_[a-f0-9]{64}", "storage delivery ID")
        return f"storage/deliveries/{delivery_id}"

    def list_storage_destinations(self) -> list[dict[str, Any]]:
        """List customer storage destinations (``storage:read``)."""
        return self._json("storage/destinations")

    def create_storage_destination(self, *, name: str, provider: str, bucket: str, **options: Any) -> dict[str, Any]:
        """Create an S3, GCS or Azure destination (``storage:write``, owner/admin).

        Pass provider fields such as ``region``, ``role_arn``, ``account``,
        ``prefix``, ``visibility`` or ``credentials`` as keyword arguments.
        Credentials are sent once and never returned; verify before use.
        """
        return self._json("storage/destinations", "POST", json={**options, "name": name, "provider": provider, "bucket": bucket})

    def update_storage_destination(self, destination_id: str, *, enabled: bool | None = None,
                                   credentials: str | None = None) -> dict[str, Any]:
        """Enable/disable a destination or replace its credentials (which clears verification)."""
        body: dict[str, Any] = {}
        if enabled is not None: body["enabled"] = enabled
        if credentials is not None: body["credentials"] = credentials
        if not body:
            raise ValueError("Provide enabled or credentials")
        return self._json(self._destination_path(destination_id), "PATCH", json=body)

    def delete_storage_destination(self, destination_id: str) -> None:
        """Delete a destination and its stored credentials."""
        self._request(self._destination_path(destination_id), "DELETE", False)

    def verify_storage_destination(self, destination_id: str) -> dict[str, Any]:
        """Write and read a connection probe; returns the destination with ``verified_at`` set."""
        return self._json(self._destination_path(destination_id) + "/verify", "POST")

    def list_storage_deliveries(self, destination_id: str, *, after: str | None = None) -> dict[str, Any]:
        """List up to 50 deliveries. Returns ``items`` and ``next_cursor`` (pass it as ``after``)."""
        params = {}
        if after is not None:
            _check_id(after, r"std_[a-f0-9]{64}", "delivery cursor")
            params["after"] = after
        return self._json(self._destination_path(destination_id) + "/deliveries", params=params)

    def create_storage_delivery(self, destination_id: str, asset_id: str, *, key: str | None = None) -> dict[str, Any]:
        """Move an Etchv-hosted watermarked asset to a destination; returns the queued delivery."""
        self._asset_path(asset_id)
        body = {"asset_id": asset_id, **({"key": key} if key is not None else {})}
        return self._json(self._destination_path(destination_id) + "/deliveries", "POST", accept=(202,), json=body)

    def get_storage_delivery(self, identifier: str) -> dict[str, Any]:
        """Read a storage delivery (``storage:read``)."""
        return self._json(self._delivery_path(identifier))

    def retry_storage_delivery(self, delivery_id: str) -> dict[str, Any]:
        """Retry a failed or cancelled upload without another credit."""
        return self._json(self._delivery_path(delivery_id) + "/retry", "POST", accept=(202,))

    def download_storage_delivery(self, delivery_id: str) -> bytes:
        """Download a stored object through Etchv (the delivery must be ``stored``)."""
        return self._request(self._delivery_path(delivery_id) + "/content", "GET", False).content

    # Synchronous watermarking

    def embed_image(self, image: bytes, data: dict[str, Any], *, filename: str = "image.png",
                    idempotency_key: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None) -> EmbedResult:
        """Watermark an image and wait for the verified file in its original format.

        Retries transient failures with the same idempotency key and polls
        pending jobs until ``timeout``; raises ``DeadlineExceededError`` then.
        """
        return self._embed_media("images", image, data, filename, idempotency_key, storage_destination_id, storage_key)

    def embed_document(self, document: bytes, data: dict[str, Any], *, filename: str = "document.pdf",
                       idempotency_key: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None) -> EmbedResult:
        """Watermark a PDF and wait for the verified PDF (see ``embed_image``)."""
        return self._embed_media("documents", document, data, filename, idempotency_key, storage_destination_id, storage_key)

    def embed_video(self, video: bytes, data: dict[str, Any], *, filename: str = "video.mp4",
                    idempotency_key: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None) -> EmbedResult:
        """Watermark an MP4/MOV video and wait for the verified video (see ``embed_image``)."""
        return self._embed_media("videos", video, data, filename, idempotency_key, storage_destination_id, storage_key)

    def _embed_media(self, media: str, image: bytes, data: dict[str, Any], filename: str,
                     idempotency_key: str | None, destination: str | None, storage_key: str | None) -> EmbedResult:
        if not isinstance(data, dict) or not data:
            raise ValueError("data must be a non-empty JSON object")
        encoded = json.dumps(data, allow_nan=False)
        response = self._post(self._storage_path(f"watermarks/{media}", destination, storage_key), image, filename, {"data": encoded}, idempotency_key)
        return self._embedding_result(response)

    def _embedding_result(self, response: httpx.Response) -> EmbedResult:
        watermark_id = response.headers.get("x-watermark-id", "")
        content_type = response.headers.get("content-type", "").split(";")[0]
        extension = _image_extension(response.content, content_type)
        if not extension or not _valid_id(watermark_id):
            raise EtchvError(200, "Invalid embedding response", response.headers.get("x-request-id"))
        match = re.search(r'filename="([A-Za-z0-9._-]+)"', response.headers.get("content-disposition", ""))
        filename = match.group(1) if match else f"image-watermarked.{extension}"
        return EmbedResult(response.content, watermark_id, response.headers.get("x-request-id"), content_type, filename, response.headers.get("x-asset-id"), response.headers.get("x-source-asset-id"), response.headers.get("x-storage-delivery-id"))

    # Synchronous detection

    def detect_image(self, image: bytes, *, filename: str = "image.png",
                     idempotency_key: str | None = None) -> DetectionResult:
        """Detect a watermark in an image. Not retried automatically."""
        return self._detect_media("images", image, filename, idempotency_key)

    def detect_document(self, document: bytes, *, filename: str = "document.pdf",
                        idempotency_key: str | None = None) -> DetectionResult:
        """Detect watermarks page by page in a PDF. Not retried automatically."""
        return self._detect_media("documents", document, filename, idempotency_key)

    def detect_video(self, video: bytes, *, filename: str = "video.mp4",
                     idempotency_key: str | None = None) -> DetectionResult:
        """Detect watermarks frame by frame in a video, polling the durable job."""
        return self._detect_media("videos", video, filename, idempotency_key)

    def _detect_media(self, media: str, image: bytes, filename: str, idempotency_key: str | None) -> DetectionResult:
        return self._detection_result(self._post(f"watermarks/{media}/detect", image, filename, None, idempotency_key))

    def _detection_result(self, response: httpx.Response) -> DetectionResult:
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


def _check_id(value: Any, pattern: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {label}")


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _image_extension(data: bytes, mime: str) -> str | None:
    if mime in ("video/mp4", "video/quicktime") and data[4:8] == b"ftyp": return "mp4" if mime == "video/mp4" else "mov"
    if mime == "application/pdf" and data.startswith(b"%PDF-"): return "pdf"
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
