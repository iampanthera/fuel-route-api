"""Typed API errors. Every error response has the shape
{"error": {"code": ..., "message": ..., "details": {...}}}."""


class ApiError(Exception):
    status = 400
    code = "INVALID_REQUEST"

    def __init__(self, message: str, details: dict | None = None, headers: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.headers = headers or {}

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class InvalidRequest(ApiError):
    status, code = 400, "INVALID_REQUEST"


class LocationAmbiguous(ApiError):
    status, code = 400, "LOCATION_AMBIGUOUS"


class LocationNotFound(ApiError):
    status, code = 422, "LOCATION_NOT_FOUND"


class OutsideServiceArea(ApiError):
    status, code = 422, "LOCATION_OUTSIDE_SERVICE_AREA"


class InfeasibleRoute(ApiError):
    status, code = 422, "INFEASIBLE_ROUTE"


# --- routing provider errors ------------------------------------------------ #
class RoutingError(ApiError):
    """Base for provider failures. `retryable` means another provider may succeed."""

    retryable = True


class NoRoute(RoutingError):
    status, code, retryable = 422, "NO_ROUTE", False


class RouteTooLong(RoutingError):
    status, code, retryable = 422, "ROUTE_TOO_LONG", True


class RoutingTimeout(RoutingError):
    status, code = 504, "ROUTING_TIMEOUT"


class RoutingRateLimited(RoutingError):
    status, code = 503, "ROUTING_RATE_LIMITED"


class RoutingProviderError(RoutingError):
    status, code = 502, "ROUTING_PROVIDER_ERROR"
