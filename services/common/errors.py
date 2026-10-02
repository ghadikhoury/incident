class ServiceError(Exception):
    """An expected failure that should become an HTTP error response.

    error_type is a short machine-readable category (e.g. "DependencyTimeout"),
    which ends up in logs and lets Incident group failures later.
    """

    def __init__(self, status_code: int, error_type: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
