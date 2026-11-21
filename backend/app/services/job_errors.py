from datetime import datetime, timedelta


class RevisionLockUnavailable(TimeoutError):
    """A revision operation is owned by another worker or waiting for backoff."""

    def __init__(
        self,
        message: str = "Another worker is processing this PR revision operation.",
        *,
        retry_at: datetime | None = None,
        lease_owner: str = "unknown",
        operation: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.retry_at = retry_at or (datetime.utcnow() + timedelta(seconds=30))
        self.lease_owner = lease_owner
        self.operation = operation
