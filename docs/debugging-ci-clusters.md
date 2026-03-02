# Debugging OpenShift CI Clusters

This guide covers how to access, debug, and hold open ephemeral OpenShift CI clusters
used for E2E testing.

## Overview

OpenShift CI provisions ephemeral clusters for E2E tests. These clusters are
automatically deleted when the CI job terminates. This document explains how to:

1. Access a running CI cluster for live debugging
2. Hold a cluster open for extended debugging
3. Download artifacts from completed jobs
4. Debug common CI failures

## Quick Reference

| Task | Command/Action |
|------|----------------|
| Download artifacts | `./scripts/download-ci-artifacts.sh --url "<PROW_URL>"` |
| Login to running cluster | `./scripts/ocp-ci-cluster-login.sh "<PROW_URL>"` (requires pool access) |
| Hold cluster open | Add `sleep` to test script (see below) |
| View logs online | Click "Details" on GitHub CI check |

## Important: Cluster Pool Access

The `cost-onprem-chart` CI uses **shared OpenShift CI cluster pools** (`ci-cluster-pool`),
which have restricted access. Unlike project-specific pools (e.g., RHDH's `rhdh-pool-admins`),
you cannot directly retrieve credentials from shared pools without special permissions.

**Recommended approach:** Hold the cluster open and print credentials in the CI job itself
(see "Holding Clusters Open" below).

## Accessing a Running CI Cluster

### Prerequisites

- `oc` CLI installed
- Access to the cluster pool admin group (if using cluster claims)

### Method 1: Using the Login Script

```bash
./scripts/ocp-ci-cluster-login.sh
# Enter the Prow URL when prompted
```

Or with the URL directly:

```bash
./scripts/ocp-ci-cluster-login.sh "https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/50/pull-ci-insights-onprem-cost-onprem-chart-main-e2e/2014360404288868352"
```

### Method 2: From CI Artifacts (If Available)

CI jobs using cluster claims store the kubeconfig in artifacts. However, for shared
pools this may require the cluster to still be running:

```bash
# Download artifacts
./scripts/download-ci-artifacts.sh --url "<PROW_URL>"

# Look for kubeconfig in artifacts
find ./ci-artifacts-* -name "kubeconfig*" -o -name "*.kubeconfig"

# If found, use the kubeconfig
export KUBECONFIG=./ci-artifacts-pr50-xxx/artifacts/e2e/.../kubeconfig
oc get pods -n cost-onprem
```

**Note:** The kubeconfig only works while the cluster is running. Once the CI job
completes, the cluster is deleted and the kubeconfig becomes invalid.

### Method 3: Save Credentials in CI Job

Modify the CI job to explicitly save credentials to artifacts. Add to your test script:

```bash
# Save cluster access info to artifacts
mkdir -p "${ARTIFACT_DIR:-/tmp}/cluster-access"
oc whoami --show-server > "${ARTIFACT_DIR:-/tmp}/cluster-access/api-server.txt"
oc get route -n openshift-console console -o jsonpath='{.spec.host}' > "${ARTIFACT_DIR:-/tmp}/cluster-access/console.txt" 2>/dev/null || true
```

Then download artifacts to get the cluster info (useful if you're holding the cluster open).

## Holding Clusters Open for Debugging

**IMPORTANT:** Ephemeral clusters are deleted as soon as the CI job terminates.
To keep a cluster alive for debugging, you must prevent the job from completing.

### Option 1: Add Sleep and Print Credentials (Recommended)

Since shared cluster pools have restricted access, the best approach is to have the
CI job itself print the cluster credentials before sleeping.

Add this to `scripts/deploy-test-cost-onprem.sh` before the exit:

```bash
# At the end of the script, before exit:
echo "=== HOLDING CLUSTER OPEN FOR DEBUGGING ==="
echo "Cluster will be available for 1 hour"
echo ""
echo "=== CLUSTER ACCESS INFORMATION ==="
echo "API Server: $(oc whoami --show-server)"
echo "Console: $(oc get route -n openshift-console console -o jsonpath='{.spec.host}' 2>/dev/null || echo 'N/A')"
echo "Current User: $(oc whoami)"
echo ""
echo "To login from your machine:"
echo "  oc login $(oc whoami --show-server) --insecure-skip-tls-verify=true"
echo ""
echo "Note: You'll need the kubeconfig from CI artifacts or cluster pool access"
echo "==================================="
echo ""
echo "Press Ctrl+C in CI to release early"
sleep 3600  # 1 hour
exit "${OVERALL_RESULT}"
```

### Option 2: Conditional Debug Hold

For PR testing, make it conditional so it only holds when needed:

```bash
# Only hold open if DEBUG_HOLD_CLUSTER is set
if [[ "${DEBUG_HOLD_CLUSTER:-}" == "true" ]]; then
    echo "=== HOLDING CLUSTER OPEN FOR DEBUGGING ==="
    echo "API Server: $(oc whoami --show-server)"
    echo "Cluster will be available for ${DEBUG_HOLD_TIME:-3600} seconds"
    sleep "${DEBUG_HOLD_TIME:-3600}"
fi
```

Then trigger with a commit message or PR label that sets the variable.

### Option 2: Add Sleep to CI Step

If you have access to modify the CI configuration in `openshift/release`, you can
add a sleep step:

```yaml
# In the CI step configuration
steps:
  test:
    - ref: insights-onprem-cost-onprem-chart-e2e
    - as: hold-cluster
      commands: |
        echo "Holding cluster open for debugging..."
        sleep 3600
      from: src
```

### Option 3: Interactive Debug Step

For more control, add an interactive hold that waits for a signal:

```bash
# Add to test script
if [[ "${DEBUG_HOLD_CLUSTER:-}" == "true" ]]; then
    echo "=== CLUSTER HELD FOR DEBUGGING ==="
    echo "Cluster: $(oc whoami --show-server)"
    echo "Namespace: $(oc project -q)"
    echo ""
    echo "To release the cluster, create this file in the CI pod:"
    echo "  touch /tmp/release-cluster"
    echo ""
    while [[ ! -f /tmp/release-cluster ]]; do
        sleep 30
        echo "Still waiting... ($(date))"
    done
    echo "Release signal received, continuing..."
fi
```

## Downloading CI Artifacts

For completed jobs, download artifacts to debug locally:

```bash
# From Prow URL (copy from GitHub "Details" link)
./scripts/download-ci-artifacts.sh --url "https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/50/pull-ci-insights-onprem-cost-onprem-chart-main-e2e/2014360404288868352"

# From PR number and build ID
./scripts/download-ci-artifacts.sh 50 2014360404288868352
```

### Key Artifacts

| Path | Description |
|------|-------------|
| `build-log.txt` | Main CI operator log |
| `artifacts/e2e/insights-onprem-cost-onprem-chart-e2e/build-log.txt` | **Pytest output** |
| `artifacts/junit_operator.xml` | JUnit test results |
| `finished.json` | Job completion status |

### View Logs Online

```
# Prow Dashboard (with JUnit viewer)
https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/<PR>/pull-ci-insights-onprem-cost-onprem-chart-main-e2e/<BUILD_ID>

# Raw build log
https://storage.googleapis.com/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/<PR>/pull-ci-insights-onprem-cost-onprem-chart-main-e2e/<BUILD_ID>/build-log.txt

# Browse all artifacts
https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/<PR>/pull-ci-insights-onprem-cost-onprem-chart-main-e2e/<BUILD_ID>/
```

## Common Debugging Scenarios

### Test Failures

1. Download artifacts: `./scripts/download-ci-artifacts.sh --url "<URL>"`
2. Check pytest output: `cat ci-artifacts-*/artifacts/e2e/*/build-log.txt`
3. Look for stack traces and assertion errors
4. Check JUnit XML for test timing and failure details

### Deployment Failures

1. Check the main build log for Helm errors
2. Look for pod startup failures in the pytest output
3. If cluster is still running, login and check:
   ```bash
   oc get pods -n cost-onprem
   oc describe pod <failing-pod> -n cost-onprem
   oc logs <failing-pod> -n cost-onprem
   ```

### Timeout Issues

1. Check if tests are hanging on data processing
2. Look for "waiting for" messages in pytest output
3. Consider increasing timeouts or checking MASU processing

### Network/Connectivity Issues

1. Check if services are reachable within the cluster
2. Verify routes are created correctly
3. Check NetworkPolicy configurations

## CI Job Structure

The E2E job (`pull-ci-insights-onprem-cost-onprem-chart-main-e2e`) runs these steps:

1. **ipi-install-rbac** - RBAC setup (~10s)
2. **insights-onprem-s4-deploy** - Deploy S4 for S3 storage (~40s)
3. **insights-onprem-cost-onprem-chart-e2e** - Deploy chart and run pytest (~18m)

The main test step:
- Deploys the Helm chart with S4 integration
- Runs pytest with JUnit output
- Collects artifacts on completion

## Cluster Claim Pools (For Reference)

If your CI uses cluster claims (not currently used by cost-onprem-chart), clusters
are provisioned from pools managed by the `hosted-mgmt` cluster. Pool admins can
access these clusters via the login script.

Common pool patterns:
- `rhdh-4-17-us-east-2` - RHDH pools
- `ocp-4-17-aws-us-east-2` - Generic OCP pools

## Troubleshooting

### "Cluster claim not found"

The job may not use cluster claims. For cost-onprem-chart, clusters are provisioned
inline. Use artifact download instead.

### "Namespace expired or deleted"

The CI job has completed and the cluster was cleaned up. You can only access
clusters while the job is running.

### "Forbidden" accessing namespace

You need to be added to the cluster pool admin group. Contact the QE team.

### Can't find KUBECONFIG in artifacts

The KUBECONFIG may be in a different location or format. Check:
```bash
find ./ci-artifacts-* -type f -name "*kube*"
grep -r "server:" ./ci-artifacts-*/
```

## Related Documentation

- [OpenShift CI Cluster Claims](https://docs.ci.openshift.org/docs/how-tos/cluster-claim/)
- [CI Operator Documentation](https://docs.ci.openshift.org/docs/architecture/ci-operator/)
- [Test Artifacts](https://docs.ci.openshift.org/docs/architecture/step-registry/#sharing-data-between-steps)
