#!/bin/bash
#
# Sync IQE tests marked with @pytest.mark.cost_ocp_on_prem
#
# This script finds all IQE test files containing the cost_ocp_on_prem marker
# and copies them to our iqe_compat test suite.
#
# Usage:
#   ./scripts/sync-iqe-tests.sh [OPTIONS] [IQE_REPO_PATH]
#
# Options:
#   --strict    Only copy files with no cloud provider dependencies (default)
#   --relaxed   Copy all files with cost_ocp_on_prem marker (some tests may fail)
#
# Arguments:
#   IQE_REPO_PATH - Path to iqe-cost-management-plugin repo (default: ../iqe-cost-management-plugin)
#
# Example:
#   ./scripts/sync-iqe-tests.sh /path/to/iqe-cost-management-plugin
#   ./scripts/sync-iqe-tests.sh --relaxed /path/to/iqe-cost-management-plugin
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_DIR="${REPO_ROOT}/tests/suites/iqe_compat/imported"

# Parse options
STRICT_MODE=true
IQE_REPO=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --strict)
            STRICT_MODE=true
            shift
            ;;
        --relaxed)
            STRICT_MODE=false
            shift
            ;;
        *)
            IQE_REPO="$1"
            shift
            ;;
    esac
done

# Default IQE repo path (sibling directory)
IQE_REPO="${IQE_REPO:-${REPO_ROOT}/../iqe-cost-management-plugin}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Verify IQE repo exists
if [[ ! -d "${IQE_REPO}" ]]; then
    log_error "IQE repo not found at: ${IQE_REPO}"
    echo "Please provide the path to iqe-cost-management-plugin:"
    echo "  $0 /path/to/iqe-cost-management-plugin"
    exit 1
fi

IQE_TESTS_DIR="${IQE_REPO}/iqe_cost_management/tests"

if [[ ! -d "${IQE_TESTS_DIR}" ]]; then
    log_error "IQE tests directory not found at: ${IQE_TESTS_DIR}"
    exit 1
fi

log_info "IQE repo: ${IQE_REPO}"
log_info "Target directory: ${TARGET_DIR}"
log_info "Mode: $([ "${STRICT_MODE}" == "true" ] && echo "strict" || echo "relaxed")"

# Create target directory
mkdir -p "${TARGET_DIR}"

# Find all test files with cost_ocp_on_prem marker
log_info "Finding tests marked with @pytest.mark.cost_ocp_on_prem..."

FOUND_FILES=$(grep -r "@pytest.mark.cost_ocp_on_prem" "${IQE_TESTS_DIR}" --include="*.py" -l 2>/dev/null || true)

if [[ -z "${FOUND_FILES}" ]]; then
    log_warn "No test files found with @pytest.mark.cost_ocp_on_prem marker"
    exit 0
fi

# Count files
FILE_COUNT=$(echo "${FOUND_FILES}" | wc -l | tr -d ' ')
log_info "Found ${FILE_COUNT} test files with cost_ocp_on_prem marker"

# Track statistics
COPIED=0
SKIPPED=0
SKIPPED_FILES=""

# Copy each file
for src_file in ${FOUND_FILES}; do
    # Get relative path from tests directory
    rel_path="${src_file#${IQE_TESTS_DIR}/}"
    filename=$(basename "${src_file}")
    
    # Determine target filename (flatten directory structure, prefix with source dir)
    # e.g., rest_api/v1/test__ocp_cost_reports.py -> test_restapi_v1_ocp_cost_reports.py
    dir_prefix=$(dirname "${rel_path}" | tr '/' '_')
    if [[ "${dir_prefix}" == "." ]]; then
        target_filename="${filename}"
    else
        # Remove leading test_ or test__ and add prefix
        clean_name="${filename#test_}"
        clean_name="${clean_name#_}"
        target_filename="test_${dir_prefix}_${clean_name}"
    fi
    
    target_file="${TARGET_DIR}/${target_filename}"
    
    # Check if file contains unsupported features
    SKIP_REASON=""
    
    # Always skip UI tests
    if grep -q "from iqe_cost_management.tests.ui\|@pytest.mark.ui" "${src_file}" 2>/dev/null; then
        SKIP_REASON="UI test"
    fi
    
    # Always skip stage-only tests
    if grep -q "stage_only\|prod_only" "${src_file}" 2>/dev/null; then
        SKIP_REASON="stage/prod only test"
    fi
    
    # In strict mode, apply additional filters
    if [[ "${STRICT_MODE}" == "true" && -z "${SKIP_REASON}" ]]; then
        # Check for fixtures we don't support yet
        if grep -q "cost_ocp_source_0\|cost_ocp_ros_source\|cost_ocp_on_aws" "${src_file}" 2>/dev/null; then
            SKIP_REASON="requires data fixtures (cost_ocp_source_*)"
        fi
        
        # Check for AWS/Azure/GCP specific imports (not supported in on-prem)
        if grep -q "aws_ec2_helpers\|azure_helpers\|gcp_helpers\|cost_aws_\|cost_azure_\|cost_gcp_" "${src_file}" 2>/dev/null; then
            SKIP_REASON="requires cloud provider helpers (AWS/Azure/GCP)"
        fi
        
        # Check for pandas (not in our dependencies)
        if grep -q "import pandas\|from pandas" "${src_file}" 2>/dev/null; then
            SKIP_REASON="requires pandas (not in dependencies)"
        fi
        
        # Check for complex IQE fixtures we don't support
        if grep -q "cost_currency_exchange_rates\|cost_tag_settings\|cost_category_settings" "${src_file}" 2>/dev/null; then
            SKIP_REASON="requires complex IQE fixtures"
        fi
    fi
    
    if [[ -n "${SKIP_REASON}" ]]; then
        log_warn "Skipping ${rel_path}: ${SKIP_REASON}"
        SKIPPED=$((SKIPPED + 1))
        SKIPPED_FILES="${SKIPPED_FILES}\n  - ${rel_path}: ${SKIP_REASON}"
        continue
    fi
    
    # Copy the file
    cp "${src_file}" "${target_file}"
    log_success "Copied: ${rel_path} -> ${target_filename}"
    COPIED=$((COPIED + 1))
done

# Create __init__.py if it doesn't exist
if [[ ! -f "${TARGET_DIR}/__init__.py" ]]; then
    cat > "${TARGET_DIR}/__init__.py" << 'EOF'
"""
Imported IQE Tests.

This directory contains test files automatically synced from the
iqe-cost-management-plugin repository. These tests are marked with
@pytest.mark.cost_ocp_on_prem and are compatible with cost-onprem.

DO NOT EDIT THESE FILES DIRECTLY - they will be overwritten on next sync.

To sync tests, run:
    ./scripts/sync-iqe-tests.sh

To run these tests:
    NAMESPACE=cost-onprem ./scripts/run-pytest.sh -m cost_ocp_on_prem
"""
EOF
fi

# Create a manifest file
MANIFEST_FILE="${TARGET_DIR}/MANIFEST.md"
cat > "${MANIFEST_FILE}" << EOF
# IQE Test Sync Manifest

**Last synced:** $(date -u +"%Y-%m-%d %H:%M:%S UTC")
**Source:** ${IQE_REPO}
**Marker:** @pytest.mark.cost_ocp_on_prem

## Statistics

- **Total files found:** ${FILE_COUNT}
- **Files copied:** ${COPIED}
- **Files skipped:** ${SKIPPED}

## Copied Files

EOF

# List copied files
for f in "${TARGET_DIR}"/test_*.py; do
    if [[ -f "$f" ]]; then
        echo "- $(basename "$f")" >> "${MANIFEST_FILE}"
    fi
done

if [[ ${SKIPPED} -gt 0 ]]; then
    cat >> "${MANIFEST_FILE}" << EOF

## Skipped Files

The following files were skipped because they require features not yet supported:
$(echo -e "${SKIPPED_FILES}")

EOF
fi

cat >> "${MANIFEST_FILE}" << EOF

## Running Tests

\`\`\`bash
# Run all imported IQE tests
NAMESPACE=cost-onprem ./scripts/run-pytest.sh suites/iqe_compat/imported -v

# Run only cost_ocp_on_prem marked tests
NAMESPACE=cost-onprem ./scripts/run-pytest.sh -m cost_ocp_on_prem -v
\`\`\`

## Notes

- These tests are automatically synced from IQE and should not be edited directly
- Some tests may fail due to API differences between cloud and on-prem
- Tests requiring data fixtures (cost_ocp_source_*) are skipped until we implement data setup
EOF

echo ""
log_info "=========================================="
log_info "Sync Complete"
log_info "=========================================="
log_info "Files copied: ${COPIED}"
log_info "Files skipped: ${SKIPPED}"
log_info "Target directory: ${TARGET_DIR}"
log_info "Manifest: ${MANIFEST_FILE}"
echo ""
log_info "To run the imported tests:"
echo "  NAMESPACE=cost-onprem ./scripts/run-pytest.sh suites/iqe_compat/imported -v"
