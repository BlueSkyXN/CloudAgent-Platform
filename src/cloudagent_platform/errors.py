from __future__ import annotations


class PayloadTooLargeError(ValueError):
    pass


class ConnectorRequestError(ValueError):
    """A redacted outbound connector failure suitable for worker handling."""

    def __init__(self, status_code: int | None, response_summary: str = "") -> None:
        detail = f"HTTP {status_code}" if status_code is not None else "a transport error"
        super().__init__(f"connector request failed with {detail}")
        self.status_code = status_code
        self.response_summary = response_summary
