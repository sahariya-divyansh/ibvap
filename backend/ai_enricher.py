"""Background AI event enricher with token-bucket limiter and circuit breaker."""

from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from backend.fallback_summary import rule_based_analysis
from backend.gemini_client import (
    AnalysisResult,
    GeminiAnalyzer,
    sanitize_sensitive_text,
)

LOGGER = logging.getLogger(__name__)

WHITELISTED_EVENT_TYPES = frozenset(
    {
        "VIRTUAL_FENCE_CROSS",
        "RESTRICTED_ZONE_ENTRY",
        "LOITERING",
        "CAMERA_TAMPER",
        "SUSPICIOUS_SEQUENCE",
    }
)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if exception indicates a rate limit or quota exhaustion (429)."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", getattr(exc, "code", None))
    return bool(
        status_code == 429
        or "429" in msg
        or "resourceexhausted" in name
        or "resource_exhausted" in msg
        or "rate limit" in msg
        or "quota exceeded" in msg
        or "too many requests" in msg
    )


def _is_server_error(exc: Exception) -> bool:
    """Return True if exception indicates a 5xx server-side error."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", getattr(exc, "code", None))
    return bool(
        (status_code is not None and isinstance(status_code, int) and 500 <= status_code <= 599)
        or any(code in msg for code in ["500", "502", "503", "504"])
        or any(n in name for n in ["internalserver", "serviceunavailable", "badgateway", "gatewaytimeout"])
    )


def _is_timeout_error(exc: Exception) -> bool:
    """Return True if exception indicates a client or server timeout."""
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    status_code = getattr(exc, "status_code", getattr(exc, "code", None))
    return bool(
        status_code in (408, 504)
        or isinstance(exc, TimeoutError)
        or "timeout" in name
        or "timeout" in msg
        or "deadline exceeded" in msg
    )


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract retry-after delay in seconds if present in exception or headers."""
    if hasattr(exc, "retry_after"):
        try:
            return float(exc.retry_after)
        except (ValueError, TypeError):
            pass
    response = getattr(exc, "response", None)
    if response and hasattr(response, "headers"):
        headers = response.headers
        val = headers.get("retry-after") or headers.get("Retry-After")
        if val:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    m = re.search(r"retry[-_]after[:=]\s*(\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except (ValueError, TypeError):
            pass
    return None


class AIEnricher:
    """Asynchronous background enricher for security events using Gemini with fallback."""

    def __init__(
        self,
        analyzer: GeminiAnalyzer | None = None,
        fallback: Callable[[dict[str, Any]], AnalysisResult] | None = None,
        on_result: Callable[[str, AnalysisResult, str, str], None] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        rpm: float | None = None,
        queue_size: int = 50,
    ) -> None:
        """Initialize the background enricher."""
        self.analyzer = analyzer
        self.fallback = fallback or rule_based_analysis
        self.on_result = on_result
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep

        raw_rpm = rpm if rpm is not None else os.getenv("IBVAP_GEMINI_RPM", "8")
        self.rpm = max(0.1, float(raw_rpm))
        self.queue_size = max(1, queue_size)

        # Threading and queue state
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._queue: collections.deque[tuple[str, dict[str, Any], Any, bool]] = collections.deque()
        self._cache: dict[str, AnalysisResult] = {}
        self._in_flight: set[str] = set()

        # Rate limiter and metrics state
        self._last_call_time = 0.0
        self._call_timestamps: collections.deque[float] = collections.deque()

        # Circuit breaker state
        self._consecutive_failures = 0
        self._offline_until = 0.0
        self._probing = False
        self._online = True
        self._last_error: str | None = None

        self._stopped = False
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="AIEnricherWorker",
            daemon=True,
        )
        self._worker_thread.start()

    @property
    def enabled(self) -> bool:
        """Return True if Gemini analyzer is configured and ready."""
        if self.analyzer is None:
            return False
        try:
            return self.analyzer.client is not None
        except Exception:  # noqa: BLE001
            return False

    def submit(
        self,
        event_id: str,
        meta: dict[str, Any],
        image_loader: Callable[[], bytes | None] | bytes | None = None,
        force: bool = False,
    ) -> bool:
        """Submit an event for analysis non-blockingly.

        Returns True if queued, False if discarded due to deduplication.
        Drops oldest queued item when queue capacity is reached.
        """
        with self._condition:
            if self._stopped:
                return False

            if not force and (event_id in self._cache or event_id in self._in_flight):
                return False

            if len(self._queue) >= self.queue_size:
                dropped = self._queue.popleft()
                dropped_id = dropped[0]
                self._in_flight.discard(dropped_id)
                LOGGER.warning(
                    "AIEnricher queue full (%d); dropped oldest event %s",
                    self.queue_size,
                    dropped_id,
                )

            self._in_flight.add(event_id)
            self._queue.append((event_id, meta, image_loader, force))
            self._condition.notify()
            return True

    def stop(self) -> None:
        """Idempotently shut down the background worker."""
        with self._condition:
            if self._stopped:
                return
            self._stopped = True
            self._condition.notify_all()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        """Return current status snapshot with sanitized errors."""
        with self._lock:
            now = self.clock()
            self._prune_calls(now)
            is_online = self._online and (now >= self._offline_until)
            return {
                "enabled": self.enabled,
                "online": is_online,
                "queue_size": len(self._queue),
                "calls_last_minute": len(self._call_timestamps),
                "last_error": sanitize_sensitive_text(self._last_error) if self._last_error else None,
            }

    def _worker_loop(self) -> None:
        """Main loop executed by the daemon worker thread."""
        while True:
            with self._condition:
                while not self._queue and not self._stopped:
                    self._condition.wait()
                if self._stopped and not self._queue:
                    break
                event_id, meta, image_loader, force = self._queue.popleft()

            try:
                self._process_event(event_id, meta, image_loader, force)
            except Exception:
                LOGGER.exception("Unexpected error processing event %s", event_id)
            finally:
                with self._lock:
                    self._in_flight.discard(event_id)

    def _process_event(
        self,
        event_id: str,
        meta: dict[str, Any],
        image_loader: Callable[[], bytes | None] | bytes | None,
        force: bool,
    ) -> None:
        """Handle individual event enrichment with fallback and circuit breaker rules."""
        event_type = meta.get("event_type")

        # 1. Whitelist check: only whitelisted types invoke Gemini
        if event_type not in WHITELISTED_EVENT_TYPES:
            fallback_res = self.fallback(meta)
            self._cache[event_id] = fallback_res
            self._invoke_on_result(event_id, fallback_res, source="rules", status="skipped_whitelist")
            return

        # 2. Enabled check
        if not self.enabled:
            fallback_res = self.fallback(meta)
            self._cache[event_id] = fallback_res
            self._invoke_on_result(event_id, fallback_res, source="rules", status="disabled")
            return

        # 3. Circuit breaker check
        now = self.clock()
        is_probe = False
        if now < self._offline_until:
            # Circuit breaker is OPEN: offline fallback immediately
            fallback_res = self.fallback(meta)
            self._cache[event_id] = fallback_res
            self._invoke_on_result(event_id, fallback_res, source="rules", status="offline")
            return
        if self._offline_until > 0.0 and now >= self._offline_until:
            is_probe = True

        # Load image bytes if loader is provided
        image_bytes: bytes | None = None
        if callable(image_loader):
            try:
                image_bytes = image_loader()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to load image for event %s: %s", event_id, exc)
        elif isinstance(image_loader, (bytes, bytearray)):
            image_bytes = bytes(image_loader)

        # 4. Token-bucket limiter pacing
        self._wait_for_token()

        # 5. Call analyzer with exponential backoff on retryable errors
        max_retries = 3
        attempt = 0
        while True:
            try:
                assert self.analyzer is not None
                result = self.analyzer.analyze_event(meta=meta, image_bytes=image_bytes)
                # Success: reset circuit breaker
                with self._lock:
                    self._consecutive_failures = 0
                    self._offline_until = 0.0
                    self._online = True
                    self._probing = False
                    self._cache[event_id] = result
                self._invoke_on_result(event_id, result, source="gemini", status="success")
                return

            except Exception as exc:  # noqa: BLE001
                self._last_error = sanitize_sensitive_text(str(exc))
                can_retry = (
                    _is_rate_limit_error(exc)
                    or _is_server_error(exc)
                    or _is_timeout_error(exc)
                ) and (attempt < max_retries)

                if can_retry:
                    retry_after = _extract_retry_after(exc)
                    if retry_after is not None and retry_after > 0:
                        backoff = retry_after
                    else:
                        backoff = (2**attempt) * 1.0  # 1s, 2s, 4s
                    attempt += 1
                    LOGGER.info(
                        "Retrying Gemini call for event %s (attempt %d/%d) after %.1fs backoff",
                        event_id,
                        attempt,
                        max_retries,
                        backoff,
                    )
                    self.sleep(backoff)
                    continue

                # Failure reached (retries exhausted or non-retryable)
                with self._lock:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 3 or is_probe:
                        self._offline_until = self.clock() + 60.0
                        self._online = False
                        self._probing = False
                        LOGGER.warning(
                            "AIEnricher circuit breaker opened for 60s (failures=%d)",
                            self._consecutive_failures,
                        )

                fallback_res = self.fallback(meta)
                self._cache[event_id] = fallback_res
                self._invoke_on_result(event_id, fallback_res, source="rules", status="fallback")
                return

    def _wait_for_token(self) -> None:
        """Ensure requests are spaced according to rpm."""
        interval = 60.0 / self.rpm
        now = self.clock()
        elapsed = now - self._last_call_time
        wait_time = interval - elapsed
        if wait_time > 0:
            self.sleep(wait_time)
        now = self.clock()
        self._last_call_time = now
        with self._lock:
            self._call_timestamps.append(now)
            self._prune_calls(now)

    def _prune_calls(self, now: float) -> None:
        """Prune call timestamps older than 60 seconds."""
        cutoff = now - 60.0
        while self._call_timestamps and self._call_timestamps[0] < cutoff:
            self._call_timestamps.popleft()

    def _invoke_on_result(
        self,
        event_id: str,
        result: AnalysisResult,
        source: str,
        status: str,
    ) -> None:
        """Dispatch on_result callback safely from worker thread."""
        if self.on_result is None:
            return
        try:
            self.on_result(event_id, result, source, status)
        except Exception:
            LOGGER.exception("Error in on_result callback for event %s", event_id)
