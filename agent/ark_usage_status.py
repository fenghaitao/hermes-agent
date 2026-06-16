"""Background poller for Volcengine Ark Coding-Plan usage → CLI status bar.

Self-contained: the signed-OpenAPI (Volcengine signature v4) request logic for
the ``GetCodingPlanUsage`` action is vendored here, so the repo-root
``usage.py`` script is **not** required at runtime (it remains a standalone
convenience CLI). Fetches the session/5h, weekly, and monthly quota windows on
a slow background cadence and caches a compact pre-formatted label that the
prompt_toolkit status bar can read on its ~1s render tick **without ever
touching the network on the render thread**.

Display-only; it never raises into the UI. The probe stays dormant unless
``display.ark_usage.enabled`` is true in ``config.yaml`` AND both
``VOLC_ACCESSKEY`` / ``VOLC_SECRETKEY`` credentials are present in the
environment — otherwise ``start()`` is a no-op and ``short_label()`` returns
``""`` so the status bar simply omits the segment.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Volcengine Ark OpenAPI (signed GET) — vendored from the repo-root usage.py
# so this feature carries no dependency on that standalone script. Endpoint and
# signature scheme match the console "subscribe" page's GetCodingPlanUsage.
# --------------------------------------------------------------------------- #
_OPENAPI_HOST = "open.volcengineapi.com"
_OPENAPI_REGION = "cn-beijing"
_OPENAPI_SERVICE = "ark"
_HTTP_TIMEOUT = 30.0


def _action() -> str:
    return os.environ.get("ARK_USAGE_ACTION", "GetCodingPlanUsage")


def _version() -> str:
    return os.environ.get("ARK_USAGE_VERSION", "2024-01-01")


def _sign(secret: bytes, msg: str) -> bytes:
    return hmac.new(secret, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret_key: str, date: str) -> bytes:
    k = _sign(secret_key.encode(), date)
    k = _sign(k, _OPENAPI_REGION)
    k = _sign(k, _OPENAPI_SERVICE)
    return _sign(k, "request")


def _openapi_get(action: str, version: str) -> dict:
    """Signed GET against Volcengine OpenAPI (signature v4); returns parsed JSON.

    Returns ``{}`` when AK/SK are absent (the caller pre-checks) or on a
    transport error. HTTP error bodies are parsed and returned as-is so the
    caller can inspect ``ResponseMetadata.Error``.
    """
    ak = os.environ.get("VOLC_ACCESSKEY")
    sk = os.environ.get("VOLC_SECRETKEY")
    if not (ak and sk):
        return {}

    q = {"Action": action, "Version": version}
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(q.items())
    )

    now = _dt.datetime.now(_dt.timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_headers = (
        f"host:{_OPENAPI_HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    signed_headers = "host;x-content-sha256;x-date"
    canonical_request = "\n".join(
        ["GET", "/", canonical_query, canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{short_date}/{_OPENAPI_REGION}/{_OPENAPI_SERVICE}/request"
    string_to_sign = "\n".join(
        [
            "HMAC-SHA256",
            x_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(sk, short_date), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"HMAC-SHA256 Credential={ak}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    req = urllib.request.Request(
        f"https://{_OPENAPI_HOST}/?{canonical_query}",
        method="GET",
        headers={
            "Host": _OPENAPI_HOST,
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
            "Authorization": authorization,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except Exception:
            return {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Display helpers (vendored so cli.py imports them from here, not usage.py).
# --------------------------------------------------------------------------- #
def format_timestamp(ts: int) -> str:
    """Local time string for a unix timestamp, or ``"-"`` for non-positive."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def quota_bar(pct: float, width: int = 16) -> str:
    """Render a ``[####----]`` usage bar for a 0-100 percentage."""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------- #
# Quota-window labeling.
# --------------------------------------------------------------------------- #
# Map raw API "Level" names onto the short labels shown in the status bar. The
# live API returns session/weekly/monthly; accept a few variants and fall back
# to a 3-char slug for anything unrecognized.
_LEVEL_LABELS = {
    "session": "5h",
    "5h": "5h",
    "fivehour": "5h",
    "five_hour": "5h",
    "weekly": "wk",
    "week": "wk",
    "monthly": "mo",
    "month": "mo",
}
# Render order: 5h first (the tightest window), then weekly, then monthly.
_LEVEL_ORDER = {"5h": 0, "wk": 1, "mo": 2}

# Hard floor on the refresh cadence so a misconfigured value can't hammer the
# Volcengine OpenAPI from the background thread.
_MIN_REFRESH_SECONDS = 30.0


class ArkUsageProbe:
    """Polls Ark Coding-Plan quota in the background and caches a short label."""

    def __init__(self, refresh_seconds: float = 120.0) -> None:
        try:
            refresh = float(refresh_seconds)
        except (TypeError, ValueError):
            refresh = 120.0
        self._refresh = max(_MIN_REFRESH_SECONDS, refresh)
        self._lock = threading.Lock()
        # (label, percent_used, reset_ts) per window, sorted by _LEVEL_ORDER.
        self._windows: List[Tuple[str, float, int]] = []
        self._label: str = ""
        self._session_percent: Optional[float] = None
        self._last_fetch_at: float = 0.0
        self._started = False

    @staticmethod
    def _have_credentials() -> bool:
        return bool(
            os.environ.get("VOLC_ACCESSKEY") and os.environ.get("VOLC_SECRETKEY")
        )

    def start(self) -> None:
        """Start the background poller once.

        No-op if already started or if the AK/SK credentials are absent — this
        is safe to call on every status-bar render tick.
        """
        with self._lock:
            if self._started or not self._have_credentials():
                return
            self._started = True
        thread = threading.Thread(
            target=self._run, name="ark-usage-probe", daemon=True
        )
        thread.start()

    def _run(self) -> None:
        while True:
            try:
                self._fetch_once()
            except Exception as exc:  # the poller must never die
                logger.debug("Ark usage probe fetch failed: %s", exc)
            time.sleep(self._refresh)

    def _fetch_once(self) -> None:
        # Re-check each cycle so revoked/edited creds quietly stop updating.
        if not self._have_credentials():
            return

        result = _openapi_get(_action(), _version())
        if not isinstance(result, dict) or not result:
            return
        meta = result.get("ResponseMetadata", {}) or {}
        if meta.get("Error"):
            logger.debug("Ark usage OpenAPI error: %s", meta.get("Error"))
            return

        res = result.get("Result", {}) or {}
        windows: List[Tuple[str, float, int]] = []
        for q in res.get("QuotaUsage", []) or []:
            raw_level = str(q.get("Level", "")).strip().lower()
            label = _LEVEL_LABELS.get(raw_level, (raw_level[:3] or "?"))
            try:
                pct = float(q.get("Percent", 0) or 0)
            except (TypeError, ValueError):
                pct = 0.0
            try:
                reset_ts = int(q.get("ResetTimestamp", 0) or 0)
            except (TypeError, ValueError):
                reset_ts = 0
            windows.append((label, pct, reset_ts))

        windows.sort(key=lambda w: _LEVEL_ORDER.get(w[0], 99))
        label = " · ".join(f"{lbl} {pct:.0f}%" for lbl, pct, _ in windows)
        session_pct = next(
            (pct for lbl, pct, _ in windows if lbl == "5h"), None
        )

        with self._lock:
            self._windows = windows
            self._label = label
            self._session_percent = session_pct
            self._last_fetch_at = time.time()

    def short_label(self) -> str:
        """Compact cached label, e.g. ``"5h 42% · wk 71% · mo 58%"`` (or "")."""
        with self._lock:
            return self._label

    def windows(self) -> List[Tuple[str, float, int]]:
        """Cached per-window ``(label, percent_used, reset_ts)`` (sorted)."""
        with self._lock:
            return list(self._windows)

    def fetch_now(self) -> None:
        """Force one synchronous fetch (for on-demand callers like ``/usage``).

        Swallows all errors; check :meth:`windows` afterward for the result.
        """
        try:
            self._fetch_once()
        except Exception as exc:
            logger.debug("Ark usage on-demand fetch failed: %s", exc)

    def session_percent(self) -> Optional[float]:
        """Percent-used of the tightest (5h) window, for status-bar coloring."""
        with self._lock:
            return self._session_percent


_probe: Optional[ArkUsageProbe] = None
_probe_lock = threading.Lock()


def get_probe(refresh_seconds: float = 120.0) -> ArkUsageProbe:
    """Return the process-wide :class:`ArkUsageProbe` singleton."""
    global _probe
    with _probe_lock:
        if _probe is None:
            _probe = ArkUsageProbe(refresh_seconds=refresh_seconds)
        return _probe
