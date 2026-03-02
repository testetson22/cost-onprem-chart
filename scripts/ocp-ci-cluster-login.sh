#!/bin/bash

# Login to an OpenShift CI ephemeral cluster for debugging
#
# This script helps you connect to the ephemeral cluster used by a CI job
# so you can debug test failures interactively.
#
# Usage:
#   ./scripts/ocp-ci-cluster-login.sh [PROW_URL]
#
# Prerequisites:
#   - oc CLI installed
#   - Member of the cluster pool admin group (for cluster claim access)
#
# Note: The ephemeral cluster is deleted when the CI job terminates.
# To keep the cluster alive longer, see the "Holding Clusters Open" section below.

set -uo pipefail
# Note: Not using -e (errexit) to allow proper error handling

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

show_help() {
    cat << 'HELP'
Login to OpenShift CI Ephemeral Cluster

This script retrieves credentials from a running CI job and logs you into
the ephemeral cluster for interactive debugging.

Usage:
  ./scripts/ocp-ci-cluster-login.sh [PROW_URL]

Arguments:
  PROW_URL    Optional. Prow job URL. If not provided, you'll be prompted.

Examples:
  # Interactive mode
  ./scripts/ocp-ci-cluster-login.sh

  # With URL
  ./scripts/ocp-ci-cluster-login.sh "https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/insights-onprem_cost-onprem-chart/50/pull-ci-insights-onprem-cost-onprem-chart-main-e2e/2014360404288868352"

Prerequisites:
  - oc CLI installed
  - Access to hosted-mgmt cluster (cluster pool admin)

IMPORTANT: The cluster is deleted when the CI job terminates!
See docs/debugging-ci-clusters.md for how to hold clusters open.
HELP
    exit 0
}

# Check if prow log URL is provided as parameter, otherwise prompt for it
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    show_help
fi

if [[ $# -eq 0 ]]; then
    read -rp "Enter the Prow job URL: " input_url
else
    input_url="$1"
fi

# Extract job ID and name from URL
id=$(echo "$input_url" | awk -F'/' '{print $NF}')
job=$(echo "$input_url" | awk -F'/' '{print $(NF-1)}')

build_log_url="https://prow.ci.openshift.org/log?container=test&id=${id}&job=${job}"

log_info "Fetching cluster info from build log..."
log_info "Build log URL: $build_log_url"

# Fetch the build log
build_log=$(curl -s "$build_log_url")

# Try to find the claimed cluster name from the build log
# Format: "The claimed cluster ci-ocp-4-20-amd64-aws-us-east-1-ggwgj is ready"
cluster_name=$(echo "$build_log" | grep -oE "claimed cluster [^ ]+ is ready" | sed -E 's/claimed cluster ([^ ]+) is ready/\1/' | head -1)

if [[ -z "$cluster_name" ]]; then
    # Try alternate format: look for cluster claim ID
    claim_id=$(echo "$build_log" | grep -oE "cluster claim [^/]+/[^ ]+" | head -1 | awk -F'/' '{print $2}')
    if [[ -n "$claim_id" ]]; then
        log_warning "Found cluster claim ID: $claim_id"
        log_warning "But cluster may not be ready yet or job is still running."
    fi
    
    log_error "Could not find claimed cluster name in build log."
    log_error "The job may not use cluster claims, may still be provisioning,"
    log_error "or may have already completed."
    log_error ""
    log_error "For cost-onprem-chart CI jobs that don't use cluster claims,"
    log_error "use the KUBECONFIG from the CI artifacts instead:"
    log_error ""
    log_error "  ./scripts/download-ci-artifacts.sh --url \"$input_url\""
    log_error ""
    exit 1
fi

log_info "Claimed cluster: $cluster_name"

# Extract the pool namespace from the build log
# Format: "Claiming cluster from pool ci-cluster-pool/ci-ocp-4-20-amd64-aws-us-east-1"
pool_namespace=$(echo "$build_log" | grep -oE "from pool [^/]+/" | sed 's/from pool //' | tr -d '/' | head -1)
if [[ -z "$pool_namespace" ]]; then
    pool_namespace="ci-cluster-pool"  # Default for openshift-ci pools
fi

log_info "Pool namespace: $pool_namespace"

# Login to hosted-mgmt cluster
log_info "Logging into hosted-mgmt cluster..."
log_info "This will open a browser for SSO authentication."
oc login --web https://api.hosted-mgmt.ci.devcluster.openshift.com:6443

# Check if we can access the ClusterDeployment
log_info "Looking up ClusterDeployment: $cluster_name in namespace $pool_namespace"

# Get the ClusterDeployment - capture both stdout and stderr
cd_info=$(oc get clusterdeployment "$cluster_name" -n "$pool_namespace" -o json 2>&1) || true
cd_exit_code=$?

log_info "ClusterDeployment lookup returned (exit code: $cd_exit_code)"

# Check for common error patterns
if [[ -z "$cd_info" ]]; then
    log_error "Empty response from ClusterDeployment lookup."
    exit 1
fi

if echo "$cd_info" | grep -qiE "NotFound|not found|doesn't have|no resources found"; then
    log_error "ClusterDeployment ${cluster_name} not found in namespace ${pool_namespace}."
    log_error "The cluster may have been deleted (CI clusters are deleted when the job terminates)."
    exit 1
fi

if echo "$cd_info" | grep -qi "Forbidden"; then
    log_error "You do not have access to ClusterDeployment in namespace '$pool_namespace'."
    log_error ""
    log_error "For shared OpenShift CI cluster pools (like ci-cluster-pool), access is restricted."
    log_error "Options:"
    log_error "  1. Request access to the cluster pool from the OpenShift CI team"
    log_error "  2. Use the KUBECONFIG from CI artifacts (if the job saved it):"
    log_error "     ./scripts/download-ci-artifacts.sh --url \"$input_url\""
    log_error "  3. Hold the cluster open and get credentials from the CI job output"
    log_error ""
    log_error "See: docs/debugging-ci-clusters.md for more options"
    oc logout 2>/dev/null || true
    exit 1
fi

if echo "$cd_info" | grep -qiE "error|Error"; then
    log_error "Error looking up ClusterDeployment:"
    echo "$cd_info"
    exit 1
fi

# Verify we got valid JSON
if ! echo "$cd_info" | jq -e '.kind' > /dev/null 2>&1; then
    log_error "Invalid response from ClusterDeployment lookup (not valid JSON):"
    echo "$cd_info" | head -20
    exit 1
fi

log_success "Found ClusterDeployment: $(echo "$cd_info" | jq -r '.metadata.name')"

# Get the API URL from ClusterDeployment
api_url=$(echo "$cd_info" | jq -r '.status.apiURL // empty')
web_console_url=$(echo "$cd_info" | jq -r '.status.webConsoleURL // empty')

if [[ -z "$api_url" ]]; then
    log_error "Could not get API URL from ClusterDeployment."
    log_error "The cluster may still be provisioning."
    log_info ""
    log_info "ClusterDeployment status:"
    echo "$cd_info" | jq '.status' 2>/dev/null || echo "$cd_info"
    exit 1
fi

log_info "Cluster API URL: $api_url"
[[ -n "$web_console_url" ]] && log_info "Web Console URL: $web_console_url"

# Get the admin password secret reference - try multiple locations
admin_secret_name=$(echo "$cd_info" | jq -r '.spec.clusterMetadata.adminPasswordSecretRef.name // empty')

if [[ -z "$admin_secret_name" ]]; then
    # Try status location
    admin_secret_name=$(echo "$cd_info" | jq -r '.status.adminPasswordSecretRef.name // empty')
fi

if [[ -z "$admin_secret_name" ]]; then
    # Common naming convention
    admin_secret_name="${cluster_name}-admin-password"
fi

log_info "Looking up admin password from secret: $admin_secret_name"

password=""

# Try the referenced secret first
if [[ -n "$admin_secret_name" ]]; then
    password=$(oc get secret "$admin_secret_name" -n "$pool_namespace" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)
fi

# Try alternate secret names if not found
if [[ -z "$password" ]]; then
    log_info "Trying alternate secret names..."
    for secret_suffix in "admin-password" "0-admin-password" "admin-kubeconfig"; do
        secret_name="${cluster_name}-${secret_suffix}"
        if oc get secret "$secret_name" -n "$pool_namespace" > /dev/null 2>&1; then
            log_info "Found secret: $secret_name"
            if [[ "$secret_suffix" == *"kubeconfig"* ]]; then
                # Extract password from kubeconfig
                password=$(oc get secret "$secret_name" -n "$pool_namespace" -o jsonpath='{.data.kubeconfig}' 2>/dev/null | base64 -d | grep -E "^\s+password:" | head -1 | awk '{print $2}' || true)
            else
                password=$(oc get secret "$secret_name" -n "$pool_namespace" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)
            fi
            [[ -n "$password" ]] && break
        fi
    done
fi

# List available secrets for debugging if still not found
if [[ -z "$password" ]]; then
    log_error "Could not retrieve admin password."
    log_info ""
    log_info "Cluster: $cluster_name"
    log_info "Namespace: $pool_namespace"
    log_info "API URL: $api_url"
    log_info ""
    log_info "Available secrets matching cluster name:"
    oc get secrets -n "$pool_namespace" 2>/dev/null | grep -E "$cluster_name|admin" | head -10 || true
    log_info ""
    log_info "ClusterDeployment spec.clusterMetadata:"
    echo "$cd_info" | jq '.spec.clusterMetadata // .spec' 2>/dev/null || true
    exit 1
fi

log_success "Retrieved admin password"

# Logout from hosted-mgmt
oc logout

# Login to the ephemeral cluster
log_info "Logging into ephemeral cluster at $api_url..."
if ! oc login "$api_url" --username kubeadmin --password "$password" --insecure-skip-tls-verify=true; then
    log_error "Failed to login to cluster."
    log_info "Cluster: $cluster_name"
    log_info "API URL: $api_url"
    log_info "Username: kubeadmin"
    log_info "Password: $password"
    exit 1
fi

# Try to switch to the test namespace
if oc get namespace cost-onprem > /dev/null 2>&1; then
    oc project cost-onprem
    log_success "Switched to cost-onprem namespace"
fi

echo ""
log_success "Successfully logged into ephemeral cluster!"
echo ""
log_info "Cluster: $cluster_name"
log_info "API URL: $api_url"
log_info "Username: kubeadmin"
log_info "Password: $password"
echo ""

# Use console URL from ClusterDeployment if available, otherwise derive from API URL
if [[ -n "$web_console_url" ]]; then
    console_url="$web_console_url"
else
    console_url=$(echo "$api_url" | sed 's|https://api\.|https://console-openshift-console.apps.|' | sed 's|:6443||')
fi

# Prompt to open console
read -rp "Open OpenShift web console in browser? (y/n): " open_console

if [[ "$open_console" == "y" || "$open_console" == "Y" ]]; then
    # Try to get actual console URL from cluster (now that we're logged in)
    actual_console=$(oc get route -n openshift-console console -o jsonpath='{.spec.host}' 2>/dev/null || true)
    if [[ -n "$actual_console" ]]; then
        console_url="https://${actual_console}"
    fi
    
    log_info "Opening: $console_url"
    log_info "Username: kubeadmin"
    log_info "Password: $password"
    
    # Copy password to clipboard if possible
    if command -v pbcopy &> /dev/null; then
        echo -n "$password" | pbcopy
        log_success "Password copied to clipboard"
    fi
    
    sleep 2
    
    # Open browser
    if command -v open &> /dev/null; then
        open "$console_url"
    elif command -v xdg-open &> /dev/null; then
        xdg-open "$console_url"
    else
        log_warning "Could not open browser. Visit: $console_url"
    fi
fi
