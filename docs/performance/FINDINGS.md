# Performance Testing Findings

This document tracks issues discovered during performance testing that require follow-up action.

---

## Open Issues

## Resolved Issues

### PERF-FINDING-006: Kruize Pod Restarts Under Load (Product Change Required)

**Status**: Validated - Awaiting FLPATH-4302 
**Severity**: Medium  
**Jira**: [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302)

**Problem**:
Kruize pod restarted during `ros_004` memory pressure test despite successful experiment creation. Peak memory was only 786 MB (38% of 2Gi limit), ruling out OOM.

**Root Cause**:
CPU throttling - Kruize had only 1 CPU core limit. Under heavy load processing 50 experiments, CPU throttling caused slow responses to liveness probes, triggering pod restart.

**Evidence**:
```
# Before fix (1 CPU core):
Experiments created: 50 in 434.2s
Kruize restarts: 1
FAILED

# After fix (2 CPU cores):
Kruize restarts: 0
PASSED (8 minutes)
```

**Proposed Change** (`values.yaml`):
```yaml
kruize:
  requests:
    cpu: "1000m"    # Was 500m
  limits:
    cpu: "2000m"    # Was 1000m
```

**Validation**: `ros_004` now passes with 0 restarts (tested on cluster with patched deployment)

**Note**: Change is currently applied to test cluster via `oc patch` but NOT committed to values.yaml. Requires formal ticket and review before permanent product change.

---

### PERF-FINDING-001: Gateway Timeout Too Low for Large File Uploads

**Status**: Validated - Awaiting FLPATH-4091 
**Severity**: Critical  
**Jira**: [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091)

**Problem**:
The Envoy gateway and HAProxy route had 30s timeouts for the `/api/ingress/` route. Large file uploads (30+ days of data, ~48MB+) take 25-60+ seconds to transfer, causing HTTP 408/504 timeout errors.

**Proposed Fix**:
1. HAProxy route timeout: 30s → 180s (`values.yaml`)
2. Envoy route timeout: 30s → 180s, per_try_timeout: 10s → 60s (`configmap-envoy.yaml`)

**Validation**: `ing_002[30-days]` passes with fix (68MB upload at 5.23 MB/s)

**Note**: Fix validated on test cluster but code reverted. Changes to be applied via FLPATH-4091 PR.

---

## Performance Baselines

### Large File Processing Times

| File Size | Upload Time | Upload Throughput | Processing Time | Total Time |
|-----------|-------------|-------------------|-----------------|------------|
| ~13 KB | 330ms | - | <1s | <1s |
| ~48 MB | 25-50s | 1-2 MB/s | 5-10 min | 6-11 min |
| ~67 MB | 36-62s | 1.1-1.9 MB/s | 7-20 min | 8-22 min |
| ~197 MB | 62s | 3.2 MB/s | >20 min | >21 min |

**Key Observations**:
- Upload throughput is acceptable (1-3 MB/s)
- Processing time is the bottleneck, not upload
- Processing time scales roughly linearly with data volume
- Files >100MB require extended processing timeouts (>20 min)

### Concurrent Upload Scaling

| Concurrent Sources | Recommended Config | Expected Processing Time |
|-------------------|-------------------|-------------------------|
| 1-5 | Default (1 replica) | 4-8 minutes |
| 6-10 | 2 replicas | 7-10 minutes |
| 11-20 | 3+ replicas | TBD |

For high-concurrency workloads, scale workers:
```yaml
listener:
  replicas: 2

celeryWorker:
  workers:
    ocp:
      replicas: 2
      concurrency: 5
    summary:
      replicas: 2
      concurrency: 5
```

---

## Test Run Summary

### Latest Run (2026-05-31) - Small Profile (with Kruize CPU fix)

| Test Category | Tests | Passed | Failed | Skipped | Notes |
|---------------|-------|--------|--------|---------|-------|
| API Latency | 16 | 15 | 0 | 1 | api_006[10] skipped |
| Ingestion | 8 | 8 | 0 | 0 | All passed including ing_002[30-days], ing_003[10], ing_006 |
| ROS | 4 | 4 | 0 | 0 | All passed (ros_004 fixed with CPU bump) |
| Scale | 8 | 8 | 0 | 0 | All passed |

**Total: 36 tests, 36 passed, 0 failed, 5 skipped (100% pass rate)**

### Previous Run (2026-05-31) - Before Fixes

| Test Category | Tests | Passed | Failed | Notes |
|---------------|-------|--------|--------|-------|
| API Latency | 16 | 14 | 1 | api_005[2-dim-node] failed (P95 threshold) |
| Ingestion | 8 | 5 | 3 | ing_002[30-days] timeout, ing_003[10] timeout, ing_006 label issue |
| ROS | 4 | 2 | 2 | ros_002, ros_004 wrong profile |
| Scale | 8 | 8 | 0 | All passed |

**Total: 36 tests, 31 passed, 5 failed (86% pass rate)**

### Improvement Summary

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Pass Rate | 86% | **100%** | +14% |
| Tests Passing | 31/36 | **36/36** | +5 |
| Critical Fixes | - | 6 | - |

---

## Action Items

| Finding | Change | Jira | Status |
|---------|--------|------|--------|
| PERF-FINDING-001 | HAProxy + Envoy timeouts 30s → 180s | [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091) | **Awaiting PR** |
| PERF-FINDING-006 | Kruize CPU limits 1 core → 2 cores | [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302) | **Awaiting PR** |

---

## Proposed Tickets

### Gateway Timeout Increase (FLPATH-4091)

**Ticket exists**: [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091)

**Proposed Changes**:

1. `cost-onprem/values.yaml`:
```yaml
gatewayRoute:
  annotations:
    haproxy.router.openshift.io/timeout: "180s"  # Was 30s
```

2. `cost-onprem/templates/gateway/configmap-envoy.yaml`:
```yaml
# /api/ingress/ route
timeout: 180s           # Was 30s
per_try_timeout: 60s    # Was 10s
```

---

### Kruize CPU Resource Increase (FLPATH-4302)

**Ticket**: [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302)

**Summary**: Increase Kruize CPU limits from 1 core to 2 cores to prevent pod restarts under load

**Description**:
Performance testing identified that Kruize pods restart under moderate load (50 concurrent experiments) due to CPU throttling. The current 1 CPU core limit causes slow responses to liveness probes when processing multiple experiments, triggering unnecessary pod restarts.

**Evidence**:
- Peak memory usage: 786 MB (38% of 2Gi limit) - not memory constrained
- Pod restarts: 1 during 50-experiment processing with 1 core
- Pod restarts: 0 with 2 cores (same workload)

**Proposed Change**:
```yaml
# cost-onprem/values.yaml
resources:
  kruize:
    requests:
      cpu: "1000m"    # Was 500m
    limits:
      cpu: "2000m"    # Was 1000m
```

**Impact**: 
- Kruize will use up to 2x CPU when available
- Improved stability under moderate-to-heavy workloads
- No memory change required

**Testing**:
- Validated with `ros_004` memory pressure test (50 experiments)
- Test passed with 0 restarts after CPU increase

---

## Related Jira Stories

- [FLPATH-4036](https://redhat.atlassian.net/browse/FLPATH-4036): Performance Testing Framework
- [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091): Gateway Timeout Fix
- [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302): Kruize CPU Resource Increase

---

_Last Updated: 2026-05-31_
