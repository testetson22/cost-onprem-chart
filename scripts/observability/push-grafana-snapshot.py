#!/usr/bin/env python3
"""
Grafana Dashboard Linker for Performance Runs
FLPATH-4061

After a perf run, if Grafana is reachable:
  1. Imports the cost-onprem dashboard set (if not already present)
  2. Creates a Grafana snapshot of the run's time window (permanent, no Prometheus required)
  3. Generates a live dashboard link scoped to the run's time range (requires cluster up)
  4. Patches the perf-run-report.html to embed both links

Usage:
    # Auto-detect Grafana from oc route (cluster must be logged in)
    python3 scripts/observability/push-grafana-snapshot.py \\
        --run-dir tests/perf-runs/<run-id>

    # Explicit Grafana URL
    python3 scripts/observability/push-grafana-snapshot.py \\
        --run-dir tests/perf-runs/<run-id> \\
        --grafana-url https://grafana-grafana.apps.my-cluster.example.com

    # Dry-run (print URLs, don't modify report)
    python3 scripts/observability/push-grafana-snapshot.py \\
        --run-dir tests/perf-runs/<run-id> --dry-run

Environment variables:
    GRAFANA_URL         Grafana base URL (overrides oc route discovery)
    GRAFANA_USER        Grafana admin username (default: admin)
    GRAFANA_PASSWORD    Grafana admin password (default: admin)
    GRAFANA_NAMESPACE   OpenShift namespace for Grafana route (default: grafana)
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Grafana API helpers
# ---------------------------------------------------------------------------

class GrafanaClient:
    def __init__(self, base_url: str, user: str = "admin", password: str = "admin"):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        # Basic auth header
        import base64
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth_header = f"Basic {creds}"

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.auth_header)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # Don't verify TLS for OpenShift self-signed certs
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()
            raise RuntimeError(f"Grafana API {method} {path} → HTTP {e.code}: {body_text[:200]}") from e

    def health(self) -> bool:
        try:
            r = self._request("GET", "/api/health")
            return r.get("database") == "ok"
        except Exception:
            return False

    def get_datasource_uid(self) -> Optional[str]:
        """Return UID of the first Prometheus/Thanos datasource."""
        try:
            sources = self._request("GET", "/api/datasources")
            for s in sources:
                if s.get("type") in ("prometheus", "grafana-thanos"):
                    return s.get("uid")
        except Exception:
            pass
        return None

    def create_datasource_if_missing(self, thanos_url: str) -> str:
        """Ensure a Prometheus datasource exists, return its UID."""
        uid = self.get_datasource_uid()
        if uid:
            return uid
        body = {
            "name": "Prometheus",
            "type": "prometheus",
            "access": "proxy",
            "url": thanos_url,
            "isDefault": True,
            "jsonData": {
                "httpMethod": "POST",
                "tlsSkipVerify": True,
            },
        }
        try:
            r = self._request("POST", "/api/datasources", body)
            return r.get("datasource", {}).get("uid") or r.get("uid", "prometheus")
        except Exception:
            return "prometheus"

    def dashboard_exists(self, uid: str) -> bool:
        try:
            self._request("GET", f"/api/dashboards/uid/{uid}")
            return True
        except Exception:
            return False

    def get_datasource_name(self) -> str:
        """Return the name of the first Prometheus datasource."""
        try:
            sources = self._request("GET", "/api/datasources")
            for s in sources:
                if s.get("type") in ("prometheus", "grafana-thanos"):
                    return s.get("name", "Prometheus")
        except Exception:
            pass
        return "Prometheus"

    def import_dashboard(self, dashboard: dict, datasource_uid: str) -> Optional[str]:
        """Import a dashboard JSON, replacing datasource UIDs. Returns the new slug URL."""
        dash = deepcopy(dashboard)
        dash.pop("id", None)
        dash.pop("version", None)
        # Replace all datasource references
        _replace_datasource(dash, datasource_uid)
        # Set the datasource template variable's current value so it resolves on load
        ds_name = self.get_datasource_name()
        for var in dash.get("templating", {}).get("list", []):
            if var.get("type") == "datasource" and var.get("name") == "datasource":
                var["current"] = {"text": ds_name, "value": datasource_uid}
                break

        body = {
            "dashboard": dash,
            "overwrite": True,
            "folderId": 0,
            "inputs": [{"name": "*", "type": "datasource", "pluginId": "prometheus", "value": datasource_uid}],
        }
        try:
            r = self._request("POST", "/api/dashboards/db", body)
            return r.get("uid") or dash.get("uid")
        except Exception:
            return None

    def save_dashboard(self, dashboard: dict) -> dict:
        """Save (upsert) a dashboard. Returns the API response with uid, id, url."""
        dash = deepcopy(dashboard)
        dash.pop("id", None)
        body = {"dashboard": dash, "overwrite": True, "folderId": 0}
        return self._request("POST", "/api/dashboards/db", body)

    def get_dashboard(self, uid: str) -> dict:
        """Return the full saved dashboard JSON (includes numeric id needed for snapshots)."""
        return self._request("GET", f"/api/dashboards/uid/{uid}")

    def create_snapshot(self, dashboard: dict, name: str, expires: int = 0) -> dict:
        """
        Create a Grafana snapshot. Returns {url, deleteUrl, key}.

        Grafana 12+ requires the dashboard to have been previously saved
        (so it has a numeric id). This method saves first, then snapshots.
        expires=0 means no expiry.
        """
        # Step 1: save the dashboard to get a numeric id
        save_resp = self.save_dashboard(dashboard)
        uid = save_resp.get("uid")
        if not uid:
            raise RuntimeError(f"Failed to save dashboard: {save_resp}")

        # Step 2: retrieve the full saved JSON (with id)
        full = self.get_dashboard(uid)
        full_dash = full.get("dashboard", dashboard)

        # Step 3: create snapshot with the full dashboard (including numeric id)
        body = {
            "dashboard": full_dash,
            "name": name,
            "expires": expires,
        }
        return self._request("POST", "/api/snapshots", body)

    def get_dashboard_url(self, uid: str, from_ms: int, to_ms: int,
                          namespace: str = "cost-onprem") -> str:
        try:
            r    = self._request("GET", f"/api/dashboards/uid/{uid}")
            slug = r.get("meta", {}).get("slug", uid)
            # Use the datasource UID (not name) so var-datasource resolves correctly
            ds_uid = self.get_datasource_uid() or "prometheus"
            return (
                f"{self.base_url}/d/{uid}/{slug}"
                f"?orgId=1&from={from_ms}&to={to_ms}"
                f"&var-namespace={namespace}&var-datasource={ds_uid}"
            )
        except Exception:
            return f"{self.base_url}/d/{uid}?from={from_ms}&to={to_ms}"


def _replace_datasource(obj, uid: str) -> None:
    """Recursively replace datasource UIDs/values in a dashboard dict."""
    if isinstance(obj, dict):
        if "datasource" in obj and isinstance(obj["datasource"], dict):
            obj["datasource"]["uid"] = uid
        if obj.get("type") == "datasource" and "current" in obj:
            obj["current"]["value"] = uid
        for v in obj.values():
            _replace_datasource(v, uid)
    elif isinstance(obj, list):
        for item in obj:
            _replace_datasource(item, uid)


# ---------------------------------------------------------------------------
# Snapshot dashboard builder (works without live Prometheus data)
# ---------------------------------------------------------------------------

def _stat_panel(panel_id: int, title: str, value, unit: str,
                color: str, x: int, y: int, w: int = 4, h: int = 3) -> dict:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "options": {
            "colorMode": "background",
            "graphMode": "none",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "color": {"fixedColor": color, "mode": "fixed"},
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]},
            }
        },
        "targets": [{"datasource": {"type": "grafana", "uid": "-- Grafana --"},
                     "queryType": "randomWalk", "refId": "A"}],
        "snapshotData": [{"fields": [
            {"name": "Value", "type": "number", "values": [value],
             "config": {"color": {"fixedColor": color, "mode": "fixed"}, "unit": unit}},
        ]}],
    }


def _bar_panel(panel_id: int, title: str, labels: list, values: list,
               colors: list, x: int, y: int, w: int = 24, h: int = 8) -> dict:
    return {
        "id": panel_id,
        "type": "barchart",
        "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "options": {
            "xField": "Test",
            "orientation": "horizontal",
            "legend": {"displayMode": "hidden"},
            "tooltip": {"mode": "single"},
            "barMaxWidth": 16,
        },
        "fieldConfig": {
            "defaults": {"unit": "s", "color": {"mode": "fixed", "fixedColor": "green"}},
            "overrides": [
                {"matcher": {"id": "byName", "options": label},
                 "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}]}
                for label, col in zip(labels, colors)
            ],
        },
        "targets": [{"datasource": {"type": "grafana", "uid": "-- Grafana --"},
                     "queryType": "randomWalk", "refId": "A"}],
        "snapshotData": [{"fields": [
            {"name": "Test", "type": "string", "values": labels},
            {"name": "Duration (s)", "type": "number", "values": values,
             "config": {"unit": "s"}},
        ]}],
    }


def _table_panel(panel_id: int, title: str, rows: list[dict],
                 x: int, y: int, w: int = 24, h: int = 12) -> dict:
    if not rows:
        return {"id": panel_id, "type": "text", "title": title,
                "gridPos": {"h": 2, "w": w, "x": x, "y": y},
                "options": {"content": "No results", "mode": "markdown"}}
    cols = list(rows[0].keys())
    fields = []
    for col in cols:
        vals = [r.get(col, "") for r in rows]
        ftype = "number" if vals and isinstance(vals[0], (int, float)) else "string"
        field: dict = {"name": col, "type": ftype, "values": vals}
        if col in ("Status",):
            field["config"] = {"custom": {"displayMode": "color-background"}}
        fields.append(field)

    return {
        "id": panel_id,
        "type": "table",
        "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
        "options": {"sortBy": [], "frameIndex": 0},
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Status"},
                 "properties": [
                     {"id": "custom.displayMode", "value": "color-background"},
                     {"id": "mappings", "value": [
                         {"type": "value", "options": {
                             "PASS": {"color": "green", "index": 0},
                             "FAIL": {"color": "red",   "index": 1},
                         }},
                     ]},
                 ]},
            ],
        },
        "targets": [{"datasource": {"type": "grafana", "uid": "-- Grafana --"},
                     "queryType": "randomWalk", "refId": "A"}],
        "snapshotData": [{"fields": fields}],
    }


def build_snapshot_dashboard(run_id: str, results: list[dict], metadata: dict) -> dict:
    """Build a Grafana dashboard populated with static snapshot data."""
    profile   = metadata.get("perf_profile", "unknown")
    chart_ver = metadata.get("chart_version", "unknown")
    ts_str    = metadata.get("created_at", "")[:19].replace("T", " ")

    passed  = sum(1 for r in results if r.get("passed"))
    failed  = len(results) - passed
    dur_min = round(sum(
        sum(t.get("duration_seconds", 0) for t in r.get("timings", []))
        for r in results
    ) / 60, 1)

    avg_tput = 0.0
    tput_count = 0
    for r in results:
        upload = (r.get("metrics") or {}).get("upload") or {}
        if upload.get("upload_mb_per_second"):
            avg_tput += upload["upload_mb_per_second"]
            tput_count += 1
    avg_tput = round(avg_tput / tput_count, 3) if tput_count else 0.0

    # Timeline data
    timeline_labels = []
    timeline_values = []
    timeline_colors = []
    table_rows = []
    for r in results:
        short = r["test_name"].replace("test_perf_", "").replace("_baseline", "")[:40]
        dur_s = round(sum(t.get("duration_seconds", 0) for t in r.get("timings", [])), 1)
        ok    = r.get("passed", True)
        timeline_labels.append(short)
        timeline_values.append(dur_s)
        timeline_colors.append("#27ae60" if ok else "#e74c3c")

        m     = r.get("metrics") or {}
        bits  = []
        upload = m.get("upload") or {}
        if upload.get("upload_mb_per_second"):
            bits.append(f'{upload["upload_mb_per_second"]:.3f} MB/s')
        if "aggregate_p95" in m:
            bits.append(f'p95={round(m["aggregate_p95"]*1000,1)}ms')
        table_rows.append({
            "Test": short,
            "Status": "PASS" if ok else "FAIL",
            "Duration (s)": dur_s,
            "Key Metric": " · ".join(bits) if bits else "—",
            "Error": (r.get("error_message") or "")[:80],
        })

    panels = [
        # Row 1: KPI stats
        _stat_panel(1, "Tests Passed",   passed,   "none", "#27ae60", 0, 0, 4, 3),
        _stat_panel(2, "Tests Failed",   failed,   "none", "#e74c3c" if failed else "#27ae60", 4, 0, 4, 3),
        _stat_panel(3, "Total Duration", dur_min,  "m",    "#2980b9", 8, 0, 4, 3),
        _stat_panel(4, "Avg Upload",     avg_tput, "MBs",  "#8e44ad", 12, 0, 4, 3),
        _stat_panel(5, "Profile",        profile,  "none", "#7f8c8d", 16, 0, 4, 3),
        _stat_panel(6, "Chart Version",  chart_ver,"none", "#7f8c8d", 20, 0, 4, 3),
        # Row 2: Duration timeline
        _bar_panel(7, "Test Duration (seconds)", timeline_labels, timeline_values,
                   timeline_colors, 0, 3),
        # Row 3: Full results table
        _table_panel(8, "All Test Results", table_rows, 0, 11),
    ]

    return {
        "uid": f"perf-run-{run_id[:20]}",
        "title": f"Perf Run: {run_id} ({profile})",
        "description": f"Chart {chart_ver} · {ts_str} UTC · {passed}/{len(results)} passed",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "",
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {},
        "annotations": {"list": []},
        "links": [],
        "panels": panels,
        "tags": ["perf", "cost-onprem", profile, chart_ver],
        "templating": {"list": []},
    }


# ---------------------------------------------------------------------------
# Cluster / URL detection
# ---------------------------------------------------------------------------

def detect_grafana_url(namespace: str = "grafana") -> Optional[str]:
    """Try to find Grafana URL from the active OpenShift cluster."""
    try:
        result = subprocess.run(
            ["oc", "get", "route", "grafana", "-n", namespace,
             "-o", "jsonpath={.spec.host}"],
            capture_output=True, text=True, timeout=10
        )
        host = result.stdout.strip()
        if host and host != "pending":
            return f"https://{host}"
    except Exception:
        pass
    # Also try any route in the namespace (operator creates grafana-route by default)
    try:
        result = subprocess.run(
            ["oc", "get", "routes", "-n", namespace,
             "-o", "jsonpath={.items[0].spec.host}"],
            capture_output=True, text=True, timeout=10
        )
        host = result.stdout.strip()
        if host and host != "pending":
            return f"https://{host}"
    except Exception:
        pass
    return None


def start_port_forward(namespace: str = "grafana", local_port: int = 13000) -> Optional[subprocess.Popen]:
    """Start oc port-forward for Grafana service in the background. Returns the process."""
    try:
        proc = subprocess.Popen(
            ["oc", "port-forward", "-n", namespace, "svc/grafana-service",
             f"{local_port}:3000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import time
        time.sleep(2)  # Allow port-forward to establish
        if proc.poll() is not None:
            return None  # Process died
        return proc
    except Exception:
        return None


def _grafana_version(client: "GrafanaClient") -> str:
    try:
        import ssl, urllib.request
        req = urllib.request.Request(f"{client.base_url}/api/health")
        req.add_header("Authorization", client.auth_header)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
            return json.loads(r.read()).get("version", "?")
    except Exception:
        return "?"


def detect_thanos_url() -> str:
    """Get the Thanos Querier URL for user workload monitoring."""
    # From inside the cluster this is always the same
    return "https://thanos-querier.openshift-monitoring.svc.cluster.local:9091"


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


def run_time_range_ms(metadata: dict, results: list[dict]) -> tuple[int, int]:
    """Return (from_ms, to_ms) for the run's time window."""
    ts_raw = metadata.get("created_at", "")
    if ts_raw:
        try:
            # Parse ISO 8601 with or without timezone
            ts_raw = ts_raw.replace("Z", "+00:00")
            start = datetime.fromisoformat(ts_raw)
        except Exception:
            start = datetime.now(timezone.utc) - timedelta(hours=7)
    else:
        start = datetime.now(timezone.utc) - timedelta(hours=7)

    total_s = sum(
        sum(t.get("duration_seconds", 0) for t in r.get("timings", []))
        for r in results
    )
    end = start + timedelta(seconds=max(total_s, 300))

    # Add 5-minute margins for context
    from_ms = int((start - timedelta(minutes=5)).timestamp() * 1000)
    to_ms   = int((end   + timedelta(minutes=5)).timestamp() * 1000)
    return from_ms, to_ms


# ---------------------------------------------------------------------------
# HTML report patching
# ---------------------------------------------------------------------------

def patch_report_html(report_path: Path, snapshot_url: Optional[str],
                      live_url: Optional[str]) -> None:
    """Inject (or replace) the Grafana links banner in an existing perf-run-report.html."""
    if not report_path.exists():
        return
    html = report_path.read_text(encoding="utf-8")

    # Build the grafana links banner
    links_html_parts = []
    if snapshot_url:
        links_html_parts.append(
            f'<a href="{snapshot_url}" target="_blank" class="g-btn">📸 Grafana Snapshot</a>'
        )
    if live_url:
        links_html_parts.append(
            f'<a href="{live_url}" target="_blank" class="g-btn g-btn-live">📡 Live Dashboard</a>'
        )

    if not links_html_parts:
        return

    banner = (
        '\n  <div class="grafana-bar" id="grafana-bar">'
        '<span class="g-label">Grafana:</span> '
        + " ".join(links_html_parts)
        + "</div>\n"
    )

    style = """
  <style>
  .grafana-bar{background:#1a1a2e;color:#eee;padding:10px 24px;border-radius:8px;
    display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap;}
  .g-label{font-size:12px;color:#aaa;font-weight:600;text-transform:uppercase;letter-spacing:.5px;}
  .g-btn{display:inline-block;padding:6px 14px;border-radius:5px;text-decoration:none;
    font-size:12px;font-weight:600;background:#e6521e;color:#fff;}
  .g-btn:hover{background:#c44415;}
  .g-btn-live{background:#2980b9;}
  .g-btn-live:hover{background:#1e6090;}
  </style>\n"""

    import re

    # If a grafana-bar already exists, replace it entirely (allows URL updates on re-runs)
    if 'id="grafana-bar"' in html:
        html = re.sub(
            r'<div class="grafana-bar" id="grafana-bar">.*?</div>',
            banner.strip(),
            html,
            flags=re.DOTALL,
        )
        report_path.write_text(html, encoding="utf-8")
        print(f"[OK] Updated Grafana links in {report_path.name}")
        return

    # First injection: add style block + banner after the opening page div
    if '<div class="page">' in html:
        html = html.replace('<div class="page">', '<div class="page">' + style + banner, 1)
        report_path.write_text(html, encoding="utf-8")
        print(f"[OK] Patched {report_path.name} with Grafana links")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Link Grafana dashboard to a perf run")
    parser.add_argument("--run-dir",      required=True, help="Path to perf run directory")
    parser.add_argument("--grafana-url",  default=None,  help="Grafana base URL")
    parser.add_argument("--grafana-user", default=None,  help="Grafana admin user")
    parser.add_argument("--grafana-pass", default=None,  help="Grafana admin password")
    parser.add_argument("--namespace",    default="grafana", help="OpenShift namespace for Grafana")
    parser.add_argument("--dry-run",      action="store_true", help="Print URLs without modifying files")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"[ERROR] Run directory not found: {run_dir}", file=sys.stderr)
        raise SystemExit(1)

    # Resolve credentials
    grafana_url  = args.grafana_url  or os.environ.get("GRAFANA_URL")
    grafana_user = args.grafana_user or os.environ.get("GRAFANA_USER", "admin")
    grafana_pass = args.grafana_pass or os.environ.get("GRAFANA_PASSWORD", "admin")

    # Auto-detect if not provided
    if not grafana_url:
        print("[INFO] Detecting Grafana URL from cluster route...")
        grafana_url = detect_grafana_url(args.namespace)
        if not grafana_url:
            print("[WARN] Grafana not found on cluster — skipping dashboard linking")
            print("       Set GRAFANA_URL or deploy with SKIP_GRAFANA=false ./deploy-observability.sh")
            raise SystemExit(0)

    print(f"[INFO] Grafana URL: {grafana_url}")

    # Load run data
    session  = load_session(run_dir)
    metadata = load_metadata(run_dir)
    results  = (session or {}).get("results", [])
    run_id   = run_dir.name

    if not results:
        print("[WARN] No session results found — snapshot will be sparse")

    # Build dashboard regardless (needed for dry-run preview too)
    dashboard = build_snapshot_dashboard(run_id, results, metadata)

    if args.dry_run:
        print("[DRY-RUN] Would create Grafana snapshot and live dashboard link")
        print(f"  Dashboard title:   {dashboard['title']}")
        print(f"  Snapshot panels:   {len(dashboard['panels'])}")
        print(f"  Tests in snapshot: {len(results)}")
        from_ms, to_ms = run_time_range_ms(metadata, results)
        print(f"  Time range:        from={from_ms} to={to_ms}")
        print(f"  Live URL preview:  {grafana_url}/d/...?from={from_ms}&to={to_ms}")
        return

    # Connect — try direct URL first, fall back to port-forward
    pf_proc = None
    effective_url = grafana_url
    client = GrafanaClient(effective_url, grafana_user, grafana_pass)

    if not client.health():
        print(f"[INFO] Direct URL not reachable — trying port-forward to grafana-service...")
        local_port = 13000
        pf_proc = start_port_forward(args.namespace, local_port)
        if pf_proc:
            effective_url = f"http://localhost:{local_port}"
            client = GrafanaClient(effective_url, grafana_user, grafana_pass)
            if not client.health():
                pf_proc.terminate()
                print(f"[WARN] Grafana not reachable via port-forward either — skipping", file=sys.stderr)
                raise SystemExit(0)
            print(f"[OK] Grafana reachable via port-forward (localhost:{local_port})")
        else:
            print(f"[WARN] Grafana at {grafana_url} is not healthy — skipping", file=sys.stderr)
            raise SystemExit(0)
    print(f"[OK] Grafana is healthy (v{_grafana_version(client)})")

    # Ensure datasource exists
    thanos_url    = detect_thanos_url()
    datasource_uid = client.create_datasource_if_missing(thanos_url)
    print(f"[INFO] Datasource UID: {datasource_uid}")

    snapshot_url     = None
    live_url         = None
    public_grafana_url = grafana_url  # the original route URL (before any port-forward)

    # 1. Create permanent snapshot (static, works forever)
    print("[INFO] Creating Grafana snapshot...")
    try:
        snap = client.create_snapshot(dashboard, name=dashboard["title"], expires=0)
        snapshot_url = snap.get("url") or snap.get("externalUrl")
        # Grafana returns internal URL (e.g. localhost:3000); replace with public route
        if snapshot_url:
            for internal in ["http://localhost:3000", "http://localhost:13000",
                             "https://localhost:3000", effective_url]:
                if internal in snapshot_url:
                    snapshot_url = snapshot_url.replace(internal, public_grafana_url)
                    break
            if not snapshot_url.startswith("http"):
                snapshot_url = f"{public_grafana_url}{snapshot_url}"
        print(f"[OK] Snapshot created: {snapshot_url}")
        if snap.get("deleteUrl") or snap.get("deleteKey"):
            delete_raw = snap.get("deleteUrl", f"{effective_url}/api/snapshots-delete/{snap.get('deleteKey','')}")
            # Replace any internal address (port-forward or Grafana's own localhost:3000)
            delete = delete_raw
            for internal in [effective_url, "http://localhost:3000", "https://localhost:3000"]:
                delete = delete.replace(internal, public_grafana_url)
            print(f"     Delete URL: {delete}")
    except Exception as e:
        print(f"[WARN] Could not create snapshot: {e}")

    # 2. Import live dashboards and build time-ranged link
    dashboards_dir = Path(__file__).parent / "dashboards"
    imported_uid   = None
    if dashboards_dir.exists():
        print("[INFO] Importing live dashboards...")
        for dash_file in sorted(dashboards_dir.glob("*.json")):
            try:
                dash_json = json.loads(dash_file.read_text())
                uid = client.import_dashboard(dash_json, datasource_uid)
                if uid and "overview" in dash_file.name:
                    imported_uid = uid
                print(f"  [OK] {dash_file.name} → uid={uid}")
            except Exception as e:
                print(f"  [WARN] {dash_file.name}: {e}")

    # Build live URL with time range — prefer the perf-run dashboard (uses standard k8s metrics)
    preferred_uid = "cost-onprem-perf-run"
    live_uid = preferred_uid if client.dashboard_exists(preferred_uid) else imported_uid
    if live_uid:
        from_ms, to_ms = run_time_range_ms(metadata, results)
        namespace = metadata.get("namespace", "cost-onprem")
        live_url  = client.get_dashboard_url(live_uid, from_ms, to_ms, namespace)
        # Replace internal effective_url with the public route URL
        if pf_proc and effective_url in live_url:
            live_url = live_url.replace(effective_url, public_grafana_url)
        print(f"[OK] Live dashboard URL: {live_url}")
    elif dashboards_dir.exists():
        # Fallback: link to Grafana home with time range
        from_ms, to_ms = run_time_range_ms(metadata, results)
        live_url = f"{grafana_url}/?from={from_ms}&to={to_ms}"

    # Replace any remaining port-forward localhost URLs with the public route URL
    if pf_proc:
        if snapshot_url and "localhost" in snapshot_url:
            snapshot_url = snapshot_url.replace(effective_url, public_grafana_url)
        if live_url and "localhost" in live_url:
            live_url = live_url.replace(effective_url, public_grafana_url)

    # 3. Patch the HTML report
    report_path = run_dir / "reports" / "perf-run-report.html"
    if report_path.exists():
        patch_report_html(report_path, snapshot_url, live_url)
    else:
        print(f"[INFO] Run report not found at {report_path} — skipping HTML patch")
        print("       Generate it first with: python3 generate-perf-run-report.py --run-dir ...")

    # 4. Write links to a sidecar file for CI/shell consumption
    links_file = run_dir / "reports" / "grafana-links.json"
    links = {}
    if snapshot_url:
        links["snapshot_url"] = snapshot_url
    if live_url:
        links["live_dashboard_url"] = live_url
    links["grafana_base_url"] = public_grafana_url
    links_file.write_text(json.dumps(links, indent=2))
    print(f"[OK] Links written to {links_file}")

    # Clean up port-forward
    if pf_proc:
        pf_proc.terminate()

    print()
    print("=== Grafana Links ===")
    if snapshot_url:
        print(f"  Snapshot (permanent): {snapshot_url}")
    if live_url:
        print(f"  Live dashboard:       {live_url}")
    print()


if __name__ == "__main__":
    main()
