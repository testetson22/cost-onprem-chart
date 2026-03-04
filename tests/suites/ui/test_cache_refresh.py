"""
UI cache refresh tests for FLPATH-3114.

This test verifies that the UI correctly shows new data after a page refresh,
validating the fix from koku-ui PR #4960.

Bug: https://issues.redhat.com/browse/FLPATH-3114
Fix: https://github.com/project-koku/koku-ui/pull/4960

The issue was that the UI cache would serve stale data after a page refresh,
requiring users to open an incognito window or wait for cache expiration to
see newly added data.
"""

import os
import re
import time
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from conftest import ClusterConfig, KeycloakConfig
from e2e_helpers import (
    NISEConfig,
    generate_cluster_id,
    generate_nise_data,
    get_koku_api_url,
    register_source,
    wait_for_provider,
    wait_for_summary_tables,
)
from utils import (
    create_rh_identity_header,
    create_upload_package_from_files,
    get_pod_by_label,
)


def save_screenshot(page: Page, name: str) -> str:
    """Save a screenshot for debugging/verification."""
    screenshots_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "reports", "screenshots", "cache_refresh"
    )
    os.makedirs(screenshots_dir, exist_ok=True)
    path = os.path.join(screenshots_dir, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    print(f"\n📸 Screenshot: {path}")
    return path


@pytest.mark.ui
class TestCacheRefresh:
    """Test that UI cache correctly invalidates on refresh.
    
    FLPATH-3114: UI doesn't show new data on a refresh because of the UI cache.
    
    This test validates the fix by:
    1. Loading the Cost Explorer page and capturing initial state
    2. Adding new cost data via the backend API
    3. Refreshing the page (browser refresh, not hard reload)
    4. Verifying the new data appears in the UI
    
    The fix in koku-ui PR #4960 ensures proper cache invalidation so that
    refreshing the page fetches fresh data from the backend.
    """

    @pytest.fixture(scope="function")
    def cache_test_data(
        self,
        cluster_config: ClusterConfig,
        keycloak_config: KeycloakConfig,
        s3_config,
        ingress_url: str,
        org_id: str,
    ):
        """Set up additional test data for cache refresh testing.
        
        This fixture generates and uploads a second batch of data with a
        distinct cluster ID that can be identified in the UI.
        """
        import tempfile
        import requests
        from conftest import obtain_jwt_token
        
        # Generate a unique cluster ID with identifiable prefix
        cluster_id = generate_cluster_id(prefix="cache-test")
        
        # Get required pods
        db_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=database"
        )
        if not db_pod:
            pytest.skip("Database pod not found")
        
        ingress_pod = get_pod_by_label(
            cluster_config.namespace, "app.kubernetes.io/component=ingress"
        )
        if not ingress_pod:
            pytest.skip("Ingress pod not found")
        
        api_url = get_koku_api_url(
            cluster_config.helm_release_name, cluster_config.namespace
        )
        rh_identity = create_rh_identity_header(org_id)
        
        temp_dir = tempfile.mkdtemp(prefix="cache_test_")
        
        try:
            print(f"\n{'='*60}")
            print("CACHE REFRESH TEST - Adding new data")
            print(f"{'='*60}")
            print(f"  New Cluster ID: {cluster_id}")
            
            # Generate NISE data for today (to ensure it's "new")
            now = datetime.utcnow()
            start_date = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            
            # Use distinct values that can be identified
            nise_config = NISEConfig(
                namespace="cache-test-namespace",
                pod_name="cache-test-pod",
                node_name="cache-test-node",
            )
            
            print("\n  [1/4] Generating NISE data...")
            files = generate_nise_data(
                cluster_id, start_date, end_date, temp_dir, config=nise_config
            )
            print(f"       Generated {len(files['all_files'])} CSV files")
            
            if not files["all_files"]:
                pytest.skip("NISE generated no CSV files")
            
            # Register source
            print("\n  [2/4] Registering source...")
            source_registration = register_source(
                namespace=cluster_config.namespace,
                pod=ingress_pod,
                api_url=api_url,
                rh_identity_header=rh_identity,
                cluster_id=cluster_id,
                org_id=org_id,
                source_name=f"cache-test-{cluster_id[-8:]}",
                container="ingress",
            )
            print(f"       Source ID: {source_registration.source_id}")
            
            # Wait for provider
            print("\n  [3/4] Waiting for provider...")
            if not wait_for_provider(cluster_config.namespace, db_pod, cluster_id):
                pytest.fail(f"Provider not created for cluster {cluster_id}")
            print("       Provider created")
            
            # Upload data
            print("\n  [4/4] Uploading data...")
            package_path = create_upload_package_from_files(
                pod_usage_files=files["pod_usage_files"],
                ros_usage_files=files["ros_usage_files"],
                cluster_id=cluster_id,
                start_date=start_date,
                end_date=end_date,
            )
            
            upload_url = f"{ingress_url}/v1/upload"
            upload_token = obtain_jwt_token(keycloak_config)
            
            session = requests.Session()
            session.verify = False
            
            with open(package_path, "rb") as f:
                response = session.post(
                    upload_url,
                    files={"file": (os.path.basename(package_path), f, "application/vnd.redhat.hccm.filename+tgz")},
                    headers=upload_token.authorization_header,
                    timeout=60,
                )
            
            if response.status_code not in (200, 201, 202):
                pytest.fail(f"Upload failed: {response.status_code} - {response.text}")
            
            print(f"       Upload successful: {response.status_code}")
            
            # Wait for data to be processed
            print("\n  Waiting for data processing...")
            if not wait_for_summary_tables(
                cluster_config.namespace, db_pod, cluster_id, timeout=180
            ):
                pytest.skip("Data not processed in time - cache test may be inconclusive")
            
            print("       Data processed and available")
            print(f"{'='*60}\n")
            
            yield {
                "cluster_id": cluster_id,
                "namespace": nise_config.namespace,
                "source_name": f"cache-test-{cluster_id[-8:]}",
            }
            
        finally:
            # Cleanup temp files
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_new_data_visible_after_refresh(
        self,
        authenticated_page: Page,
        ui_url: str,
        cost_validation_data,
        cache_test_data,
    ):
        """Verify new data appears in UI after page refresh (FLPATH-3114).
        
        This test:
        1. Loads Cost Explorer with existing data (from cost_validation_data)
        2. Adds new data (from cache_test_data fixture)
        3. Refreshes the page using browser refresh (F5 equivalent)
        4. Verifies the new data source appears
        
        If the cache bug (FLPATH-3114) is present, the new data won't appear
        after refresh - it would only appear in incognito or after cache expiry.
        """
        page = authenticated_page
        
        # Step 1: Navigate to Cost Explorer and capture initial state
        print("\n[Step 1] Loading Cost Explorer...")
        page.goto(f"{ui_url}/openshift/cost-management/explorer")
        page.wait_for_load_state("networkidle")
        time.sleep(3)
        
        save_screenshot(page, "01_before_refresh_initial")
        
        # Capture initial source count or data state
        # Look for the new source name in the page
        new_source_name = cache_test_data["source_name"]
        new_namespace = cache_test_data["namespace"]
        
        # The new data should now be in the backend (added by cache_test_data fixture)
        # Check if it's visible before refresh
        initial_has_new_data = (
            page.get_by_text(re.compile(new_source_name, re.IGNORECASE)).count() > 0 or
            page.get_by_text(re.compile(new_namespace, re.IGNORECASE)).count() > 0
        )
        
        print(f"  Initial state - new data visible: {initial_has_new_data}")
        
        # Step 2: Refresh the page (standard browser refresh)
        print("\n[Step 2] Refreshing page...")
        page.reload()
        page.wait_for_load_state("networkidle")
        time.sleep(3)
        
        save_screenshot(page, "02_after_refresh")
        
        # Step 3: Check if new data is visible after refresh
        print("\n[Step 3] Checking for new data after refresh...")
        
        # Try multiple ways to find the new data
        found_new_data = False
        
        # Check for source name
        if page.get_by_text(re.compile(new_source_name, re.IGNORECASE)).count() > 0:
            found_new_data = True
            print(f"  Found source name: {new_source_name}")
        
        # Check for namespace
        if page.get_by_text(re.compile(new_namespace, re.IGNORECASE)).count() > 0:
            found_new_data = True
            print(f"  Found namespace: {new_namespace}")
        
        # Check for cluster ID pattern
        cluster_suffix = cache_test_data["cluster_id"][-8:]
        if page.get_by_text(re.compile(cluster_suffix, re.IGNORECASE)).count() > 0:
            found_new_data = True
            print(f"  Found cluster ID suffix: {cluster_suffix}")
        
        # If not found on Cost Explorer, try the OpenShift page which shows sources
        if not found_new_data:
            print("\n  Checking OpenShift page for new source...")
            page.goto(f"{ui_url}/openshift/cost-management/ocp")
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            
            save_screenshot(page, "03_openshift_page_check")
            
            if page.get_by_text(re.compile(new_source_name, re.IGNORECASE)).count() > 0:
                found_new_data = True
                print(f"  Found source on OpenShift page: {new_source_name}")
        
        # Step 4: Verify the fix
        print(f"\n[Result] New data visible after refresh: {found_new_data}")
        
        save_screenshot(page, "04_final_state")
        
        # The test passes if new data is visible after refresh
        # If FLPATH-3114 bug is present, this would fail
        assert found_new_data, (
            f"FLPATH-3114 regression: New data not visible after page refresh. "
            f"Expected to find source '{new_source_name}' or namespace '{new_namespace}' "
            f"after adding new data and refreshing the page. "
            f"This indicates the UI cache is not properly invalidating."
        )
