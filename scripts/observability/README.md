# Observability Stack

Metrics collection infrastructure for Cost On-Prem performance testing.

**Jira:** [FLPATH-4061](https://redhat.atlassian.net/browse/FLPATH-4061)

## Purpose

Collect performance metrics during test runs, export to JSON, and publish to S3-compatible storage for historical analysis. The Grafana dashboards serve as a **reference for which metrics to collect**, not as the primary visualization tool.

## Quick Start

```bash
# 1. Deploy metrics collection infrastructure
./deploy-observability.sh

# 2. Run performance tests while collecting metrics
TEST_RUN_ID=baseline-v0.2.20 ./collect-metrics.sh --continuous 30

# 3. Upload results to S3 (Red Hat Ecosystem QE MinIO)
# S3_BUCKET is intentionally not defaulted to prevent accidental uploads
S3_ENDPOINT="https://minio-s3-ecosystem-qe-ai--pipeline.apps.gpc.ocp-hub.prod.psi.redhat.com" \
S3_BUCKET="eco-bucket-perf-scale" \
./collect-metrics.sh --upload
```

## Scripts

| Script | Description |
|--------|-------------|
| `deploy-observability.sh` | Deploy Prometheus exporters for PostgreSQL/Valkey |
| `collect-metrics.sh` | Capture metrics snapshots and export to JSON/S3 |
| `generate-perf-matrix-report.py` | Generate HTML matrix report from `perf-runs/` output |

### Matrix Report

Generates a self-contained HTML page showing the
[listener CPU × load profile matrix](../../docs/performance/TEST-MATRIX.md#listener-cpu-sizing-scenarios)
with inline results, pass/fail badges, ingestion metrics, and links to full reports:

```bash
# Generate from default perf-runs/ directory
python3 scripts/observability/generate-perf-matrix-report.py

# Custom paths
python3 scripts/observability/generate-perf-matrix-report.py \
  --runs-dir ./perf-runs \
  --output perf-matrix-report.html
```

Open `perf-matrix-report.html` in a browser. Each cell in the matrix represents
one or more completed runs at that CPU config × profile combination. Empty cells
indicate scenarios not yet executed.

## Components

| Component | Description |
|-----------|-------------|
| **User Workload Monitoring** | OpenShift's built-in Prometheus with 30-day retention |
| **postgres_exporter** | PostgreSQL metrics (connections, queries, locks) |
| **valkey-exporter** | Valkey/Redis metrics (memory, clients, operations) |
| **collect-metrics.sh** | Query Prometheus and export to JSON/S3 |

## Dashboards (Reference)

The dashboard JSON files define which metrics to collect. They can be imported into any Grafana instance if real-time visualization is needed.

```
dashboards/
├── overview.json        # High-level health and KPIs
├── ingress.json         # Upload latency, Kafka metrics
├── processing.json      # Celery queues, task performance
├── database.json        # PostgreSQL connections, queries, locks
├── ros.json             # Kruize and ROS recommendation metrics
└── infrastructure.json  # Node resources, Valkey cache
```

## Metrics Output

```bash
metrics-snapshots/
└── baseline-v0.2.20/
    ├── snapshot_20260519_103000.json
    ├── snapshot_20260519_103030.json
    ├── ...
    └── summary.json
```

## Documentation

See [docs/performance/OBSERVABILITY.md](../../docs/performance/OBSERVABILITY.md) for:

- Full metrics reference
- S3 upload configuration
- PromQL query examples
