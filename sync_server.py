#!/usr/bin/env python3
"""
Local sync server — http://localhost:8899
Run once at login (launchd), stays alive in background.

Routes:
  /ping        — health check (used by navbar to detect if server is running)
  /sync-popup  — auto-starting popup; closes itself and notifies opener when done
  /sync        — SSE stream of scraper output
  /            — standalone page (fallback)
"""
import subprocess, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SCRAPER = Path(__file__).parent / 'scrape_pwarehouse.py'
PYTHON  = sys.executable   # use whatever python is running this server
GV_URL  = 'https://web-production-2eea96.up.railway.app'
PORT    = 8899

# ── Popup page: opens small, syncs automatically, closes itself ────────────────
POPUP = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Sync pWarehouse</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0 }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0f0f1a; color: #e0d4f7; display: flex;
    flex-direction: column; height: 100vh; overflow: hidden;
  }
  #header {
    background: #3b0764; padding: 12px 16px;
    font-size: 14px; font-weight: 600; flex-shrink: 0;
    display: flex; align-items: center; gap: 10px;
  }
  #spinner {
    width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
    border-top-color: #fff; border-radius: 50%;
    animation: spin .7s linear infinite; flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg) } }
  #log {
    flex: 1; overflow-y: auto; padding: 12px 16px;
    font-family: 'SF Mono', monospace; font-size: 12px;
    line-height: 1.65; white-space: pre-wrap; color: #d4f0c0;
  }
  #footer {
    padding: 10px 16px; background: #1a1a2e; flex-shrink: 0;
    font-size: 13px; font-weight: 600; min-height: 38px;
  }
  .ok  { color: #6fcf97 }
  .err { color: #eb5757 }
</style>
</head>
<body>
<div id="header">
  <div id="spinner"></div>
  <span id="title">Sincronizando pWarehouse…</span>
</div>
<div id="log"></div>
<div id="footer"></div>
<script>
(function() {
  const log    = document.getElementById('log');
  const footer = document.getElementById('footer');
  const title  = document.getElementById('title');
  const spinner = document.getElementById('spinner');

  const es = new EventSource('/sync');

  es.onmessage = function(e) {
    if (e.data.startsWith('__DONE__')) {
      es.close();
      spinner.style.display = 'none';
      const ok = parseInt(e.data.replace('__DONE__', '')) === 0;
      if (ok) {
        title.textContent = '✓ Sincronización completada';
        footer.innerHTML = '<span class="ok">✓ Listo — cerrando en 2 segundos…</span>';
        setTimeout(function() {
          // Tell the Goodvalley page to reload, then close popup
          try { if (window.opener) window.opener.postMessage('gv_sync_done', '*'); } catch(_) {}
          window.close();
        }, 2000);
      } else {
        title.textContent = 'Error al sincronizar';
        footer.innerHTML = '<span class="err">✗ Error — revisá el log arriba.</span>';
      }
    } else {
      log.textContent += e.data + '\\n';
      log.scrollTop = log.scrollHeight;
    }
  };

  es.onerror = function() {
    es.close();
    spinner.style.display = 'none';
    title.textContent = 'Error de conexión';
    footer.innerHTML = '<span class="err">✗ No se pudo conectar al servidor local.</span>';
  };
})();
</script>
</body>
</html>"""

# ── Standalone page (fallback, same as before) ─────────────────────────────────
PAGE = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sync pWarehouse · Goodvalley</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f4fb;color:#1a1a2e}}
  header{{background:#3b0764;color:#fff;padding:16px 32px;display:flex;align-items:center;gap:16px}}
  header h1{{font-size:18px;font-weight:600}}
  header a{{color:#d4b8f0;font-size:13px;text-decoration:none;margin-left:auto}}
  main{{max-width:720px;margin:48px auto;padding:0 24px}}
  h2{{font-size:22px;font-weight:700;margin-bottom:8px;color:#3b0764}}
  p{{color:#555;margin-bottom:24px;line-height:1.5}}
  #run-btn{{background:#3b0764;color:#fff;border:none;border-radius:8px;padding:14px 32px;font-size:16px;font-weight:600;cursor:pointer}}
  #run-btn:disabled{{background:#aaa;cursor:not-allowed}}
  #log{{display:none;margin-top:24px;background:#0f0f1a;color:#d4f0c0;border-radius:8px;padding:20px 24px;font-family:'SF Mono',monospace;font-size:13px;line-height:1.7;white-space:pre-wrap;max-height:480px;overflow-y:auto}}
  #status{{margin-top:16px;font-weight:600;font-size:15px}}
  .ok{{color:#2e7d32}} .err{{color:#c62828}}
</style>
</head>
<body>
<header>
  <h1>Goodvalley · Sync pWarehouse</h1>
  <a href="{GV_URL}">← Volver a Goodvalley</a>
</header>
<main>
  <h2>Sincronizar inventario</h2>
  <p>Importa todos los bins de ciruela desde pWarehouse8 a Goodvalley.</p>
  <button id="run-btn" onclick="startSync()">Sincronizar ahora</button>
  <div id="log"></div>
  <div id="status"></div>
</main>
<script>
function startSync(){{
  const btn=document.getElementById('run-btn'),log=document.getElementById('log'),status=document.getElementById('status');
  btn.disabled=true; btn.textContent='Sincronizando…';
  log.style.display='block'; log.textContent=''; status.textContent='';
  const es=new EventSource('/sync');
  es.onmessage=function(e){{
    if(e.data.startsWith('__DONE__')){{
      es.close();
      const ok=parseInt(e.data.replace('__DONE__',''))===0;
      status.textContent=ok?'✓ Completado.':'✗ Error.';
      status.className=ok?'ok':'err';
      btn.disabled=false; btn.textContent='Sincronizar de nuevo';
    }}else{{log.textContent+=e.data+'\\n';log.scrollTop=log.scrollHeight;}}
  }};
  es.onerror=function(){{es.close();status.textContent='✗ Conexión perdida.';status.className='err';btn.disabled=false;btn.textContent='Reintentar';}};
}}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/sync-popup':
            self._html(POPUP)
        elif self.path == '/sync':
            self._stream_sync()
        elif self.path == '/ping':
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'ok')
        elif self.path == '/':
            self._html(PAGE)
        else:
            self.send_response(404)
            self.end_headers()

    def _html(self, content):
        data = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream_sync(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        proc = subprocess.Popen(
            [PYTHON, str(SCRAPER)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            msg = f'data: {line.rstrip()}\n\n'
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                proc.terminate()
                return
        proc.wait()
        try:
            self.wfile.write(f'data: __DONE__{proc.returncode}\n\n'.encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Goodvalley sync server → http://localhost:{PORT}')
    server.serve_forever()
