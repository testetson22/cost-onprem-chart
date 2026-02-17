# IQE-Compatible Test Suite

This directory contains tests adapted from [iqe-cost-management-plugin](https://github.com/RedHatInsights/iqe-cost-management-plugin) that run against cost-onprem deployments using an adapter layer.

## Overview

The adapter layer (`tests/iqe_adapter/`) translates IQE's `application` fixture interface to our existing test infrastructure, allowing IQE tests to run with minimal modification.

## Running Tests

```bash
# Run all IQE-compatible tests
NAMESPACE=cost-onprem ./scripts/run-pytest.sh tests/suites/iqe_compat/ -v

# Run with IQE markers
NAMESPACE=cost-onprem ./scripts/run-pytest.sh tests/suites/iqe_compat/ -m "cost_ocp_on_prem" -v

# Run smoke tests only
NAMESPACE=cost-onprem ./scripts/run-pytest.sh tests/suites/iqe_compat/ -m "cost_ocp_on_prem and smoke" -v
```

## Architecture

```
tests/
├── iqe_adapter/                    # Adapter layer
│   ├── __init__.py                 # CostOnPremApplication, call_api
│   ├── api_adapters.py             # CostModelsApiAdapter, IntegrationsApiAdapter
│   └── exceptions.py               # IQE-compatible exceptions
│
└── suites/iqe_compat/              # IQE-compatible tests
    ├── conftest.py                 # IQE fixtures (application, tenant_create)
    ├── test_adapter_smoke.py       # Adapter verification tests
    └── test_*.py                   # Adapted IQE tests
```

## IQE Interface Mapping

| IQE Interface | Adapter Implementation |
|---------------|------------------------|
| `application.config.current_env` | Returns `"cost_onprem"` |
| `application.user.identity.org_id` | Decoded from `X-Rh-Identity` header |
| `application.cost_management.rest_client.client.call_api()` | `requests.Session` calls |
| `application.cost_management.rest_client.cost_models_api.*` | REST calls to `/cost-models/` |
| `application.cost_management.rest_client.integrations_api.*` | REST calls to `/sources/` |
| `call_api(path, application, ...)` | Helper function wrapping `call_api()` |

## Test Status

| File | Tests | Passing | Skipped | Notes |
|------|-------|---------|---------|-------|
| test_adapter_smoke.py | 5 | TBD | 0 | Adapter verification |

## Adding IQE Tests

### Option 1: Copy and Adapt

```bash
# Copy test file from IQE
cp $IQE_PLUGIN_PATH/tests/rest_api/v1/test__ocp_cost_reports.py \
   tests/suites/iqe_compat/

# Modify imports at top of file:
# FROM: from iqe_cost_management.fixtures.helpers import call_api
# TO:   from tests.suites.iqe_compat.conftest import call_api
```

### Option 2: Symlink (if IQE in PYTHONPATH)

```bash
cd tests/suites/iqe_compat
ln -s $IQE_PLUGIN_PATH/tests/rest_api/v1/test__ocp_cost_reports.py .
```

## Limitations

The adapter does not support:

- **Kafka message verification** - On-prem doesn't use Kafka for ROS
- **Vault credential lookups** - Not applicable to on-prem
- **MASU internal endpoints** - Would need PodAdapter (not yet implemented)
- **Stage/prod-specific tests** - Skipped based on `current_env`

## Troubleshooting

### "Tenant schema not accessible"

The `tenant_create` fixture verifies the API is accessible. If it fails:
1. Check that the deployment is running: `kubectl get pods -n cost-onprem`
2. Verify gateway is accessible: `curl $GATEWAY_URL/cost-management/v1/status/`

### Import errors

If IQE helpers aren't available, fallback implementations are used. To use actual IQE helpers:

```bash
pip install iqe-cost-management-plugin
```

### Test failures due to missing data

Many IQE tests expect data to exist. Ensure E2E tests have run first to populate data:

```bash
NAMESPACE=cost-onprem ./scripts/run-pytest.sh tests/suites/e2e/ -v
```
