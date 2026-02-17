"""
IQE-Compatible Adapter for cost-onprem-chart Tests.

This module provides an adapter layer that allows IQE (iqe-cost-management-plugin)
tests to run against cost-onprem deployments without modification.

The adapter translates IQE's `application` fixture interface to our existing
test infrastructure (gateway_url, authenticated_session, rh_identity_header).

Usage:
    from tests.iqe_adapter import CostOnPremApplication

    @pytest.fixture(scope="session")
    def application(gateway_url, authenticated_session, rh_identity_header):
        return CostOnPremApplication(
            gateway_url=gateway_url,
            session=authenticated_session,
            rh_identity_header=rh_identity_header
        )

IQE Interface Mapping:
    application.config.current_env -> "cost_onprem"
    application.user.identity -> decoded X-Rh-Identity
    application.cost_management.rest_client.client.call_api() -> requests calls
    application.cost_management.rest_client.cost_models_api -> CostModelsApiAdapter
    application.cost_management.rest_client.integrations_api -> IntegrationsApiAdapter
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

from .api_adapters import CostModelsApiAdapter, IntegrationsApiAdapter
from .exceptions import ApiException


@dataclass
class ConfigAdapter:
    """
    Provides application.config interface.
    
    IQE uses application.config.current_env to determine which environment
    is being tested (local, smoke, stage, prod, clowder_smoke, etc.).
    
    For cost-onprem, we always return "cost_onprem" to allow tests to
    branch on this value if needed.
    """
    current_env: str = "cost_onprem"
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by key. Returns default for most lookups."""
        config_map = {
            "current_env": self.current_env,
        }
        return config_map.get(key, default)
    
    def __getattr__(self, name: str) -> Any:
        """Allow attribute access for nested config (e.g., config.minio.url)."""
        # Return None for unknown attributes to avoid AttributeError
        return None


@dataclass
class IdentityAdapter:
    """Provides nested identity structure for application.user.identity."""
    org_id: str = ""
    account_number: str = ""
    type: str = "User"
    internal: Dict[str, Any] = field(default_factory=dict)
    user: Dict[str, Any] = field(default_factory=dict)
    
    def __getattr__(self, name: str) -> Any:
        """Allow attribute access for identity fields."""
        if name in self.__dict__:
            return self.__dict__[name]
        return None
    
    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like access for identity fields."""
        return getattr(self, key, default)


@dataclass
class AuthAdapter:
    """Provides application.user.auth interface."""
    username: str = ""
    password: str = ""
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get auth value by key."""
        return getattr(self, key, default)


@dataclass
class UserAdapter:
    """
    Provides application.user interface.
    
    IQE tests access user identity via:
        - application.user.identity.org_id
        - application.user.identity.account_number
        - application.user.auth.username
    """
    identity: IdentityAdapter = field(default_factory=IdentityAdapter)
    auth: AuthAdapter = field(default_factory=AuthAdapter)
    
    @classmethod
    def from_rh_identity(cls, rh_identity_header: str) -> "UserAdapter":
        """
        Create UserAdapter from X-Rh-Identity header.
        
        The header is base64-encoded JSON containing identity information.
        """
        try:
            decoded = json.loads(base64.b64decode(rh_identity_header))
            identity_data = decoded.get("identity", {})
            
            identity = IdentityAdapter(
                org_id=str(identity_data.get("org_id", "")),
                account_number=str(identity_data.get("account_number", "")),
                type=identity_data.get("type", "User"),
                internal=identity_data.get("internal", {}),
                user=identity_data.get("user", {}),
            )
            
            return cls(identity=identity)
        except Exception:
            return cls()
    
    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like access for user fields."""
        return getattr(self, key, default)


class ClientAdapter:
    """
    Translates IQE call_api() to requests calls.
    
    IQE uses:
        application.cost_management.rest_client.client.call_api(
            "/reports/openshift/costs/", "GET", response_type="object"
        )
    
    This adapter translates that to:
        session.get(f"{base_url}/reports/openshift/costs/")
    """
    
    def __init__(self, base_url: str, session: requests.Session):
        self.base_url = base_url.rstrip("/")
        self.session = session
    
    def call_api(
        self,
        path: str,
        method: str = "GET",
        response_type: str = "object",
        body: Any = None,
        query_params: Optional[Dict[str, Any]] = None,
        header_params: Optional[Dict[str, str]] = None,
        **kwargs
    ) -> Tuple[Any, int, Dict[str, str]]:
        """
        IQE-compatible API call.
        
        Args:
            path: API path (e.g., "/reports/openshift/costs/")
            method: HTTP method (GET, POST, PUT, DELETE)
            response_type: "object" for JSON, anything else for text
            body: Request body (will be JSON-encoded)
            query_params: Query string parameters
            header_params: Additional headers
            **kwargs: Passed to requests
        
        Returns:
            Tuple of (response_data, status_code, headers)
        """
        url = f"{self.base_url}{path}"
        
        # Build headers
        headers = {}
        if header_params:
            headers.update(header_params)
        
        # Make request
        response = self.session.request(
            method=method,
            url=url,
            json=body if body else None,
            params=query_params,
            headers=headers if headers else None,
            **kwargs
        )
        
        # Parse response
        if response_type == "object":
            try:
                data = response.json()
            except json.JSONDecodeError:
                data = response.text
        else:
            data = response.text
        
        return (data, response.status_code, dict(response.headers))


class RestClientAdapter:
    """
    Provides rest_client interface with sub-APIs.
    
    IQE accesses APIs via:
        - application.cost_management.rest_client.client.call_api()
        - application.cost_management.rest_client.cost_models_api.*
        - application.cost_management.rest_client.integrations_api.*
    """
    
    def __init__(self, base_url: str, session: requests.Session):
        self.client = ClientAdapter(base_url, session)
        self.cost_models_api = CostModelsApiAdapter(base_url, session)
        self.integrations_api = IntegrationsApiAdapter(base_url, session)


class CostManagementAdapter:
    """
    Provides application.cost_management interface.
    
    This is the main entry point for cost management API access in IQE.
    """
    
    def __init__(self, base_url: str, session: requests.Session):
        self.rest_client = RestClientAdapter(base_url, session)
        self.config: Dict[str, Any] = {}
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get config value."""
        return self.config.get(key, default)


class CostOnPremApplication:
    """
    IQE-compatible application fixture for cost-onprem.
    
    This is the main adapter class that provides the `application` fixture
    interface that IQE tests expect.
    
    Usage:
        @pytest.fixture(scope="session")
        def application(gateway_url, authenticated_session, rh_identity_header):
            return CostOnPremApplication(
                gateway_url=gateway_url,
                session=authenticated_session,
                rh_identity_header=rh_identity_header
            )
    
    Then IQE tests can use:
        def test_something(application, tenant_create):
            result = call_api("/reports/openshift/costs/", application)
            assert result["data"]
    """
    
    def __init__(
        self,
        gateway_url: str,
        session: requests.Session,
        rh_identity_header: str,
    ):
        """
        Initialize the IQE-compatible application.
        
        Args:
            gateway_url: Base URL for the gateway (e.g., "https://gateway.example.com/api")
            session: Authenticated requests.Session
            rh_identity_header: Base64-encoded X-Rh-Identity header
        """
        # Build API base URL
        api_base = f"{gateway_url.rstrip('/')}/cost-management/v1"
        
        # Initialize adapters
        self.config = ConfigAdapter()
        self.user = UserAdapter.from_rh_identity(rh_identity_header)
        self.cost_management = CostManagementAdapter(api_base, session)
        
        # Store raw values for debugging
        self._gateway_url = gateway_url
        self._rh_identity_header = rh_identity_header
    
    def __repr__(self) -> str:
        return (
            f"CostOnPremApplication("
            f"env={self.config.current_env}, "
            f"org_id={self.user.identity.org_id})"
        )


# Convenience function matching IQE's call_api pattern
def call_api(
    path: str,
    application: CostOnPremApplication,
    group_by: Optional[Dict[str, str]] = None,
    filter: Optional[Dict[str, Any]] = None,
    other_params: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    IQE-compatible call_api helper function.
    
    This matches the signature used in IQE tests:
        from iqe_cost_management.fixtures.helpers import call_api
        result = call_api(OPENSHIFT_COST_PATH, application, group_by={"project": "*"})
    
    Args:
        path: API path (e.g., "/reports/openshift/costs/")
        application: CostOnPremApplication instance
        group_by: Group-by parameters (e.g., {"project": "*"})
        filter: Filter parameters (e.g., {"time_scope_value": "-1"})
        other_params: Simple key-value params (e.g., {"start_date": "2026-01-01"})
        **kwargs: Additional dict parameters that get encoded as key[subkey]=value
    
    Returns:
        JSON response data
    """
    # Build query params
    query_params: Dict[str, Any] = {}
    
    # Handle group_by - encoded as group_by[key]=value
    if group_by:
        for key, value in group_by.items():
            query_params[f"group_by[{key}]"] = value
    
    # Handle filter - encoded as filter[key]=value
    if filter:
        for key, value in filter.items():
            query_params[f"filter[{key}]"] = value
    
    # Handle other_params - simple key=value pairs
    if other_params:
        for key, value in other_params.items():
            # Convert datetime objects to strings
            if hasattr(value, 'isoformat'):
                value = value.isoformat()
            query_params[key] = value
    
    # Handle additional kwargs - these are dicts that get encoded as key[subkey]=value
    for kwarg_name, kwarg_value in kwargs.items():
        if kwarg_value is not None:
            if isinstance(kwarg_value, dict):
                for key, value in kwarg_value.items():
                    query_params[f"{kwarg_name}[{key}]"] = value
            elif isinstance(kwarg_value, list):
                for item in kwarg_value:
                    if isinstance(item, dict):
                        for key, value in item.items():
                            query_params[f"{kwarg_name}[{key}]"] = value
            else:
                query_params[kwarg_name] = kwarg_value
    
    # Make the call
    result, status_code, headers = application.cost_management.rest_client.client.call_api(
        path,
        "GET",
        response_type="object",
        query_params=query_params if query_params else None,
    )
    
    # IQE's call_api raises on non-200
    if status_code >= 400:
        raise ApiException(status=status_code, reason=str(result))
    
    return result


# Export public API
__all__ = [
    "CostOnPremApplication",
    "ConfigAdapter",
    "UserAdapter",
    "ClientAdapter",
    "CostManagementAdapter",
    "RestClientAdapter",
    "call_api",
    "ApiException",
]
