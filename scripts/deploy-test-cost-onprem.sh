#!/bin/bash
set -euo pipefail

################################################################################
# Cost Management On-Premise OpenShift with JWT Authentication Deployment Script
#
# This script orchestrates the complete setup for Cost Management on
# OpenShift by wrapping the authoritative scripts from cost-onprem-chart repository.
#
# Based on: https://github.com/insights-onprem/cost-onprem-chart/blob/main/scripts/README.md
# Section: JWT Authentication Setup
#
# Usage:
#   ./deploy-test-cost-onprem.sh [OPTIONS]
#
# Options:
#
#   Execution mode:
#   --skip-deploy             Skip all deployment steps, run tests only
#   --skip-chart-tests        Skip chart pytest suite
#   --iqe-only                Run only IQE tests (skip deployment and chart tests)
#   --run-iqe                 Run IQE cost-management tests after deployment
#   --dry-run                 Show what would be executed without running
#
#   Deployment control:
#   --skip-rhbk               Skip Red Hat Build of Keycloak (RHBK) deployment
#   --skip-kafka              Skip Kafka/AMQ Streams deployment
#   --skip-helm               Skip COST Helm chart installation
#   --skip-tls                Skip TLS certificate setup
#   --skip-image-override     Skip creating custom values file for image override
#   --deploy-s4               Deploy S4 (Super Simple Storage Service) for S3-compatible storage
#   --s4-namespace NAME       S4 deployment namespace (default: s4-test)
#   --namespace NAME          Target namespace (default: cost-onprem)
#   --image-tag TAG           Custom image tag for cost-onprem-ocp-backend services
#   --use-local-chart         Use local Helm chart instead of GitHub release
#   --devel                   Include pre-release (rc) charts in Helm installation
#   --chart-version VERSION   Pin a specific Helm chart version (e.g., 0.2.9, 0.3.0-rc1)
#
#   Test options:
#   --iqe-marker EXPR         Pytest marker for IQE tests (default: cost_ocp_on_prem)
#   --iqe-profile PROFILE     IQE test profile: smoke, extended, stable, full (default: stable)
#   --listener-cpu LIMIT      Temporarily set listener CPU limit (e.g., 500m, 1000m, 'max', or 'none' to disable).
#                             Defaults to 'max' for --run-perf / --perf-only runs because the listener
#                             is the principal processing bottleneck and 300m throttles all ingestion tests.
#   --include-ui              Include UI tests (requires Playwright system dependencies)
#   --run-perf                Run performance tests after deployment (FLPATH-4036)
#   --perf-profile PROFILE    Performance profile: baseline, small, medium, large (default: baseline)
#   --perf-only               Run only performance tests (skip deployment and chart tests)
#
#   Observability options (FLPATH-4061):
#   --deploy-observability    Deploy postgres_exporter and valkey-exporter for metrics
#   --collect-metrics         Collect Prometheus metrics during performance tests
#   --upload-metrics          Upload metrics to S3 after tests (requires S3_BUCKET to be set)
#   --metrics-interval SECS   Metrics collection interval in seconds (default: 30)
#
#   Other:
#   --save-versions [FILE]    Save deployment version info to JSON file (default: version_info.json)
#   --verbose                 Enable verbose output
#   --help                    Display this help message
#
#   Backward-compatible aliases:
#   --tests-only              Alias for --skip-deploy
#   --skip-test               Alias for --skip-chart-tests
#
# Environment Variables:
#   KUBECONFIG               Path to kubeconfig file (default: ~/.kube/config)
#   KUBEADMIN_PASSWORD_FILE  Path to kubeadmin password file
#   SHARED_DIR               Shared directory containing kubeadmin-password
#   OPENSHIFT_API            OpenShift API URL (auto-detected from kubeconfig)
#   OPENSHIFT_USERNAME       OpenShift username (default: kubeadmin)
#   OPENSHIFT_PASSWORD       OpenShift password (auto-detected from files)
#   OPENSHIFT_VALUES_FILE    Helm values file path relative to repo root (default: openshift-values.yaml)
#
# Note: This script will automatically login to OpenShift using credentials from:
#       1. KUBECONFIG file (for API URL)
#       2. KUBEADMIN_PASSWORD_FILE or SHARED_DIR/kubeadmin-password (for password)
#       If already logged in, it will skip the login step.
#
# Prerequisites:
#   - oc CLI installed and configured
#   - kubectl CLI installed and configured
#   - helm CLI installed (v3+)
#   - yq installed for YAML/JSON processing
#   - OpenShift cluster with admin access
#
# Examples:
#   # Full deployment + chart tests (default)
#   ./deploy-test-cost-onprem.sh --namespace cost-onprem --verbose
#
#   # Deploy only — skip all tests
#   ./deploy-test-cost-onprem.sh --skip-chart-tests
#
#   # Run chart tests against existing deployment
#   ./deploy-test-cost-onprem.sh --skip-deploy
#
#   # Run only IQE tests with listener CPU boost
#   ./deploy-test-cost-onprem.sh --iqe-only --listener-cpu max --iqe-profile smoke
#
#   # Full deployment + chart tests + IQE tests
#   ./deploy-test-cost-onprem.sh --run-iqe --iqe-profile smoke
#
#   # Skip RHBK if already deployed
#   ./deploy-test-cost-onprem.sh --skip-rhbk
#
#   # Dry run to preview what would execute
#   ./deploy-test-cost-onprem.sh --dry-run --verbose
#
# Validation:
#   Flag parsing is tested by .github/workflows/validate-deploy-test-script.yml
#   which runs --dry-run for every flag permutation. Run locally with:
#     ./scripts/qe/test-gh-workflow-locally.sh .github/workflows/validate-deploy-test-script.yml
#
################################################################################

# Script metadata
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Default configuration
NAMESPACE="${NAMESPACE:-cost-onprem}"
USE_LOCAL_CHART="${USE_LOCAL_CHART:-false}"
USE_HELM_DEVEL="${USE_HELM_DEVEL:-false}"
CHART_VERSION="${CHART_VERSION:-}"
VERBOSE="${VERBOSE:-false}"
DRY_RUN="${DRY_RUN:-false}"
TESTS_ONLY="${TESTS_ONLY:-false}"
INCLUDE_UI="${INCLUDE_UI:-false}"
RUN_IQE="${RUN_IQE:-false}"
IQE_MARKER="${IQE_MARKER:-cost_ocp_on_prem}"
IQE_PROFILE="${IQE_PROFILE:-stable}"
RUN_PERF="${RUN_PERF:-false}"
PERF_PROFILE="${PERF_PROFILE:-baseline}"
PERF_ONLY="${PERF_ONLY:-false}"
DEPLOY_OBSERVABILITY="${DEPLOY_OBSERVABILITY:-false}"
COLLECT_METRICS="${COLLECT_METRICS:-false}"
UPLOAD_METRICS="${UPLOAD_METRICS:-false}"
METRICS_INTERVAL="${METRICS_INTERVAL:-30}"
METRICS_COLLECTOR_PID=""

# Performance output directory - unified structure for metrics + test results + reports
# Format: {PERF_OUTPUT_DIR}/{TEST_RUN_ID}/ with subdirs: metrics/, results/, reports/
# Use tests/perf-runs to align with pytest conftest.py output location
PERF_OUTPUT_DIR="${PERF_OUTPUT_DIR:-tests/perf-runs}"
# TEST_RUN_ID is auto-generated: {chart_version}-{perf_profile}-{epoch}
TEST_RUN_ID="${TEST_RUN_ID:-}"
SAVE_VERSIONS="${SAVE_VERSIONS:-false}"
VERSION_INFO_FILE="${VERSION_INFO_FILE:-version_info.json}"
LISTENER_CPU_LIMIT="${LISTENER_CPU_LIMIT:-}"

# S4 deployment configuration
DEPLOY_S4="${DEPLOY_S4:-false}"
S4_NAMESPACE="${S4_NAMESPACE:-s4-test}"

# CPU boost state tracking (for cleanup on exit)
CPU_BOOST_APPLIED=false
ORIGINAL_LISTENER_CPU_LIMIT=""
ORIGINAL_LISTENER_CPU_REQUEST=""

# OpenShift authentication
KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/config}"
OPENSHIFT_USERNAME="${OPENSHIFT_USERNAME:-kubeadmin}"
OPENSHIFT_API="${OPENSHIFT_API:-}"
OPENSHIFT_PASSWORD="${OPENSHIFT_PASSWORD:-}"
KUBEADMIN_PASSWORD_FILE="${KUBEADMIN_PASSWORD_FILE:-}"
SHARED_DIR="${SHARED_DIR:-}"

# Local scripts directory (this script sits alongside the other scripts)
LOCAL_SCRIPTS_DIR="${SCRIPT_DIR}"
SCRIPT_DEPLOY_RHBK="deploy-rhbk.sh"  # Red Hat Build of Keycloak (RHBK)
SCRIPT_DEPLOY_KAFKA="deploy-kafka.sh"
SCRIPT_DEPLOY_S4="deploy-s4-test.sh"  # S4 (Super Simple Storage Service)
SCRIPT_INSTALL_HELM="install-helm-chart.sh"
SCRIPT_SETUP_TLS="setup-cost-mgmt-tls.sh"
OPENSHIFT_VALUES_FILE="${OPENSHIFT_VALUES_FILE:-openshift-values.yaml}"

# Step flags (default: run all steps)
SKIP_RHBK=false  # Red Hat Build of Keycloak
SKIP_KAFKA=false
SKIP_HELM=false
SKIP_TLS=false
SKIP_TEST=false

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

################################################################################
# Cleanup trap for CPU boost
################################################################################

cleanup_on_exit() {
    local exit_code=$?
    
    # Stop metrics collection if running
    if [[ -n "${METRICS_COLLECTOR_PID}" ]]; then
        echo ""
        echo -e "${YELLOW}[CLEANUP] Stopping metrics collection...${NC}"
        kill -TERM "${METRICS_COLLECTOR_PID}" 2>/dev/null || true
    fi
    
    # Reset CPU boost if applied
    if [[ "${CPU_BOOST_APPLIED}" == "true" ]] && [[ -n "${ORIGINAL_LISTENER_CPU_LIMIT}" ]]; then
        echo ""
        echo -e "${YELLOW}[CLEANUP] Resetting listener CPU to original values...${NC}"
        reset_listener_cpu 2>/dev/null || true
    fi
    exit $exit_code
}

# Register cleanup trap for any exit (including errors from set -e)
trap cleanup_on_exit EXIT

################################################################################
# Logging functions
################################################################################

log_info() {
    echo -e "${BLUE}ℹ INFO:${NC} $*"
}

log_success() {
    echo -e "${GREEN}✅ SUCCESS:${NC} $*"
}

log_warning() {
    echo -e "${YELLOW}⚠ WARNING:${NC} $*"
}

log_error() {
    echo -e "${RED}❌ ERROR:${NC} $*" >&2
}

log_step() {
    echo -e "${CYAN}▶${NC} $*"
}

log_verbose() {
    if [[ "${VERBOSE}" == "true" ]]; then
        echo -e "${CYAN}[VERBOSE]${NC} $*" >&2
    fi
}

################################################################################
# Utility functions
################################################################################

show_help() {
    sed -n '/^# Usage:/,/^################################################################################$/p' "$0" | sed 's/^# \?//'
    exit 0
}

check_prerequisites() {
    log_step "Checking prerequisites"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Skipping prerequisite checks"
        return 0
    fi

    local missing_tools=()

    # Check required tools
    for tool in oc kubectl helm yq; do
        if ! command -v "$tool" &> /dev/null; then
            missing_tools+=("$tool")
        else
            log_verbose "Found: $tool ($(command -v "$tool"))"
        fi
    done

    if [[ ${#missing_tools[@]} -gt 0 ]]; then
        log_error "Missing required tools: ${missing_tools[*]}"
        log_error "Please install missing tools and try again"
        log_error ""
        log_error "Installation instructions:"
        log_error "  macOS:  brew install kubectl yq"
        log_error "  Linux:  kubectl: https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/  |  yq: https://github.com/mikefarah/yq#install"
        exit 1
    fi

    log_success "All required tools are installed"
}

detect_openshift_credentials() {
    log_info "Detecting OpenShift credentials from environment..."

    # Detect API URL from kubeconfig if not set
    if [[ -z "${OPENSHIFT_API}" ]] && [[ -f "${KUBECONFIG}" ]]; then
        OPENSHIFT_API=$(yq e '.clusters[0].cluster.server' "${KUBECONFIG}" 2>/dev/null || echo "")
        if [[ -n "${OPENSHIFT_API}" ]]; then
            log_verbose "Detected API URL from kubeconfig: ${OPENSHIFT_API}"
        fi
    fi

    # Detect password from files if not set
    if [[ -z "${OPENSHIFT_PASSWORD}" ]]; then
        if [[ -n "${KUBEADMIN_PASSWORD_FILE}" ]] && [[ -s "${KUBEADMIN_PASSWORD_FILE}" ]]; then
            OPENSHIFT_PASSWORD="$(cat "${KUBEADMIN_PASSWORD_FILE}")"
            log_verbose "Loaded password from KUBEADMIN_PASSWORD_FILE"
        elif [[ -n "${SHARED_DIR}" ]] && [[ -s "${SHARED_DIR}/kubeadmin-password" ]]; then
            OPENSHIFT_PASSWORD="$(cat "${SHARED_DIR}/kubeadmin-password")"
            log_verbose "Loaded password from SHARED_DIR/kubeadmin-password"
        fi
    fi
}

login_to_openshift() {
    log_step "Logging into OpenShift"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would login to OpenShift"
        return 0
    fi

    # Detect credentials from environment
    detect_openshift_credentials

    # Check if credentials are available
    if [[ -z "${OPENSHIFT_API}" ]]; then
        log_error "OPENSHIFT_API not set and could not be detected from kubeconfig"
        log_error "Please set OPENSHIFT_API environment variable or ensure KUBECONFIG is valid"
        return 1
    fi

    if [[ -z "${OPENSHIFT_PASSWORD}" ]]; then
        log_error "OPENSHIFT_PASSWORD not set and could not be detected from files"
        log_error "Please set one of:"
        log_error "  - OPENSHIFT_PASSWORD environment variable"
        log_error "  - KUBEADMIN_PASSWORD_FILE pointing to password file"
        log_error "  - SHARED_DIR containing kubeadmin-password file"
        return 1
    fi

    # Configure kubeconfig to skip TLS verification
    if [[ -f "${KUBECONFIG}" ]]; then
        log_verbose "Configuring kubeconfig to skip TLS verification..."
        yq -i 'del(.clusters[].cluster.certificate-authority-data) | .clusters[].cluster.insecure-skip-tls-verify=true' "${KUBECONFIG}" 2>/dev/null || true
    fi

    # Attempt login
    log_info "Logging in as ${OPENSHIFT_USERNAME} to ${OPENSHIFT_API}..."
    if oc login "${OPENSHIFT_API}" \
        --username="${OPENSHIFT_USERNAME}" \
        --password="${OPENSHIFT_PASSWORD}" \
        --insecure-skip-tls-verify=true &> /dev/null; then
        log_success "Successfully logged into OpenShift"
    else
        log_error "Failed to login to OpenShift"
        log_error "Please verify credentials and API URL"
        return 1
    fi
}

check_oc_connection() {
    log_step "Verifying OpenShift connection"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would verify OpenShift connection"
        return 0
    fi

    # Check if already logged in
    if ! oc whoami &> /dev/null; then
        log_info "Not currently logged into OpenShift, attempting automatic login..."

        # Try to login automatically
        if ! login_to_openshift; then
            log_error "Automatic login failed"
            log_error ""
            log_error "Manual login options:"
            log_error "  1. Set environment variables:"
            log_error "     export OPENSHIFT_API='https://api.example.com:6443'"
            log_error "     export OPENSHIFT_PASSWORD='your-password'"
            log_error ""
            log_error "  2. Or login manually:"
            log_error "     oc login https://api.example.com:6443"
            log_error ""
            exit 1
        fi
    else
        log_success "Already logged into OpenShift"
    fi

    local current_user
    current_user=$(oc whoami)
    local current_server
    current_server=$(oc whoami --show-server)

    log_success "Connected to OpenShift as: ${current_user}"
    log_info "Server: ${current_server}"

    # Check if user has admin privileges
    if oc auth can-i create clusterrole &> /dev/null; then
        log_success "User has cluster-admin privileges"
    else
        log_warning "User may not have sufficient privileges for cluster-scoped resources"
        log_warning "Some deployment steps may fail without admin access"
    fi
}

execute_script() {
    local script_name="$1"
    shift
    local script_path="${LOCAL_SCRIPTS_DIR}/${script_name}"

    if [[ ! -f "${script_path}" ]]; then
        log_error "Script not found: ${script_path}"
        return 1
    fi
    if [[ ! -x "${script_path}" ]]; then
        chmod +x "${script_path}" 2>/dev/null || true
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would execute: ${script_path} $*"
        return 0
    fi

    log_info "Executing: ${script_path} $*"

    local exit_code=0
    if [[ "${VERBOSE}" == "true" ]]; then
        bash -x "${script_path}" "$@" || exit_code=$?
    else
        "${script_path}" "$@" || exit_code=$?
    fi

    return ${exit_code}
}

create_namespace() {
    log_step "Creating namespace: ${NAMESPACE}"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would create namespace ${NAMESPACE}"
        return 0
    fi

    if oc get namespace "${NAMESPACE}" &> /dev/null; then
        log_info "Namespace ${NAMESPACE} already exists"
    else
        oc create namespace "${NAMESPACE}"
        log_success "Created namespace: ${NAMESPACE}"
    fi

    # Label namespace for Cost Management Metrics Operator
    log_info "Labeling namespace for Cost Management Metrics Operator..."
    oc label namespace "${NAMESPACE}" cost_management_optimizations=true --overwrite
    log_success "Namespace labeled successfully"
}

################################################################################
# Deployment steps
################################################################################

deploy_rhbk() {
    if [[ "${SKIP_RHBK}" == "true" ]]; then
        log_warning "Skipping Red Hat Build of Keycloak (RHBK) deployment (--skip-rhbk)"
        return 0
    fi

    log_step "Deploying Red Hat Build of Keycloak (RHBK) (1/6)"

    # Export environment variables for RHBK script
    # export NAMESPACE="${NAMESPACE}"

    if [[ "${VERBOSE}" == "true" ]]; then
        export VERBOSE="true"
    fi

    local rhbk_args=()
    local chart_values="${PROJECT_ROOT}/cost-onprem/values.yaml"
    if [[ -f "${chart_values}" ]]; then
        rhbk_args+=(-f "${chart_values}")
        log_info "Provisioning Keycloak users from: ${chart_values}"
    fi

    if ! execute_script "${SCRIPT_DEPLOY_RHBK}" "${rhbk_args[@]}"; then
        log_error "Red Hat Build of Keycloak (RHBK) deployment failed"
        exit 1
    fi

    log_success "Red Hat Build of Keycloak (RHBK) deployment completed"
}

deploy_kafka() {
    if [[ "${SKIP_KAFKA}" == "true" ]]; then
        log_warning "Skipping Kafka/AMQ Streams deployment (--skip-kafka)"
        return 0
    fi

    log_step "Deploying Kafka/AMQ Streams (2/6)"

    # Export environment variables for AMQ Streams script
    # export KAFKA_NAMESPACE="${NAMESPACE}"
    export STORAGE_CLASS="${STORAGE_CLASS:-}"

    log_verbose "Using storage class: ${STORAGE_CLASS}"

    if [[ "${VERBOSE}" == "true" ]]; then
        export VERBOSE="true"
    fi

    if ! execute_script "${SCRIPT_DEPLOY_KAFKA}"; then
        log_error "Kafka/AMQ Streams deployment failed"
        exit 1
    fi

    log_success "Kafka/AMQ Streams deployment completed"
}

deploy_s4() {
    if [[ "${DEPLOY_S4}" != "true" ]]; then
        log_verbose "Skipping S4 deployment (--deploy-s4 not specified)"
        return 0
    fi

    log_step "Deploying S4 (Super Simple Storage Service) (3/6)"
    log_info "S4 namespace: ${S4_NAMESPACE}"

    # Export environment variables for S4 script
    export S4_RELEASE_TAG="${S4_RELEASE_TAG:-}"
    export S4_REPO="${S4_REPO:-}"
    export STORAGE_SIZE="${STORAGE_SIZE:-}"

    if [[ "${VERBOSE}" == "true" ]]; then
        export VERBOSE="true"
    fi

    # Pass S4_NAMESPACE as first argument to deploy-s4-test.sh
    if ! execute_script "${SCRIPT_DEPLOY_S4}" "${S4_NAMESPACE}"; then
        log_error "S4 deployment failed"
        log_info "To troubleshoot S4 deployment:"
        log_info "  1. Check S4 pod status: kubectl get pods -n ${S4_NAMESPACE}"
        log_info "  2. Check S4 pod logs: kubectl logs -n ${S4_NAMESPACE} -l app.kubernetes.io/name=s4"
        log_info "  3. Check S4 service: kubectl get svc s4 -n ${S4_NAMESPACE}"
        log_info "  4. Clean up S4: ${LOCAL_SCRIPTS_DIR}/${SCRIPT_DEPLOY_S4} ${S4_NAMESPACE} cleanup"
        exit 1
    fi

    # Set environment variables for Helm installation to use S4
    export S3_ENDPOINT="s4.${S4_NAMESPACE}.svc.cluster.local"
    export S3_PORT="7480"
    export S3_USE_SSL="false"

    # Copy storage credentials from S4 namespace to chart namespace
    # The install-helm-chart.sh script looks for credentials in the chart namespace
    if [[ "${S4_NAMESPACE}" != "${NAMESPACE}" ]]; then
        log_info "Copying storage credentials from ${S4_NAMESPACE} to ${NAMESPACE}..."
        if kubectl get secret cost-onprem-storage-credentials -n "${S4_NAMESPACE}" >/dev/null 2>&1; then
            # Extract credentials from S4 namespace
            local access_key
            local secret_key
            access_key=$(kubectl get secret cost-onprem-storage-credentials -n "${S4_NAMESPACE}" -o jsonpath='{.data.access-key}' | base64 -d)
            secret_key=$(kubectl get secret cost-onprem-storage-credentials -n "${S4_NAMESPACE}" -o jsonpath='{.data.secret-key}' | base64 -d)
            
            # Create secret in chart namespace
            kubectl create secret generic cost-onprem-storage-credentials \
                --namespace="${NAMESPACE}" \
                --from-literal=access-key="${access_key}" \
                --from-literal=secret-key="${secret_key}" \
                --dry-run=client -o yaml | kubectl apply -f -
            log_success "Storage credentials copied to ${NAMESPACE}"
        else
            log_warning "Storage credentials secret not found in ${S4_NAMESPACE}"
        fi
    fi

    log_success "S4 deployment completed"
    log_info "S3 endpoint configured: ${S3_ENDPOINT}:${S3_PORT} (SSL: ${S3_USE_SSL})"
    log_info "Storage credentials secret: cost-onprem-storage-credentials (in ${NAMESPACE})"
}

deploy_helm_chart() {
    if [[ "${SKIP_HELM}" == "true" ]]; then
        log_warning "Skipping Cost On-Prem Helm chart installation (--skip-helm)"
        return 0
    fi

    log_step "Deploying Cost On-Prem Helm chart (4/6)"

    # Use the values file (default: openshift-values.yaml, configurable via OPENSHIFT_VALUES_FILE)
    local values_file="${PROJECT_ROOT}/${OPENSHIFT_VALUES_FILE}"
    download_openshift_values "${values_file}"
    export VALUES_FILE="${values_file}"

    # Export environment variables for Helm script
    export NAMESPACE="${NAMESPACE}"
    export JWT_AUTH_ENABLED="true"
    export USE_LOCAL_CHART="${USE_LOCAL_CHART}"
    export USE_HELM_DEVEL="${USE_HELM_DEVEL}"
    [[ -n "${CHART_VERSION}" ]] && export CHART_VERSION="${CHART_VERSION}"
    # Note: S3 setup behavior depends on values.yaml configuration:
    # - If objectStorage.endpoint is set: Script skips S3 auto-detection and bucket creation
    # - If not set: Script auto-detects (S4, NooBaa, external OBC) and creates buckets
    # SKIP_S3_SETUP=true can be used to skip bucket creation in CI environments
    # Pytests have their own S3 preflight checks that will setup the S3 buckets if they are not already present.

    if [[ "${VERBOSE}" == "true" ]]; then
        export VERBOSE="true"
    fi

    if ! execute_script "${SCRIPT_INSTALL_HELM}"; then
        log_error "Helm chart deployment failed"
        log_info ""
        log_info "To troubleshoot:"
        log_info "  1. Check Helm release status: helm list -n ${NAMESPACE}"
        log_info "  2. Check pod status: oc get pods -n ${NAMESPACE}"
        log_info "  3. View pod logs: oc logs -n ${NAMESPACE} <pod-name>"
        log_info "  4. Check events: oc get events -n ${NAMESPACE} --sort-by='.lastTimestamp'"
        exit 1
    fi

    log_success "Cost On-Prem Helm chart deployment completed"
}

download_openshift_values() {
    local values_file="$1"

    log_info "Using local OpenShift values file from repository"
    log_verbose "Path: ${values_file}"
    if [[ ! -f "${values_file}" ]]; then
        log_error "OpenShift values file not found at: ${values_file}"
        log_error "Ensure ${OPENSHIFT_VALUES_FILE} exists at the repository root"
        return 1
    fi

    if [[ "${VERBOSE}" == "true" ]]; then
        log_verbose "Values file contents (first 30 lines):"
        head -30 "${values_file}" | while IFS= read -r line; do
            log_verbose "  ${line}"
        done
    fi
}

setup_tls() {
    if [[ "${SKIP_TLS}" == "true" ]]; then
        log_warning "Skipping TLS certificate setup (--skip-tls)"
        return 0
    fi

    log_step "Configuring TLS certificates (5/6)"

    # Export environment variables for TLS script
    export NAMESPACE="${NAMESPACE}"

    if [[ "${VERBOSE}" == "true" ]]; then
        export VERBOSE="true"
    fi

    execute_script "${SCRIPT_SETUP_TLS}"

    log_success "TLS certificate setup completed"
}


run_tests() {
    # Apply listener CPU boost early - benefits both chart tests and IQE tests
    # Note: CPU_BOOST_APPLIED is a global variable, cleanup is handled by trap
    if [[ -n "${LISTENER_CPU_LIMIT}" ]]; then
        if validate_cpu_limit "${LISTENER_CPU_LIMIT}"; then
            local effective_cpu_limit="${LISTENER_CPU_LIMIT}"
            if [[ "${LISTENER_CPU_LIMIT}" == "max" ]]; then
                calculate_max_listener_cpu
                effective_cpu_limit="${MAX_LISTENER_CPU}m"
                log_info "Calculated maximum available CPU: ${effective_cpu_limit}"
            fi
            
            log_step "Setting listener CPU limit to ${effective_cpu_limit}"
            if set_listener_cpu "${effective_cpu_limit}"; then
                CPU_BOOST_APPLIED=true
            else
                log_warning "Continuing with current listener CPU"
            fi
        fi
    fi
    
    # Helper to reset CPU (also called by trap on exit)
    cleanup_cpu_boost() {
        if [[ "${CPU_BOOST_APPLIED}" == "true" ]]; then
            reset_listener_cpu
            CPU_BOOST_APPLIED=false
        fi
    }
    
    if [[ "${SKIP_TEST}" == "true" ]]; then
        log_warning "Skipping cost-onprem chart tests (--skip-chart-tests)"
        # Still run IQE tests if requested
        if [[ "${RUN_IQE}" == "true" ]]; then
            run_iqe_tests
        fi
        # Still run performance tests if requested
        if [[ "${RUN_PERF}" == "true" ]]; then
            run_performance_tests
        fi
        cleanup_cpu_boost
        return 0
    fi

    log_step "Running cost-onprem chart tests (6/6)"

    # Ensure we're logged in to OpenShift for JWT test
    if [[ "${DRY_RUN}" != "true" ]]; then
        if ! oc whoami -t &> /dev/null; then
            log_info "Not logged in to OpenShift with a user that has an available token, attempting login for JWT test..."
            if ! login_to_openshift; then
                log_warning "Failed to login to OpenShift, skipping JWT test"
                # Still run IQE tests if requested
                if [[ "${RUN_IQE}" == "true" ]]; then
                    run_iqe_tests
                fi
                return 0
            fi
        fi
    fi
    
    # Export environment variables for pytest
    export NAMESPACE="${NAMESPACE}"
    export HELM_RELEASE_NAME="${HELM_RELEASE_NAME:-cost-onprem}"
    export KEYCLOAK_NAMESPACE="${KEYCLOAK_NAMESPACE:-keycloak}"

    if [[ "${VERBOSE}" == "true" ]]; then
        export VERBOSE="true"
    fi
    
    # Run pytest test suite
    local pytest_script="${LOCAL_SCRIPTS_DIR}/run-pytest.sh"
    if [[ ! -x "${pytest_script}" ]]; then
        log_error "Pytest runner not found at: ${pytest_script}"
        exit 1
    fi
    
    log_info "Running pytest test suite..."
    
    # Build pytest arguments
    local pytest_args=()
    if [[ "${VERBOSE}" == "true" ]]; then
        pytest_args+=("-v")
    fi
    if [[ "${INCLUDE_UI}" == "true" ]]; then
        # Override default "not ui" marker to include UI tests
        pytest_args+=("-m" "")
        log_info "Including UI tests (Playwright)"
    fi
    
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would execute: ${pytest_script} ${pytest_args[*]:-}"
        return 0
    fi
    
    # Track test failures - continue running all tests, fail at the end
    local chart_tests_failed=false
    local iqe_tests_failed=false
    local perf_tests_failed=false
    
    # Run chart tests
    # Note: cost_validation tests have their own E2E setup with 300s provider timeout
    if ! "${pytest_script}" ${pytest_args[@]+"${pytest_args[@]}"}; then
        log_error "Pytest test suite failed"
        log_info "JUnit report available at: tests/reports/junit.xml"
        chart_tests_failed=true
    else
        log_success "Pytest test suite completed"
    fi
    
    # Run IQE tests if requested (continue even if chart tests failed)
    if [[ "${RUN_IQE}" == "true" ]]; then
        if ! run_iqe_tests; then
            iqe_tests_failed=true
        fi
    fi
    
    # Run performance tests if requested (continue even if other tests failed)
    if [[ "${RUN_PERF}" == "true" ]]; then
        if ! run_performance_tests; then
            perf_tests_failed=true
        fi
    fi
    
    # Reset listener CPU after all tests
    cleanup_cpu_boost
    
    # Report overall status
    if [[ "${chart_tests_failed}" == "true" ]] || [[ "${iqe_tests_failed}" == "true" ]] || [[ "${perf_tests_failed}" == "true" ]]; then
        echo ""
        log_error "Test failures detected:"
        [[ "${chart_tests_failed}" == "true" ]] && log_error "  - Chart tests (pytest) failed"
        [[ "${iqe_tests_failed}" == "true" ]] && log_error "  - IQE tests failed"
        [[ "${perf_tests_failed}" == "true" ]] && log_error "  - Performance tests failed"
        return 1
    fi
}

################################################################################
# Resource Management for Test Runs
################################################################################

# Store original resource values for restoration
ORIGINAL_LISTENER_CPU_LIMIT=""
ORIGINAL_LISTENER_CPU_REQUEST=""

# Parse CPU value to millicores (e.g., "500m" -> 500, "1" -> 1000)
parse_cpu_to_millicores() {
    local cpu_value="$1"
    if [[ "${cpu_value}" =~ ^([0-9]+)m$ ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "${cpu_value}" =~ ^([0-9]+)$ ]]; then
        echo "$((${BASH_REMATCH[1]} * 1000))"
    else
        echo "0"
    fi
}

# Calculate maximum available CPU on the node where listener runs.
# Sets MAX_LISTENER_CPU (millicores, no "m" suffix) as a global variable.
calculate_max_listener_cpu() {
    MAX_LISTENER_CPU=""
    local release="${HELM_RELEASE_NAME:-cost-onprem}"
    local listener_deploy="${release}-koku-listener"
    
    # Get the node where listener is running
    local listener_node
    listener_node=$(kubectl get pods -n "${NAMESPACE}" -l "app.kubernetes.io/component=listener" \
        -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)
    
    if [[ -z "${listener_node}" ]]; then
        log_warning "Could not determine listener node, using default max of 2000m"
        MAX_LISTENER_CPU="2000"
        return
    fi
    
    # Get node's allocatable CPU
    local allocatable_cpu
    allocatable_cpu=$(kubectl get node "${listener_node}" -o jsonpath='{.status.allocatable.cpu}' 2>/dev/null)
    local allocatable_millicores
    allocatable_millicores=$(parse_cpu_to_millicores "${allocatable_cpu}")
    
    # Get current CPU requests on the node
    local used_requests
    used_requests=$(kubectl describe node "${listener_node}" 2>/dev/null | grep -A5 "Allocated resources" | grep "cpu" | awk '{print $2}' | sed 's/[^0-9]//g')
    
    if [[ -z "${used_requests}" ]]; then
        used_requests=0
    fi
    
    # Get listener's current request
    local listener_request
    listener_request=$(kubectl get deploy "${listener_deploy}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null || echo "150m")
    local listener_request_millicores
    listener_request_millicores=$(parse_cpu_to_millicores "${listener_request}")
    
    # Calculate available: allocatable - used + listener's current (since we're replacing it)
    # Leave 500m buffer for system overhead
    local available=$((allocatable_millicores - used_requests + listener_request_millicores - 500))
    
    # Cap at 4000m (4 cores) as a reasonable maximum for a single pod
    if [[ "${available}" -gt 4000 ]]; then
        available=4000
    fi
    
    # Minimum of 500m
    if [[ "${available}" -lt 500 ]]; then
        available=500
    fi
    
    log_verbose "Node ${listener_node}: allocatable=${allocatable_millicores}m, used=${used_requests}m, listener=${listener_request_millicores}m, available=${available}m"
    MAX_LISTENER_CPU="${available}"
}

# Validate CPU limit format and value
validate_cpu_limit() {
    local cpu_limit="$1"
    
    # Special cases
    if [[ "${cpu_limit}" == "max" ]] || [[ "${cpu_limit}" == "none" ]]; then
        return 0
    fi
    
    # Check format (must be like "500m" or "1")
    if [[ ! "${cpu_limit}" =~ ^[0-9]+m?$ ]]; then
        log_error "Invalid CPU limit format: ${cpu_limit}"
        log_error "Expected format: <number>m (e.g., 500m, 1000m), <number> (e.g., 1, 2), or 'max'"
        return 1
    fi
    
    local millicores
    millicores=$(parse_cpu_to_millicores "${cpu_limit}")
    
    # Sanity check: must be at least 100m and at most 4000m (4 cores)
    if [[ "${millicores}" -lt 100 ]]; then
        log_error "CPU limit too low: ${cpu_limit} (minimum: 100m)"
        return 1
    fi
    
    if [[ "${millicores}" -gt 4000 ]]; then
        log_error "CPU limit too high: ${cpu_limit} (maximum: 4000m / 4 cores)"
        return 1
    fi
    
    return 0
}

set_listener_cpu() {
    local new_limit="$1"
    
    log_step "Setting listener CPU limit to ${new_limit}"
    
    local release="${HELM_RELEASE_NAME:-cost-onprem}"
    local listener_deploy="${release}-koku-listener"
    
    # Get current values for restoration later
    ORIGINAL_LISTENER_CPU_LIMIT=$(kubectl get deploy "${listener_deploy}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}' 2>/dev/null || echo "")
    ORIGINAL_LISTENER_CPU_REQUEST=$(kubectl get deploy "${listener_deploy}" -n "${NAMESPACE}" \
        -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null || echo "")
    
    if [[ -z "${ORIGINAL_LISTENER_CPU_LIMIT}" ]]; then
        log_warning "Could not get current listener CPU limit"
        return 1
    fi
    
    # Parse values for comparison
    local current_millicores new_millicores
    current_millicores=$(parse_cpu_to_millicores "${ORIGINAL_LISTENER_CPU_LIMIT}")
    new_millicores=$(parse_cpu_to_millicores "${new_limit}")
    
    log_info "Current listener CPU: limit=${ORIGINAL_LISTENER_CPU_LIMIT}, request=${ORIGINAL_LISTENER_CPU_REQUEST}"
    
    # Check if new limit is same as current
    if [[ "${current_millicores}" -eq "${new_millicores}" ]]; then
        log_info "Listener CPU limit already set to ${new_limit}, no change needed"
        ORIGINAL_LISTENER_CPU_LIMIT=""  # Clear so we don't reset later
        return 0
    fi
    
    # Warn if decreasing CPU
    if [[ "${new_millicores}" -lt "${current_millicores}" ]]; then
        log_warning "Decreasing CPU limit from ${ORIGINAL_LISTENER_CPU_LIMIT} to ${new_limit}"
    fi
    
    # Calculate request as half of limit (standard practice)
    local new_request="$((new_millicores / 2))m"
    
    log_info "Setting listener CPU: limit=${new_limit}, request=${new_request}"
    
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would patch ${listener_deploy} CPU to limit=${new_limit}, request=${new_request}"
        return 0
    fi
    
    # Patch listener deployment
    if kubectl patch deploy "${listener_deploy}" -n "${NAMESPACE}" --type='json' \
        -p="[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/resources/limits/cpu\", \"value\": \"${new_limit}\"},
             {\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/resources/requests/cpu\", \"value\": \"${new_request}\"}]" \
        &>/dev/null; then
        log_success "Listener CPU set to ${new_limit}"
        
        # Wait for rollout
        log_info "Waiting for listener rollout..."
        if ! kubectl rollout status deploy/"${listener_deploy}" -n "${NAMESPACE}" --timeout=120s; then
            log_warning "Rollout timed out, but continuing..."
        fi
    else
        log_error "Failed to set listener CPU"
        ORIGINAL_LISTENER_CPU_LIMIT=""  # Clear so we don't try to reset
        return 1
    fi
}

reset_listener_cpu() {
    if [[ -z "${ORIGINAL_LISTENER_CPU_LIMIT}" ]]; then
        return 0
    fi
    
    log_step "Resetting listener CPU to original values"
    
    local release="${HELM_RELEASE_NAME:-cost-onprem}"
    local listener_deploy="${release}-koku-listener"
    
    log_info "Resetting listener CPU: limit=${ORIGINAL_LISTENER_CPU_LIMIT}, request=${ORIGINAL_LISTENER_CPU_REQUEST}"
    
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would reset ${listener_deploy} CPU"
        return 0
    fi
    
    if kubectl patch deploy "${listener_deploy}" -n "${NAMESPACE}" --type='json' \
        -p="[{\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/resources/limits/cpu\", \"value\": \"${ORIGINAL_LISTENER_CPU_LIMIT}\"},
             {\"op\": \"replace\", \"path\": \"/spec/template/spec/containers/0/resources/requests/cpu\", \"value\": \"${ORIGINAL_LISTENER_CPU_REQUEST}\"}]" \
        &>/dev/null; then
        log_success "Listener CPU reset to ${ORIGINAL_LISTENER_CPU_LIMIT}"
    else
        log_warning "Failed to reset listener CPU - manual intervention may be needed"
        log_warning "Expected values: limit=${ORIGINAL_LISTENER_CPU_LIMIT}, request=${ORIGINAL_LISTENER_CPU_REQUEST}"
    fi
}

################################################################################
# Observability Stack (FLPATH-4061)
################################################################################

deploy_observability() {
    if [[ "${DEPLOY_OBSERVABILITY}" != "true" ]]; then
        log_verbose "Skipping observability deployment (--deploy-observability not specified)"
        return 0
    fi

    log_step "Deploying observability stack (FLPATH-4061)"

    local observability_script="${LOCAL_SCRIPTS_DIR}/deploy-observability.sh"
    if [[ ! -x "${observability_script}" ]]; then
        log_error "Observability deployment script not found at: ${observability_script}"
        return 1
    fi

    # Export environment variables for deploy-observability.sh
    export NAMESPACE="${NAMESPACE}"
    export HELM_RELEASE_NAME="${HELM_RELEASE_NAME:-cost-onprem}"
    export SKIP_GRAFANA="true"  # We don't need Grafana for metrics collection

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would execute: ${observability_script}"
        return 0
    fi

    if ! "${observability_script}"; then
        log_warning "Observability deployment had issues, continuing..."
    else
        log_success "Observability stack deployed"
    fi
}

start_metrics_collection() {
    if [[ "${COLLECT_METRICS}" != "true" ]]; then
        return 0
    fi

    log_step "Starting metrics collection"

    local collect_script="${LOCAL_SCRIPTS_DIR}/observability/collect-metrics.sh"
    if [[ ! -x "${collect_script}" ]]; then
        log_error "Metrics collection script not found at: ${collect_script}"
        return 1
    fi

    # Generate TEST_RUN_ID if not already set
    if [[ -z "${TEST_RUN_ID}" ]]; then
        local chart_version="unknown"
        local epoch_time
        epoch_time=$(date +%s)
        
        # Try to get chart version from Helm release
        if command -v helm &>/dev/null; then
            local helm_chart
            helm_chart=$(helm list -n "${NAMESPACE}" -o json 2>/dev/null | jq -r ".[0].chart // empty" 2>/dev/null)
            if [[ -n "$helm_chart" ]]; then
                chart_version=$(echo "$helm_chart" | sed 's/.*-\([0-9][0-9.]*\)/\1/' | tr '.' '-')
            fi
        fi
        
        TEST_RUN_ID="${chart_version}-${PERF_PROFILE}-${epoch_time}"
    fi

    # Create unified output directory structure
    mkdir -p "${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/metrics"
    mkdir -p "${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/results"
    mkdir -p "${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/reports"

    # Export variables for collect-metrics.sh and pytest
    export NAMESPACE="${NAMESPACE}"
    export PERF_PROFILE="${PERF_PROFILE}"
    export HELM_RELEASE_NAME="${HELM_RELEASE_NAME:-cost-onprem}"
    export TEST_RUN_ID="${TEST_RUN_ID}"
    export PERF_OUTPUT_DIR="${PERF_OUTPUT_DIR}"
    # collect-metrics.sh uses OUTPUT_DIR for its output
    export OUTPUT_DIR="${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/metrics"
    # Tell collect-metrics.sh not to add another TEST_RUN_ID subdirectory
    export METRICS_FLAT_OUTPUT=true

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would start metrics collection"
        log_info "  TEST_RUN_ID: ${TEST_RUN_ID}"
        log_info "  Output: ${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/"
        return 0
    fi

    log_info "Starting metrics collection (interval: ${METRICS_INTERVAL}s)"
    log_info "  TEST_RUN_ID: ${TEST_RUN_ID}"
    log_info "  Output: ${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/"
    log_info "    metrics/  - Prometheus snapshots"
    log_info "    results/  - Test results JSON"
    log_info "    reports/  - JUnit XML, HTML report"

    # Start metrics collection in background
    "${collect_script}" --continuous "${METRICS_INTERVAL}" &
    METRICS_COLLECTOR_PID=$!

    log_success "Metrics collection started (PID: ${METRICS_COLLECTOR_PID})"
}

stop_metrics_collection() {
    if [[ -z "${METRICS_COLLECTOR_PID}" ]]; then
        return 0
    fi

    log_step "Stopping metrics collection"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would stop metrics collection"
        return 0
    fi

    # Send SIGTERM to gracefully stop collection
    if kill -0 "${METRICS_COLLECTOR_PID}" 2>/dev/null; then
        kill -TERM "${METRICS_COLLECTOR_PID}" 2>/dev/null || true
        wait "${METRICS_COLLECTOR_PID}" 2>/dev/null || true
        log_success "Metrics collection stopped"
    fi

    METRICS_COLLECTOR_PID=""
}

upload_perf_results_to_s3() {
    if [[ "${UPLOAD_METRICS}" != "true" ]]; then
        return 0
    fi

    log_step "Uploading performance results to S3"

    if [[ -z "${S3_BUCKET:-}" ]]; then
        log_warning "S3_BUCKET not set, skipping upload"
        return 0
    fi

    if [[ -z "${TEST_RUN_ID:-}" ]]; then
        log_warning "TEST_RUN_ID not set, skipping upload"
        return 0
    fi

    local test_run_dir="${PERF_OUTPUT_DIR}/${TEST_RUN_ID}"
    if [[ ! -d "${test_run_dir}" ]]; then
        log_warning "Test run directory not found: ${test_run_dir}"
        return 0
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would upload ${test_run_dir} to s3://${S3_BUCKET}/${S3_PREFIX:-cost-onprem-performance/}${TEST_RUN_ID}/"
        return 0
    fi

    # Generate metadata.json with test run context
    generate_metadata_json

    # Generate flat perf-summary.json for Grafana Infinity datasource queries
    local summary_script="$(dirname "${BASH_SOURCE[0]}")/observability/generate-perf-summary.py"
    if [[ -f "${summary_script}" ]] && command -v python3 &>/dev/null; then
        python3 "${summary_script}" --run-dir "${test_run_dir}" 2>/dev/null \
            && log_info "Generated perf-summary.json" \
            || log_warning "Could not generate perf-summary.json (non-fatal)"
    fi

    # Upload entire directory recursively
    local s3_prefix="${S3_PREFIX:-cost-onprem-performance/}${TEST_RUN_ID}"
    local endpoint_arg=""
    if [[ -n "${S3_ENDPOINT:-}" ]]; then
        endpoint_arg="--endpoint-url ${S3_ENDPOINT}"
    fi

    log_info "Uploading to s3://${S3_BUCKET}/${s3_prefix}/"

    if command -v aws &>/dev/null; then
        aws s3 sync "${test_run_dir}" "s3://${S3_BUCKET}/${s3_prefix}/" ${endpoint_arg} --no-progress
    elif command -v mc &>/dev/null; then
        if [[ -n "${AWS_ACCESS_KEY_ID:-}" ]] && [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
            mc alias set s3upload "${S3_ENDPOINT:-https://s3.amazonaws.com}" \
                "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" --api S3v4 &>/dev/null
            mc mirror "${test_run_dir}" "s3upload/${S3_BUCKET}/${s3_prefix}/"
        else
            mc mirror "${test_run_dir}" "s3/${S3_BUCKET}/${s3_prefix}/"
        fi
    elif command -v s3cmd &>/dev/null; then
        local s3cmd_args=()
        [[ -n "${S3_ENDPOINT:-}" ]] && s3cmd_args+=("--host=${S3_ENDPOINT}" "--host-bucket=${S3_ENDPOINT}/${S3_BUCKET}")
        [[ -n "${AWS_ACCESS_KEY_ID:-}" ]] && s3cmd_args+=("--access_key=${AWS_ACCESS_KEY_ID}")
        [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] && s3cmd_args+=("--secret_key=${AWS_SECRET_ACCESS_KEY}")
        s3cmd sync "${test_run_dir}/" "s3://${S3_BUCKET}/${s3_prefix}/" "${s3cmd_args[@]}"
    else
        log_error "No S3 client found (aws, mc, or s3cmd). Install one of them."
        return 1
    fi

    log_success "Uploaded to s3://${S3_BUCKET}/${s3_prefix}/"

    # Update the bucket-level index.json so Grafana can list all runs
    if [[ -f "${summary_script}" ]] && command -v python3 &>/dev/null; then
        S3_ENDPOINT="${S3_ENDPOINT:-}" S3_BUCKET="${S3_BUCKET}" \
        S3_PREFIX="${S3_PREFIX:-cost-onprem-performance}" \
        python3 "${summary_script}" --run-dir "${test_run_dir}" --update-index 2>/dev/null \
            || log_warning "Could not update bucket index.json (non-fatal)"
    fi
}

generate_metadata_json() {
    local metadata_file="${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/metadata.json"
    
    log_info "Generating metadata.json..."
    
    # Get cluster info
    local ocp_version="unknown"
    local node_count=0
    local storage_type="unknown"
    
    if command -v oc &>/dev/null; then
        ocp_version=$(oc get clusterversion version -o jsonpath='{.status.desired.version}' 2>/dev/null || echo "unknown")
        node_count=$(oc get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ' || echo "0")
        
        # Detect storage type
        if oc get storagecluster -n openshift-storage &>/dev/null; then
            storage_type="ODF"
        elif oc get storageclass s4-storage &>/dev/null; then
            storage_type="S4"
        fi
    fi
    
    # Get chart version
    local chart_version="unknown"
    if command -v helm &>/dev/null; then
        chart_version=$(helm list -n "${NAMESPACE}" -o json 2>/dev/null | jq -r ".[0].app_version // .[0].chart // \"unknown\"" 2>/dev/null || echo "unknown")
    fi
    
    # Count files in each subdirectory
    local metrics_count=$(find "${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/metrics" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
    local results_count=$(find "${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/results" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
    local reports_count=$(find "${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/reports" -type f 2>/dev/null | wc -l | tr -d ' ')
    
    cat > "${metadata_file}" <<EOF
{
  "test_run_id": "${TEST_RUN_ID}",
  "chart_version": "${chart_version}",
  "perf_profile": "${PERF_PROFILE}",
  "listener_cpu_limit": "${LISTENER_CPU:-default}",
  "namespace": "${NAMESPACE}",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "cluster_info": {
    "ocp_version": "${ocp_version}",
    "node_count": ${node_count},
    "storage_type": "${storage_type}",
    "platform": "${CLUSTER_PLATFORM:-unknown}"
  },
  "file_counts": {
    "metrics": ${metrics_count},
    "results": ${results_count},
    "reports": ${reports_count}
  }
}
EOF
    
    log_success "Generated: ${metadata_file}"
}

################################################################################
# Performance Test Execution (FLPATH-4036)
################################################################################

run_performance_tests() {
    log_step "Running performance tests (FLPATH-4036)"

    local pytest_script="${LOCAL_SCRIPTS_DIR}/run-pytest.sh"
    if [[ ! -x "${pytest_script}" ]]; then
        log_error "Pytest runner not found at: ${pytest_script}"
        return 1
    fi

    log_info "Running performance tests with profile: ${PERF_PROFILE}"

    # Export performance profile for tests
    export PERF_PROFILE="${PERF_PROFILE}"

    # Listener CPU boost — always applied for perf tests unless explicitly disabled.
    # The listener is the principal processing bottleneck: at the chart default (300m)
    # it throttles every ingestion test, producing results that measure the CPU cap
    # rather than actual pipeline throughput.  This matches the behaviour of chart
    # tests and IQE plugin tests, both of which complete significantly faster with
    # --listener-cpu max.  Pass --listener-cpu none to skip the boost intentionally.
    local perf_listener_cpu="${LISTENER_CPU_LIMIT:-max}"
    if [[ "${perf_listener_cpu}" != "none" ]] && [[ "${CPU_BOOST_APPLIED:-false}" != "true" ]]; then
        local effective_cpu_limit="${perf_listener_cpu}"
        if [[ "${perf_listener_cpu}" == "max" ]]; then
            calculate_max_listener_cpu
            effective_cpu_limit="${MAX_LISTENER_CPU}m"
            log_info "Listener CPU boost: calculated max = ${effective_cpu_limit}"
        fi
        if validate_cpu_limit "${effective_cpu_limit}"; then
            log_step "Boosting listener CPU to ${effective_cpu_limit} for performance tests"
            if set_listener_cpu "${effective_cpu_limit}"; then
                CPU_BOOST_APPLIED=true
                log_success "Listener CPU boosted to ${effective_cpu_limit} (was ${ORIGINAL_LISTENER_CPU_LIMIT})"
            else
                log_warning "Could not boost listener CPU — results may reflect the 300m throttle"
            fi
        fi
    elif [[ "${CPU_BOOST_APPLIED:-false}" == "true" ]]; then
        log_info "Listener CPU already boosted by run_tests() — skipping duplicate boost"
    else
        log_warning "Listener CPU boost disabled (--listener-cpu none) — results will reflect chart defaults"
    fi

    # Start metrics collection if requested (also sets up unified output dir)
    start_metrics_collection

    # Build pytest arguments for performance tests
    local perf_args=("--performance")
    if [[ "${VERBOSE}" == "true" ]]; then
        perf_args+=("-v")
    fi
    # Show stdout for visibility into long-running tests
    perf_args+=("-s")

    # Determine output location based on whether we have unified output setup
    local output_info="tests/reports/performance/"
    if [[ -n "${TEST_RUN_ID:-}" ]]; then
        output_info="${PERF_OUTPUT_DIR}/${TEST_RUN_ID}/"
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would execute: ${pytest_script} ${perf_args[*]}"
        stop_metrics_collection
        return 0
    fi

    log_info "Performance test command: ${pytest_script} ${perf_args[*]}"
    log_info "Output directory: ${output_info}"

    local test_result=0
    if ! "${pytest_script}" "${perf_args[@]}"; then
        log_error "Performance tests failed"
        log_info "Check ${output_info} for detailed results"
        test_result=1
    else
        log_success "Performance tests completed"
        log_info "Results: ${output_info}"
    fi

    # Stop metrics collection and upload if requested
    stop_metrics_collection
    upload_perf_results_to_s3

    # Generate visual run report if we have a structured output directory
    if [[ -n "${TEST_RUN_ID:-}" ]]; then
        local run_dir="${PERF_OUTPUT_DIR}/${TEST_RUN_ID}"
        local scripts_dir="$(dirname "${BASH_SOURCE[0]}")/observability"

        local run_report_script="${scripts_dir}/generate-perf-run-report.py"
        if [[ -f "${run_report_script}" ]] && command -v python3 &>/dev/null; then
            log_info "Generating visual run report..."
            if python3 "${run_report_script}" --run-dir "${run_dir}" 2>/dev/null; then
                log_success "Visual report: ${run_dir}/reports/perf-run-report.html"
            else
                log_warning "Could not generate visual run report (non-fatal)"
            fi
        fi

        # Link Grafana dashboard if available (non-fatal if cluster is down or Grafana not deployed)
        local grafana_script="${scripts_dir}/push-grafana-snapshot.py"
        if [[ -f "${grafana_script}" ]] && command -v python3 &>/dev/null; then
            log_info "Linking Grafana dashboard (if cluster is up)..."
            if python3 "${grafana_script}" \
                --run-dir "${run_dir}" \
                ${GRAFANA_URL:+--grafana-url "${GRAFANA_URL}"} \
                ${GRAFANA_USER:+--grafana-user "${GRAFANA_USER}"} \
                ${GRAFANA_PASSWORD:+--grafana-pass "${GRAFANA_PASSWORD}"} \
                --namespace "${GRAFANA_NAMESPACE:-grafana}" 2>/dev/null; then
                local links_file="${run_dir}/reports/grafana-links.json"
                if [[ -f "${links_file}" ]]; then
                    local snap_url
                    snap_url=$(python3 -c "import json; d=json.load(open('${links_file}')); print(d.get('snapshot_url',''))" 2>/dev/null)
                    [[ -n "${snap_url}" ]] && log_success "Grafana snapshot: ${snap_url}"
                fi
            fi
        fi
    fi

    return ${test_result}
}

################################################################################
# IQE Test Execution
################################################################################

run_iqe_tests() {
    log_step "Running IQE cost-management tests"
    
    local iqe_script="${LOCAL_SCRIPTS_DIR}/run-iqe-tests.sh"
    if [[ ! -x "${iqe_script}" ]]; then
        log_error "IQE test runner not found at: ${iqe_script}"
        return 1
    fi
    
    log_info "Running IQE tests with profile: ${IQE_PROFILE}, marker: ${IQE_MARKER}"
    
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would execute: ${iqe_script} --namespace ${NAMESPACE} --profile ${IQE_PROFILE} --marker '${IQE_MARKER}'"
        return 0
    fi
    
    # Export environment variables for IQE script
    export NAMESPACE="${NAMESPACE}"
    export HELM_RELEASE_NAME="${HELM_RELEASE_NAME:-cost-onprem}"
    export IQE_MARKER="${IQE_MARKER}"
    
    local test_result=0
    if ! "${iqe_script}" --namespace "${NAMESPACE}" --profile "${IQE_PROFILE}" --marker "${IQE_MARKER}"; then
        log_error "IQE tests failed"
        log_info "IQE JUnit report available at: tests/reports/iqe_junit.xml"
        log_info "IQE output log available at: tests/reports/iqe_output.log"
        test_result=1
    else
        log_success "IQE tests completed"
    fi
    
    return ${test_result}
}

################################################################################
# Version tracking
################################################################################

save_version_info() {
    log_step "Saving deployment version information"
    
    local check_components_script="${LOCAL_SCRIPTS_DIR}/qe/check-components.sh"
    
    if [[ ! -x "${check_components_script}" ]]; then
        log_warning "check-components.sh not found at: ${check_components_script}"
        log_warning "Skipping version info generation"
        return 0
    fi
    
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY RUN: Would execute: MODE=deployment-info NAMESPACE=${NAMESPACE} ${check_components_script}"
        log_info "DRY RUN: Would save version info to: ${VERSION_INFO_FILE}"
        return 0
    fi
    
    # Export environment variables for check-components.sh
    export NAMESPACE="${NAMESPACE}"
    export HELM_RELEASE_NAME="${HELM_RELEASE_NAME:-cost-onprem}"
    export VERSION_INFO_FILE="${VERSION_INFO_FILE}"
    export MODE="deployment-info"
    
    if "${check_components_script}"; then
        log_success "Version info saved to: ${VERSION_INFO_FILE}"
        
        # Display summary if verbose
        if [[ "${VERBOSE}" == "true" ]] && [[ -f "${VERSION_INFO_FILE}" ]]; then
            log_verbose "Version info contents:"
            cat "${VERSION_INFO_FILE}"
        fi
    else
        log_warning "Failed to generate version info"
    fi
}

################################################################################
# Main deployment workflow
################################################################################

print_summary() {
    echo ""

    # Show execution mode
    if [[ "${PERF_ONLY}" == "true" ]]; then
        log_info "Mode: Performance-only (--perf-only)"
    elif [[ "${TESTS_ONLY}" == "true" ]] && [[ "${SKIP_TEST}" == "true" ]] && [[ "${RUN_IQE}" == "true" ]]; then
        log_info "Mode: IQE-only (--iqe-only)"
    elif [[ "${TESTS_ONLY}" == "true" ]]; then
        log_info "Mode: Tests-only (--skip-deploy)"
    else
        log_info "Mode: Full deployment"
    fi

    log_info "Deployment Configuration:"
    echo "  Namespace:           ${NAMESPACE}"
    [[ "${DEPLOY_S4}" == "true" ]] && echo "  S4 Namespace:        ${S4_NAMESPACE}"
    [[ "${OPENSHIFT_VALUES_FILE}" != "openshift-values.yaml" ]] && echo "  Values File:         ${OPENSHIFT_VALUES_FILE}"
    echo "  Use Local Chart:     ${USE_LOCAL_CHART}"
    [[ "${USE_HELM_DEVEL}" == "true" ]] && echo "  Include Pre-release: ${USE_HELM_DEVEL}"
    [[ -n "${CHART_VERSION}" ]] && echo "  Chart Version:       ${CHART_VERSION}"
    echo ""
    log_info "Steps to execute:"
    [[ "${SKIP_RHBK}" == "false" ]] && echo "  ✓ Deploy Red Hat Build of Keycloak (RHBK)" || echo "  ✗ Deploy RHBK (SKIPPED)"
    [[ "${SKIP_KAFKA}" == "false" ]] && echo "  ✓ Deploy Kafka/AMQ Streams" || echo "  ✗ Deploy Kafka/AMQ Streams (SKIPPED)"
    [[ "${DEPLOY_S4}" == "true" ]] && echo "  ✓ Deploy S4 Storage (namespace: ${S4_NAMESPACE})" || echo "  ✗ Deploy S4 Storage (OPTIONAL)"
    [[ "${SKIP_HELM}" == "false" ]] && echo "  ✓ Deploy Cost On-Prem Helm Chart" || echo "  ✗ Deploy Cost On-Prem Helm Chart (SKIPPED)"
    [[ "${SKIP_TLS}" == "false" ]] && echo "  ✓ Setup TLS Certificates" || echo "  ✗ Setup TLS Certificates (SKIPPED)"
    [[ "${SKIP_TEST}" == "false" ]] && echo "  ✓ Run Chart Tests" || echo "  ✗ Run Chart Tests (SKIPPED)"
    if [[ "${DEPLOY_OBSERVABILITY}" == "true" ]]; then
        echo "  ✓ Deploy Observability (postgres_exporter, valkey-exporter)"
    else
        echo "  ✗ Deploy Observability (OPTIONAL)"
    fi
    if [[ "${PERF_ONLY}" == "true" ]] || [[ "${RUN_PERF}" == "true" ]]; then
        local perf_opts="profile: ${PERF_PROFILE}"
        [[ "${COLLECT_METRICS}" == "true" ]] && perf_opts="${perf_opts}, collect-metrics: ${METRICS_INTERVAL}s"
        [[ "${UPLOAD_METRICS}" == "true" ]] && perf_opts="${perf_opts}, upload-to-s3"
        echo "  ✓ Run Performance Tests (${perf_opts})"
    else
        echo "  ✗ Run Performance Tests (OPTIONAL)"
    fi
    if [[ "${RUN_IQE}" == "true" ]]; then
        local iqe_opts="profile: ${IQE_PROFILE}, marker: ${IQE_MARKER}"
        [[ -n "${LISTENER_CPU_LIMIT}" ]] && iqe_opts="${iqe_opts}, listener-cpu: ${LISTENER_CPU_LIMIT}"
        echo "  ✓ Run IQE Tests (${iqe_opts})"
    else
        echo "  ✗ Run IQE Tests (OPTIONAL)"
    fi
    echo ""
}

print_completion() {
    echo ""
    log_success "Deployment completed successfully"
    echo ""
    log_info "Cost On-Prem with JWT authentication deployed to namespace: ${NAMESPACE}"
    echo ""
    log_info "Next steps:"
    echo "  1. Verify: oc get pods -n ${NAMESPACE}"
    echo "  2. Check route: oc get route -n ${NAMESPACE}"
    echo "  3. View logs: oc logs -n ${NAMESPACE} -l app.kubernetes.io/component=ingress -f"
    echo ""
}

main() {
    echo ""
    echo -e "${CYAN}Cost On-Prem OpenShift with JWT Authentication Deployment${NC}"
    echo ""

    # Parse command line arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --skip-rhbk)
                SKIP_RHBK=true
                shift
                ;;
            --skip-kafka)
                SKIP_KAFKA=true
                shift
                ;;
            --skip-helm)
                SKIP_HELM=true
                shift
                ;;
            --skip-tls)
                SKIP_TLS=true
                shift
                ;;
            --skip-test|--skip-chart-tests)
                SKIP_TEST=true
                shift
                ;;
            --deploy-s4)
                DEPLOY_S4=true
                shift
                ;;
            --s4-namespace)
                S4_NAMESPACE="$2"
                shift 2
                ;;
            --namespace)
                NAMESPACE="$2"
                shift 2
                ;;
            --image-tag)
                IMAGE_TAG="$2"
                shift 2
                ;;
            --use-local-chart)
                USE_LOCAL_CHART=true
                shift
                ;;
            --devel)
                USE_HELM_DEVEL=true
                shift
                ;;
            --chart-version)
                CHART_VERSION="$2"
                shift 2
                ;;
            --verbose)
                VERBOSE=true
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --tests-only|--skip-deploy)
                TESTS_ONLY=true
                shift
                ;;
            --include-ui)
                INCLUDE_UI=true
                shift
                ;;
            --run-perf)
                RUN_PERF=true
                shift
                ;;
            --perf-profile)
                PERF_PROFILE="$2"
                shift 2
                ;;
            --perf-only)
                PERF_ONLY=true
                SKIP_RHBK=true
                SKIP_KAFKA=true
                SKIP_HELM=true
                SKIP_TLS=true
                SKIP_TEST=true
                RUN_PERF=true
                shift
                ;;
            --deploy-observability)
                DEPLOY_OBSERVABILITY=true
                shift
                ;;
            --collect-metrics)
                COLLECT_METRICS=true
                shift
                ;;
            --upload-metrics)
                UPLOAD_METRICS=true
                shift
                ;;
            --metrics-interval)
                METRICS_INTERVAL="$2"
                shift 2
                ;;
            --run-iqe)
                RUN_IQE=true
                shift
                ;;
            --iqe-only)
                TESTS_ONLY=true
                SKIP_TEST=true
                RUN_IQE=true
                shift
                ;;
            --iqe-marker)
                IQE_MARKER="$2"
                shift 2
                ;;
            --iqe-profile)
                IQE_PROFILE="$2"
                shift 2
                ;;
            --listener-cpu)
                LISTENER_CPU_LIMIT="$2"
                shift 2
                ;;
            --save-versions)
                SAVE_VERSIONS=true
                # Check if next argument is a file path (not another flag)
                if [[ -n "${2:-}" ]] && [[ ! "$2" =~ ^-- ]]; then
                    VERSION_INFO_FILE="$2"
                    shift
                fi
                shift
                ;;
            --help|-h)
                show_help
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Use --help for usage information"
                exit 1
                ;;
        esac
    done

    # Validate flag combinations
    if [[ "${USE_LOCAL_CHART}" == "true" ]]; then
        if [[ "${USE_HELM_DEVEL}" == "true" ]]; then
            log_error "Cannot use --devel with --use-local-chart"
            exit 1
        fi
        if [[ -n "${CHART_VERSION}" ]]; then
            log_error "Cannot use --chart-version with --use-local-chart"
            exit 1
        fi
    fi

    # In tests-only / skip-deploy mode, skip all deployment steps
    if [[ "${TESTS_ONLY}" == "true" ]]; then
        SKIP_RHBK=true
        SKIP_KAFKA=true
        SKIP_HELM=true
        SKIP_TLS=true
    fi

    # Show deployment summary
    print_summary

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_warning "DRY RUN MODE: No changes will be made"
        echo ""
    fi

    # Execute deployment steps
    check_prerequisites
    check_oc_connection
    if [[ "${TESTS_ONLY}" != "true" ]]; then
        create_namespace
    fi

    deploy_rhbk
    deploy_kafka
    deploy_s4

    # Run Helm sanity test before deploying complex chart
    if [[ "${SKIP_HELM}" == "false" ]] && [[ "${DRY_RUN}" == "false" ]]; then
        log_info "Running Helm sanity test to verify basic functionality..."
        if ! bash "${SCRIPT_DIR}/helm-sanity-test.sh"; then
            log_error "Helm sanity test failed - aborting deployment"
            exit 1
        fi
    fi

    deploy_helm_chart
    setup_tls
    
    # Deploy observability after Helm chart so services exist for auto-detection
    deploy_observability
    
    # Run tests and capture result (don't exit on failure)
    local test_result=0
    run_tests || test_result=$?

    # Save version information if requested
    if [[ "${SAVE_VERSIONS}" == "true" ]]; then
        save_version_info
    fi

    # Print completion message
    if [[ "${DRY_RUN}" == "false" ]]; then
        if [[ "${test_result}" -eq 0 ]]; then
            print_completion
        else
            echo ""
            log_error "Deployment completed but tests failed (exit code: ${test_result})"
            echo ""
        fi
    else
        echo ""
        log_info "DRY RUN completed. No changes were made."
        echo ""
    fi

    exit ${test_result}
}

# Run main function
main "$@"