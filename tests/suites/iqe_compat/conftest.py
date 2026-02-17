"""
IQE-Compatible Fixtures for cost-onprem Tests.

This module provides pytest fixtures that match the IQE interface,
allowing IQE tests to run against cost-onprem deployments.

Key fixtures:
- application: Main IQE-compatible application object
- tenant_create: Verifies tenant schema exists
- call_api: IQE-compatible API helper function
"""

import requests as requests_lib
import pytest

from tests.iqe_adapter import CostOnPremApplication, call_api as iqe_call_api
from tests.iqe_adapter.exceptions import ApiException
from conftest import obtain_jwt_token

# Import OCP fixtures from IQE fixture modules
# These provide data setup fixtures like cost_ocp_ros_source_0
try:
    from iqe_cost_management.fixtures.ocp_fixtures import *  # noqa: F401, F403
except ImportError as e:
    import logging
    logging.getLogger(__name__).warning(f"Could not import OCP fixtures: {e}")

# Re-export call_api for IQE test imports
call_api = iqe_call_api


@pytest.fixture(scope="session")
def iqe_jwt_token(keycloak_config):
    """
    Session-scoped JWT token for IQE tests.
    
    Note: This token may expire during long test runs (>5 min).
    For production use, consider implementing token refresh.
    """
    return obtain_jwt_token(keycloak_config)


@pytest.fixture(scope="session")
def iqe_authenticated_session(iqe_jwt_token, gateway_url: str):
    """
    Session-scoped authenticated session for IQE tests.
    
    Creates a requests.Session with JWT authentication that persists
    across all IQE tests in the session.
    """
    session = requests_lib.Session()
    session.headers.update({
        "Authorization": f"Bearer {iqe_jwt_token.access_token}",
        "Content-Type": "application/json",
    })
    session.verify = False
    return session


@pytest.fixture(scope="session")
def iqe_rh_identity_header(org_id) -> str:
    """
    Session-scoped X-Rh-Identity header for IQE tests.
    
    Creates a base64-encoded identity header from the org_id fixture.
    """
    import base64
    import json
    
    # Build identity structure matching what Koku expects
    identity = {
        "identity": {
            "org_id": org_id,
            "account_number": "10001",
            "type": "User",
            "user": {
                "username": "test",
                "email": "test@example.com",
                "is_org_admin": True,
            },
            "internal": {
                "org_id": org_id,
            },
        },
        "entitlements": {
            "cost_management": {"is_entitled": True},
        },
    }
    
    return base64.b64encode(json.dumps(identity).encode()).decode()


@pytest.fixture(scope="session")
def application(
    gateway_url: str,
    iqe_authenticated_session,
    iqe_rh_identity_header: str,
) -> CostOnPremApplication:
    """
    IQE-compatible application fixture.
    
    Provides the central 'application' object that IQE tests depend on.
    Maps our existing fixtures to the IQE interface.
    """
    return CostOnPremApplication(
        gateway_url=gateway_url,
        session=iqe_authenticated_session,
        rh_identity_header=iqe_rh_identity_header,
    )


@pytest.fixture(scope="session")
def tenant_create(application: CostOnPremApplication) -> str:
    """
    Verify tenant schema exists.
    
    In IQE, this creates the schema if needed by uploading initial data.
    For cost-onprem, the deployment handles schema creation, so we just
    verify the API is accessible.
    
    Returns:
        "schema exists" if successful
    """
    try:
        result, status_code, _ = application.cost_management.rest_client.client.call_api(
            "/reports/openshift/costs/", "GET"
        )
        if status_code == 200:
            return "schema exists"
        else:
            pytest.skip(f"Tenant schema not accessible (status {status_code})")
    except Exception as e:
        pytest.skip(f"Tenant schema check failed: {e}")


@pytest.fixture(scope="session")
def cost_ocp_tags_setup():
    """
    IQE fixture for tag setup.
    
    Not needed for cost-onprem tests as tags are set up during data upload.
    """
    return None


# API path constants (matching IQE's constants.py)
OPENSHIFT_COST_PATH = "/reports/openshift/costs/"
OPENSHIFT_COMPUTE_PATH = "/reports/openshift/compute/"
OPENSHIFT_MEMORY_PATH = "/reports/openshift/memory/"
OPENSHIFT_VOLUME_PATH = "/reports/openshift/volumes/"
OPENSHIFT_NETWORK_PATH = "/reports/openshift/network/"
OPENSHIFT_GPU_PATH = "/reports/openshift/gpu/"
OPENSHIFT_TAGS_PATH = "/tags/openshift/"
OPENSHIFT_FORECASTING_PATH = "/forecasts/openshift/costs/"
OPENSHIFT_VIRTUAL_MACHINES_PATH = "/reports/openshift/resources/virtual-machines/"
RECOMMENDATIONS = "/recommendations/openshift/"
COST_MODELS_PATH = "/cost-models/"
SOURCES_PATH = "/sources/"


# Time filter constants (matching IQE's api_params.py)
all_time_filters = [
    {"time_scope_value": "-1", "time_scope_units": "month"},
    {"time_scope_value": "-2", "time_scope_units": "month"},
    {"time_scope_value": "-10", "time_scope_units": "day"},
    {"time_scope_value": "-30", "time_scope_units": "day"},
]


# Try to import additional IQE helpers if available
try:
    from iqe_cost_management.fixtures.helpers import (
        report_line_items,
        calculate_total,
        tolerance_value,
        get_today_date,
        get_start_date_previous_month,
        get_start_date_current_month,
    )
    from iqe_cost_management.fixtures.constants import (
        OPENSHIFT_COST_PATH as IQE_OPENSHIFT_COST_PATH,
    )
    IQE_HELPERS_AVAILABLE = True
except ImportError:
    IQE_HELPERS_AVAILABLE = False
    
    # Provide fallback implementations
    import datetime
    
    def get_today_date():
        return datetime.date.today()
    
    def get_start_date_previous_month():
        today = datetime.date.today()
        first_of_month = today.replace(day=1)
        last_month = first_of_month - datetime.timedelta(days=1)
        return last_month.replace(day=1)
    
    def get_start_date_current_month():
        return datetime.date.today().replace(day=1)
    
    def report_line_items(report_data):
        """Extract line items from report response."""
        return report_data.get("data", [])
    
    def calculate_total(report_data, key="cost"):
        """Calculate total from report meta."""
        meta = report_data.get("meta", {})
        total = meta.get("total", {})
        return total.get(key, {}).get("total", {}).get("value", 0)
    
    def tolerance_value(value, tolerance=0.01):
        """Check if value is within tolerance."""
        return abs(value) <= tolerance
