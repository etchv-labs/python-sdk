from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4
import math
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, BinaryIO, Literal, TypedDict
from urllib.parse import urlsplit, urlencode

import httpx

__version__ = "1.2.0"
USER_AGENT = f"etchv-python/{__version__}"

Accelerator = Literal["cpu", "gpu"]
"""Processing hardware for embedding and detection: ``"cpu"`` (default) or ``"gpu"``."""


class EtchvError(RuntimeError):
    """An HTTP or protocol failure returned by the Etchv API.

    ``status_code`` is the HTTP status (``0`` when the client deadline passed
    before a durable job finished), ``detail`` is the parsed error body and
    ``request_id`` is the ``X-Request-ID`` (or job ID) to quote to support.
    When the API returns a structured error, ``code`` (for example
    ``"rate_limited"``), ``message`` and ``limit`` are its machine-readable
    code, explanation and the limit that applied (``message`` is also set for a
    plain-text ``detail``); otherwise they are ``None``. A structured
    ``message`` is included in the exception text. ``retry_after`` is the
    ``Retry-After`` delay in seconds, or ``None`` when the response sent none.
    The exception text never contains the API key or the raw response body.
    """

    def __init__(self, status_code: int, detail: Any, request_id: str | None = None, *,
                 retry_after: float | None = None):
        inner = detail.get("detail") if isinstance(detail, dict) else None
        structured = inner if isinstance(inner, dict) else {}
        message = structured.get("message") if structured else inner
        self.message: str | None = message if isinstance(message, str) else None
        self.code: str | None = structured.get("code") if isinstance(structured.get("code"), str) else None
        limit = structured.get("limit")
        self.limit: int | None = limit if type(limit) is int else None
        text = f"Etchv request failed (HTTP {status_code})"
        if structured and self.message:
            text += f": {self.message}"
        if request_id:
            text += f"; request ID {request_id}"
        super().__init__(text)
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id
        self.retry_after = retry_after


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
    """HTTP 429: too many requests (``code`` ``"rate_limited"``) or too many
    concurrent jobs (``"concurrency_limited"``). Wait ``retry_after`` seconds."""


class DeadlineExceededError(EtchvError):
    """The client ``timeout`` passed before a durable job finished (status 0).

    The job may still complete. ``detail`` contains the ``idempotency_key``
    and ``request_id`` (when known) for recovery.
    """


class BatchSubmitError(EtchvError):
    """``submit_batch`` failed after the batch was created: an upload (or the
    start) could not complete.

    ``batch_id`` and ``idempotency_key`` are always set. Call ``submit_batch``
    again with that ``idempotency_key`` and the same items: files that already
    arrived are skipped, the rest are uploaded and the batch starts.
    ``status_code`` is the HTTP status of the failure, or 0 for a network error,
    timeout or unreadable file (see ``__cause__``). ``index`` and ``filename``
    name the file when one failed.
    """

    def __init__(self, status_code: int, detail: Any, request_id: str | None = None, *,
                 retry_after: float | None = None):
        super().__init__(status_code, detail, request_id, retry_after=retry_after)
        inner = detail.get("detail") if isinstance(detail, dict) else None
        inner = inner if isinstance(inner, dict) else {}
        self.batch_id: str | None = inner.get("batch_id")
        self.idempotency_key: str | None = inner.get("idempotency_key")
        self.index: int | None = inner.get("index")
        self.filename: str | None = inner.get("filename")


_ERRORS: dict[int, type[EtchvError]] = {
    401: AuthenticationError, 402: PaymentRequiredError, 403: PermissionDeniedError,
    404: NotFoundError, 409: ConflictError, 410: GoneError, 429: RateLimitError,
}


def _error(status_code: int, detail: Any, request_id: str | None, retry_after: float | None = None) -> EtchvError:
    return _ERRORS.get(status_code, EtchvError)(status_code, detail, request_id, retry_after=retry_after)


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
    accelerator: Accelerator | None = None
    """Hardware that processed the file (``"cpu"`` or ``"gpu"``), when reported."""


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
    accelerator: Accelerator | None = None
    """Hardware that ran detection (``"cpu"`` or ``"gpu"``), when reported."""


BatchStatus = Literal["draft", "starting", "processing", "assembling", "completed", "failed", "cancelled", "expired"]
"""Batch lifecycle. ``completed``, ``failed``, ``cancelled`` and ``expired`` are final."""

BatchItemStatus = Literal["pending", "rejected", "queued", "running", "retrying", "succeeded", "failed"]
"""One file's state inside a batch."""


class BatchItem(TypedDict):
    """One file for ``submit_batch``: its name (the extension sets the media type),
    the file as bytes or a path, and the forensic data to embed."""
    filename: str
    file: bytes | str | os.PathLike[str]
    data: dict[str, Any]


class ZipBatchItem(TypedDict):
    """One zip member for ``submit_batch_zip``: its exact path inside the zip and its data."""
    filename: str
    data: dict[str, Any]


@dataclass(frozen=True)
class BatchEntry:
    """One file of a batch as the API reports it.

    ``error_code`` explains a ``rejected`` or ``failed`` item (for example
    ``upload_not_received``, ``invalid_input`` or ``insufficient_credits``);
    such items are not charged. ``credits`` is what the item cost (0 when
    refunded) once it was accepted.
    """
    index: int
    filename: str
    status: BatchItemStatus
    size: int | None = None
    upload_id: str | None = None
    request_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    credits: int | None = None
    status_url: str | None = None
    result_url: str | None = None
    result_expires_at: str | None = None


@dataclass(frozen=True)
class Batch:
    """A batch of up to 100 files. ``items`` is empty in ``list_batches`` pages.

    ``counts`` holds ``pending``, ``accepted``, ``rejected``, ``succeeded``,
    ``failed`` and ``in_progress``; ``credits`` holds ``reserved``, ``charged``
    and ``refunded``. The ``archive_*`` fields are set when the batch was
    created with ``archive=True``.
    """
    batch_id: str
    status: BatchStatus
    item_count: int
    archive: bool
    accelerator: Accelerator | None
    webhook_id: str | None
    storage_destination_id: str | None
    counts: Mapping[str, int]
    credits: Mapping[str, int]
    cancel_requested: bool
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    upload_expires_at: str | None
    status_url: str
    archive_status: str | None = None
    archive_url: str | None = None
    archive_expires_at: str | None = None
    items: tuple[BatchEntry, ...] = ()

    @property
    def done(self) -> bool:
        """True once the batch is final: ``completed``, ``failed``, ``cancelled`` or ``expired``."""
        return self.status in _FINAL_BATCH


@dataclass(frozen=True)
class BatchPage:
    """One page of ``list_batches``, newest first. Pass ``next_cursor`` as ``before``."""
    data: tuple[Batch, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class BatchItemResult:
    """One item from ``iter_batch_results``: the verified file, or why there is none.

    ``ok`` is true when ``result`` holds the watermarked file. Otherwise
    ``error_code`` (and usually ``error_detail``) explain the failure; the
    item's credits were refunded or never charged.
    """
    index: int
    filename: str
    status: str
    request_id: str | None = None
    result: EmbedResult | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


MB = 1024 * 1024
MAX_BATCH_ITEMS = 100
UPLOAD_MIN_BYTES_PER_SECOND = 128 * 1024
"""Slowest uplink an upload is given time for (about 1 Mbps), on top of the client ``timeout``."""
ARCHIVE_MAX_BYTES = 1024 * MB + 64 * MB
"""Largest batch archive the SDK downloads (1 GiB of results plus 64 MiB for the zip itself)."""
ZIP_BATCH_MAX_BYTES = 55 * MB
_FINAL_BATCH = ("completed", "failed", "cancelled", "expired")
LARGE_FILE_THRESHOLD = 40 * MB
EMBED_MAX_BYTES = 50 * MB
DETECT_MAX_BYTES = 192 * MB
SYNC_DETECT_MAX_BYTES = 95 * MB
_UPLOAD_KINDS = {"images": "image", "documents": "document", "videos": "video"}


class Etchv:
    """Synchronous Etchv API client authenticated with an ``X-API-Key``.

    ``timeout`` (seconds) bounds each call, including automatic polling of
    durable jobs. Use as a context manager or call ``close()``. Files larger
    than ``large_file_threshold`` bytes are sent through an upload session
    (``upload_file``) instead of in the request body.
    """

    def __init__(self, api_key: str, *, base_url: str = "https://api.etchv.com",
                 timeout: float = 120, transport: httpx.BaseTransport | None = None,
                 large_file_threshold: int = LARGE_FILE_THRESHOLD):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        url = urlsplit(base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("base_url must use HTTPS (HTTP is allowed for localhost)")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if type(large_file_threshold) is not int or large_file_threshold < 1:
            raise ValueError("large_file_threshold must be a positive number of bytes")
        self._timeout = timeout
        self._large_file_threshold = large_file_threshold
        # Signed upload URLs carry their own authorization: never send the API key there.
        self._uploads = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport,
                                     follow_redirects=False)
        self._client = httpx.Client(base_url=base_url.rstrip("/") + "/", timeout=timeout,
                                    headers={"X-API-Key": api_key, "User-Agent": USER_AGENT}, transport=transport,
                                    follow_redirects=False)

    def close(self) -> None:
        """Close the underlying HTTP connection pools."""
        self._client.close()
        self._uploads.close()

    def __enter__(self) -> Etchv:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _post(self, path: str, image: bytes, filename: str,
              data: dict[str, str] | None, idempotency_key: str | None) -> httpx.Response:
        route = path.split("?")[0]
        detect = "/detect" in route
        limit = DETECT_MAX_BYTES if detect else EMBED_MAX_BYTES
        if not isinstance(image, bytes) or not image or len(image) > limit:
            raise ValueError(f"file must contain 1 byte to {limit // MB} MB")
        headers = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        durable = path.split("?")[0].endswith("/async") or path.split("?")[0] in ("watermarks/images", "watermarks/documents", "watermarks/videos", "watermarks/videos/detect")
        if durable and not idempotency_key:
            headers["Idempotency-Key"] = uuid4().hex
        if len(image) > self._large_file_threshold:
            # Too large for one request body: upload once, then every retry sends the same upload_id.
            kind = "detect" if detect else _UPLOAD_KINDS[route.split("/")[1]]
            upload = self.upload_file(kind, image, filename=filename)
            return self._request(path, "POST", durable, headers=headers,
                                 data={**(data or {}), "upload_id": upload["upload_id"]})
        return self._request(path, "POST", durable, headers=headers,
                             files={"file": (filename, image, "application/octet-stream")}, data=data)

    def _request(self, path: str, method: str, durable: bool, *, accept: tuple[int, ...] = (),
                 retry: tuple[int, ...] = (429, 502, 503, 504), **kwargs: Any) -> httpx.Response:
        async_submission = path.split("?")[0].endswith("/async")
        detection_job = path.split("?")[0] == "watermarks/videos/detect" or "detection-jobs/" in path
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
            if durable and response.status_code in retry and not (isinstance(detail, dict) and detail.get("status") == "failed"):
                delay = _retry_after(response.headers.get("retry-after")) if response.status_code == 429 else None
                pause(1 if delay is None else min(5, max(.01, delay)))
                continue
            raise _error(response.status_code, detail, request_id, _retry_after(response.headers.get("retry-after")))
        raise DeadlineExceededError(0, {"message": "Client deadline exceeded; the job may still complete", "idempotency_key": idempotency_key, "request_id": request_id}, request_id)

    def _json(self, path: str, method: str = "GET", *, accept: tuple[int, ...] = (), **kwargs: Any) -> Any:
        response = self._request(path, method, False, accept=accept, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise EtchvError(response.status_code, "Invalid JSON response", response.headers.get("x-request-id")) from None

    # Upload sessions

    def upload_file(self, kind: str, file: bytes, *, filename: str = "file") -> dict[str, Any]:
        """Upload a file once to a signed URL and return its session (``upload_id``, ``status``).

        ``kind`` is ``"image"``, ``"document"``, ``"video"`` or ``"detect"``. Pass the
        ``upload_id`` to an embed or detect request instead of the file. The embed and
        detect methods do this automatically above ``large_file_threshold``.
        """
        if kind not in ("image", "document", "video", "detect"):
            raise ValueError('kind must be "image", "document", "video" or "detect"')
        if not isinstance(file, bytes) or not file:
            raise ValueError("file must contain at least 1 byte")
        session = self._json("uploads", "POST", json={"kind": kind, "filename": filename, "size": len(file)})
        upload = session.get("upload") if isinstance(session, dict) else None
        if not isinstance(upload, dict) or upload.get("method") != "PUT" or not str(upload.get("url", "")).startswith("https://"):
            raise EtchvError(201, "Invalid upload session response", None)
        self._put_upload(upload["url"], file)
        return {k: v for k, v in session.items() if k != "upload"} | {"status": "received"}

    def _put_upload(self, url: str, file: bytes) -> None:
        # The upload client has no API key: the signature in the URL is the credential.
        # ``timeout`` bounds each network read or write (an idle timeout), and retries
        # stop after ``timeout`` plus the time the file needs on a slow uplink.
        deadline = time.monotonic() + self._timeout + len(file) / UPLOAD_MIN_BYTES_PER_SECOND
        while True:
            try:
                response = self._uploads.put(url, content=file, headers={"Content-Type": "application/octet-stream"},
                                             timeout=self._timeout)
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
                continue
            if response.status_code == 200:
                return
            if response.status_code in (500, 502, 503, 504) and time.monotonic() < deadline:
                time.sleep(1)
                continue
            raise _error(response.status_code, response.text[:1000] or "Upload refused", None)

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

    @staticmethod
    def _accelerator_path(path: str, accelerator: Accelerator | None) -> str:
        if accelerator is None:
            return path
        if accelerator not in ("cpu", "gpu"):
            raise ValueError('accelerator must be "cpu" or "gpu"')
        return path + ("&" if "?" in path else "?") + f"accelerator={accelerator}"

    def submit_embed(self, media: str, file: bytes, data: dict[str, Any], *, filename: str = "file",
                     idempotency_key: str | None = None, webhook_id: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None,
                     accelerator: Accelerator | None = None) -> dict[str, Any]:
        """Submit a background embedding job and return its 202 JSON receipt without polling.

        ``media`` is ``"images"``, ``"documents"`` or ``"videos"``. Transient
        failures are retried with the same idempotency key (generated when omitted).
        ``accelerator="gpu"`` requests GPU processing (see ``embed_image``).
        """
        if not isinstance(data, dict) or not data:
            raise ValueError("data must be a non-empty JSON object")
        path = self._storage_path(self._async_path(media, False, webhook_id), storage_destination_id, storage_key)
        return self._post(self._accelerator_path(path, accelerator), file, filename,
                          {"data": json.dumps(data, allow_nan=False)}, idempotency_key).json()

    def submit_detection(self, media: str, file: bytes, *, filename: str = "file",
                         idempotency_key: str | None = None, webhook_id: str | None = None,
                         accelerator: Accelerator | None = None) -> dict[str, Any]:
        """Submit a background detection job and return its 202 JSON receipt without polling."""
        path = self._accelerator_path(self._async_path(media, True, webhook_id), accelerator)
        return self._post(path, file, filename, None, idempotency_key).json()

    def get_job(self, request_id: str, *, detect: bool = False) -> dict[str, Any]:
        """Read a job receipt. Set ``detect=True`` for detection jobs.

        ``accelerator_requested`` and ``accelerator`` report the requested and
        actual processing hardware.
        """
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

    # Batches

    def submit_batch(self, items: Sequence[BatchItem], *, archive: bool = False, webhook_id: str | None = None,
                     accelerator: Accelerator | None = None, storage_destination_id: str | None = None,
                     idempotency_key: str | None = None, upload_concurrency: int = 4) -> Batch:
        """Watermark up to 100 files as one batch and return it once started.

        Creates the batch, uploads every file to its signed URL
        (``upload_concurrency`` at a time, never with the API key), then starts
        it. Each item is ``{"filename", "file", "data"}`` where ``file`` is bytes
        or a path. Call ``wait_for_batch`` and ``iter_batch_results`` (or
        ``download_batch_archive`` with ``archive=True``) for the results.

        Transient failures are retried with the same ``idempotency_key``
        (generated when omitted). If an upload or the start fails for good,
        ``BatchSubmitError`` carries the ``batch_id`` and ``idempotency_key``:
        call again with that key and the same items to upload the rest and
        start. Raises ``GoneError`` (``code`` ``batch_expired``) when resuming a
        batch that was not started within 24 hours.
        """
        files = _batch_files(items)
        if type(upload_concurrency) is not int or not 1 <= upload_concurrency <= 16:
            raise ValueError("upload_concurrency must be an integer from 1 to 16")
        key = _idempotency_key(idempotency_key)
        body = {"items": [{"filename": name, "size": size, "data": data} for name, _, size, data in files],
                **_batch_options(archive, webhook_id, accelerator, storage_destination_id)}
        # 503 means batch uploads are unavailable: raise at once (submit_batch_zip still works).
        response = self._request("watermarks/batches", "POST", True, retry=(429, 502, 504),
                                 headers={"Idempotency-Key": key}, json=body)
        payload = _response_json(response)
        batch = _batch(payload, response)
        if batch.status == "expired":
            raise GoneError(410, {"detail": {
                "code": "batch_expired", "batch_id": batch.batch_id, "idempotency_key": key,
                "message": "This batch was not started within 24 hours and expired; "
                           "submit the files again with a new idempotency_key"}}, response.headers.get("x-request-id"))
        if batch.status != "draft":
            return batch  # A replay of a batch that already started (or was canceled).
        uploads = []
        for item in payload.get("items") or []:
            upload = item.get("upload") if isinstance(item, dict) else None
            if upload is None or item.get("upload_received") is True:
                continue  # Already uploaded (a resumed batch).
            index = item.get("index")
            if (type(index) is not int or not 0 <= index < len(files) or not isinstance(upload, dict)
                    or upload.get("method") != "PUT" or not str(upload.get("url", "")).startswith("https://")):
                raise EtchvError(response.status_code, "Invalid batch upload response", response.headers.get("x-request-id"))
            uploads.append((index, upload["url"]))
        self._upload_batch(batch.batch_id, key, files, uploads, upload_concurrency)
        try:
            return self._batch_call(f"watermarks/batches/{batch.batch_id}/start", "POST", accept=(202,))[0]
        except (EtchvError, httpx.TransportError) as error:
            status = error.status_code if isinstance(error, EtchvError) else 0
            if 400 <= status < 500:
                raise  # Definitive (for example 410 when the draft expired): resuming would not help.
            raise _submit_error(error, batch.batch_id, key, "batch_start_failed",
                                "Starting the batch failed") from error

    def _upload_batch(self, batch_id: str, key: str, files: list[tuple[str, Any, int, dict[str, Any]]],
                      uploads: list[tuple[int, str]], concurrency: int) -> None:
        def put(index: int, url: str) -> None:
            filename, source, size, _ = files[index]
            try:
                content = bytes(source) if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
                if len(content) != size:
                    raise ValueError(f"{filename} changed size after the batch was created")
                self._put_upload(url, content)
            except Exception as error:
                raise _submit_error(error, batch_id, key, "batch_upload_failed", f"Uploading {filename} (item {index}) failed",
                                    index=index, filename=filename) from error
        if not uploads:
            return
        with ThreadPoolExecutor(max_workers=min(concurrency, len(uploads))) as pool:
            futures = [pool.submit(put, index, url) for index, url in uploads]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    def submit_batch_zip(self, zip_file: bytes | str | os.PathLike[str], items: Sequence[ZipBatchItem], *,
                         archive: bool = False, webhook_id: str | None = None, accelerator: Accelerator | None = None,
                         storage_destination_id: str | None = None, idempotency_key: str | None = None) -> Batch:
        """Create and start a batch from one zip (up to 55 MB) of files already together.

        ``items`` lists every member as ``{"filename": <exact member path>, "data": {...}}``;
        the API refuses members missing from the list and vice versa. The batch
        starts at once. Retried like ``submit_batch``.
        """
        content = bytes(zip_file) if isinstance(zip_file, (bytes, bytearray)) else Path(zip_file).read_bytes()
        if not content.startswith(b"PK") or len(content) > ZIP_BATCH_MAX_BYTES:
            raise ValueError(f"zip_file must be a zip of up to {ZIP_BATCH_MAX_BYTES // MB} MB")
        _check_batch_count(items)
        members = []
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("filename"), str) or not item["filename"]:
                raise ValueError("Each zip item needs a filename (the member's path in the zip) and data")
            members.append({"filename": item["filename"], "data": _batch_data(item.get("data"), item["filename"])})
        manifest = {"items": members, **_batch_options(archive, webhook_id, accelerator, storage_destination_id)}
        response = self._request("watermarks/batches/zip", "POST", True, accept=(202,),
                                 headers={"Idempotency-Key": _idempotency_key(idempotency_key)},
                                 files={"archive": ("batch.zip", content, "application/zip")},
                                 data={"manifest": json.dumps(manifest, allow_nan=False)})
        return _batch(_response_json(response), response)

    def _batch_call(self, path: str, method: str = "GET", **kwargs: Any) -> tuple[Batch, float | None]:
        response = self._request(path, method, True, **kwargs)
        return _batch(_response_json(response), response), _retry_after(response.headers.get("retry-after"))

    def get_batch(self, batch_id: str) -> Batch:
        """Read a batch with every item's status (one request; see ``wait_for_batch`` to poll)."""
        return self._batch_call(_batch_path(batch_id))[0]

    def wait_for_batch(self, batch_id: str, *, timeout: float = 3600, poll_interval: float | None = None) -> Batch:
        """Poll a batch until it is final and return it.

        Waits as long as the API's ``Retry-After`` asks between polls (or
        ``poll_interval`` seconds, whichever is longer). Raises
        ``DeadlineExceededError`` after ``timeout`` seconds; the batch keeps running.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if poll_interval is not None and (not math.isfinite(poll_interval) or poll_interval <= 0):
            raise ValueError("poll_interval must be positive and finite")
        path = _batch_path(batch_id)
        deadline = time.monotonic() + timeout
        while True:
            batch, retry_after = self._batch_call(path)
            if batch.done:
                return batch
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceededError(0, {"message": "Client deadline exceeded; the batch is still running",
                                                "batch_id": batch_id, "status": batch.status}, None)
            delay = max(1.0, retry_after) if retry_after is not None else (poll_interval or 2)
            time.sleep(min(max(delay, poll_interval or 0), remaining))

    def iter_batch_results(self, batch_id: str, *, timeout: float = 3600) -> Iterator[BatchItemResult]:
        """Wait for a batch to finish, then yield one ``BatchItemResult`` per item in order.

        Succeeded items carry the verified file in ``result`` (downloaded as you
        iterate; results are kept 24 hours). Other items carry ``error_code``.
        """
        batch = self.wait_for_batch(batch_id, timeout=timeout)
        for entry in batch.items:
            if entry.status == "succeeded" and entry.request_id:
                yield BatchItemResult(entry.index, entry.filename, entry.status, entry.request_id,
                                      self.get_embed_result(entry.request_id))
            else:
                code = entry.error_code or (batch.status if batch.status in ("cancelled", "expired") else entry.status)
                yield BatchItemResult(entry.index, entry.filename, entry.status, entry.request_id,
                                      error_code=code, error_detail=entry.error_detail)

    def download_batch_archive(self, batch_id: str, *, timeout: float = 3600) -> bytes:
        """Wait for and download the zip of a batch created with ``archive=True``.

        The zip holds every successful result plus ``manifest.json`` listing
        files and failures. It can reach 1 GB: use ``download_batch_archive_to``
        to stream it to a file instead of holding it in memory. Raises
        ``ConflictError`` (``code`` ``archive_not_requested``,
        ``batch_not_started``, ``archive_too_large`` or ``archive_unavailable``),
        ``GoneError`` after 24 hours or for an expired draft, and
        ``DeadlineExceededError`` when it is not ready within ``timeout`` seconds.
        An archive above ``ARCHIVE_MAX_BYTES`` raises ``EtchvError`` with ``code``
        ``archive_too_large`` and is not retried.
        """
        buffer = io.BytesIO()
        self._stream_archive(batch_id, timeout, buffer.write)
        return buffer.getvalue()

    def download_batch_archive_to(self, batch_id: str, destination: str | os.PathLike[str] | BinaryIO, *,
                                  timeout: float = 3600) -> int:
        """Like ``download_batch_archive``, but streams the zip to a path or a binary
        file object and returns the number of bytes written.

        A path is written through a ``.part`` file that replaces it only once the
        download is complete. Each network read may take up to the client
        ``timeout``; the download as a whole has no time limit.
        """
        if not isinstance(destination, (str, os.PathLike)):
            return self._stream_archive(batch_id, timeout, destination.write)
        target = Path(destination)
        partial = target.with_name(target.name + ".part")
        try:
            with partial.open("wb") as handle:
                written = self._stream_archive(batch_id, timeout, handle.write)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return written

    def _stream_archive(self, batch_id: str, timeout: float, write: Any) -> int:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        path = _batch_path(batch_id) + "/archive"
        deadline = time.monotonic() + timeout
        while True:
            receiving = False
            try:
                # The client timeout applies to each network read, so a large archive on a
                # slow link keeps going as long as bytes keep arriving.
                with self._client.stream("GET", path, timeout=self._timeout) as response:
                    request_id = response.headers.get("x-request-id")
                    retry_after = _retry_after(response.headers.get("retry-after"))
                    if response.status_code == 200:
                        receiving = True
                        try:
                            length = int(response.headers.get("content-length", "0"))
                        except ValueError:
                            length = 0
                        if length > ARCHIVE_MAX_BYTES:
                            raise _archive_too_large(request_id)
                        return _write_zip(response, write, request_id)
                    response.read()
                    if response.status_code == 202:
                        delay = max(1.0, 2.0 if retry_after is None else retry_after)
                    elif response.status_code in (429, 502, 503, 504):
                        delay = max(1.0, 1.0 if retry_after is None else min(5.0, retry_after))
                    else:
                        try:
                            detail = response.json()
                        except ValueError:
                            detail = response.text[:1000]
                        raise _error(response.status_code, detail, request_id, retry_after)
            except httpx.TransportError:
                if receiving or time.monotonic() >= deadline:
                    raise  # Part of the archive may already be written: do not append a second copy.
                delay = 1.0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceededError(0, {"message": "Client deadline exceeded; the archive is not ready yet",
                                                "batch_id": batch_id}, None)
            time.sleep(min(delay, remaining))

    def cancel_batch(self, batch_id: str) -> Batch:
        """Cancel a batch.

        A draft is canceled at once. In a started batch, files still waiting
        fail with ``error_code`` ``"cancelled"`` and are refunded; queued and
        running files finish. The batch then ends as ``cancelled``.
        """
        return self._batch_call(_batch_path(batch_id) + "/cancel", "POST")[0]

    def list_batches(self, *, limit: int = 20, before: str | None = None) -> BatchPage:
        """List batches newest first (``limit`` 1–50), without items. Pass ``next_cursor`` as ``before``."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        params: dict[str, Any] = {"limit": limit}
        if before is not None:
            _check_id(before, r"bat_[a-f0-9]{32}", "batch cursor")
            params["before"] = before
        response = self._request("watermarks/batches", "GET", True, params=params)
        payload = _response_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EtchvError(200, "Invalid batch list response", response.headers.get("x-request-id"))
        cursor = payload.get("next_cursor")
        return BatchPage(tuple(_batch(entry, response) for entry in payload["data"]),
                         cursor if isinstance(cursor, str) else None)

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
                    idempotency_key: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None,
                    accelerator: Accelerator | None = None) -> EmbedResult:
        """Watermark an image and wait for the verified file in its original format.

        Retries transient failures with the same idempotency key and polls
        pending jobs until ``timeout``; raises ``DeadlineExceededError`` then.
        ``accelerator="gpu"`` (Business and Enterprise plans) uses 3x credits
        and runs on CPU at normal credits when no GPU is ready; the result's
        ``accelerator`` reports the hardware used.
        """
        return self._embed_media("images", image, data, filename, idempotency_key, storage_destination_id, storage_key, accelerator)

    def embed_document(self, document: bytes, data: dict[str, Any], *, filename: str = "document.pdf",
                       idempotency_key: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None,
                       accelerator: Accelerator | None = None) -> EmbedResult:
        """Watermark a PDF and wait for the verified PDF (see ``embed_image``)."""
        return self._embed_media("documents", document, data, filename, idempotency_key, storage_destination_id, storage_key, accelerator)

    def embed_video(self, video: bytes, data: dict[str, Any], *, filename: str = "video.mp4",
                    idempotency_key: str | None = None, storage_destination_id: str | None = None, storage_key: str | None = None,
                    accelerator: Accelerator | None = None) -> EmbedResult:
        """Watermark an MP4/MOV video and wait for the verified video (see ``embed_image``)."""
        return self._embed_media("videos", video, data, filename, idempotency_key, storage_destination_id, storage_key, accelerator)

    def _embed_media(self, media: str, image: bytes, data: dict[str, Any], filename: str,
                     idempotency_key: str | None, destination: str | None, storage_key: str | None,
                     accelerator: Accelerator | None) -> EmbedResult:
        if not isinstance(data, dict) or not data:
            raise ValueError("data must be a non-empty JSON object")
        encoded = json.dumps(data, allow_nan=False)
        path = self._accelerator_path(self._storage_path(f"watermarks/{media}", destination, storage_key), accelerator)
        response = self._post(path, image, filename, {"data": encoded}, idempotency_key)
        return self._embedding_result(response)

    def _embedding_result(self, response: httpx.Response) -> EmbedResult:
        watermark_id = response.headers.get("x-watermark-id", "")
        content_type = response.headers.get("content-type", "").split(";")[0]
        extension = _image_extension(response.content, content_type)
        if not extension or not _valid_id(watermark_id):
            raise EtchvError(200, "Invalid embedding response", response.headers.get("x-request-id"))
        match = re.search(r'filename="([A-Za-z0-9._-]+)"', response.headers.get("content-disposition", ""))
        filename = match.group(1) if match else f"image-watermarked.{extension}"
        return EmbedResult(response.content, watermark_id, response.headers.get("x-request-id"), content_type, filename, response.headers.get("x-asset-id"), response.headers.get("x-source-asset-id"), response.headers.get("x-storage-delivery-id"),
                           _accelerator(response.headers.get("x-etchv-accelerator")))

    # Synchronous detection

    def detect_image(self, image: bytes, *, filename: str = "image.png",
                     idempotency_key: str | None = None, accelerator: Accelerator | None = None) -> DetectionResult:
        """Detect a watermark in an image. Not retried automatically.

        ``accelerator="gpu"`` requests GPU processing (see ``embed_image``).
        """
        return self._detect_media("images", image, filename, idempotency_key, accelerator)

    def detect_document(self, document: bytes, *, filename: str = "document.pdf",
                        idempotency_key: str | None = None, accelerator: Accelerator | None = None) -> DetectionResult:
        """Detect watermarks page by page in a PDF. Not retried automatically."""
        return self._detect_media("documents", document, filename, idempotency_key, accelerator)

    def detect_video(self, video: bytes, *, filename: str = "video.mp4",
                     idempotency_key: str | None = None, accelerator: Accelerator | None = None) -> DetectionResult:
        """Detect watermarks frame by frame in a video, polling the durable job."""
        return self._detect_media("videos", video, filename, idempotency_key, accelerator)

    def _detect_media(self, media: str, image: bytes, filename: str, idempotency_key: str | None,
                      accelerator: Accelerator | None) -> DetectionResult:
        if media != "videos" and isinstance(image, bytes) and len(image) > SYNC_DETECT_MAX_BYTES:
            # Synchronous image and PDF detection stops at 95 MB; larger delivered files run as a job.
            receipt = self.submit_detection(media, image, filename=filename, idempotency_key=idempotency_key, accelerator=accelerator)
            return self.get_detection_result(receipt["request_id"])
        path = self._accelerator_path(f"watermarks/{media}/detect", accelerator)
        return self._detection_result(self._post(path, image, filename, None, idempotency_key))

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
        accelerator = _accelerator(response.headers.get("x-etchv-accelerator")) or _accelerator(result.get("accelerator"))
        return DetectionResult(detected, confidence, identifier, response.headers.get("x-request-id"), tuple(units), accelerator)


def _check_id(value: Any, pattern: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {label}")


def _write_zip(response: httpx.Response, write: Any, request_id: str | None) -> int:
    total, head = 0, b""
    for chunk in response.iter_bytes():
        if total == 0:
            head += chunk
            if len(head) < 2:
                continue
            if not head.startswith(b"PK"):
                raise EtchvError(200, "Invalid archive response", request_id)
            chunk = head
        if total + len(chunk) > ARCHIVE_MAX_BYTES:
            raise _archive_too_large(request_id)
        write(chunk)
        total += len(chunk)
    if total == 0:
        raise EtchvError(200, "Invalid archive response", request_id)
    return total


def _archive_too_large(request_id: str | None) -> EtchvError:
    return EtchvError(200, {"detail": {"code": "archive_too_large", "message": f"The archive is larger than "
                                       f"{ARCHIVE_MAX_BYTES // MB} MiB; download each item's result instead"}}, request_id)


def _submit_error(error: BaseException, batch_id: str, key: str, code: str, what: str, *,
                  index: int | None = None, filename: str | None = None) -> BatchSubmitError:
    status = error.status_code if isinstance(error, EtchvError) else 0
    reason = f"HTTP {status}" if status else type(error).__name__
    detail: dict[str, Any] = {
        "code": code, "batch_id": batch_id, "idempotency_key": key,
        "message": f"{what} ({reason}); call submit_batch again with idempotency_key={key!r} and the same items to resume"}
    if index is not None:
        detail.update(index=index, filename=filename)
    return BatchSubmitError(status, {"detail": detail}, getattr(error, "request_id", None),
                            retry_after=getattr(error, "retry_after", None))


def _batch_path(batch_id: str) -> str:
    _check_id(batch_id, r"bat_[a-f0-9]{32}", "batch ID")
    return f"watermarks/batches/{batch_id}"


def _idempotency_key(key: str | None) -> str:
    if key is None:
        return uuid4().hex
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        raise ValueError("idempotency_key must be 8–128 letters, digits, hyphens or underscores")
    return key


def _check_batch_count(items: Any) -> None:
    if isinstance(items, (str, bytes, Mapping)) or not isinstance(items, Sequence):
        raise ValueError("items must be a list of batch items")
    if not 1 <= len(items) <= MAX_BATCH_ITEMS:
        raise ValueError(f"A batch takes 1 to {MAX_BATCH_ITEMS} files; got {len(items)}. "
                         f"Split larger sets into several batches.")


def _batch_data(data: Any, filename: str) -> dict[str, Any]:
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{filename}: data must be a non-empty JSON object")
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError(f"{filename}: data must contain JSON values") from None
    return data


def _batch_files(items: Any) -> list[tuple[str, Any, int, dict[str, Any]]]:
    _check_batch_count(items)
    files = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("filename"), str) or not item["filename"]:
            raise ValueError("Each batch item needs a filename, a file and data")
        filename, source = item["filename"], item.get("file")
        if isinstance(source, (bytes, bytearray)):
            size = len(source)
        elif isinstance(source, (str, os.PathLike)):
            size = os.stat(source).st_size
        else:
            raise ValueError(f"{filename}: file must be bytes or a path")
        if size < 1:
            raise ValueError(f"{filename}: file is empty")
        files.append((filename, source, size, _batch_data(item.get("data"), filename)))
    return files


def _batch_options(archive: bool, webhook_id: str | None, accelerator: Accelerator | None,
                   storage_destination_id: str | None) -> dict[str, Any]:
    if type(archive) is not bool:
        raise ValueError("archive must be a boolean")
    options: dict[str, Any] = {"archive": archive}
    if webhook_id is not None:
        _check_id(webhook_id, r"wh_[a-f0-9]{32}", "webhook ID")
        options["webhook_id"] = webhook_id
    if accelerator is not None:
        if accelerator not in ("cpu", "gpu"):
            raise ValueError('accelerator must be "cpu" or "gpu"')
        options["accelerator"] = accelerator
    if storage_destination_id is not None:
        if archive:
            raise ValueError("archive and storage_destination_id cannot be combined")
        _check_id(storage_destination_id, r"dst_[a-f0-9]{32}", "storage destination ID")
        options["storage_destination_id"] = storage_destination_id
    return options


def _response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise EtchvError(response.status_code, "Invalid JSON response", response.headers.get("x-request-id")) from None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _batch(payload: Any, response: httpx.Response) -> Batch:
    if (not isinstance(payload, dict) or not re.fullmatch(r"bat_[a-f0-9]{32}", str(payload.get("batch_id", "")))
            or not isinstance(payload.get("status"), str) or not isinstance(payload.get("items", []), list)):
        raise EtchvError(response.status_code, "Invalid batch response", response.headers.get("x-request-id"))
    entries = []
    for item in payload.get("items", []):
        if not isinstance(item, dict) or type(item.get("index")) is not int or not isinstance(item.get("status"), str):
            raise EtchvError(response.status_code, "Invalid batch item", response.headers.get("x-request-id"))
        text = {name: _optional_str(item.get(name)) for name in (
            "upload_id", "request_id", "error_code", "error_detail", "status_url", "result_url", "result_expires_at")}
        entries.append(BatchEntry(
            index=item["index"], filename=str(item.get("filename", "")), status=item["status"],
            size=item["size"] if type(item.get("size")) is int else None,
            credits=item["credits"] if type(item.get("credits")) is int else None, **text))
    counts, credits = payload.get("counts"), payload.get("credits")
    return Batch(
        batch_id=payload["batch_id"], status=payload["status"],
        item_count=payload["item_count"] if type(payload.get("item_count")) is int else len(entries),
        archive=payload.get("archive") is True, accelerator=_accelerator(payload.get("accelerator")),
        webhook_id=_optional_str(payload.get("webhook_id")),
        storage_destination_id=_optional_str(payload.get("storage_destination_id")),
        counts=dict(counts) if isinstance(counts, dict) else {}, credits=dict(credits) if isinstance(credits, dict) else {},
        cancel_requested=payload.get("cancel_requested") is True,
        created_at=_optional_str(payload.get("created_at")), started_at=_optional_str(payload.get("started_at")),
        completed_at=_optional_str(payload.get("completed_at")),
        upload_expires_at=_optional_str(payload.get("upload_expires_at")),
        status_url=_optional_str(payload.get("status_url")) or f"/watermarks/batches/{payload['batch_id']}",
        archive_status=_optional_str(payload.get("archive_status")), archive_url=_optional_str(payload.get("archive_url")),
        archive_expires_at=_optional_str(payload.get("archive_expires_at")), items=tuple(entries))


def _accelerator(value: Any) -> Accelerator | None:
    return value if value in ("cpu", "gpu") else None


def _retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP date), or ``None``."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


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
