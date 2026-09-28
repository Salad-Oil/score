"""Typed errors so the engine can react differently to each failure class."""

from __future__ import annotations


class RoostooError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(RoostooError):
    """Configuration is missing or inconsistent."""


class TransportError(RoostooError):
    """The request never produced a usable HTTP response (DNS, TLS, timeout).

    This is the dangerous class for order placement: the request may or may not
    have reached the matching engine, so callers must reconcile rather than
    blindly retry.
    """


class APIError(RoostooError):
    """The exchange answered HTTP 200 with ``Success: false``.

    Roostoo signals application-level failures inside a 200 response, so a
    naive client that only checks the status code will keep trading on errors.
    """

    def __init__(self, err_msg: str, endpoint: str, payload: object = None):
        super().__init__(f"{endpoint}: {err_msg}")
        self.err_msg = err_msg
        self.endpoint = endpoint
        self.payload = payload

    @property
    def is_retryable(self) -> bool:
        # Transient exchange-side conditions worth one more attempt.
        return any(
            token in self.err_msg.lower()
            for token in ("timeout", "busy", "try again", "rate limit", "too many")
        )


class RateLimitError(RoostooError):
    """Local self-throttle tripped; the caller asked faster than we allow."""


class RiskRejection(RoostooError):
    """An order was refused by the risk layer before it ever reached the wire."""

    def __init__(self, reason: str, order: object = None):
        super().__init__(reason)
        self.reason = reason
        self.order = order
