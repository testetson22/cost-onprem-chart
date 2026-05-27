#!/usr/bin/env python3
"""
Generate perf-summary.json — a flat, Infinity-datasource-queryable summary of a perf run.

This file is uploaded to MinIO alongside the raw results and lets a persistent
Grafana instance (with the Infinity datasource) visualize historical runs without
needing access to the test cluster or its Prometheus instance.

Output: <run-dir>/results/perf-summary.json

Schema:
  {
    "run":     { run-level metadata },
    "tests":   [ { per-test flat row }, ... ],
    "api":     [ { per-endpoint latency row }, ... ],
    "ingestion": [ { per-ingestion-test row }, ... ]
  }

Usage:
    python3 scripts/observability/generate-perf-summary.py --run-dir tests/perf-runs/<id>
    python3 scripts/observability/generate-perf-summary.py --run-dir tests/perf-runs/<id> --update-index
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_session(run_dir: Path) -> Optional[dict]:
    for sf in sorted((run_dir / "results").glob("session_*.json")):
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return None


def load_metadata(run_dir: Path) -> dict:
    p = run_dir / "metadata.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def parse_junit(run_dir: Path) -> dict:
    reports_dir = run_dir / "reports"
    named = reports_dir / "junit.xml"
    candidates = [named] if named.exists() else sorted(reports_dir.glob("*.xml"))
    for xml_path in candidates:
        try:
            root = ET.parse(xml_path).getroot()
            suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
            total = errors = failures = skipped = 0
            duration = 0.0
            for suite in suites:
                total    += int(suite.get("tests",    0))
                errors   += int(suite.get("errors",   0))
                failures += int(suite.get("failures", 0))
                skipped  += int(suite.get("skipped",  0))
                try:
                    duration += float(suite.get("time", 0))
                except ValueError:
                    pass
            return {
                "total": total,
                "passed": total - errors - failures - skipped,
                "failed": failures + errors,
                "skipped": skipped,
                "duration_s": round(duration, 1),
            }
        except Exception:
            continue
    return {}


def _import_report_module():
    """Dynamically import generate-perf-run-report to reuse KPI_THRESHOLDS."""
    script_dir = Path(__file__).resolve().parent
    report_path = script_dir / "generate-perf-run-report.py"
    if not report_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("perf_run_report", report_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Summary generation
# ---------------------------------------------------------------------------

def build_summary(run_dir: Path) -> dict:
    session  = load_session(run_dir)
    metadata = load_metadata(run_dir)
    junit    = parse_junit(run_dir)
    results  = (session or {}).get("results", [])
    run_id   = run_dir.name

    # Run-level metadata
    total_s = sum(
        sum(t.get("duration_seconds", 0) for t in r.get("timings", []))
        for r in results
    )
    cluster = metadata.get("cluster_info") or (results[0].get("cluster_info") if results else {}) or {}

    run_meta = {
        "run_id":        run_id,
        "timestamp":     metadata.get("created_at") or (results[0].get("timestamp") if results else ""),
        "chart_version": metadata.get("chart_version") or (results[0].get("chart_version") if results else "unknown"),
        "profile":       metadata.get("perf_profile") or (results[0].get("profile") if results else "unknown"),
        "total_tests":   junit.get("total", len(results)),
        "passed":        junit.get("passed", sum(1 for r in results if r.get("passed"))),
        "failed":        junit.get("failed", sum(1 for r in results if not r.get("passed"))),
        "duration_min":  round(total_s / 60, 1),
        "ocp_version":   cluster.get("ocp_version", ""),
        "node_count":    cluster.get("node_count", 0),
        "storage_type":  cluster.get("storage_type", ""),
        "namespace":     metadata.get("namespace", "cost-onprem"),
    }

    # Flat test rows — one row per test result
    test_rows = []
    for r in results:
        dur_s = round(sum(t.get("duration_seconds", 0) for t in r.get("timings", [])), 1)
        m = r.get("metrics") or {}
        test_rows.append({
            "run_id":       run_id,
            "test_name":    r["test_name"],
            "short_name":   r["test_name"].replace("test_perf_", "").replace("_baseline", "")[:50],
            "status":       "PASS" if r.get("passed") else "FAIL",
            "passed":       1 if r.get("passed") else 0,
            "duration_s":   dur_s,
            "duration_min": round(dur_s / 60, 2),
            "error":        (r.get("error_message") or "")[:120],
            "profile":      r.get("profile", run_meta["profile"]),
            "chart_version": r.get("chart_version", run_meta["chart_version"]),
            "timestamp":    r.get("timestamp", ""),
            # Carry through summary metrics for quick access
            "upload_throughput_mb_s": round(
                (m.get("upload") or {}).get("upload_mb_per_second", 0) or
                m.get("upload_throughput_mb_s", 0), 4
            ),
            "listener_cpu_cores": round(m.get("listener_cpu_cores", 0), 4),
            "api_p95_ms": round(m.get("aggregate_p95", 0) * 1000, 1),
            "within_window": int(m.get("within_window", -1)),
        })

    # API latency rows — one row per endpoint per iteration count
    api_rows = []
    for r in results:
        if "api" not in r.get("test_name", ""):
            continue
        m = r.get("metrics") or {}
        # API-001/004/005/006 style: metrics.results keyed by endpoint
        for ep, ep_data in (m.get("results") or {}).items():
            lat = ep_data.get("latencies") or {}
            if not lat:
                continue
            ep_short = ep.rstrip("/").split("/")[-1] or ep
            iterations = ep_data.get("iterations", m.get("iterations", 0))
            api_rows.append({
                "run_id":     run_id,
                "test":       r["test_name"].replace("test_perf_", "")[:40],
                "endpoint":   ep_short,
                "iterations": iterations,
                "p50_ms":     round(lat.get("p50", 0) * 1000, 2),
                "p95_ms":     round(lat.get("p95", 0) * 1000, 2),
                "p99_ms":     round(lat.get("p99", 0) * 1000, 2),
                "avg_ms":     round(lat.get("avg", 0) * 1000, 2),
                "success_rate": round(ep_data.get("success_rate", 1.0), 4),
                "passed":     1 if r.get("passed") else 0,
                "profile":    r.get("profile", run_meta["profile"]),
            })
        # API-002 style: latencies dict directly in metrics
        lat = m.get("latencies")
        if isinstance(lat, dict) and "p50" in lat and not m.get("results"):
            api_rows.append({
                "run_id":      run_id,
                "test":        r["test_name"].replace("test_perf_", "")[:40],
                "endpoint":    f'{m.get("concurrent_users","?")}users',
                "iterations":  m.get("total_requests", 0),
                "p50_ms":      round(lat.get("p50", 0) * 1000, 2),
                "p95_ms":      round(lat.get("p95", 0) * 1000, 2),
                "p99_ms":      round(lat.get("p99", 0) * 1000, 2),
                "avg_ms":      round(lat.get("avg", 0) * 1000, 2),
                "success_rate": round(m.get("success_rate", 1.0), 4),
                "passed":      1 if r.get("passed") else 0,
                "profile":     r.get("profile", run_meta["profile"]),
            })

    # Ingestion rows — one row per ingestion test
    ing_rows = []
    for r in results:
        if "ing" not in r.get("test_name", ""):
            continue
        m = r.get("metrics") or {}
        timings = {t["name"]: round(t["duration_seconds"], 2) for t in r.get("timings", [])}
        upload = m.get("upload") or {}
        ing_rows.append({
            "run_id":              run_id,
            "test":                r["test_name"].replace("test_perf_", "")[:50],
            "passed":              1 if r.get("passed") else 0,
            "profile":             m.get("profile", r.get("profile", run_meta["profile"])),
            "upload_size_mb":      round(
                upload.get("package_size_mb") or m.get("actual_size_mb", 0), 2
            ),
            "upload_speed_mb_s":   round(
                upload.get("upload_mb_per_second") or m.get("upload_throughput_mb_s", 0), 4
            ),
            "upload_time_s":       round(
                upload.get("upload_seconds") or timings.get("data_generation_and_upload", 0), 1
            ),
            "processing_time_s":   round(
                m.get("processing_time_seconds") or timings.get("processing_wait", 0) or
                timings.get("summary_table_wait", 0), 1
            ),
            "processing_time_min": round(
                (m.get("processing_time_seconds") or timings.get("processing_wait", 0) or
                 timings.get("summary_table_wait", 0)) / 60, 2
            ),
            "listener_cpu_cores":  round(m.get("listener_cpu_cores", 0), 4),
            "concurrent_sources":  m.get("concurrent_sources", 1),
            "within_window":       int(m.get("within_window", -1)),
            "error":               (r.get("error_message") or "")[:120],
            "chart_version":       r.get("chart_version", run_meta["chart_version"]),
        })

    # KPI evaluation (import thresholds from the report generator)
    kpi_violations = 0
    kpi_warnings = 0
    try:
        report_module = _import_report_module()
        if report_module:
            for r in results:
                evals = report_module.evaluate_kpis(r)
                for e in evals:
                    if e["status"] == "red":
                        kpi_violations += 1
                    elif e["status"] == "yellow":
                        kpi_warnings += 1
                    test_name = r["test_name"]
                    for row in test_rows:
                        if row["test_name"] == test_name and "kpi_status" not in row:
                            worst = "green"
                            for ev in evals:
                                if ev["status"] == "red":
                                    worst = "red"
                                    break
                                if ev["status"] == "yellow":
                                    worst = "yellow"
                            row["kpi_status"] = worst
                            break
    except Exception:
        pass

    run_meta["kpi_violations"] = kpi_violations
    run_meta["kpi_warnings"] = kpi_warnings

    return {
        "run":       run_meta,
        "tests":     test_rows,
        "api":       api_rows,
        "ingestion": ing_rows,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

def update_s3_index(run_dir: Path, summary: dict,
                    s3_endpoint: str, s3_bucket: str, s3_prefix: str,
                    aws_key: str, aws_secret: str) -> bool:
    """
    Download the current index.json from the bucket, append/update this run's
    entry, and re-upload.  Returns True on success.
    """
    import tempfile
    import urllib.request
    import urllib.error
    import base64

    index_key  = f"{s3_prefix.rstrip('/')}/index.json"
    index_url  = f"{s3_endpoint.rstrip('/')}/{s3_bucket}/{index_key}"
    run_entry  = {
        "run_id":        summary["run"]["run_id"],
        "timestamp":     summary["run"]["timestamp"],
        "chart_version": summary["run"]["chart_version"],
        "profile":       summary["run"]["profile"],
        "passed":        summary["run"]["passed"],
        "failed":        summary["run"]["failed"],
        "total_tests":   summary["run"]["total_tests"],
        "duration_min":  summary["run"]["duration_min"],
        "summary_path":  f"{s3_prefix.rstrip('/')}/{summary['run']['run_id']}/results/perf-summary.json",
    }

    # Try to load existing index
    index: dict = {"runs": [], "updated_at": ""}
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        # TODO: add S3 auth headers for private buckets
        with urllib.request.urlopen(index_url, context=ctx, timeout=10) as r:
            index = json.loads(r.read())
    except Exception:
        pass  # New index

    # Upsert this run
    runs = [x for x in index.get("runs", []) if x.get("run_id") != run_entry["run_id"]]
    runs.insert(0, run_entry)
    index = {"runs": runs, "updated_at": datetime.now(timezone.utc).isoformat()}

    # Write locally
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text(json.dumps(index, indent=2))

    # Upload via aws cli / mc
    try:
        endpoint_arg = f"--endpoint-url {s3_endpoint}" if s3_endpoint else ""
        cmd = f"aws s3 cp {tmp} s3://{s3_bucket}/{index_key} {endpoint_arg} --no-progress"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                env={**os.environ, "AWS_ACCESS_KEY_ID": aws_key,
                                     "AWS_SECRET_ACCESS_KEY": aws_secret},
                                timeout=30)
        tmp.unlink(missing_ok=True)
        if result.returncode == 0:
            print(f"[OK] Index updated: s3://{s3_bucket}/{index_key}")
            return True
        else:
            print(f"[WARN] Index upload failed: {result.stderr[:200]}")
    except Exception as e:
        print(f"[WARN] Could not update index: {e}")
    tmp.unlink(missing_ok=True)
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate perf-summary.json for a run")
    parser.add_argument("--run-dir",      required=True, help="Path to perf run directory")
    parser.add_argument("--update-index", action="store_true",
                        help="Also update the bucket-level index.json")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"[ERROR] Run directory not found: {run_dir}", file=sys.stderr)
        raise SystemExit(1)

    summary = build_summary(run_dir)
    out = run_dir / "results" / "perf-summary.json"
    out.write_text(json.dumps(summary, indent=2))

    r = summary["run"]
    print(f"[OK] {out}")
    print(f"     {r['total_tests']} tests · {r['passed']}/{r['total_tests']} passed · {r['duration_min']} min")
    print(f"     {len(summary['api'])} API rows · {len(summary['ingestion'])} ingestion rows")

    if args.update_index:
        s3_endpoint = os.environ.get("S3_ENDPOINT", "")
        s3_bucket   = os.environ.get("S3_BUCKET", "")
        s3_prefix   = os.environ.get("S3_PREFIX", "cost-onprem-performance")
        aws_key     = os.environ.get("AWS_ACCESS_KEY_ID", "")
        aws_secret  = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if s3_bucket:
            update_s3_index(run_dir, summary, s3_endpoint, s3_bucket, s3_prefix,
                            aws_key, aws_secret)
        else:
            print("[WARN] S3_BUCKET not set — skipping index update")


if __name__ == "__main__":
    main()
