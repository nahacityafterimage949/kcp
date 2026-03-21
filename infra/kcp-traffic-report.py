#!/usr/bin/env python3
"""
KCP Traffic Reporter
====================
Consulta a API de traffic do GitHub + health dos peers KCP e envia
um relatório HTML por email.

Configuração via variáveis de ambiente (arquivo /etc/kcp/traffic-report.env):
    GITHUB_TOKEN        — Personal Access Token com permissão repo (read traffic)
    GITHUB_REPO         — ex: kcp-protocol/kcp
    REPORT_EMAIL_TO     — destinatário (pode ser múltiplos: a@x.com,b@x.com)
    REPORT_EMAIL_FROM   — remetente (ex: reports@kcp-protocol.org)
    SMTP_HOST           — ex: smtp.gmail.com
    SMTP_PORT           — ex: 587
    SMTP_USER           — usuário SMTP
    SMTP_PASS           — senha / app password
    REPORT_INTERVAL_H   — intervalo em horas (default: 1)
    PEERS_JSON_URL      — URL do registry de peers (default: https://kcp-protocol.org/peers.json)

Uso:
    python3 kcp-traffic-report.py           # envia agora e sai
    python3 kcp-traffic-report.py --daemon  # loop contínuo (systemd gerencia)
    python3 kcp-traffic-report.py --dry-run # imprime relatório sem enviar
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kcp-traffic")

# ─── Config ──────────────────────────────────────────────────────────────────

def load_env(path: str = "/etc/kcp/traffic-report.env"):
    """Load key=value env file if it exists."""
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())

def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

# ─── GitHub API ──────────────────────────────────────────────────────────────

def gh_get(path: str) -> dict | list:
    token = cfg("GITHUB_TOKEN")
    repo  = cfg("GITHUB_REPO", "kcp-protocol/kcp")
    url   = f"https://api.github.com/repos/{repo}/{path}"
    req   = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "kcp-traffic-reporter/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning(f"GitHub API {path}: HTTP {e.code}")
        return {}
    except Exception as e:
        log.warning(f"GitHub API {path}: {e}")
        return {}

def fetch_traffic() -> dict:
    """Return combined traffic data from GitHub API."""
    views     = gh_get("traffic/views")
    clones    = gh_get("traffic/clones")
    referrers = gh_get("traffic/popular/referrers")
    paths     = gh_get("traffic/popular/paths")
    stars     = gh_get("")  # repo info — has stargazers_count
    return {
        "views":     views,
        "clones":    clones,
        "referrers": referrers if isinstance(referrers, list) else [],
        "paths":     paths     if isinstance(paths, list)     else [],
        "stars":     stars.get("stargazers_count", "?") if isinstance(stars, dict) else "?",
        "forks":     stars.get("forks_count", "?")       if isinstance(stars, dict) else "?",
        "watchers":  stars.get("subscribers_count", "?") if isinstance(stars, dict) else "?",
    }

# ─── Peer Health ─────────────────────────────────────────────────────────────

def fetch_peer_health() -> list[dict]:
    """Probe each peer in peers.json and return health info."""
    peers_url = cfg("PEERS_JSON_URL", "https://kcp-protocol.org/peers.json")
    try:
        req = urllib.request.Request(peers_url, headers={"User-Agent": "kcp-traffic-reporter/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            registry = json.loads(r.read())
    except Exception as e:
        log.warning(f"peers.json fetch failed: {e}")
        return []

    results = []
    for peer in registry.get("peers", []):
        health_url = peer.get("health_url", "")
        t0 = time.time()
        try:
            req = urllib.request.Request(
                health_url,
                headers={
                    "User-Agent": "kcp-traffic-reporter/1.0",
                    "X-KCP-Client": "kcp-traffic-reporter/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                h = json.loads(r.read())
            ms = int((time.time() - t0) * 1000)
            results.append({
                "name": peer.get("name", ""),
                "url":  peer.get("url", ""),
                "status": "ok",
                "latency_ms": ms,
                "artifacts": h.get("artifacts", "?"),
                "peers_known": h.get("peers", "?"),
                "node_id": h.get("node_id", "")[:12],
                "kcp_version": h.get("kcp_version", "?"),
            })
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            results.append({
                "name": peer.get("name", ""),
                "url":  peer.get("url", ""),
                "status": "offline",
                "latency_ms": ms,
                "artifacts": "?",
                "peers_known": "?",
                "node_id": "",
                "kcp_version": "?",
                "error": str(e),
            })
    return results

# ─── Report Builder ───────────────────────────────────────────────────────────

def build_report(traffic: dict, peers: list[dict], generated_at: datetime) -> tuple[str, str]:
    """Build (subject, html_body) for the email."""

    views  = traffic.get("views", {})
    clones = traffic.get("clones", {})
    repo   = cfg("GITHUB_REPO", "kcp-protocol/kcp")
    ts     = generated_at.strftime("%d/%m/%Y %H:%M UTC")

    total_views   = views.get("count", 0)
    unique_views  = views.get("uniques", 0)
    total_clones  = clones.get("count", 0)
    unique_clones = clones.get("uniques", 0)
    stars         = traffic.get("stars", "?")

    # Daily sparkline data (last 14 days)
    daily = views.get("views", [])
    max_count = max((d.get("count", 0) for d in daily), default=1) or 1

    def spark_bar(n: int, max_n: int, width: int = 20) -> str:
        filled = round(n / max_n * width)
        return "█" * filled + "░" * (width - filled)

    daily_rows = ""
    for d in daily[-7:]:  # last 7 days only in email
        date  = d.get("timestamp", "")[:10]
        count = d.get("count", 0)
        uniq  = d.get("uniques", 0)
        bar   = spark_bar(count, max_count)
        color = "#22c55e" if count > 0 else "#94a3b8"
        daily_rows += f"""
        <tr>
          <td style="padding:6px 12px;color:#64748b;font-size:13px">{date}</td>
          <td style="padding:6px 12px;font-weight:600;color:{color}">{count}</td>
          <td style="padding:6px 12px;color:#64748b">{uniq}</td>
          <td style="padding:6px 12px;font-family:monospace;font-size:12px;color:{color}">{bar}</td>
        </tr>"""

    # Referrers
    ref_rows = ""
    for r in traffic.get("referrers", [])[:5]:
        ref_rows += f"""
        <tr>
          <td style="padding:6px 12px;color:#1e293b">{r.get('referrer','?')}</td>
          <td style="padding:6px 12px;font-weight:600">{r.get('count',0)}</td>
          <td style="padding:6px 12px;color:#64748b">{r.get('uniques',0)}</td>
        </tr>"""
    if not ref_rows:
        ref_rows = '<tr><td colspan="3" style="padding:12px;color:#94a3b8;text-align:center">Nenhum referrer registrado</td></tr>'

    # Peers
    peer_rows = ""
    for p in peers:
        ok     = p["status"] == "ok"
        dot    = "🟢" if ok else "🔴"
        latency = f"{p['latency_ms']}ms" if ok else "—"
        arts   = p.get("artifacts", "?")
        peer_rows += f"""
        <tr>
          <td style="padding:8px 12px">{dot} {p['name']}</td>
          <td style="padding:8px 12px;font-family:monospace;font-size:12px;color:#64748b">{p['node_id']}</td>
          <td style="padding:8px 12px;color:{'#22c55e' if ok else '#ef4444'}">{p['status']}</td>
          <td style="padding:8px 12px">{latency}</td>
          <td style="padding:8px 12px">{arts}</td>
        </tr>"""
    if not peer_rows:
        peer_rows = '<tr><td colspan="5" style="padding:12px;color:#94a3b8;text-align:center">Sem dados de peers</td></tr>'

    live_peers = sum(1 for p in peers if p["status"] == "ok")
    subject = f"[KCP] Traffic Report — {total_views} views · {unique_views} únicos · {live_peers}/{len(peers)} peers live — {ts}"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;margin:0;padding:0">
<div style="max-width:680px;margin:0 auto;padding:32px 16px">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#2563eb);border-radius:12px;padding:28px 32px;margin-bottom:24px">
    <div style="font-size:11px;font-weight:700;letter-spacing:2px;color:rgba(255,255,255,.6);text-transform:uppercase">Knowledge Context Protocol</div>
    <div style="font-size:22px;font-weight:800;color:#fff;margin-top:6px">Traffic Report</div>
    <div style="font-size:13px;color:rgba(255,255,255,.7);margin-top:4px">{ts} · {repo}</div>
  </div>

  <!-- Stats row -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px">
    {"".join(f'''<div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px;text-align:center">
      <div style="font-size:26px;font-weight:800;color:{c}">{v}</div>
      <div style="font-size:11px;color:#94a3b8;margin-top:4px;text-transform:uppercase;letter-spacing:.5px">{l}</div>
    </div>''' for v, l, c in [
        (total_views, "Views (14d)", "#2563eb"),
        (unique_views, "Únicos (14d)", "#7c3aed"),
        (total_clones, "Clones (14d)", "#0891b2"),
        (stars, "⭐ Stars", "#f59e0b"),
    ])}
  </div>

  <!-- Daily views -->
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:20px">
    <div style="font-weight:700;font-size:14px;color:#1e293b;margin-bottom:12px">📈 Views diárias (últimos 7 dias)</div>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f8fafc">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Data</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Views</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Únicos</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Barra</th>
        </tr>
      </thead>
      <tbody>{daily_rows}</tbody>
    </table>
  </div>

  <!-- Referrers -->
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:20px">
    <div style="font-weight:700;font-size:14px;color:#1e293b;margin-bottom:12px">🔗 Top Referrers</div>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f8fafc">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Origem</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Views</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Únicos</th>
        </tr>
      </thead>
      <tbody>{ref_rows}</tbody>
    </table>
  </div>

  <!-- Peers -->
  <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:20px;margin-bottom:20px">
    <div style="font-weight:700;font-size:14px;color:#1e293b;margin-bottom:12px">🌐 Status dos Peers — <span style="color:#22c55e">{live_peers}/{len(peers)} online</span></div>
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#f8fafc">
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Peer</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Node ID</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Status</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Latência</th>
          <th style="padding:8px 12px;text-align:left;font-size:12px;color:#64748b;font-weight:600">Artefatos</th>
        </tr>
      </thead>
      <tbody>{peer_rows}</tbody>
    </table>
  </div>

  <!-- Footer -->
  <div style="text-align:center;font-size:12px;color:#94a3b8;padding-top:16px">
    KCP Traffic Reporter · <a href="https://kcp-protocol.org" style="color:#2563eb">kcp-protocol.org</a> · gerado às {ts}
  </div>
</div>
</body>
</html>"""

    return subject, html


# ─── Email Sender ─────────────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
    smtp_host = cfg("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(cfg("SMTP_PORT", "587"))
    smtp_user = cfg("SMTP_USER")
    smtp_pass = cfg("SMTP_PASS")
    from_addr = cfg("REPORT_EMAIL_FROM", smtp_user)
    to_addrs  = [a.strip() for a in cfg("REPORT_EMAIL_TO").split(",") if a.strip()]

    if not to_addrs:
        log.error("REPORT_EMAIL_TO não configurado — email não enviado")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, msg.as_string())
        log.info(f"Email enviado para {to_addrs}")
    except Exception as e:
        log.error(f"Falha ao enviar email: {e}")
        raise


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_once(dry_run: bool = False):
    now = datetime.now(timezone.utc)
    log.info("Coletando dados de traffic do GitHub...")
    traffic = fetch_traffic()

    log.info("Verificando saúde dos peers...")
    peers = fetch_peer_health()

    log.info("Gerando relatório...")
    subject, html = build_report(traffic, peers, now)

    if dry_run:
        print("\n" + "=" * 70)
        print(f"SUBJECT: {subject}")
        print("=" * 70)
        # Print simplified text version
        v = traffic.get("views", {})
        print(f"\nViews (14d): {v.get('count',0)} | Únicos: {v.get('uniques',0)} | Stars: {traffic.get('stars','?')}")
        print("\nPeers:")
        for p in peers:
            st = "🟢" if p["status"] == "ok" else "🔴"
            print(f"  {st} {p['name']}: {p['status']} {p.get('latency_ms','')}ms | {p.get('artifacts','?')} artefatos")
        print("\nDaily views (últimos 7 dias):")
        for d in traffic.get("views", {}).get("views", [])[-7:]:
            print(f"  {d['timestamp'][:10]}  {d['count']:>4} views  {d['uniques']:>3} únicos")
        print("\nReferrers:")
        for r in traffic.get("referrers", [])[:5]:
            print(f"  {r.get('referrer','?'):<30} {r.get('count',0):>4} views")
        print("=" * 70 + "\n")
        return

    send_email(subject, html)


def main():
    parser = argparse.ArgumentParser(description="KCP Traffic Reporter")
    parser.add_argument("--daemon",  action="store_true", help="Loop contínuo (para systemd)")
    parser.add_argument("--dry-run", action="store_true", help="Imprime sem enviar email")
    args = parser.parse_args()

    load_env()

    if not cfg("GITHUB_TOKEN"):
        log.error("GITHUB_TOKEN não configurado. Configure em /etc/kcp/traffic-report.env")
        sys.exit(1)

    if args.daemon:
        interval_h = float(cfg("REPORT_INTERVAL_H", "1"))
        interval_s = interval_h * 3600
        log.info(f"Modo daemon — relatório a cada {interval_h}h")
        while True:
            try:
                run_once(dry_run=args.dry_run)
            except Exception as e:
                log.error(f"Erro no ciclo: {e}")
            log.info(f"Próximo relatório em {interval_h}h...")
            time.sleep(interval_s)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
