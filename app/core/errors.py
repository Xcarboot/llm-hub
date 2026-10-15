class GatewayError(Exception):
    status_code = 500
    code = "gateway_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConflictError(GatewayError):
    status_code = 409
    code = "conflict"


class InsufficientBalanceError(GatewayError):
    status_code = 402
    code = "insufficient_balance"


class InfrastructureUnavailableError(GatewayError):
    status_code = 503
    code = "infrastructure_unavailable"


class UpstreamError(GatewayError):
    status_code = 502
    code = "upstream_error"


class UpstreamMeteringError(UpstreamError):
    code = "upstream_metering_overage"
