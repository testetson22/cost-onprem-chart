"""
IQE Sources API Exceptions Stub.
"""


class ApiException(Exception):
    """Stub for IQE Sources API Exception."""
    
    def __init__(self, status=None, reason=None, http_resp=None):
        self.status = status
        self.reason = reason
        self.http_resp = http_resp
        super().__init__(f"ApiException(status={status}, reason={reason})")
