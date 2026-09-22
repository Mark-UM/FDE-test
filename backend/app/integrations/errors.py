"""Safe error context; do not embed external payloads, free text or credentials."""


class ExternalError(Exception):
    def __init__(
        self,
        *,
        service: str,
        operation: str,
        request_id: str,
        status_code: int | None = None,
        source_code: str | None = None,
    ) -> None:
        self.service = service
        self.operation = operation
        self.request_id = request_id
        self.status_code = status_code
        self.source_code = source_code
        super().__init__(f"{type(self).__name__}: {service}.{operation} ({request_id})")


class ExternalNotFound(ExternalError):
    pass


class ExternalTimeout(ExternalError):
    pass


class ExternalUnavailable(ExternalError):
    pass


class ExternalInvalidResponse(ExternalError):
    pass


class ExternalConflict(ExternalError):
    pass


class ExternalRejected(ExternalError):
    """Other 4xx, including authentication or request rejection; never empty success."""
