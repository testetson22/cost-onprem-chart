#!/usr/bin/env python3
"""
Performance Run Visual Report Generator
FLPATH-4061 / FLPATH-4036

Generates a self-contained HTML snapshot of a single perf run including:
- KPI summary cards
- Test pass/fail timeline
- API latency charts (p50/p95/p99 per endpoint)
- Ingestion throughput and processing time charts
- Concurrent upload scaling chart
- Prometheus metrics time-series (if metrics snapshots are available)

Usage:
    python3 scripts/observability/generate-perf-run-report.py --run-dir tests/perf-runs/<run-id>
    python3 scripts/observability/generate-perf-run-report.py --run-dir tests/perf-runs/<run-id> --output report.html

The report is fully self-contained (Chart.js loaded from CDN, with inline fallback data).
"""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# KPI thresholds — green / yellow / red
#
# Each entry maps a test-name pattern to a list of KPI checks.  Every check
# has a ``metric`` (dot-path into the result's metrics dict), a comparison
# direction, and two boundaries.  The colours work as follows:
#
#   green  : value OP green_threshold
#   yellow : value OP yellow_threshold  (but not green)
#   red    : otherwise
#
# ``op`` is one of "<" (lower is better, e.g. latency) or ">" (higher is
# better, e.g. throughput).  For boolean metrics use "==" with 1/0.
# ---------------------------------------------------------------------------

KPI_THRESHOLDS: dict[str, list[dict]] = {
    # --- API Latency (P95 targets in seconds) ---
    "api_001": [
        {"label": "P95 latency",   "metric": "aggregate_p95",          "op": "<", "green": 2.0,  "yellow": 5.0,  "unit": "s"},
        {"label": "Success rate",   "metric": "aggregate_success_rate", "op": ">", "green": 0.95, "yellow": 0.80, "unit": "%"},
    ],
    "api_002": [
        {"label": "P95 latency",   "metric": "aggregate_p95",  "op": "<", "green": 2.0,  "yellow": 5.0,  "unit": "s"},
        {"label": "Success rate",   "metric": "success_rate",   "op": ">", "green": 0.95, "yellow": 0.80, "unit": "%"},
    ],
    "api_003": [
        {"label": "P95 latency",   "metric": "aggregate_p95",  "op": "<", "green": 2.0,  "yellow": 5.0,  "unit": "s"},
    ],
    "api_004": [
        {"label": "P95 latency",   "metric": "aggregate_p95",  "op": "<", "green": 1.5,  "yellow": 3.0,  "unit": "s"},
        {"label": "Success rate",   "metric": "success_rate",   "op": ">", "green": 0.95, "yellow": 0.80, "unit": "%"},
    ],
    "api_005": [
        {"label": "P95 latency",   "metric": "aggregate_p95",  "op": "<", "green": 5.0,  "yellow": 10.0, "unit": "s"},
        {"label": "Success rate",   "metric": "success_rate",   "op": ">", "green": 0.90, "yellow": 0.70, "unit": "%"},
    ],
    "api_006": [
        {"label": "P95 latency",   "metric": "aggregate_p95",  "op": "<", "green": 2.0,  "yellow": 5.0,  "unit": "s"},
        {"label": "Success rate",   "metric": "success_rate",   "op": ">", "green": 0.95, "yellow": 0.80, "unit": "%"},
    ],
    "api_status": [
        {"label": "P95 latency",   "metric": "aggregate_p95",  "op": "<", "green": 0.5,  "yellow": 1.0,  "unit": "s"},
    ],
    # --- Ingestion ---
    "ing_001": [
        {"label": "Processing done", "metric": "processing_completed", "op": "==", "green": 1, "yellow": 1, "unit": "bool"},
    ],
    "ing_003": [
        {"label": "All processed", "metric": "processing_completed", "op": "==", "green": 1, "yellow": 1, "unit": "bool"},
    ],
    "ing_004": [
        {"label": "Processing done", "metric": "processing_completed", "op": "==", "green": 1, "yellow": 1, "unit": "bool"},
    ],
    "ing_005": [
        {"label": "Error rate",    "metric": "error_rate",           "op": "<", "green": 0.05, "yellow": 0.10, "unit": "%"},
    ],
    "ing_006": [
        {"label": "Within 6h window", "metric": "within_window", "op": "==", "green": 1, "yellow": 1, "unit": "bool"},
    ],
    # --- ROS ---
    "ros_001": [
        {"label": "Experiments",      "metric": "experiment_count",        "op": ">", "green": 1, "yellow": 1, "unit": ""},
        {"label": "E2E time",         "metric": "total_e2e_time_sec",     "op": "<", "green": 120, "yellow": 300, "unit": "s"},
    ],
    "ros_002": [
        {"label": "Experiments (90%)", "metric": "experiment_count",       "op": ">", "green": 45, "yellow": 25, "unit": ""},
    ],
    "ros_004": [
        {"label": "Experiments (80%)", "metric": "experiment_count",       "op": ">", "green": 80, "yellow": 50, "unit": ""},
    ],
    # --- Scale ---
    "scale_002": [
        {"label": "API P95 at ramp",  "metric": "final_p95_latency",     "op": "<", "green": 2.0, "yellow": 5.0, "unit": "s"},
    ],
    "scale_004": [
        {"label": "Success rate",     "metric": "success_rate",           "op": ">", "green": 0.95, "yellow": 0.90, "unit": "%"},
    ],
    "scale_005": [
        {"label": "P95 latency",      "metric": "p95_latency",           "op": "<", "green": 2.0, "yellow": 3.0, "unit": "s"},
    ],
    # --- Soak ---
    "soak_002": [
        {"label": "Memory growth/day", "metric": "max_daily_growth_pct", "op": "<", "green": 2.0, "yellow": 5.0, "unit": "%"},
    ],
}


def _resolve_metric(metrics: dict, path: str):
    """Resolve a dot-separated metric path, e.g. 'upload.upload_mb_per_second'."""
    obj = metrics
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def evaluate_kpis(result: dict) -> list[dict]:
    """Evaluate KPI thresholds for a single test result.

    Returns a list of dicts: {label, value, unit, status, green, yellow}.
    status is one of 'green', 'yellow', 'red'.
    """
    test_name = result.get("test_name", "")
    metrics = result.get("metrics") or {}
    evaluations = []

    for pattern, checks in KPI_THRESHOLDS.items():
        if pattern not in test_name:
            continue
        for check in checks:
            val = _resolve_metric(metrics, check["metric"])
            if val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue

            op = check["op"]
            g = check["green"]
            y = check["yellow"]

            if op == "<":
                status = "green" if val < g else ("yellow" if val < y else "red")
            elif op == ">":
                status = "green" if val > g else ("yellow" if val > y else "red")
            elif op == "==":
                status = "green" if val == g else "red"
            else:
                status = "green"

            evaluations.append({
                "label": check["label"],
                "value": val,
                "unit": check["unit"],
                "status": status,
                "green": g,
                "yellow": y,
            })
    return evaluations


def aggregate_kpi_status(all_evaluations: list[dict]) -> str:
    """Return worst status across all evaluations."""
    statuses = {e["status"] for e in all_evaluations}
    if "red" in statuses:
        return "red"
    if "yellow" in statuses:
        return "yellow"
    return "green"


def _kpi_status_icon(status: str) -> str:
    icons = {"green": "&#9679;", "yellow": "&#9679;", "red": "&#9679;"}
    return f'<span style="color:var(--kpi-{status});font-size:16px;vertical-align:middle;">{icons.get(status, "")}</span>'


def _build_kpi_scorecard(all_evals: list[dict], per_test: dict[str, list[dict]]) -> str:
    """Build the KPI scorecard HTML table."""
    rows = []
    for test_name, evals in per_test.items():
        short = test_name.replace("test_perf_", "").replace("_baseline", "")
        for e in evals:
            fmt_val = f'{e["value"]:.3f}' if isinstance(e["value"], float) else str(e["value"])
            fmt_thresh = f'G&lt;{e["green"]} Y&lt;{e["yellow"]}' if e.get("unit") != "bool" else "pass/fail"
            rows.append(
                f'<tr>'
                f'<td>{short}</td>'
                f'<td>{e["label"]}</td>'
                f'<td>{fmt_val} {e["unit"]}</td>'
                f'<td>{fmt_thresh}</td>'
                f'<td>{_kpi_status_icon(e["status"])} {e["status"].upper()}</td>'
                f'</tr>'
            )
    return (
        '<table class="results-table" style="margin-bottom:20px;">'
        '<thead><tr><th>Test</th><th>KPI</th><th>Value</th><th>Thresholds</th><th>Status</th></tr></thead>'
        '<tbody>' + "\n".join(rows) + '</tbody></table>'
    )


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
    meta_path = run_dir / "metadata.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except Exception:
            pass
    return {}


def load_grafana_links(run_dir: Path) -> dict:
    """Load grafana-links.json if present (written by push-grafana-snapshot.py)."""
    for candidate in [run_dir / "reports" / "grafana-links.json",
                      run_dir / "grafana-links.json"]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except Exception:
                pass
    return {}


def load_metrics_snapshots(run_dir: Path) -> list[dict]:
    """Load Prometheus metric snapshots from metrics/ directory."""
    snapshots = []
    metrics_dir = run_dir / "metrics"
    if not metrics_dir.exists():
        return snapshots
    for sf in sorted(metrics_dir.glob("snapshot_*.json")):
        try:
            snapshots.append(json.loads(sf.read_text()))
        except Exception:
            pass
    return snapshots


def parse_junit(run_dir: Path) -> Optional[dict]:
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
                "total": total, "passed": total - errors - failures - skipped,
                "failed": failures + errors, "skipped": skipped,
                "duration_s": round(duration, 1),
            }
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Chart data extraction
# ---------------------------------------------------------------------------

def extract_api_latency(results: list[dict]) -> dict:
    """Extract per-endpoint p50/p95/p99 latency data from API tests."""
    endpoints: dict = {}
    for r in results:
        if "api" not in r.get("test_name", ""):
            continue
        m = r.get("metrics") or {}
        # API-001 and similar: metrics.results is a dict keyed by endpoint path
        for ep, ep_data in (m.get("results") or {}).items():
            lat = ep_data.get("latencies") or {}
            if not lat:
                continue
            short_ep = ep.rstrip("/").split("/")[-1] or ep
            label = f'{short_ep} ({r["test_name"].split("[")[-1].rstrip("]")} iter)'
            endpoints[label] = {
                "p50": round(lat.get("p50", 0) * 1000, 1),
                "p95": round(lat.get("p95", 0) * 1000, 1),
                "p99": round(lat.get("p99", 0) * 1000, 1),
                "passed": r.get("passed", True),
            }
        # API-002: latencies dict directly in metrics
        if "latencies" in m and isinstance(m["latencies"], dict):
            lat = m["latencies"]
            users = m.get("concurrent_users", "?")
            label = f'{r["test_name"].replace("test_perf_","").split("[")[0]} {users}u'
            endpoints[label] = {
                "p50": round(lat.get("p50", 0) * 1000, 1),
                "p95": round(lat.get("p95", 0) * 1000, 1),
                "p99": round(lat.get("p99", 0) * 1000, 1),
                "passed": r.get("passed", True),
            }
    return endpoints


def extract_ingestion_throughput(results: list[dict]) -> list[dict]:
    """Extract upload throughput and processing time for ingestion tests."""
    rows = []
    for r in results:
        name = r.get("test_name", "")
        if "ing" not in name:
            continue
        m = r.get("metrics") or {}
        timings = {t["name"]: round(t["duration_seconds"], 1) for t in r.get("timings", [])}

        # ING-001 style
        upload = m.get("upload") or {}
        if upload.get("upload_mb_per_second"):
            rows.append({
                "label": name.replace("test_perf_", "").replace("_baseline", "")[:35],
                "throughput": round(upload["upload_mb_per_second"], 3),
                "size_mb": round(upload.get("package_size_mb", 0), 1),
                "upload_s": round(upload.get("upload_seconds", 0), 1),
                "processing_s": round(timings.get("summary_table_wait", timings.get("processing_wait", 0)), 1),
                "passed": r.get("passed", True),
            })
        # ING-004 style
        if "upload_throughput_mb_s" in m:
            rows.append({
                "label": name.replace("test_perf_", "").replace("_baseline", "")[:35],
                "throughput": round(m["upload_throughput_mb_s"], 3),
                "size_mb": round(m.get("actual_size_mb", 0), 1),
                "upload_s": round(m.get("upload_time_seconds", 0), 1),
                "processing_s": round(m.get("processing_time_seconds", 0) / 60, 2),
                "passed": r.get("passed", True),
            })
        # ING-005 (high frequency)
        if "total_uploads" in m and "test_duration_minutes" in m:
            total_mb = m.get("total_data_mb", 0)
            duration_s = timings.get("high_frequency_test", m["test_duration_minutes"] * 60)
            rows.append({
                "label": name.replace("test_perf_", "").replace("_baseline", "")[:35],
                "throughput": round(total_mb / duration_s, 3) if duration_s > 0 else 0,
                "size_mb": round(total_mb, 1),
                "upload_s": round(duration_s, 1),
                "processing_s": 0,
                "passed": r.get("passed", True),
            })
    return rows


def extract_concurrent_scaling(results: list[dict]) -> list[dict]:
    """ING-003: throughput vs concurrent sources."""
    rows = []
    for r in results:
        if "ing_003" not in r.get("test_name", ""):
            continue
        m = r.get("metrics") or {}
        timings = {t["name"]: t["duration_seconds"] for t in r.get("timings", [])}
        up_s = timings.get("concurrent_uploads", 1)
        total_mb = m.get("total_upload_mb", 0)
        rows.append({
            "concurrent": m.get("concurrent_sources", 0),
            "throughput": round(total_mb / up_s, 3) if up_s > 0 else 0,
            "upload_s": round(up_s, 2),
            "total_mb": round(total_mb, 2),
            "processing_s": round(timings.get("processing_wait_all", 0), 1),
            "passed": r.get("passed", True),
        })
    return sorted(rows, key=lambda x: x["concurrent"])


def extract_test_timeline(results: list[dict]) -> list[dict]:
    """All tests with duration and pass/fail for the timeline bar chart."""
    rows = []
    for r in results:
        total_s = sum(t["duration_seconds"] for t in r.get("timings", []))
        if total_s == 0:
            total_s = 1
        rows.append({
            "name": r["test_name"].replace("test_perf_", "").replace("_baseline", "")[:45],
            "duration_s": round(total_s, 1),
            "passed": r.get("passed", True),
            "error": (r.get("error_message") or "")[:80],
        })
    return rows


def extract_prometheus_series(snapshots: list[dict]) -> dict:
    """Extract time-series data from Prometheus metric snapshots."""
    series: dict = {
        "timestamps": [],
        "listener_cpu": [],
        "celery_queue_total": [],
        "db_connections": [],
        "memory_mb": [],
    }
    for snap in snapshots:
        ts = snap.get("timestamp", "")
        series["timestamps"].append(ts[:19].replace("T", " "))

        # Listener CPU
        cpu = snap.get("listener_cpu_cores") or snap.get("metrics", {}).get("listener_cpu_cores")
        series["listener_cpu"].append(round(cpu, 3) if cpu is not None else None)

        # Celery queue depth
        queues = snap.get("celery_queues") or snap.get("metrics", {}).get("celery_queues") or {}
        total_q = sum(queues.values()) if isinstance(queues, dict) else None
        series["celery_queue_total"].append(total_q)

        # DB connections
        db_conn = snap.get("db_connections") or snap.get("metrics", {}).get("db_connections")
        series["db_connections"].append(db_conn)

        # Memory
        mem = snap.get("process_memory_mb") or snap.get("metrics", {}).get("process_memory_mb")
        series["memory_mb"].append(round(mem, 1) if mem is not None else None)

    return series


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def js_array(values: list) -> str:
    return json.dumps([v if v is not None else "null" for v in values])


def js_colors(values: list[bool], true_color="#27ae60", false_color="#e74c3c") -> str:
    return json.dumps([true_color if v else false_color for v in values])


def render_html(run_dir: Path, output_path: Path) -> None:
    session        = load_session(run_dir)
    metadata       = load_metadata(run_dir)
    junit          = parse_junit(run_dir)
    snapshots      = load_metrics_snapshots(run_dir)
    grafana_links  = load_grafana_links(run_dir)

    if not session:
        print(f"[WARN] No session JSON found in {run_dir}/results/ — report will be sparse")
        results = []
    else:
        results = session.get("results", [])

    # Extract all chart data
    api_latency     = extract_api_latency(results)
    ing_throughput  = extract_ingestion_throughput(results)
    concurrent      = extract_concurrent_scaling(results)
    timeline        = extract_test_timeline(results)
    prom_series     = extract_prometheus_series(snapshots)

    # KPIs
    run_id       = run_dir.name
    chart_ver    = metadata.get("chart_version", session.get("results", [{}])[0].get("chart_version", "unknown") if results else "unknown")
    profile      = metadata.get("perf_profile", results[0].get("profile", "unknown") if results else "unknown")
    ts_raw       = metadata.get("created_at", session.get("timestamp", "") if session else "")
    ts_str       = ts_raw[:19].replace("T", " ") + " UTC" if ts_raw else "unknown"
    cluster_info = metadata.get("cluster_info", results[0].get("cluster_info", {}) if results else {})
    # Session results are the source of truth for pass/fail because they
    # capture metric-based failures (e.g. 0% success_rate) that may not
    # trigger a pytest assertion.  JUnit is only used for skipped count and
    # duration since it includes tests the session collector never sees.
    session_passed = sum(1 for r in results if r.get("passed"))
    session_failed = sum(1 for r in results if not r.get("passed"))
    if results:
        passed  = session_passed
        failed  = session_failed
        total   = junit["total"] if junit else len(results)
        skipped = junit["skipped"] if junit else 0
    elif junit:
        total   = junit["total"]
        passed  = junit["passed"]
        failed  = junit["failed"]
        skipped = junit["skipped"]
    else:
        total = passed = failed = skipped = 0
    dur_min = round((junit["duration_s"] if junit else sum(
        sum(t["duration_seconds"] for t in r.get("timings", [])) for r in results
    )) / 60, 1)

    # Avg upload throughput
    avg_throughput = round(
        sum(r["throughput"] for r in ing_throughput if r["throughput"] > 0) /
        max(len([r for r in ing_throughput if r["throughput"] > 0]), 1), 3
    )

    # KPI evaluation
    all_kpi_evals: list[dict] = []
    per_test_kpis: dict[str, list[dict]] = {}
    for r in results:
        evals = evaluate_kpis(r)
        if evals:
            all_kpi_evals.extend(evals)
            per_test_kpis[r["test_name"]] = evals
    overall_kpi = aggregate_kpi_status(all_kpi_evals) if all_kpi_evals else "green"
    kpi_green  = sum(1 for e in all_kpi_evals if e["status"] == "green")
    kpi_yellow = sum(1 for e in all_kpi_evals if e["status"] == "yellow")
    kpi_red    = sum(1 for e in all_kpi_evals if e["status"] == "red")
    kpi_total  = len(all_kpi_evals)

    has_prom   = len(snapshots) > 0
    has_api    = len(api_latency) > 0
    has_ing    = len(ing_throughput) > 0
    has_conc   = len(concurrent) > 0

    # Conditional chart card HTML blocks (built before the f-string)
    api_chart_html = (
        '<div class="chart-card wide"><h3>API Response Latency (ms)</h3>'
        '<canvas id="latencyChart"></canvas></div>'
    ) if has_api else ""

    throughput_chart_html = (
        '<div class="chart-card"><h3>Upload Throughput (MB/s)</h3>'
        '<canvas id="throughputChart"></canvas></div>'
    ) if has_ing else ""

    proc_chart_html = (
        '<div class="chart-card"><h3>Processing Time per Ingestion Test (min)</h3>'
        '<canvas id="procChart"></canvas></div>'
    ) if has_ing else ""

    conc_chart_html = (
        '<div class="chart-card"><h3>Concurrent Upload Scaling</h3>'
        '<canvas id="concChart"></canvas></div>'
    ) if has_conc else ""

    cpu_chart_html = (
        '<div class="chart-card wide"><h3>Listener CPU (cores) over time</h3>'
        '<canvas id="cpuChart"></canvas></div>'
    ) if has_prom else ""

    queue_chart_html = (
        '<div class="chart-card"><h3>Celery Queue Depth over time</h3>'
        '<canvas id="queueChart"></canvas></div>'
    ) if has_prom else ""

    # Chart.js data blobs
    tl_labels  = js_array([r["name"] for r in timeline])
    tl_durations = js_array([r["duration_s"] for r in timeline])
    tl_colors  = js_colors([r["passed"] for r in timeline])

    api_labels = js_array(list(api_latency.keys()))
    api_p50    = js_array([v["p50"] for v in api_latency.values()])
    api_p95    = js_array([v["p95"] for v in api_latency.values()])
    api_p99    = js_array([v["p99"] for v in api_latency.values()])

    ing_labels  = js_array([r["label"] for r in ing_throughput])
    ing_tput    = js_array([r["throughput"] for r in ing_throughput])
    ing_proc    = js_array([r["processing_s"] for r in ing_throughput])
    ing_colors  = js_colors([r["passed"] for r in ing_throughput])

    conc_labels   = js_array([str(r["concurrent"]) for r in concurrent])
    conc_tput     = js_array([r["throughput"] for r in concurrent])
    conc_proc     = js_array([r["processing_s"] for r in concurrent])

    prom_ts    = js_array(prom_series["timestamps"])
    prom_cpu   = js_array(prom_series["listener_cpu"])
    prom_queue = js_array(prom_series["celery_queue_total"])
    prom_mem   = js_array(prom_series["memory_mb"])

    # Conditional JS blocks built before the f-string
    js_api_chart = (
        f"new Chart(document.getElementById('latencyChart'), {{ type: 'bar', data: {{ labels: {api_labels},"
        f" datasets: [{{ label: 'p50', data: {api_p50}, backgroundColor: 'rgba(41,128,185,0.7)', borderRadius: 2 }},"
        f" {{ label: 'p95', data: {api_p95}, backgroundColor: 'rgba(230,126,34,0.7)', borderRadius: 2 }},"
        f" {{ label: 'p99', data: {api_p99}, backgroundColor: 'rgba(231,76,60,0.7)', borderRadius: 2 }}] }},"
        f" options: {{ responsive: true, plugins: {{ legend: {{ position: 'top' }},"
        f"   annotation: {{ annotations: {{"
        f"     greenLine: {{ type: 'line', yMin: 2000, yMax: 2000, borderColor: 'rgba(39,174,96,0.8)', borderWidth: 2, borderDash: [6,3],"
        f"       label: {{ display: true, content: 'KPI: Green (2s)', position: 'start', backgroundColor: 'rgba(39,174,96,0.8)', font: {{size:10}} }} }},"
        f"     yellowLine: {{ type: 'line', yMin: 5000, yMax: 5000, borderColor: 'rgba(243,156,18,0.8)', borderWidth: 2, borderDash: [6,3],"
        f"       label: {{ display: true, content: 'KPI: Warn (5s)', position: 'start', backgroundColor: 'rgba(243,156,18,0.8)', font: {{size:10}} }} }}"
        f"   }} }} }},"
        f" scales: {{ x: {{ ticks: {{ font: {{ size: 9 }}, maxRotation: 45 }} }},"
        f" y: {{ grid: {{ color: gridColor }}, title: {{ display: true, text: 'ms' }} }} }} }} }});"
    ) if has_api else ""

    js_throughput_chart = (
        f"new Chart(document.getElementById('throughputChart'), {{ type: 'bar', data: {{ labels: {ing_labels},"
        f" datasets: [{{ label: 'MB/s', data: {ing_tput}, backgroundColor: {ing_colors}, borderRadius: 3 }}] }},"
        f" options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},"
        f" scales: {{ x: {{ ticks: {{ font: {{ size: 9 }} }} }},"
        f" y: {{ grid: {{ color: gridColor }}, title: {{ display: true, text: 'MB/s' }} }} }} }} }});"
    ) if has_ing else ""

    js_proc_chart = (
        f"new Chart(document.getElementById('procChart'), {{ type: 'bar', data: {{ labels: {ing_labels},"
        f" datasets: [{{ label: 'Processing (min)', data: {ing_proc},"
        f" backgroundColor: 'rgba(142,68,173,0.7)', borderRadius: 3 }}] }},"
        f" options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},"
        f" scales: {{ x: {{ ticks: {{ font: {{ size: 9 }} }} }},"
        f" y: {{ grid: {{ color: gridColor }}, title: {{ display: true, text: 'minutes' }} }} }} }} }});"
    ) if has_ing else ""

    js_conc_chart = (
        f"new Chart(document.getElementById('concChart'), {{ type: 'bar', data: {{ labels: {conc_labels},"
        f" datasets: [{{ label: 'Throughput (MB/s)', data: {conc_tput},"
        f" backgroundColor: 'rgba(39,174,96,0.7)', yAxisID: 'y', borderRadius: 3 }},"
        f" {{ label: 'Processing (s)', data: {conc_proc},"
        f" backgroundColor: 'rgba(231,76,60,0.5)', yAxisID: 'y1', borderRadius: 3 }}] }},"
        f" options: {{ responsive: true, plugins: {{ legend: {{ position: 'top' }} }},"
        f" scales: {{ x: {{ title: {{ display: true, text: 'concurrent sources' }} }},"
        f" y: {{ position: 'left', title: {{ display: true, text: 'MB/s' }}, grid: {{ color: gridColor }} }},"
        f" y1: {{ position: 'right', title: {{ display: true, text: 'proc seconds' }},"
        f" grid: {{ drawOnChartArea: false }} }} }} }} }});"
    ) if has_conc else ""

    js_cpu_chart = (
        f"new Chart(document.getElementById('cpuChart'), {{ type: 'line', data: {{ labels: {prom_ts},"
        f" datasets: [{{ label: 'Listener CPU (cores)', data: {prom_cpu},"
        f" borderColor: 'rgba(41,128,185,0.9)', backgroundColor: 'rgba(41,128,185,0.1)',"
        f" tension: 0.3, fill: true, pointRadius: 2 }}] }},"
        f" options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},"
        f" scales: {{ x: {{ ticks: {{ maxRotation: 45, font: {{ size: 9 }} }} }},"
        f" y: {{ min: 0, grid: {{ color: gridColor }}, title: {{ display: true, text: 'cores' }} }} }} }} }});"
    ) if has_prom else ""

    js_queue_chart = (
        f"new Chart(document.getElementById('queueChart'), {{ type: 'line', data: {{ labels: {prom_ts},"
        f" datasets: [{{ label: 'Queue depth', data: {prom_queue},"
        f" borderColor: 'rgba(231,76,60,0.9)', backgroundColor: 'rgba(231,76,60,0.1)',"
        f" tension: 0.2, fill: true, pointRadius: 2 }}] }},"
        f" options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},"
        f" scales: {{ x: {{ ticks: {{ maxRotation: 45, font: {{ size: 9 }} }} }},"
        f" y: {{ min: 0, grid: {{ color: gridColor }}, title: {{ display: true, text: 'tasks' }} }} }} }} }});"
    ) if has_prom else ""

    ocp_ver    = cluster_info.get("ocp_version", "?")
    nodes      = cluster_info.get("node_count", "?")
    storage    = cluster_info.get("storage_type", "?")
    pass_color = "#27ae60" if failed == 0 else "#e74c3c"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Grafana links banner
    grafana_banner = ""
    snap_url = grafana_links.get("snapshot_url", "")
    live_url = grafana_links.get("live_dashboard_url", "")
    if snap_url or live_url:
        g_parts = []
        if snap_url:
            g_parts.append(f'<a href="{snap_url}" target="_blank" class="g-btn">Grafana Snapshot</a>')
        if live_url:
            g_parts.append(f'<a href="{live_url}" target="_blank" class="g-btn g-btn-live">Live Dashboard</a>')
        grafana_banner = (
            '<div class="grafana-bar" id="grafana-bar">'
            '<span class="g-label">Grafana:</span> '
            + " ".join(g_parts) + '</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Perf Run: {run_id}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
<style>
  :root {{
    --bg:#f4f6f9; --surface:#fff; --border:#dde3ec;
    --text:#2c3e50; --muted:#7f8c8d; --accent:#2980b9;
    --pass:#27ae60; --fail:#e74c3c; --warn:#e67e22;
    --kpi-green:#27ae60; --kpi-yellow:#f39c12; --kpi-red:#e74c3c;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Segoe UI',system-ui,sans-serif; background:var(--bg); color:var(--text); font-size:13px; }}
  .page {{ max-width:1280px; margin:0 auto; padding:24px; }}
  h1 {{ font-size:20px; font-weight:700; }}
  h2 {{ font-size:15px; font-weight:700; margin:28px 0 12px; padding-bottom:6px; border-bottom:2px solid var(--border); }}
  a {{ color:var(--accent); }}

  /* KPI cards */
  .kpi-row {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 18px; min-width:120px; }}
  .kpi .n {{ font-size:26px; font-weight:700; }}
  .kpi .l {{ font-size:11px; color:var(--muted); margin-top:2px; }}

  /* Meta row */
  .meta-row {{ display:flex; gap:20px; flex-wrap:wrap; font-size:12px; color:var(--muted); margin-bottom:8px; }}
  .meta-row strong {{ color:var(--text); }}

  /* Chart grid */
  .chart-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(480px,1fr)); gap:20px; }}
  .chart-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; }}
  .chart-card h3 {{ font-size:13px; font-weight:600; margin-bottom:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; }}
  .chart-card canvas {{ max-height:280px; }}
  .chart-card.wide {{ grid-column:1/-1; }}

  /* Results table */
  .results-table {{ width:100%; border-collapse:collapse; font-size:12px; background:var(--surface); border-radius:8px; overflow:hidden; border:1px solid var(--border); }}
  .results-table th {{ background:#eef2f7; padding:8px 12px; text-align:left; font-weight:600; font-size:11px; }}
  .results-table td {{ padding:7px 12px; border-top:1px solid #f0f0f0; vertical-align:top; }}
  .results-table tr:hover td {{ background:#f9fbfd; }}
  .badge {{ display:inline-block; border-radius:3px; padding:1px 6px; font-size:10px; font-weight:700; color:#fff; }}
  .pass {{ background:var(--pass); }}
  .fail {{ background:var(--fail); }}
  .skip {{ background:var(--muted); }}
  .err-msg {{ font-size:10px; color:var(--fail); max-width:320px; word-break:break-word; }}

  .footer {{ margin-top:32px; font-size:11px; color:var(--muted); text-align:center; }}

  /* Grafana links banner */
  .grafana-bar {{ background:#1a1a2e; color:#eee; padding:10px 24px; border-radius:8px; display:flex; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
  .g-label {{ font-size:12px; color:#aaa; font-weight:600; text-transform:uppercase; letter-spacing:.5px; }}
  .g-btn {{ display:inline-block; padding:6px 14px; border-radius:5px; text-decoration:none; font-size:12px; font-weight:600; background:#e6521e; color:#fff; }}
  .g-btn:hover {{ background:#c44415; }}
  .g-btn-live {{ background:#2980b9; }}
  .g-btn-live:hover {{ background:#1e6090; }}
</style>
</head>
<body>
<div class="page">

  <h1>Performance Run Report</h1>
  {grafana_banner}
  <div class="meta-row">
    <span><strong>Run:</strong> {run_id}</span>
    <span><strong>Chart:</strong> {chart_ver}</span>
    <span><strong>Profile:</strong> {profile}</span>
    <span><strong>Cluster:</strong> OCP {ocp_ver} · {nodes} nodes · {storage}</span>
    <span><strong>Started:</strong> {ts_str}</span>
    <span><strong>Generated:</strong> {generated_at}</span>
  </div>

  <!-- KPI Summary -->
  <div class="kpi-row">
    <div class="kpi" style="border-color:{pass_color}">
      <div class="n" style="color:{pass_color}">{passed}/{total}</div>
      <div class="l">Tests passed</div>
    </div>
    <div class="kpi" {"style=\"border-color:var(--fail)\"" if failed > 0 else ""}>
      <div class="n" {"style=\"color:var(--fail)\"" if failed > 0 else ""}>{failed}</div>
      <div class="l">Failed</div>
    </div>
    <div class="kpi">
      <div class="n">{dur_min} min</div>
      <div class="l">Total duration</div>
    </div>
    <div class="kpi">
      <div class="n">{avg_throughput} MB/s</div>
      <div class="l">Avg upload throughput</div>
    </div>
    {f'<div class="kpi"><div class="n">{len(snapshots)}</div><div class="l">Metrics snapshots</div></div>' if has_prom else ""}
    <div class="kpi" style="border-color:var(--kpi-{overall_kpi})">
      <div class="n" style="color:var(--kpi-{overall_kpi})">{kpi_green}/{kpi_total}</div>
      <div class="l">KPIs passing</div>
    </div>
  </div>

  <!-- KPI Scorecard -->
  <h2>KPI Scorecard</h2>
  {"<p style='color:var(--muted);font-size:12px;margin-bottom:8px;'>No KPI thresholds matched for this run's tests.</p>" if not all_kpi_evals else ""}
  {_build_kpi_scorecard(all_kpi_evals, per_test_kpis) if all_kpi_evals else ""}

  <!-- Charts -->
  <h2>Test Execution</h2>
  <div class="chart-grid">

    <div class="chart-card wide">
      <h3>Test Duration Timeline (seconds)</h3>
      <canvas id="timelineChart"></canvas>
    </div>

    {api_chart_html}

    {throughput_chart_html}

    {proc_chart_html}

    {conc_chart_html}

    {cpu_chart_html}

    {queue_chart_html}

  </div>

  <!-- Full Results Table -->
  <h2>All Test Results</h2>
  <table class="results-table">
    <thead><tr><th>Test</th><th>Status</th><th>Duration</th><th>Key Metrics</th><th>KPI</th><th>Error</th></tr></thead>
    <tbody>
    {"".join(_result_row(r, per_test_kpis.get(r["test_name"], [])) for r in results)}
    </tbody>
  </table>

  <div class="footer">Generated by generate-perf-run-report.py · {generated_at}</div>
</div>

<script>
Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";
Chart.defaults.font.size = 11;
const gridColor = 'rgba(0,0,0,0.06)';

// Timeline
new Chart(document.getElementById('timelineChart'), {{
  type: 'bar',
  data: {{
    labels: {tl_labels},
    datasets: [{{
      label: 'Duration (s)',
      data: {tl_durations},
      backgroundColor: {tl_colors},
      borderRadius: 3,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ grid: {{ color: gridColor }}, title: {{ display: true, text: 'seconds' }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }} }} }}
    }}
  }}
}});

{js_api_chart}

{js_throughput_chart}

{js_proc_chart}

{js_conc_chart}

{js_cpu_chart}

{js_queue_chart}
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"[OK] Run report written to: {output_path}")
    print(f"     {len(results)} tests · {passed}/{total} passed · {dur_min} min")


def _result_row(r: dict, kpis: list[dict] | None = None) -> str:
    name   = r["test_name"].replace("test_perf_", "").replace("_baseline", "")
    passed = r.get("passed", True)
    badge  = '<span class="badge pass">PASS</span>' if passed else '<span class="badge fail">FAIL</span>'
    dur_s  = round(sum(t["duration_seconds"] for t in r.get("timings", [])), 1)
    dur    = f"{dur_s}s" if dur_s < 120 else f"{dur_s/60:.1f}min"
    m      = r.get("metrics") or {}
    err    = r.get("error_message") or ""

    # Summarize key metric
    bits = []
    upload = m.get("upload") or {}
    if upload.get("upload_mb_per_second"):
        bits.append(f'{upload["upload_mb_per_second"]:.3f} MB/s')
    if "upload_throughput_mb_s" in m:
        bits.append(f'{m["upload_throughput_mb_s"]:.3f} MB/s')
    if "aggregate_p95" in m:
        bits.append(f'p95={round(m["aggregate_p95"]*1000,1)}ms')
    if "requests_per_second" in m:
        bits.append(f'{m["requests_per_second"]:.1f} req/s')
    if "concurrent_sources" in m:
        bits.append(f'{m["concurrent_sources"]} concurrent')
    if "within_window" in m:
        bits.append(f'6h window: {"✅" if m["within_window"] else "❌"}')
    metrics_str = " · ".join(bits) if bits else "—"

    kpi_badges = ""
    if kpis:
        kpi_parts = []
        for e in kpis:
            kpi_parts.append(f'{_kpi_status_icon(e["status"])}')
        kpi_badges = " ".join(kpi_parts)

    err_cell = f'<div class="err-msg">{err}</div>' if err else ""
    return (
        f'<tr><td>{name}</td><td>{badge}</td>'
        f'<td>{dur}</td><td>{metrics_str}</td>'
        f'<td>{kpi_badges}</td>'
        f'<td>{err_cell}</td></tr>\n'
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate visual HTML report for a single perf run")
    parser.add_argument("--run-dir", required=True, help="Path to perf run directory")
    parser.add_argument("--output", default=None, help="Output HTML path (default: <run-dir>/reports/perf-run-report.html)")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"[ERROR] Run directory not found: {run_dir}")
        raise SystemExit(1)

    if args.output:
        output_path = Path(args.output).resolve()
    else:
        reports_dir = run_dir / "reports"
        reports_dir.mkdir(exist_ok=True)
        output_path = reports_dir / "perf-run-report.html"

    render_html(run_dir, output_path)


if __name__ == "__main__":
    main()
