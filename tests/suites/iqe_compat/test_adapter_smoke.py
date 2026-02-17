"""
Adapter Smoke Tests.

These tests verify that the IQE adapter layer is working correctly
before running actual IQE tests.
"""

import pytest

from tests.iqe_adapter import CostOnPremApplication, call_api
from tests.iqe_adapter.exceptions import ApiException


@pytest.mark.iqe_compat
@pytest.mark.smoke
class TestAdapterBasics:
    """Verify basic adapter functionality."""
    
    def test_application_fixture_exists(self, application: CostOnPremApplication):
        """Verify application fixture is available and has correct type."""
        assert application is not None
        assert isinstance(application, CostOnPremApplication)
    
    def test_application_config_env(self, application: CostOnPremApplication):
        """Verify application.config.current_env returns expected value."""
        assert application.config.current_env == "cost_onprem"
    
    def test_application_user_identity(self, application: CostOnPremApplication):
        """Verify application.user.identity is populated from X-Rh-Identity."""
        # org_id should be populated from the rh_identity_header
        assert application.user.identity is not None
        # The org_id should be a non-empty string (actual value depends on test setup)
        assert isinstance(application.user.identity.org_id, str)
    
    def test_tenant_create_fixture(self, tenant_create):
        """Verify tenant_create fixture passes (schema exists)."""
        assert tenant_create == "schema exists"


@pytest.mark.iqe_compat
@pytest.mark.smoke
class TestAdapterApiCalls:
    """Verify adapter can make API calls."""
    
    def test_call_api_status_endpoint(self, application: CostOnPremApplication):
        """Verify call_api can reach the status endpoint."""
        result, status_code, headers = application.cost_management.rest_client.client.call_api(
            "/status/", "GET"
        )
        assert status_code == 200
        # Status endpoint returns various health info
        assert isinstance(result, dict)
    
    def test_call_api_reports_endpoint(self, application: CostOnPremApplication, tenant_create):
        """Verify call_api can reach the reports endpoint."""
        result, status_code, headers = application.cost_management.rest_client.client.call_api(
            "/reports/openshift/costs/", "GET"
        )
        assert status_code == 200
        # Reports endpoint returns data structure with meta, links, data
        assert "meta" in result or "data" in result
    
    def test_call_api_helper_function(self, application: CostOnPremApplication, tenant_create):
        """Verify the call_api helper function works."""
        from tests.suites.iqe_compat.conftest import OPENSHIFT_COST_PATH
        
        result = call_api(OPENSHIFT_COST_PATH, application)
        assert isinstance(result, dict)
        # Should have standard response structure
        assert "meta" in result or "data" in result


@pytest.mark.iqe_compat
@pytest.mark.smoke
class TestAdapterCostModelsApi:
    """Verify cost_models_api adapter works."""
    
    def test_list_cost_models(self, application: CostOnPremApplication):
        """Verify cost_models_api.list_cost_models() works."""
        result = application.cost_management.rest_client.cost_models_api.list_cost_models()
        assert isinstance(result, dict)
        assert "data" in result
        assert isinstance(result["data"], list)
    
    def test_list_cost_models_with_params(self, application: CostOnPremApplication):
        """Verify cost_models_api.list_cost_models() accepts parameters."""
        result = application.cost_management.rest_client.cost_models_api.list_cost_models(
            limit=5,
            source_type="OCP",
        )
        assert isinstance(result, dict)
        assert "data" in result


@pytest.mark.iqe_compat
@pytest.mark.smoke
class TestAdapterIntegrationsApi:
    """Verify integrations_api adapter works."""
    
    def test_list_sources(self, application: CostOnPremApplication):
        """Verify integrations_api.list_sources() works."""
        result = application.cost_management.rest_client.integrations_api.list_sources()
        assert isinstance(result, dict)
        assert "data" in result
        assert isinstance(result["data"], list)
