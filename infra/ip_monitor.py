#!/usr/bin/env python3
"""
KCP IP Monitor — Real-time nginx log analyser.

Parses nginx access & error logs for all KCP peers and exposes a JSON API
consumed by the admin dashboard.  Runs as a lightweight FastAPI service
behind the portal nginx (port 8099).

Endpoints:
    GET /api/stats     → aggregated IP statistics (JSON)
    GET /              → dashboard HTML

Environment:
    LOG_DIR    — nginx log directory  (default: /var/log/nginx)
    PORT       — listen port          (default: 9100)
    PEER_COUNT — number of peers      (default: 7)

Deploy:
    python3 infra/ip_monitor.py          # development
    systemctl start kcp-ip-monitor       # production
"""

from __future__ import annotations

import os
import re
import time
import glob
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ───────────────────────────────────────────────────────────────────

LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/nginx"))
PEER_COUNT = int(os.environ.get("PEER_COUNT", "7"))
PORT = int(os.environ.get("IP_MONITOR_PORT", "9100"))

# Combined log format: $remote_addr - $remote_user [$time_local] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent"
ACCESS_RE = re.compile(
    r'^(?P<ip>\S+)\s+-\s+\S+\s+\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<method>\w+)\s+(?P<path>\S+)\s+\S+"\s+'
    r'(?P<status>\d+)\s+(?P<bytes>\d+)\s+'
    r'"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)"'
)

# nginx error log for rate limiting:
# 2026/03/24 01:23:45 [error] ... limiting requests, excess: 20.123 by zone "kcp_general", client: 1.2.3.4
ERROR_LIMIT_RE = re.compile(
    r'limiting requests.*?zone\s*"(?P<zone>[^"]+)".*?client:\s*(?P<ip>[\d.]+)'
)

# fail2ban / iptables banned IPs (optional)
BANNED_IPS: set[str] = set()

# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="KCP IP Monitor", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _parse_access_logs(minutes: int = 60) -> list[dict]:
    """Parse recent nginx access log lines across all peers."""
    entries: list[dict] = []
    cutoff = time.time() - (minutes * 60)
    
    patterns = [
        str(LOG_DIR / "kcp-peer*.access.log"),
        str(LOG_DIR / "kcp-peer*.access.log.1"),  # rotated
    ]
    
    for pattern in patterns:
        for logfile in sorted(glob.glob(pattern)):
            try:
                with open(logfile, "r", errors="replace") as f:
                    for line in f:
                        m = ACCESS_RE.match(line)
                        if not m:
                            continue
                        
                        # Parse time
                        try:
                            ts = datetime.strptime(m.group("time"), "%d/%b/%Y:%H:%M:%S %z")
                            if ts.timestamp() < cutoff:
                                continue
                        except ValueError:
                            continue
                        
                        entries.append({
                            "ip": m.group("ip"),
                            "time": ts.isoformat(),
                            "timestamp": ts.timestamp(),
                            "method": m.group("method"),
                            "path": m.group("path"),
                            "status": int(m.group("status")),
                            "bytes": int(m.group("bytes")),
                            "ua": m.group("ua"),
                            "peer": Path(logfile).stem.replace(".access", ""),
                        })
            except (PermissionError, FileNotFoundError):
                continue
    
    return entries


def _parse_error_logs(minutes: int = 60) -> list[dict]:
    """Parse rate-limit events from nginx error logs."""
    events: list[dict] = []
    cutoff = time.time() - (minutes * 60)
    
    patterns = [
        str(LOG_DIR / "kcp-peer*.error.log"),
        str(LOG_DIR / "kcp-peer*.error.log.1"),
    ]
    
    for pattern in patterns:
        for logfile in sorted(glob.glob(pattern)):
            try:
                with open(logfile, "r", errors="replace") as f:
                    for line in f:
                        m = ERROR_LIMIT_RE.search(line)
                        if not m:
                            continue
                        
                        # Extract timestamp from error log: 2026/03/24 01:23:45
                        ts_match = re.match(r'(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})', line)
                        if ts_match:
                            try:
                                ts = datetime.strptime(ts_match.group(1), "%Y/%m/%d %H:%M:%S")
                                ts = ts.replace(tzinfo=timezone.utc)
                                if ts.timestamp() < cutoff:
                                    continue
                            except ValueError:
                                continue
                        
                        events.append({
                            "ip": m.group("ip"),
                            "zone": m.group("zone"),
                            "peer": Path(logfile).stem.replace(".error", ""),
                        })
            except (PermissionError, FileNotFoundError):
                continue
    
    return events


def _get_banned_ips() -> list[dict]:
    """Get IPs banned by iptables/fail2ban."""
    banned: list[dict] = []
    
    # Check iptables DROP rules
    try:
        result = subprocess.run(
            ["sudo", "iptables", "-L", "INPUT", "-n", "--line-numbers"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1] in ("DROP", "REJECT"):
                ip = parts[4] if parts[4] != "0.0.0.0/0" else ""
                if ip and "/" not in ip:
                    banned.append({"ip": ip, "action": parts[1], "source": "iptables"})
    except Exception:
        pass
    
    # Check fail2ban if available
    try:
        result = subprocess.run(
            ["sudo", "fail2ban-client", "status"],
            capture_output=True, text=True, timeout=5,
        )
        jails = re.findall(r'Jail list:\s+(.+)', result.stdout)
        if jails:
            for jail in jails[0].split(","):
                jail = jail.strip()
                jr = subprocess.run(
                    ["sudo", "fail2ban-client", "status", jail],
                    capture_output=True, text=True, timeout=5,
                )
                ips = re.findall(r'Banned IP list:\s+(.+)', jr.stdout)
                if ips:
                    for ip in ips[0].split():
                        banned.append({"ip": ip.strip(), "action": "banned", "source": f"fail2ban/{jail}"})
    except Exception:
        pass
    
    return banned


def _build_stats(minutes: int = 60) -> dict:
    """Build comprehensive IP statistics."""
    entries = _parse_access_logs(minutes)
    rate_events = _parse_error_logs(minutes)
    banned = _get_banned_ips()
    
    now = datetime.now(timezone.utc)
    
    # ── Top IPs by request count ──
    ip_counts: dict[str, int] = defaultdict(int)
    ip_bytes: dict[str, int] = defaultdict(int)
    ip_last_seen: dict[str, str] = {}
    ip_methods: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ip_statuses: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    ip_ua: dict[str, str] = {}
    
    for e in entries:
        ip = e["ip"]
        ip_counts[ip] += 1
        ip_bytes[ip] += e["bytes"]
        ip_last_seen[ip] = e["time"]
        ip_methods[ip][e["method"]] += 1
        ip_statuses[ip][e["status"]] += 1
        ip_ua[ip] = e["ua"]
    
    top_ips = sorted(ip_counts.items(), key=lambda x: x[1], reverse=True)[:50]
    top_ips_data = []
    for ip, count in top_ips:
        errors_4xx = sum(v for k, v in ip_statuses[ip].items() if 400 <= k < 500)
        errors_5xx = sum(v for k, v in ip_statuses[ip].items() if 500 <= k < 600)
        top_ips_data.append({
            "ip": ip,
            "requests": count,
            "bytes": ip_bytes[ip],
            "last_seen": ip_last_seen.get(ip, ""),
            "methods": dict(ip_methods[ip]),
            "errors_4xx": errors_4xx,
            "errors_5xx": errors_5xx,
            "user_agent": ip_ua.get(ip, "")[:120],
        })
    
    # ── Top endpoints ──
    endpoint_counts: dict[str, int] = defaultdict(int)
    endpoint_methods: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in entries:
        path = e["path"].split("?")[0]  # strip query
        endpoint_counts[path] += 1
        endpoint_methods[path][e["method"]] += 1
    
    top_endpoints = sorted(endpoint_counts.items(), key=lambda x: x[1], reverse=True)[:30]
    top_endpoints_data = [
        {
            "path": path,
            "requests": count,
            "methods": dict(endpoint_methods[path]),
        }
        for path, count in top_endpoints
    ]
    
    # ── Rate-limited IPs ──
    rl_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ev in rate_events:
        rl_counts[ev["ip"]][ev["zone"]] += 1
    
    rate_limited = sorted(rl_counts.items(), key=lambda x: sum(x[1].values()), reverse=True)[:30]
    rate_limited_data = [
        {
            "ip": ip,
            "total_events": sum(zones.values()),
            "zones": dict(zones),
        }
        for ip, zones in rate_limited
    ]
    
    # ── Status code distribution ──
    status_dist: dict[int, int] = defaultdict(int)
    for e in entries:
        status_dist[e["status"]] += 1
    
    # ── Requests per peer ──
    peer_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        peer_counts[e["peer"]] += 1
    
    # ── Requests per minute (for sparkline) ──
    rpm: dict[str, int] = defaultdict(int)
    for e in entries:
        minute_key = e["time"][:16]  # YYYY-MM-DDTHH:MM
        rpm[minute_key] += 1
    
    return {
        "generated_at": now.isoformat(),
        "window_minutes": minutes,
        "total_requests": len(entries),
        "unique_ips": len(ip_counts),
        "top_ips": top_ips_data,
        "top_endpoints": top_endpoints_data,
        "rate_limited": rate_limited_data,
        "banned": banned,
        "status_codes": dict(sorted(status_dist.items())),
        "requests_per_peer": dict(sorted(peer_counts.items())),
        "requests_per_minute": dict(sorted(rpm.items())),
    }


# ── API Routes ───────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(minutes: int = 60):
    """Return IP monitoring statistics as JSON."""
    return JSONResponse(_build_stats(minutes))


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Serve the IP Monitor dashboard."""
    html_path = Path(__file__).parent / "ip_monitor.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>KCP IP Monitor</h1><p>Dashboard HTML not found.</p>"


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"🔍 KCP IP Monitor starting on port {PORT}")
    print(f"📁 Log directory: {LOG_DIR}")
    print(f"📊 Dashboard: http://localhost:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
