"""Statistics / metrics / process-manager endpoints.

- ``GET /stats.json`` — machine-readable metrics + process snapshot (JSON).
- ``GET /stats``      — a self-contained, auto-refreshing HTML dashboard.

Both respect the optional inbound ``PROXY_API_KEY`` like every other endpoint
(they are not in the auth-exempt set). Metrics are per-worker: see
:mod:`llmproxy.metrics`.
"""

from flask import Blueprint, Response, jsonify

from ...metrics import process_info
from ..container import deps

bp = Blueprint("stats", __name__)


def _payload():
    """Assemble the combined metrics + process snapshot."""
    container = deps()
    return {
        "metrics": container.metrics.snapshot(),
        "process": process_info(container.settings),
        "models": {
            "exposed": list(container.registry.models),
            "default": container.registry.default_model,
            "embeddings": container.registry.embeddings_model,
        },
    }


@bp.route("/stats.json", methods=["GET"])
def stats_json():
    """Return the raw metrics + process snapshot as JSON."""
    return jsonify(_payload())


@bp.route("/stats", methods=["GET"])
def stats_dashboard():
    """Render the HTML metrics / process dashboard (auto-refreshing)."""
    return Response(_render(_payload()), mimetype="text/html; charset=utf-8")


# --- HTML rendering (self-contained, no external assets) --------------------

def _fmt_uptime(seconds):
    """Render a duration in seconds as ``NdNhNmNs`` (dropping leading zero units)."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _rows(mapping):
    """Render a dict as sorted ``<tr>`` label/value rows (most frequent first for counts)."""
    if not mapping:
        return '<tr><td class="k">—</td><td class="v">0</td></tr>'
    items = sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0]))
    return "".join(f'<tr><td class="k">{k}</td><td class="v">{v}</td></tr>' for k, v in items)


def _render(payload):
    """Build the dashboard HTML from a :func:`_payload` snapshot."""
    m = payload["metrics"]
    p = payload["process"]
    md = payload["models"]
    req = m["requests"]
    lat = m["latency_ms"]
    tok = m["tokens"]
    up = m["upstream"]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>llmproxy · stats</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 14px/1.5 system-ui, sans-serif;
         background: #0f1115; color: #e6e6e6; }}
  @media (prefers-color-scheme: light) {{ body {{ background:#f5f6f8; color:#1a1a1a; }} }}
  header {{ padding: 20px 24px; border-bottom: 1px solid #ffffff22; display:flex;
           align-items:baseline; gap:14px; flex-wrap:wrap; }}
  header h1 {{ margin:0; font-size:18px; }}
  header .sub {{ opacity:.6; font-size:13px; }}
  .grid {{ display:grid; gap:16px; padding:24px;
          grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
  .card {{ background:#ffffff08; border:1px solid #ffffff1a; border-radius:12px; padding:16px; }}
  @media (prefers-color-scheme: light) {{ .card {{ background:#fff; border-color:#0000001a; }} }}
  .card h2 {{ margin:0 0 12px; font-size:12px; text-transform:uppercase;
             letter-spacing:.08em; opacity:.6; }}
  .big {{ font-size:30px; font-weight:600; }}
  .kpis {{ display:flex; gap:20px; flex-wrap:wrap; }}
  .kpi small {{ display:block; opacity:.6; font-size:11px; text-transform:uppercase; letter-spacing:.06em; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:3px 0; border-bottom:1px solid #ffffff10; }}
  td.k {{ font-family: ui-monospace, monospace; opacity:.85; }}
  td.v {{ text-align:right; font-variant-numeric: tabular-nums; }}
  .foot {{ padding:0 24px 28px; opacity:.5; font-size:12px; }}
</style>
</head>
<body>
<header>
  <h1>llmproxy</h1>
  <span class="sub">statistics · metrics · process · auto-refresh 5s</span>
</header>

<div class="grid">
  <div class="card">
    <h2>Requests</h2>
    <div class="kpis">
      <div class="kpi"><span class="big">{req['total']}</span><small>total</small></div>
      <div class="kpi"><span class="big">{req['in_flight']}</span><small>in flight</small></div>
      <div class="kpi"><span class="big">{req['errors']}</span><small>errors</small></div>
    </div>
  </div>

  <div class="card">
    <h2>Latency (client-side)</h2>
    <div class="kpis">
      <div class="kpi"><span class="big">{lat['avg']}</span><small>avg ms</small></div>
      <div class="kpi"><span class="big">{lat['max']}</span><small>max ms</small></div>
      <div class="kpi"><span class="big">{lat['count']}</span><small>samples</small></div>
    </div>
  </div>

  <div class="card">
    <h2>Tokens</h2>
    <div class="kpis">
      <div class="kpi"><span class="big">{tok['prompt']}</span><small>prompt</small></div>
      <div class="kpi"><span class="big">{tok['completion']}</span><small>completion</small></div>
      <div class="kpi"><span class="big">{tok['total']}</span><small>total</small></div>
    </div>
  </div>

  <div class="card">
    <h2>Upstream (NVIDIA)</h2>
    <div class="kpis">
      <div class="kpi"><span class="big">{up['calls']}</span><small>calls</small></div>
      <div class="kpi"><span class="big">{up['errors']}</span><small>errors</small></div>
      <div class="kpi"><span class="big">{up['avg_latency_ms']}</span><small>avg ms</small></div>
      <div class="kpi"><span class="big">{up['max_latency_ms']}</span><small>max ms</small></div>
    </div>
  </div>

  <div class="card">
    <h2>Process manager</h2>
    <table>
      <tr><td class="k">pid</td><td class="v">{p['pid']}</td></tr>
      <tr><td class="k">server</td><td class="v">{p['server']}</td></tr>
      <tr><td class="k">workers</td><td class="v">{p['workers_configured']}</td></tr>
      <tr><td class="k">threads/worker</td><td class="v">{p['threads_per_worker']}</td></tr>
      <tr><td class="k">worker timeout</td><td class="v">{p['worker_timeout_seconds']}s</td></tr>
      <tr><td class="k">memory rss</td><td class="v">{p['memory_rss_mb']} MiB</td></tr>
      <tr><td class="k">uptime</td><td class="v">{_fmt_uptime(m['uptime_seconds'])}</td></tr>
      <tr><td class="k">python</td><td class="v">{p['python']}</td></tr>
    </table>
  </div>

  <div class="card">
    <h2>Requests by status</h2>
    <table>{_rows(req['by_status'])}</table>
  </div>

  <div class="card">
    <h2>Requests by path</h2>
    <table>{_rows(req['by_path'])}</table>
  </div>

  <div class="card">
    <h2>Models</h2>
    <table>
      <tr><td class="k">default</td><td class="v">{md['default']}</td></tr>
      <tr><td class="k">embeddings</td><td class="v">{md['embeddings']}</td></tr>
      <tr><td class="k">exposed</td><td class="v">{len(md['exposed'])}</td></tr>
    </table>
  </div>
</div>

<div class="foot">
  Metrics are per-worker (this response was served by PID {p['pid']}).
  JSON at <code>/stats.json</code>. Started {m['started_at']}.
</div>
</body>
</html>"""
