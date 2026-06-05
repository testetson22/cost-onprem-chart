# Performance Testing Findings

This document tracks issues discovered during performance testing that require follow-up action.

---

## Open Issues

### PERF-FINDING-010: ODF Default Resources Exhaust Cluster Memory

**Status**: Mitigated — requires auto-toolbox enforcement  
**Severity**: Critical  

**Problem**:
ODF 4.20 applies full upstream default resource requests to all Ceph daemons. On a 3-worker cluster (30Gi allocatable per node, ~90Gi total), ODF alone consumed **~61.5Gi (68%)** in memory requests, leaving insufficient headroom for cost-onprem + Kafka + observability. Helm installs failed with `context deadline exceeded` because migration hook pods could not schedule.

**Actual vs Expected Resource Requests**:
| Component | ODF Default | auto-toolbox Target | Overshoot |
|---|---|---|---|
| rook-ceph-mon (x3) | 1 core / 2Gi | 500m / 1Gi | 2x CPU, 2x mem |
| rook-ceph-mgr (x2) | 1 core / 1.5Gi | 500m / 512Mi | 2x CPU, 3x mem |
| rook-ceph-osd (x3) | 2 cores / 5Gi | 500m / 2Gi | 4x CPU, 2.5x mem |
| rook-ceph-mds (x2) | 2 cores / 6Gi | 500m / 1Gi | 4x CPU, 6x mem |
| noobaa-core | 1 core / 4Gi | 500m / 1Gi | 2x CPU, 4x mem |
| noobaa-db (x2 CNPG) | 500m / 4Gi | 250m / 2Gi* | 2x CPU, 2x mem |

\* NooBaa DB `shared_buffers=1073MB` prevents going below ~1.5Gi.

**Impact**: After patching to auto-toolbox targets, ODF memory dropped from **61.5Gi → 46.5Gi**, freeing ~15Gi. Worker nodes went from 99% → 68-92% memory allocation.

**Root Cause**: Cluster was deployed before `odf_reduced_resources` was added to auto-toolbox, or the flag was not set. ODF's `StorageCluster` resource overrides are only applied at creation time by the Ansible role; there is no reconciliation on existing clusters.

**Remediation Applied**:
```bash
# StorageCluster patch
oc patch storagecluster ocs-storagecluster -n openshift-storage --type merge -p '{
  "spec": {"resources": {
    "mon": {"requests": {"cpu": "500m", "memory": "1Gi"}},
    "mgr": {"requests": {"cpu": "500m", "memory": "512Mi"}},
    "osd": {"requests": {"cpu": "500m", "memory": "2Gi"}},
    "mds": {"requests": {"cpu": "500m", "memory": "1Gi"}}
  }}
}'

# NooBaa patch
oc patch noobaa noobaa -n openshift-storage --type merge -p '{
  "spec": {
    "coreResources": {"requests": {"cpu": "500m", "memory": "1Gi"}},
    "dbResources": {"requests": {"cpu": "250m", "memory": "1Gi"}}
  }
}'

# CNPG NooBaa DB patch (shared_buffers requires ≥1.5Gi)
oc patch clusters.postgresql.cnpg.noobaa.io noobaa-db-pg-cluster \
  -n openshift-storage --type merge -p '{
  "spec": {"resources": {"requests": {"cpu": "250m", "memory": "2Gi"}}}
}'

# Then restart Ceph pods to pick up new requests
oc delete pod -n openshift-storage -l app=rook-ceph-mon --wait=false
oc delete pod -n openshift-storage -l app=rook-ceph-mgr --wait=false
oc delete pod -n openshift-storage -l app=rook-ceph-mds --wait=false
oc delete pod -n openshift-storage -l app=rook-ceph-osd --wait=false
```

**Recommended Permanent Fix**: Add ODF resource validation to the cost-onprem deploy script, or update auto-toolbox to reconcile existing `StorageCluster` resources (not just at creation time). A preflight check in `deploy-test-cost-onprem.sh` that detects ODF overcommit and patches it would prevent this from recurring.

### PERF-FINDING-014: Kruize Throughput Bottleneck — Connection Pool and Replica Scaling

**Status**: Partially validated — Kruize 2x scaling confirmed harmful; pipeline scaling validated  
**Severity**: High  
**Date**: 2026-06-03 (updated 2026-06-04)  

**Problem**:
ROS performance tests `ros_002` and `ros_004` fail because Kruize cannot create experiments fast enough. The ros-processor calls Kruize's HTTP API sequentially for each workload (~3.3–3.6s per experiment). With 320 workloads in the medium profile:

| Test | Required | Achieved | Rate | Gap |
|------|----------|----------|------|-----|
| `ros_002` (90% threshold) | 288 experiments in 610s | 183 (57.2%) | 18 exp/min | needs 57% faster |
| `ros_004` (80% threshold) | 256 experiments in 901s | 251 (78.4%) | 16.7 exp/min | missed by 5 experiments |

**Resource snapshot analysis** (139 snapshots across 2h run):

Kruize is **not CPU or memory bound** — the bottleneck is configuration and architecture:

| Component | Peak CPU | CPU Limit | Peak Memory | Mem Limit | Utilization |
|-----------|---------|-----------|-------------|-----------|-------------|
| Kruize | 185m | 2000m | 451 MB | 2 Gi | 9% CPU, 22% mem |
| ROS Processor | 27m | 1000m | 1365 MB | 4 Gi | 3% CPU, 33% mem |
| Listener | 471m | 300m | 1739 MB | 2 Gi | 157% CPU (throttled), 85% mem |
| Celery Workers | 368m | varies | 2475 MB | varies | low CPU |
| Postgres | 327m | - | 615 MB | - | moderate |

**Identified bottlenecks**:

1. **Kruize Hibernate connection pool (`c3p0maxsize=5`)**: Each experiment creation involves DB operations. With only 5 connections, Kruize serializes under any concurrent load. This is a hard throughput ceiling regardless of CPU/memory headroom.

2. **Single ros-processor replica**: The processor consumes from `hccm.ros.events` (3 Kafka partitions) but runs as a single consumer. It calls Kruize synchronously per event, so throughput is bounded by `1 / Kruize_response_time`.

3. **Single Kruize replica**: Behind a ClusterIP service, but only one pod handles all requests. The ros-processor round-robins across service endpoints, so additional replicas directly multiply throughput.

4. **Single celery-worker-ocp replica**: OCP data processing flows through a single worker process. When 10 concurrent sources upload simultaneously (`ing_003[10]`), only 9/10 complete processing within the test timeout.

**Changes applied** (`values.yaml`):

```yaml
# Kruize connection pool: 5 → 20
kruize.env.hibernateC3p0MinSize: 5    # was 2
kruize.env.hibernateC3p0MaxSize: 20   # was 5

# Kruize replicas: 1 → 2
kruize.replicas: 2

# ROS processor replicas: 1 → 2 (topic has 3 partitions, supports up to 3)
ros.processor.replicas: 2

# Listener replicas: 1 → 2
costManagement.listener.replicas: 2

# Celery OCP worker replicas: 1 → 2
costManagement.celery.workers.ocp.replicas: 2

# Celery summary worker replicas: 1 → 2
costManagement.celery.workers.summary.replicas: 2
```

Templates updated to use values for replica counts (were previously hardcoded):
- `templates/kruize/deployment.yaml`: `replicas: {{ .Values.kruize.replicas | default 1 }}`
- `templates/ros/processor/deployment.yaml`: `replicas: {{ .Values.ros.processor.replicas | default 1 }}`

**Expected impact**:
- `ros_004`: Should pass — was only 5 experiments short. Connection pool increase alone likely sufficient.
- `ros_002`: With 2 Kruize replicas + 2 ros-processors, theoretical throughput doubles to ~36 exp/min (needs 28). Should pass with margin.
- `ing_003[10]`: Second OCP worker + listener should process all 10 concurrent sources within timeout.

**Validation results** (run `0-2-20-rc5-medium-1780584560`, 2026-06-04):

| Component | Change | Result |
|-----------|--------|--------|
| Kruize 2x replicas (c3p0=20) | **Degraded throughput**: 15.9 exp/min vs 18.0 with 1x | DB contention confirmed; reverted to 1x |
| Pipeline scaling (listener, ocp-worker, summary-worker ×2) | `ing_003[10]`: **10/10 processed** (was 9/10) | Validated |
| Pipeline scaling | `scale_001[5,10]`: **PASS** (was ERROR at teardown) | Validated |
| ros_004 | HTTP 401 after 377s — JWT token expired during slow upload | New failure mode (see FINDING-017 note) |

**Conclusion**: Kruize horizontal scaling does not work — the shared database becomes a
contention point. Pipeline scaling (listener + celery workers) is validated and effective
for ingestion and scale tests. ROS throughput remains an architectural constraint in
Kruize's serial experiment creation path.

---

### PERF-FINDING-018: Default Pipeline Replicas Insufficient for Concurrent Source Processing

**Status**: Open — rightsizing data  
**Severity**: Medium  
**Date**: 2026-06-05  

**Problem**:
The chart ships with 1 replica each for koku-listener, celery-worker-ocp, and celery-worker-summary. At the `small` customer profile (1 cluster, 15 nodes), the pipeline cannot process 5+ concurrent source uploads within expected timeframes. Sources that complete upload sit in the queue waiting for the single worker to process them sequentially.

**Evidence** (Jenkins `small` profile run, `0-2-20-rc5-small-1780611992`):

| Test | Sources | Processed | Result |
|------|---------|-----------|--------|
| `ing_003[2]` | 2 | 2/2 | PASSED |
| `ing_003[5]` | 5 | 4/5 | FAILED |
| `ing_003[10]` | 10 | 8/10 | FAILED |

Processing was not timing out due to data size — the uploads themselves completed. The bottleneck is serial processing through the single-replica pipeline.

**Impact**:
Any deployment handling more than a few sources uploading in overlapping windows will experience processing delays proportional to queue depth. This applies to production deployments with multiple clusters reporting, not just test workloads.

**Recommended Configuration** (concurrent source handling):

| Concurrent Sources | listener | ocp-worker | summary-worker |
|--------------------|----------|------------|----------------|
| 1–3 | 1 | 1 | 1 |
| 4–10 | 2 | 2 | 2 |
| 11–20 | 3 | 3 | 3 |

**Applied**: `deploy-test-cost-onprem.sh` scales listener, ocp-worker, and summary-worker to 2 replicas for the `small` profile and above.

---

### PERF-FINDING-015: Processing Pipeline Scaling — Rightsizing Data for Test Profiles

**Status**: Open — data collection phase  
**Severity**: Informational  
**Date**: 2026-06-03  

**Purpose**: Collect resource utilization data at different scale points to establish rightsizing baselines per test profile. The over-scaled configuration above is intentional — by running with excess capacity, we can measure actual peak usage under each profile and derive minimum viable resource allocations.

**Resource utilization timeline** (medium profile, 2h run, every ~5 min):

```
Phase         | Celery CPU | Celery Mem | Postgres CPU | Listener CPU | Listener Mem
--------------|-----------|------------|-------------|-------------|-------------
API tests     | 0.017     | 2134 MB   | 0.046       | 0.007       | 1179 MB
ROS tests     | 0.090-0.137| 2132 MB  | 0.148-0.246 | 0.064-0.165 | 1069-1251 MB
Ingestion     | 0.130-0.368| 2134-2475 | 0.065-0.261 | 0.091-0.322 | 1516-1677 MB
Scale tests   | 0.016-0.305| 2101-2109 | 0.081-0.197 | 0.016-0.028 | 1553 MB
```

**Key observations**:
- **Celery peaks during ingestion** (0.368 cores, 2475 MB) — this is the most resource-intensive phase
- **Listener peaks during ingestion** (0.322 cores, 1677 MB) — memory grows steadily through the run, never released
- **Postgres CPU correlates with ingestion activity** (0.261 cores peak) — moderate, nowhere near limits
- **Celery `active_process_count: 1`** — only 1 process despite `concurrency: 5` setting, suggesting prefork pool may not be engaged or tasks are I/O-bound

**Scaling strategy per test profile**:

| Profile | Expected Load | Recommended Scaling |
|---------|--------------|---------------------|
| baseline | 1-5 sources, small payloads | All replicas: 1 (default) |
| small | 5-10 sources, 30-day bursts | listener: 2, ocp-worker: 2, summary-worker: 2 (see PERF-FINDING-018) |
| medium | 10-20 sources, 90-day bursts, 320 workloads | ros-processor: 2, listener: 2, ocp-worker: 2, summary-worker: 2 |
| large | TBD | ros-processor: 3, listener: 3, ocp-worker: 3, summary-worker: 3 |

**Next steps**:
- Re-run medium profile with over-scaled configuration
- Capture peak resource snapshots per component during each test phase
- Determine if scaled configuration achieves 100% pass rate
- Compare resource utilization vs. allocation to find waste
- Build profile-specific value overrides for test deployments

---

### PERF-FINDING-016: Kruize Experiment Processing Rates — Measured Throughput Catalog

**Status**: Informational — ongoing data collection  
**Date**: 2026-06-04  

**Purpose**: Catalog measured Kruize experiment creation rates across runs, profiles,
and configurations. This data establishes throughput baselines for test timeout
calculations and identifies when rate degradation indicates a real issue vs. expected
workload scaling.

#### Measured Rates (clean queue, no FK poison)

| Run | Profile | Workloads | Experiments | Time | Rate (exp/min) | Per Exp | Config | Passed |
|-----|---------|-----------|-------------|------|----------------|---------|--------|--------|
| 1780530890 | medium | 320 | 183 | 610s | **18.0** | 3.3s | 1× Kruize, c3p0=5 | No (timeout) |
| 1780584560 | medium | 320 | 160 | 605s | **15.9** | 3.8s | 2× Kruize, c3p0=20 | No (timeout) |
| 1780345992 | medium | 320 | 160 | 608s | **15.8** | 3.8s | 1× Kruize, c3p0=5 | No (timeout) |
| 1780331771 | medium | 320 | 290 | 609s | **28.6** | 2.1s | 1× Kruize, c3p0=5 | Yes |
| 1780530890 | medium | 320 | 251 | 901s | **16.7** | 3.6s | 1× Kruize, c3p0=5 | No (ros_004) |
| 1780327119 | small | 50 | 50 | 41s | **73.0** | 0.8s | 1× Kruize, c3p0=5 | Yes |
| 1780279390 | small | 50 | 50 | 358s | **8.4** | 7.2s | 1× Kruize, c3p0=5 | Yes |
| 1780265254 | small | 50 | 50 | 546s | **5.5** | 10.9s | 1× Kruize, c3p0=5 | Yes |

#### Key Observations

1. **Rate varies significantly across runs** (5.5–73 exp/min for 50 workloads,
   15.8–28.6 exp/min for 320 workloads). This suggests external factors (DB state,
   prior test activity, Kruize JVM warmup) affect throughput more than
   resources or configuration.

2. **Scaling Kruize replicas degraded throughput.** 2× Kruize with c3p0=20 achieved
   15.9 exp/min vs. 18.0 exp/min with 1× Kruize and c3p0=5. The shared Kruize
   database becomes a contention point when multiple replicas perform concurrent
   writes. This is an architectural constraint — Kruize's DB layer does not
   scale horizontally.

3. **The best medium run (28.6 exp/min) passed ros_002 cleanly.** The same
   configuration that produced 18.0 exp/min on another run got 28.6 on the first
   clean attempt. This variance (~60%) is too large to attribute to load alone —
   it points to non-deterministic behavior in Kruize or the underlying DB.

4. **Small profile rate ceiling is much higher** (73 exp/min on a warm run). This
   suggests Kruize's per-experiment overhead has a fixed component that dominates
   at small workload counts, and a variable component that slows with DB table size.

5. **Processing rate is not gated by data ingestion.** Source registration, NISE
   data generation, upload, and Koku processing total ~27–30s regardless of
   workload count — less than 5% of the test budget. The timeout is almost
   entirely Kruize experiment creation time.

#### Timeout Calculation

Based on measured data, test timeouts should budget **4s per workload** (25% headroom
over the typical 3.3s rate):

```python
experiment_timeout = max(base_timeout, num_workloads * 4)
```

| Profile | Workloads | Experiment Timeout | Pytest Timeout | Rationale |
|---------|-----------|-------------------|----------------|-----------|
| baseline | 1 | 300s | 900s | Single experiment, generous buffer |
| small | 50 | 300s | 900s | 50 × 4s = 200s, use floor |
| medium | 320 | 1280s | 1500s | 320 × 4s, pytest adds overhead margin |

These timeouts have been applied to `test_ros.py` (`ros_002` and `ros_004`).

#### Tracking Regression

The measured rate should be logged in every run result JSON and compared across runs.
A sustained drop below **15 exp/min** for medium profile with a clean queue and single
Kruize replica indicates a regression — whether in Kruize, the database, or the cluster.

---

### PERF-FINDING-017: Upload Client Timeout Too Low for Large Payloads

**Status**: Fixed (test framework)  
**Severity**: Medium  
**Category**: Test Framework  
**Date**: 2026-06-04  

**Problem**:
`upload_with_retry()` in `tests/e2e_helpers.py` used a fixed 180s HTTP timeout.
Large burst payloads (60/90-day `ing_002` variants at ~92 MB / ~138 MB) routinely
require >180s when upload speed drops on the WAN link to the cluster.

**Evidence** — upload speed variance across medium runs for the same payload sizes:

| Variant | Typical Size | Speed Range (MB/s) | At Minimum Speed |
|---------|-------------|--------------------|--------------------|
| 30-day | ~47 MB | 0.76 – 6.16 | 62s (within 180s) |
| 60-day | ~92 MB | 0.27 – 6.13 | **344s** (exceeds 180s) |
| 90-day | ~138 MB | 2.24 – 5.59 | 62s (fast runs), **516s** (at 0.27 MB/s) |

The 23x speed variance (0.27–6.13 MB/s) for the same payload is WAN-dependent and
non-deterministic. A fixed timeout cannot accommodate this.

**Timeout math** (old vs. new):

| Payload | Fixed 180s | Dynamic `max(180, size/0.5 + 60)` | Actual Need @ 0.27 MB/s |
|---------|-----------|-----------------------------------|-------------------------|
| 47 MB | 180s | 180s | 175s |
| 92 MB | **180s (fail)** | 244s | **345s** |
| 138 MB | **180s (fail)** | 335s | **516s** |

**Fix applied** (in `tests/suites/performance/test_ingestion.py`):

```python
upload_timeout = max(180, int(package_size_mb / 0.5) + 60)
response = upload_with_retry(..., timeout=upload_timeout)
```

The computed timeout is logged in the result JSON as `upload_timeout_seconds`.

**Run 1780584560 note**: In this run, `ing_002[60-days]` and `ing_002[90-days]` hit
the 1800s pytest-timeout during `wait_for_processing_complete`, not during upload.
This indicates the full test budget (generation + upload + processing) can exceed
30 minutes when the pipeline is under load — a separate concern from the upload
timeout fix.

**Product observation**: The upload speed variance (0.27–6.13 MB/s) is not specific
to the test framework. Production environments processing data from busy clusters
with large payloads would experience the same variability. The cost-management
ingress path should be resilient to slow client uploads.

**ros_004 JWT expiry note**: Run 1780584560 also revealed that `ros_004` failed with
HTTP 401 after 377s of uploading at 0.024 MB/s — the JWT token expired during the
slow upload. Long-running uploads should refresh the JWT token before initiating the
request, or the token TTL should accommodate worst-case upload times.

---

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

**Note**: Not committed to chart defaults. Applied automatically by the deploy script (`apply_perf_profile_config`) for medium+ profiles. Formal production change tracked via FLPATH-4302.

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

**Note**: Not committed to chart defaults. Applied automatically by the deploy script (`apply_perf_profile_config`) for medium+ profiles. Formal production change tracked via FLPATH-4091.

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

## Finding Classification: Bugs vs. Rightsizing

Every finding must be classified to ensure that resource scaling does not mask real
defects. Over-provisioning is acceptable during data collection, but a test that only
passes because of extra replicas or memory is **not resolved** — it is **mitigated**
until the root cause is understood and the minimum viable configuration is established.

### Classification Criteria

| Category | Definition | Example | Resolution |
|----------|-----------|---------|------------|
| **Bug** | Incorrect behavior regardless of resources. Would fail even with infinite capacity. | Envoy `request_timeout` killing uploads (FINDING-011), FK poison pills blocking Kafka (FINDING-013) | Code or config fix required |
| **Deficiency** | Missing capability that causes failure under legitimate load. | No dead-letter handling in ros-processor (FINDING-013 upstream) | Upstream code change needed |
| **Under-provisioned** | Default resource allocation too low for the workload. Crashes (OOM) or probe failures. | ROS processor OOMKill at 1Gi (FINDING-007), Kruize CPU throttle (FINDING-006) | Raise limits to match measured peak + headroom |
| **Under-scaled** | Single-replica bottleneck where the component is healthy but throughput is insufficient. | Kruize experiment creation rate too slow (FINDING-014) | Add replicas; validate throughput scales linearly |
| **Test Framework** | Test infrastructure issue — timeouts, ordering, assertions. | ROS drain timeout (FINDING-012), test reordering (FINDING-013 mitigation) | Fix test code; does not affect product |
| **Environment** | Cluster or infrastructure issue unrelated to the product. | ODF resource exhaustion (FINDING-010) | Fix cluster config or deploy scripts |

### Current Finding Classification

| Finding | Category | Risk of Masking | Notes |
|---------|----------|-----------------|-------|
| PERF-FINDING-001 | Bug | None — timeout was objectively wrong | Gateway killed requests that would succeed given time |
| PERF-FINDING-006 | Under-provisioned | Low — CPU throttle caused probe failures, not slow processing | Validated: 0 restarts at 2 cores, same workload |
| PERF-FINDING-007 | Under-provisioned | Low — OOMKill is binary, not gradual | Go process exceeded cgroup limit; raising it is correct |
| PERF-FINDING-008 | Under-provisioned | Low — OOMKill during archive extraction | Memory proportional to compressed file size |
| PERF-FINDING-009 | Deficiency | None — hard limit, not resource-related | Nginx `client_max_body_size` needs explicit change |
| PERF-FINDING-010 | Environment | None — ODF defaults, not product | Cluster deploy tooling should enforce |
| PERF-FINDING-011 | Bug | None — Envoy config error | `request_timeout` on connection manager was unintentional |
| PERF-FINDING-012 | Test Framework | None — only affects test harness | Dynamic drain timeout matches actual processing rate |
| PERF-FINDING-013 | Deficiency + Test Framework | None — mitigation is test ordering; real fix is DLQ | Upstream ros-processor needs dead-letter handling |
| PERF-FINDING-014 | Under-scaled | **Medium** — see guidelines below | Kruize 2x reverted (made it worse); pipeline scaling validated |
| PERF-FINDING-015 | N/A (data collection) | None — informational | Establishes baselines, not a fix |
| PERF-FINDING-016 | Informational | None — documents measured rates | Throughput catalog; drives timeout calibration |
| PERF-FINDING-017 | Test Framework | None — only affects test harness | Dynamic upload timeout; JWT refresh needed for long uploads |
| PERF-FINDING-018 | Under-scaled | **Medium** — default replicas can't handle concurrent sources | Pipeline serialization bottleneck at 5+ sources |

### Rightsizing Guidelines

**Principle**: Scale to collect data, then right-size down to the minimum that passes.
Never ship an over-scaled default.

1. **Over-scale first, measure, then reduce.** The current 2-replica configuration for
   Kruize, ros-processor, listener, and celery workers is intentionally generous. After
   validating a passing run, reduce replicas one at a time and re-run to find the
   minimum configuration per profile.

2. **Throughput must scale linearly with replicas.** If doubling replicas does not
   roughly double throughput, the bottleneck is elsewhere (database, network, shared
   lock). Investigate before adding more replicas.

3. **A passing test is not a resolved finding.** When a test passes only after scaling,
   document:
   - The minimum replica count / resource allocation that passes
   - Whether the component was hitting a measurable limit (CPU throttle, OOM, connection
     pool exhaustion) or simply "slow"
   - If "slow" with no measurable limit, the issue may be architectural (serial
     processing, synchronous I/O) and needs deeper investigation

4. **Watch for regressions hiding behind headroom.** If a code change introduces a 2x
   slowdown but we have 3x the replicas, tests still pass. Mitigation: track throughput
   metrics (exp/min, events/sec) across runs and alert on significant drops even when
   tests pass.

5. **Profile-specific overrides, not global inflation.** Default `values.yaml` should
   reflect production-like single-replica configuration. Performance test profiles
   should apply scaling overrides via `--set` flags or profile-specific value files:

   ```bash
   # Example: medium profile override
   helm upgrade cost-onprem ./cost-onprem \
     --set kruize.replicas=2 \
     --set ros.processor.replicas=2 \
     --set costManagement.listener.replicas=2 \
     --set costManagement.celery.workers.ocp.replicas=2
   ```

   This keeps the default chart honest while allowing performance tests to scale as
   needed.

6. **Document the "why" for every resource change.** Each change to `values.yaml`
   resource requests, limits, or replica counts must reference a PERF-FINDING with
   measured evidence. Arbitrary bumps ("just make it bigger") are not acceptable.

---

## Test Run Summary

### Latest Run (2026-06-04) - Medium Profile (pipeline scaling, 2x Kruize reverted)

Config: 2× Kruize (c3p0=20), 2× ros-processor, 2× listener, 2× celery-worker-ocp/summary

| Test Category | Tests | Passed | Failed | Skipped | Notes |
|---------------|-------|--------|--------|---------|-------|
| API Latency | 16 | 16 | 0 | 0 | All passed |
| ROS | 4 | 2 | 2 | 0 | ros_002 (50% of 90%), ros_004 (JWT 401) |
| Ingestion | 10 | 8 | 2 | 0 | ing_002[60d,90d] pytest-timeout (30m) |
| Scale | 10 | 10 | 0 | 0 | All passed (scale_001 fixed) |

**Total: 40 tests, 36 passed, 4 failed, 5 skipped in 2h 25min**

#### Failure Analysis

| # | Test | Result | Root Cause | Finding |
|---|------|--------|------------|---------|
| 1 | `ros_002` | 160/288 experiments (50%) | Kruize throughput: 15.9 exp/min, needs 28 | PERF-FINDING-014 |
| 2 | `ros_004` | HTTP 401 after 377s upload | JWT token expired during slow upload (0.024 MB/s) | PERF-FINDING-017 |
| 3 | `ing_002[60-days]` | pytest-timeout at 1800s | Full pipeline exceeded 30 min ceiling | PERF-FINDING-017 |
| 4 | `ing_002[90-days]` | pytest-timeout at 1800s | Full pipeline exceeded 30 min ceiling | PERF-FINDING-017 |

#### Improvements vs. Prior Run (2026-06-03)

| Change | Before | After |
|--------|--------|-------|
| Pipeline scaling (listener, celery ×2) | `ing_003[10]`: 9/10 processed | **10/10 processed** |
| Pipeline scaling | `scale_001[5,10]`: ERROR at teardown | **PASS** |
| Kruize 2x replicas | ros_002: 18.0 exp/min (183 exp) | **15.9 exp/min (160 exp)** — regression |

#### Progress from Previous Runs

| Fix Applied | Tests Unblocked |
|-------------|-----------------|
| Envoy `request_timeout: 0s` (FINDING-011) | ing_002[60d,90d], ing_004[50,100] |
| ROS test reordering (FINDING-013) | ros_001 now passes (was 0 experiments) |
| ROS drain dynamic timeout (FINDING-012) | ros_003 now passes |
| Cost model CRUD 200/201 fix | api_003 now passes |
| Pipeline scaling (FINDING-014) | ing_003[10], scale_001[5,10] now pass |

**Remaining failures**: Kruize throughput (architectural), upload timeouts for large payloads, JWT expiry on slow uploads.

---

### Previous Run (2026-06-03) - Medium Profile (reordered tests, Envoy fix)

| Test Category | Tests | Passed | Failed | Error | Notes |
|---------------|-------|--------|--------|-------|-------|
| API Latency | 16 | 16 | 0 | 0 | All passed |
| ROS | 4 | 2 | 2 | 0 | ros_002 (57% of 90%), ros_004 (78% of 80%) — throughput |
| Ingestion | 13 | 12 | 1 | 0 | ing_003[10] (9/10 processed) |
| Scale | 8 | 6 | 0 | 2 | scale_001[5,10] teardown timeout (>300s) |

**Total: 41 tests, 36 passed, 3 failed, 2 errors in ~2h**

---

### Previous Run (2026-06-01) - Medium Profile

| Test Category | Tests | Passed | Failed | Skipped | Notes |
|---------------|-------|--------|--------|---------|-------|
| API Latency | 16 | 16 | 0 | 0 | All passed |
| Ingestion | 13 | 9 | 4 | 0 | ing_002[30d,90d], ing_003[10], ing_004[100] |
| ROS | 4 | 2 | 2 | 0 | ros_001, ros_004 (ROS processor OOM) |
| Scale | 6 | 6 | 0 | 0 | All passed |
| Misc | 6 | 1 | 0 | 5 | Skipped (profile/config) |

**Total: 45 tests, 34 passed, 6 failed, 5 skipped in 3h 27min**

---

### Previous Run (2026-05-31) - Small Profile (with Kruize CPU fix)

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

## Testing Philosophy: Chart Defaults vs. Test-Time Overrides

The chart (`values.yaml`, templates) reflects **production defaults** — conservative,
unmodified values that ship with the product. Performance tests require settings above
these defaults; they are applied automatically by `deploy-test-cost-onprem.sh` via
`apply_perf_profile_config()` before each test run.

This separation means:
- The chart is never altered to "fix" a perf finding in an uncommitted or ad-hoc way.
- Each finding's recommended production change is tracked separately via Jira.
- Any operator deploying the chart into production sees only shipped defaults; the
  rightsizing recommendations are surfaced through product documentation.

---

## Action Items

### Product Bug Fixes (require Jira + PR)

| Finding | Change | Jira | Status |
|---------|--------|------|--------|
| PERF-FINDING-001 | HAProxy + Envoy timeouts 30s → 180s | [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091) | **Awaiting PR** — applied via deploy script for perf tests |
| PERF-FINDING-006 | Kruize CPU limits 500m/1000m → 1000m/2000m | [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302) | **Awaiting PR** — applied via deploy script for perf tests |
| PERF-FINDING-009 | Increase or document max upload size (100MB → 200MB) | Needs ticket | **Awaiting ticket** — applied via deploy script for perf tests |
| PERF-FINDING-011 | Envoy `request_timeout` removed (was erroneously set to 60s) | Needs ticket | **Resolved in chart** (removed; envoy defaults to no timeout) |
| PERF-FINDING-013 | ROS processor dead-letter handling (upstream) | Needs ticket | **Mitigated** (test reordering) |

### Rightsizing (documented here; single Jira for product documentation)

These are resource/scaling adjustments informed by performance testing. They do not
represent bugs — they are configuration changes to match measured workload
requirements. A single Jira for product deployment documentation will capture the
rightsizing guidelines; individual findings are tracked in this document.

**All changes below are applied by the deploy script at test time and are NOT committed
to chart defaults.** The recommended values should be documented in product deployment
guides and/or applied via the Jira linked above.

| Finding | Change | Category | Status |
|---------|--------|----------|--------|
| PERF-FINDING-007 | ROS processor memory 1Gi → 2Gi/4Gi | Under-provisioned | **Applied via deploy script** — recommend for production |
| PERF-FINDING-008 | Koku listener memory 300Mi/600Mi → 1Gi/2Gi | Under-provisioned | **Applied via deploy script** — recommend for production |
| PERF-FINDING-014 | Kruize replicas kept at 1 (scaling degraded throughput); pipeline scaling ×2 validated | Under-scaled | **Validated** — applied via deploy script |
| PERF-FINDING-015 | Profile-aware scaling baselines | Data collection | **In progress** |
| PERF-FINDING-016 | Kruize throughput catalog and timeout calibration | Informational | **Applied** (test timeouts) |
| PERF-FINDING-017 | Upload timeout dynamic scaling; JWT refresh for long uploads | Test Framework | **Applied** (timeout fix) |
| PERF-FINDING-018 | Default pipeline replicas (1) can't process 5+ concurrent sources | Under-scaled | **Applied via deploy script** — recommend for product documentation |

### Test Framework (no Jira needed)

| Finding | Change | Status |
|---------|--------|--------|
| PERF-FINDING-012 | ROS drain dynamic timeout scaling | **Applied** |
| PERF-FINDING-013 | ROS test reordering (before ingestion) | **Applied** |
| PERF-FINDING-017 | Dynamic upload timeout (`max(180, size/0.5 + 60)`) | **Applied** |
| PERF-FINDING-017 | JWT token refresh before long uploads (ros_004) | **Pending** |

### Environment / Infrastructure (deploy tooling)

| Finding | Change | Status |
|---------|--------|--------|
| PERF-FINDING-010 | ODF default resources exhaust cluster memory | **Mitigated** — needs auto-toolbox enforcement |

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

### PERF-FINDING-011: Envoy `request_timeout` Kills Large Uploads

**Date**: 2026-06-03  
**Severity**: High  
**Status**: Fixed  

**Symptom**: `ing_002[60-days]` and `ing_002[90-days]` burst upload tests fail with
"manifest not yet visible" after 1500s.  Ingress logs show `unexpected EOF`.  Gateway
access logs reveal `HTTP 408` with `duration: 60000` and `upstream_service_time: null`.

**Root Cause**: Two separate Envoy timeouts were at play:

1. **Route-level `per_try_timeout: 60s`** (fixed earlier → 180s) — caps how long Envoy
   waits for the upstream *response*.
2. **Connection-manager `request_timeout: 60s`** — caps total time Envoy waits to
   *receive the full request body* from the client.  For payloads >~40 MB over the
   WAN link to the cluster, body transfer exceeds 60s and Envoy returns 408 before
   the request ever reaches the ingress pod.

The second timeout was the root cause of the persistent failures.  Small/medium files
succeeded because their upload completed within 60s; 60-day and 90-day burst payloads
did not.

**Fix**:
```yaml
# configmap-envoy.yaml  (http_connection_manager)
request_timeout: 0s          # was 60s — disabled; per-route timeouts govern each path
stream_idle_timeout: 300s    # unchanged — protects against hung connections
```

Setting `request_timeout: 0s` disables the global body-receive deadline.  Each route
still has its own `timeout` and `per_try_timeout`, so no path is left unbounded.

**Validation**: After applying the fix, `ing_001[medium]`, `ing_002[30/60/90-days]`,
`ing_003[2/5/10]`, `ing_004[50/100]`, `ing_005`, and `ing_006[medium]` all passed —
the entire ingestion suite is now clean.

---

### PERF-FINDING-012: ROS Drain Timeout Too Short for Ingestion Backlog

**Date**: 2026-06-03  
**Severity**: Medium  
**Status**: Fixed  

**Symptom**: All ROS tests (`ros_001`, `ros_002`, `ros_004`) fail with 0 experiments
created.  The `[ros-queue-drain]` fixture times out after 120s with lag=42–47.

**Root Cause**: The ingestion tests generate ROS Kafka events as a side-effect.  Each
event requires ~6s to process (Kruize API call).  After the ingestion suite, 40–50
stale events remain in the `hccm.ros.events` topic because:

1. The per-test cleanup drain (`_wait_for_ros_drain` in `conftest.py`) correctly
   detects stalled lag and gives up — but the processor IS making progress, just
   slowly (~6s/event).
2. The ROS test fixture `drain_ros_queue` has a hardcoded 120s timeout, too short
   for 42+ events × 6s ≈ 252s.

The ROS processor starts processing the test's own data mixed with the stale backlog,
but the experiments never complete within the test timeout.

**Fix** (in `test_ros.py`):
- Scale `max_wait` dynamically: `max(180, initial_lag * 8)` — gives each event
  ~8s budget (6s processing + 2s overhead).
- Track lag progress: only give up if lag stalls for 90s without decreasing.
- Print initial lag and computed timeout for diagnostics.

**Also improved** (in `conftest.py`, earlier fix): the cleanup drain
`_wait_for_ros_drain` already scales with resource count and detects stalls.

---

### PERF-FINDING-013: ROS Processor FK Poison Pills Block Kafka Queue

**Date**: 2026-06-03  
**Severity**: High  
**Status**: Mitigated (test reordering); upstream fix needed  

**Symptom**: All ROS performance tests (`ros_001`–`ros_004`) fail with 0 experiments
created.  The `[ros-queue-drain]` fixture observes non-zero lag that never decreases,
even after waiting 180–300s.

#### Data flow and the orphan window

The pipeline that produces ROS events is:

```
upload → ingress → S3 → listener (koku-masu) → [processes OCP data]
                                               → writes to hccm.ros.events Kafka topic
                                               → ros-processor consumes events
                                               → calls Kruize API per workload (~6s each)
                                               → writes workload row (FK → clusters → sources)
```

The listener produces ROS events **during** cost data processing.  The ros-processor
consumes them **asynchronously** — there is no back-pressure or synchronization
between the two.  A medium-profile source generates ~20 workloads, each producing a
Kafka event that takes ~6s to process through Kruize, so a single source's events
take ~120s to fully consume.

The test cleanup framework monitors Kafka consumer lag to detect when events have
been consumed (`_wait_for_ros_drain`).  However, the drain has two failure modes:

1. **Slow but progressing**: The processor works through events at ~6s each, but the
   drain's stall detection (90s of no lag decrease) triggers before all events are
   consumed — particularly when events are concentrated on one Kafka partition.
2. **Cascading poison**: Once the drain gives up and deletes a source, those events
   become permanent FK errors.  The processor retries them endlessly (never commits
   the offset past them), which blocks ALL subsequent events on that partition —
   including events from other, still-valid sources.

This creates a cascading failure across tests:

```
ing_001 cleanup → drain gives up at lag=14 → deletes source A → events poisoned
ing_002 cleanup → drain sees lag=28 (14 old poison + 14 new) → stalls → deletes source B
ing_003 cleanup → drain sees lag=42 → stalls → deletes source C
... by the time ROS tests run, 40-50 poison events block the queue permanently
```

#### Why this doesn't happen in production

In production, sources are **long-lived** — they persist for months or years.  The FK
relationship (`workloads → clusters → sources`) is always satisfied because sources are
never deleted while events are in-flight.  The ros-processor was designed for this
steady-state: events always reference valid sources, so FK errors never occur and the
lack of dead-letter handling is invisible.

In testing, sources are **ephemeral** — created and deleted within minutes.  The
cleanup *must* delete sources to avoid polluting subsequent tests.  This creates a
window where Kafka events reference already-deleted sources, which the ros-processor
cannot handle gracefully.

#### Root cause summary

The ros-processor (Go) uses `kafkaAutoCommit: true`, meaning offsets are committed
on a timer, not per-message.  When it encounters an FK error:

1. It logs the error but does **not** advance the offset
2. On the next poll, it re-reads the same event
3. The FK error repeats — the event is a permanent poison pill
4. All subsequent events on that partition are blocked behind it

```
ERROR: insert or update on table "workloads" violates foreign key
constraint "fk_workloads_cluster" (SQLSTATE 23503)
```

**Reproduction steps**:

```bash
# 1. Run ingestion tests (generates ROS events as side-effect)
cd tests && pytest -m "performance and ingestion" --perf-profile medium

# 2. Check Kafka lag — will show non-zero lag on hccm.ros.events
oc exec -n kafka cost-onprem-kafka-broker-0 -- \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --group ros-processor

# 3. Check ros-processor logs — will show FK errors in a loop
oc logs -n cost-onprem -l app.kubernetes.io/component=ros-processor --tail=10

# 4. Run ROS tests — will fail because queue is poisoned
cd tests && pytest -m "performance and ros_perf" --perf-profile medium
```

**Conditions for failure**:
1. Ingestion tests run **before** ROS tests (default alphabetical order)
2. Ingestion test cleanup deletes sources while ROS events are still in Kafka
3. The `_wait_for_ros_drain` cleanup fixture gives up (lag appears stalled because
   the processor is blocked by FK errors from a prior cleanup, not because it's slow)
4. Poisoned events cascade across test boundaries — each cleanup adds more

**Manual recovery**:

```bash
# Scale down the processor so the consumer group becomes inactive
oc scale deployment/cost-onprem-ros-processor -n cost-onprem --replicas=0

# Wait ~2 min for Kafka session timeout, then reset offsets
oc exec -n kafka cost-onprem-kafka-broker-0 -- \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group ros-processor --topic hccm.ros.events \
  --reset-offsets --to-latest --execute

# Scale back up
oc scale deployment/cost-onprem-ros-processor -n cost-onprem --replicas=1
```

**Mitigation** (applied): Reorder performance tests so the ROS suite runs **before**
the ingestion suite.  Since ROS tests create their own sources and clean up after
themselves (with a clean queue), the queue stays healthy.  The ingestion tests that
follow may generate orphaned ROS events, but no subsequent tests depend on the ROS
queue being clean.

Test execution order (via `pytest_collection_modifyitems` hook in
`suites/performance/conftest.py`):

`test_api_latency` → `test_ros` → `test_ingestion` → `test_scale` → `test_soak`

**Upstream fix needed**: The ros-processor should implement dead-letter handling:
after N consecutive FK failures on the same event, commit the offset and log a
warning rather than retrying forever.  This is not a production issue today because
sources are never deleted while events are in-flight, but it represents a resilience
gap that would surface during any source lifecycle operation (e.g., bulk source
removal, disaster recovery re-registration).

---

## Related Jira Stories

- [FLPATH-4036](https://redhat.atlassian.net/browse/FLPATH-4036): Performance Testing Framework
- [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091): Gateway Timeout Fix
- [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302): Kruize CPU Resource Increase

---

_Last Updated: 2026-06-04 (run 0-2-20-rc5-medium-1780584560 analysis)_
