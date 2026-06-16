# Performance Testing Findings

Product issues and sizing requirements discovered during performance testing.

---

## Product Issues

### PERF-FINDING-001: Gateway Timeout Too Low for Large File Uploads

**Status**: Awaiting PR  
**Severity**: Critical  
**Jira**: [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091)

**Problem**:
The Envoy gateway and HAProxy route had 30s timeouts for the `/api/ingress/` route. Large file uploads (30+ days of data, ~48MB+) take 25-60+ seconds to transfer, causing HTTP 408/504 timeout errors even though ingress successfully processes the upload.

**Impact**:
- Customers with large clusters cannot upload more than ~2 weeks of data at once
- HTTP 504 errors returned to clients despite successful server-side processing

**Proposed Fix**:
1. HAProxy route timeout: 30s → 180s (`values.yaml`)
2. Envoy route timeout: 30s → 180s, per_try_timeout: 10s → 60s (`configmap-envoy.yaml`)

**Validation**: `ing_002[30-days]` passes with fix (68MB upload at 5.23 MB/s).
Applied automatically by `apply_perf_profile_config()` for medium+ profiles.

---

### PERF-FINDING-006: Kruize Pod Restarts Under Load — CPU Throttling

**Status**: Awaiting PR  
**Severity**: Medium  
**Jira**: [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302)

**Problem**:
Kruize pod restarts during moderate load (50 concurrent experiments) due to CPU throttling at the 1 core limit. Peak memory was only 786 MB (38% of 2Gi limit), ruling out OOM. CPU throttling causes slow liveness probe responses, triggering restarts.

**Proposed Fix**:
```yaml
resources:
  kruize:
    requests:
      cpu: "1000m"    # Was 500m
    limits:
      cpu: "2000m"    # Was 1000m
```

**Validation**: 0 restarts at 2 cores with the same workload.
Applied automatically by `apply_perf_profile_config()` for medium+ profiles.

---

### PERF-FINDING-011: Envoy `request_timeout` Kills Large Uploads

**Status**: Fixed in chart  
**Severity**: High

**Problem**: The Envoy connection manager had `request_timeout: 60s`, which caps total time to receive the full request body from the client. For payloads >~40 MB over WAN, body transfer exceeds 60s and Envoy returns 408 before the request reaches the ingress pod.

**Fix**: `request_timeout: 0s` (disabled; per-route timeouts govern each path). `stream_idle_timeout: 300s` remains as protection against hung connections.

---

### PERF-FINDING-013: ROS Processor FK Errors Block Kafka Queue

**Status**: Mitigated; upstream fix needed  
**Severity**: High

**Problem**:
The ros-processor (Go) uses `kafkaAutoCommit: true` and does not advance the offset on FK errors. When a source is deleted while ROS events are still in Kafka, those events become permanent poison pills that block all subsequent events on that partition.

**Impact**:
Not a production issue today because sources are long-lived. However, it represents a resilience gap for any source lifecycle operation (bulk source removal, disaster recovery re-registration).

**Upstream fix needed**: Dead-letter handling — after N consecutive FK failures on the same event, commit the offset and log a warning rather than retrying forever.

---

## Sizing Requirements

### PERF-FINDING-002: Listener CPU is the Primary Ingestion Bottleneck

**Status**: Documented — sizing recommendation  
**Severity**: High

**Problem**:
The chart default CPU limit (150m request / 300m limit) throttles all ingestion workloads. At 300m, the listener runs at 157% CPU (throttled) during medium-profile ingestion.

**Evidence**:

| Component | Peak CPU | CPU Limit | Utilization |
|-----------|---------|-----------|-------------|
| Listener | 471m | 300m | 157% (throttled) |
| Kruize | 185m | 2000m | 9% |
| Celery Workers | 368m | varies | low |
| Postgres | 327m | - | moderate |

**Impact**: ~40-50% slower ingestion compared to uncapped CPU.

**Recommendation**: Production deployments handling burst ingestion or large file uploads should raise the listener CPU limit to at least 1000m.

---

### PERF-FINDING-003: Pipeline Serialization Limits Concurrent Source Processing

**Status**: Documented — scaling recommendation  
**Severity**: Medium

**Problem**:
The default single-replica listener/worker configuration cannot drain concurrent source uploads within expected processing windows. Sources queue in Kafka and process serially.

**Recommended Configuration**:

| Concurrent Sources | listener | ocp-worker | summary-worker |
|--------------------|----------|------------|----------------|
| 1–3 | 1 | 1 | 1 |
| 4–10 | 2 | 2 | 2 |
| 10+ | 3 | 3 | 3 |

---

### PERF-FINDING-004: Kruize Experiment Creation Rate ~8/min

**Status**: Documented — throughput baseline  
**Severity**: Low (informational)

**Problem**:
Kruize creates experiments at ~8 per minute (7-8.5s each), limited by its Hibernate connection pool (`c3p0maxsize=5`) and serial DB operations. Not CPU or memory bound. Scaling Kruize replicas degrades throughput (DB contention).

**Evidence** (medium profile, 160 workloads):

| Run | Experiments | Rate/min | Peak Memory | Restarts |
|-----|-------------|----------|-------------|----------|
| 1781193941 | 160/160 | 8.6 | 723 MB | 0 |

**Recommendation**: Keep Kruize at 1 replica. Upstream connection pool or batch experiment creation improvement would increase throughput.

---

### Resource Sizing — Under-Provisioned Defaults

| Component | Chart Default | Recommended | Evidence |
|-----------|-------------|-------------|----------|
| Kruize CPU | 500m/1000m | 1000m/2000m | FINDING-006: probe failures at 1 core |
| ROS processor memory | 1Gi/1Gi | 2Gi/4Gi | OOMKill during experiment processing |
| Listener memory | 300Mi/600Mi | 1Gi/2Gi (large: 2Gi/4Gi) | OOMKill during archive extraction |
| Ingress max upload | 100MB | 200MB (large: 500MB) | Hard limit; needs explicit change for large files |
| Ingress upload memory | 32MB | 64MB (large: 128MB) | FINDING-021: disk spill adds latency |
| Gateway timeouts | 30s | 180s (large: 600s) | FINDING-001, -020: 504 on medium/large uploads |
| Ingress pod memory | 1Gi/1Gi | 1Gi/2Gi (large: 2Gi/4Gi) | FINDING-022: OOM on large/concurrent uploads |
| OCP worker CPU | 250m/500m | 500m/1000m (large) | FINDING-025: 15% faster processing |
| Summary worker CPU | 250m/500m | 500m/1000m (large) | FINDING-025: 15% faster processing |
| OCP worker memory | 512Mi/1Gi | 1Gi/2Gi (large) | FINDING-025: headroom for large data sets |

---

### PERF-FINDING-020: Envoy Gateway ConfigMap Not Reloaded on Helm Upgrade

**Status**: Mitigated in perf scripts; chart fix needed  
**Severity**: Critical

**Problem**:
Envoy reads its config file at startup only — it does not watch for changes. When `helm upgrade` modifies the gateway ConfigMap (e.g. increasing `ingressTimeout` from 30s to 600s), the running gateway pod continues using the old values. This rendered all timeout overrides from `apply_perf_profile_config()` ineffective, causing HTTP 504 failures on medium/large profile uploads despite correct values in the ConfigMap.

**Evidence**: Run `#37` — Envoy ConfigMap showed 600s timeouts, but uploads failed at ~30s (the old default). All ING-001[medium/large], ING-002, ING-003[10], ING-004 tests failed with instant 504/500 errors.

**Mitigation**: `perf-testing.sh` now explicitly runs `oc rollout restart` on the gateway deployment after helm upgrade.

**Recommended chart fix**: Add a `checksum/envoy-config` pod template annotation to the gateway deployment so any ConfigMap change triggers an automatic rolling restart during `helm upgrade`. This is the [standard Helm pattern](https://helm.sh/docs/howto/charts_tips_and_tricks/#automatically-roll-deployments) for static-config applications like Envoy.

---

### PERF-FINDING-021: Ingress In-Memory Buffer Too Small for Large Uploads

**Status**: Fixed in perf profiles  
**Severity**: Medium

**Problem**:
The `INGRESS_MAXUPLOADMEM` default is 32 MB. For uploads >32 MB, insights-ingress-go spills to disk, adding latency. Combined with tight per-try timeouts, this can push uploads past the retry window.

**Fix**: `apply_perf_profile_config()` now sets `ingress.upload.maxMemory` per profile:
- medium: 64 MB
- large: 128 MB

---

### PERF-FINDING-022: Ingress Pod Memory Insufficient for Large/Concurrent Uploads

**Status**: Mitigated in perf profiles; chart fix needed  
**Severity**: High

**Problem**:
The ingress pod (`insights-ingress-go`) uses the shared `resources.application` block (1Gi memory limit). Processing large uploads requires multipart parsing, tar extraction, and S3 staging — all memory-intensive. Uploads >100 MB or 10+ concurrent uploads cause HTTP 500 errors from the ingress pod due to memory exhaustion.

**Evidence** (Run `#38`, large profile):
- ING-004[100] (101 MB) passed but ING-004[50] failed immediately after — residual memory pressure
- ING-001[large] (~200+ MB package) — HTTP 500 on all attempts
- ING-003[10] (10 concurrent uploads) — all 10 uploads returned HTTP 500

Uploads up to 138 MB succeed individually (ING-002[90-days] = 138.74 MB at 2.44 MB/s).

**Mitigation**: `apply_perf_profile_config()` now overrides `resources.application` per profile:
- medium: 1Gi/2Gi
- large: 2Gi/4Gi

**Recommended chart fix**: Give ingress its own resource block in `values.yaml` (separate from the shared `resources.application`) so it can be sized independently for large file handling without affecting other services.

---

### PERF-FINDING-024: Ingress Single-Part S3 Upload Fails for Large Payloads

**Status**: Product limitation; upstream enhancement needed  
**Severity**: Medium

**Problem**:
`insights-ingress-go` uses the minio-go `PutObject()` API for S3 staging, which performs a single-part upload. Payloads exceeding ~150 MB consistently fail with HTTP 500 against NooBaa/Ceph RGW backends. The error originates in the S3 staging step, not in multipart form parsing or pod memory limits.

**Evidence**:
- ING-002[90-days] (138 MB) passes reliably across runs #40, #41
- ING-001[large] (~200+ MB) fails with HTTP 500 on every attempt (runs #40, #41, #42)
- ING-003[10] (10 concurrent small uploads) also fails — likely S3 connection pool exhaustion under concurrent staging

**What was tried**:
- Pod memory: `resources.application` increased to 2Gi/4Gi — no effect on the 500s (pod uses only ~49 Mi at idle, 0 restarts, 0 OOM events)
- `INGRESS_MAXUPLOADMEM`: Increased from 128 MB to 512 MB in run #42 — **made things worse**. Go's `ParseMultipartForm(512MB)` pre-allocates heap per request, and the oversized allocation destabilized the pipeline (ING-002[30-days], previously reliable, failed with "manifest not yet visible" after 1500s). Reverted to 128 MB.
- Node headroom: Worker nodes at 53-67% memory requests, no evictions — cluster resources are not the constraint

**Recommended upstream enhancement**: `insights-ingress-go` should use multipart S3 uploads (e.g., minio-go `PutObject` with `PartSize` option or the AWS SDK S3 upload manager) for payloads exceeding a configurable threshold. This is the standard pattern for large object uploads to S3-compatible backends.

**Workaround**: Customers with large clusters generating >150 MB upload packages should split data into multiple smaller uploads (e.g., by time range or namespace).

---

### PERF-FINDING-025: OCP/Summary Worker CPU Throttling Slows Data Processing

**Status**: Mitigated in perf profiles  
**Severity**: Medium

**Problem**:
The chart default CPU limits for OCP and summary celery workers (250m request / 500m limit) throttle data processing throughput. When the listener ingests data faster than workers can process it, the pipeline backs up.

**Evidence** (Run #40 → #41, large profile):
- Worker CPU boosted from 250m/500m to 500m/1000m (request/limit)
- Worker memory boosted from 512Mi/1Gi to 1Gi/2Gi
- Total run time: 112 min → 97.6 min (**15% faster**)
- KPI violations: 1 → 0

**Fix**: `apply_perf_profile_config()` now overrides worker resources per profile:
- medium: 250m/1000m CPU, 512Mi/2Gi memory
- large: 500m/1000m CPU, 1Gi/2Gi memory

**Recommendation**: Production deployments processing large or frequent uploads should increase OCP and summary worker CPU limits to at least 1000m.

---

## Environment Issues

### PERF-FINDING-010: ODF Default Resources Exhaust Cluster Memory

**Status**: Mitigated  
**Severity**: Critical  
**Category**: Environment (not product)

**Problem**:
ODF 4.20 applies full upstream default resource requests. On a 3-worker cluster, ODF consumed ~61.5Gi (68%) in memory requests, leaving insufficient headroom for cost-onprem + Kafka.

**Remediation**: Patch `StorageCluster` and NooBaa resources to reduced targets. See auto-toolbox `odf_reduced_resources` flag.

---

### PERF-FINDING-019: Ceph OSD False-Full Cascade Under Load

**Status**: Documented  
**Severity**: Critical  
**Category**: Environment (not product)

**Problem**:
During medium-profile tests, Ceph reported `OSD_FULL` despite disks at 25-29% actual usage. This cascaded to all CephFS-backed pods (database, Kafka) via SELinux relabel failures.

**Recovery**: Restart OSD pods to clear false-full state, then restart affected workloads.

**Production Implication**: Clusters running cost-onprem at high utilization with CephFS may experience this failure mode. The false-full condition cascades to all CephFS-backed workloads.

---

## Performance Baselines

### Large File Processing Times

| File Size | Upload Throughput | Processing Time | Total Time |
|-----------|-------------------|-----------------|------------|
| ~13 KB | - | <1s | <1s |
| ~48 MB | 1-2 MB/s | 5-10 min | 6-11 min |
| ~67 MB | 1.1-1.9 MB/s | 7-20 min | 8-22 min |
| ~197 MB | 3.2 MB/s | >20 min | >21 min |

### Concurrent Upload Scaling (validated)

| Concurrent Sources | Replicas (listener/ocp/summary) | Result |
|--------------------|--------------------------------|--------|
| 2 | 1/1/1 | PASS |
| 5 | 2/2/2 | PASS |
| 10 | 2/2/2 | PASS |

---

## Action Items

### Product Changes (require Jira + PR)

| Finding | Change | Jira | Status |
|---------|--------|------|--------|
| FINDING-001 | HAProxy + Envoy timeouts 30s → 180s | [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091) | Awaiting PR |
| FINDING-006 | Kruize CPU limits 500m/1000m → 1000m/2000m | [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302) | Awaiting PR |
| FINDING-011 | Envoy `request_timeout` removed | — | Fixed in chart |
| FINDING-013 | ROS processor dead-letter handling | Needs ticket | Mitigated (test reordering) |
| FINDING-020 | Gateway ConfigMap checksum annotation | Needs ticket | Mitigated (perf script restart) |
| FINDING-021 | Ingress upload memory per profile | — | Fixed in perf profiles |
| FINDING-022 | Ingress pod dedicated resource block | Needs ticket | Mitigated (perf profile override) |
| FINDING-024 | Ingress multipart S3 upload for large payloads | Needs ticket | Product limitation |
| FINDING-025 | Worker CPU/memory sizing for throughput | — | Fixed in perf profiles |

### Sizing Documentation

| Finding | Recommendation | Status |
|---------|---------------|--------|
| FINDING-002 | Listener CPU ≥1000m for burst ingestion | Documented |
| FINDING-003 | Scale replicas for concurrent source count | Documented |
| FINDING-004 | Kruize at 1 replica; ~8 exp/min baseline | Documented |

---

## Related Jira

- [FLPATH-4036](https://redhat.atlassian.net/browse/FLPATH-4036): Performance Testing Framework
- [FLPATH-4091](https://redhat.atlassian.net/browse/FLPATH-4091): Gateway Timeout Fix
- [FLPATH-4302](https://redhat.atlassian.net/browse/FLPATH-4302): Kruize CPU Resource Increase
- [COST-7567](https://redhat.atlassian.net/browse/COST-7567): CoP Performance Tuning & Hardware Sizing Guidelines

---

_Last Updated: 2026-06-15_
