"""
IQE-Compatible Exception Classes.

These exceptions match the interface used by iqe-cost-management-api
to allow IQE tests to catch expected exceptions.
"""

from typing import Any, Dict, Optional


class ApiException(Exception):
    """
    IQE-compatible API exception.
    
    Matches the interface from iqe_cost_management_api.exceptions.ApiException
    """
    
    def __init__(
        self,
        status: int = 0,
        reason: str = "",
        http_resp: Optional[Any] = None,
        body: Optional[str] = None,
    ):
        self.status = status
        self.reason = reason
        self.http_resp = http_resp
        self.body = body
        
        message = f"({status}) Reason: {reason}"
        if body:
            message += f"\nBody: {body}"
        
        super().__init__(message)
    
    def __str__(self) -> str:
        return f"ApiException(status={self.status}, reason={self.reason})"
    
    def __repr__(self) -> str:
        return self.__str__()


class NotFoundException(ApiException):
    """Raised when a resource is not found (404)."""
    
    def __init__(self, reason: str = "Not Found", **kwargs):
        super().__init__(status=404, reason=reason, **kwargs)


class UnauthorizedException(ApiException):
    """Raised when authentication fails (401)."""
    
    def __init__(self, reason: str = "Unauthorized", **kwargs):
        super().__init__(status=401, reason=reason, **kwargs)


class ForbiddenException(ApiException):
    """Raised when access is forbidden (403)."""
    
    def __init__(self, reason: str = "Forbidden", **kwargs):
        super().__init__(status=403, reason=reason, **kwargs)


class BadRequestException(ApiException):
    """Raised for bad requests (400)."""
    
    def __init__(self, reason: str = "Bad Request", **kwargs):
        super().__init__(status=400, reason=reason, **kwargs)


class ServiceException(ApiException):
    """Raised for server errors (5xx)."""
    
    def __init__(self, status: int = 500, reason: str = "Internal Server Error", **kwargs):
        super().__init__(status=status, reason=reason, **kwargs)


__all__ = [
    "ApiException",
    "NotFoundException",
    "UnauthorizedException",
    "ForbiddenException",
    "BadRequestException",
    "ServiceException",
]
