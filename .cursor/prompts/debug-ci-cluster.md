# Debug OpenShift CI Cluster

Access or debug an OpenShift CI ephemeral cluster from a running or completed job.

## Important: Cluster Pool Access

The `cost-onprem-chart` CI uses **shared OpenShift CI cluster pools** which have
restricted access. You cannot directly retrieve credentials without pool admin access.

**Recommended:** Hold the cluster open and print credentials in the CI job itself.

## What do you need?

### For a COMPLETED job (download artifacts):

```bash
# Download all artifacts
./scripts/download-ci-artifacts.sh --url "<PROW_URL>"

# View pytest output
cat ci-artifacts-*/artifacts/e2e/*/build-log.txt
```

### For a RUNNING job (if you have pool access):

```bash
# Try to login (requires ci-cluster-pool access)
./scripts/ocp-ci-cluster-login.sh "<PROW_URL>"
```

**Note:** The cluster is deleted when the CI job terminates.

## Holding Clusters Open for Debugging

Add this to `scripts/deploy-test-cost-onprem.sh` before the exit to hold the cluster
open AND print access information:

```bash
echo "=== HOLDING CLUSTER OPEN FOR DEBUGGING ==="
echo "API Server: $(oc whoami --show-server)"
echo "Console: https://$(oc get route -n openshift-console console -o jsonpath='{.spec.host}')"
echo ""
echo "Cluster will be available for 1 hour"
sleep 3600
exit "${OVERALL_RESULT}"
```

Or make it conditional:

```bash
if [[ "${DEBUG_HOLD_CLUSTER:-}" == "true" ]]; then
    echo "=== HOLDING CLUSTER OPEN ==="
    echo "API Server: $(oc whoami --show-server)"
    sleep "${DEBUG_HOLD_TIME:-3600}"
fi
```

## Quick Links

| Resource | URL Pattern |
|----------|-------------|
| Prow Dashboard | `https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/<PR>/<JOB>/<BUILD_ID>` |
| Raw Build Log | `https://storage.googleapis.com/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/<PR>/<JOB>/<BUILD_ID>/build-log.txt` |
| Artifact Browser | `https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/<PR>/<JOB>/<BUILD_ID>/` |

## Full Documentation

See: `docs/debugging-ci-clusters.md`
