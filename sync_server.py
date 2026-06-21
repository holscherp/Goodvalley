#!/usr/bin/env python3
"""
Local sync server — http://localhost:8899
Run once at login (launchd), stays alive in background.
The Goodvalley navbar links here for on-demand pWarehouse sync.
"""
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SCRAPER = Path(__file__).parent / 'scrape_pwarehouse.py'
PYTHON  = '/Users/pedroholscheribanez/Desktop/BZAN2021Code/.venv/bin/python3'
GV_URL  = 'https://web-production-2eea96.up.railway.app'
PORT    = 8899

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
  header a:hover{{color:#fff}}
  main{{max-width:720px;margin:48px auto;padding:0 24px}}
  h2{{font-size:22px;font-weight:700;margin-bottom:8px;color:#3b0764}}
  p{{color:#555;margin-bottom:24px;line-height:1.5}}
  #run-btn{{
    background:#3b0764;color:#fff;border:none;border-radius:8px;
    padding:14px 32px;font-size:16px;font-weight:600;cursor:pointer;
    transition:background 0.15s
  }}
  #run-btn:hover{{background:#5b1a8a}}
  #run-btn:disabled{{background:#aaa;cursor:not-allowed}}
  #log{{
    display:none;margin-top:24px;background:#0f0f1a;color:#d4f0c0;
    border-radius:8px;padding:20px 24px;font-family:'SF Mono',monospace;
    font-size:13px;line-height:1.7;white-space:pre-wrap;max-height:480px;
    overflow-y:auto;border:1px solid #333
  }}
  #status{{margin-top:16px;font-weight:600;font-size:15px}}
  .ok{{color:#2e7d32}} .err{{color:#c62828}}
  #back{{
    display:none;margin-top:20px;background:#22863a;color:#fff;
    border:none;border-radius:8px;padding:12px 28px;font-size:15px;
    font-weight:600;cursor:pointer;text-decoration:none
  }}
  #back:hover{{background:#1a6b2e}}
</style>
</head>
<body>
<header>
  <h1>Goodvalley · Sync pWarehouse</h1>
  <a href="{GV_URL}" target="_blank">← Volver a Goodvalley</a>
</header>
<main>
  <h2>Sincronizar inventario desde pWarehouse</h2>
  <p>Hace clic en el botón para importar todos los bins de ciruela directamente desde pWarehouse8 a Goodvalley. El proceso tarda ~40 segundos.</p>

  <button id="run-btn" onclick="startSync()">Sincronizar ahora</button>

  <div id="log"></div>
  <div id="status"></div>
  <a id="back" href="{GV_URL}">Ver inventario actualizado →</a>
</main>
<script>
function startSync() {{
  const btn = document.getElementById('run-btn');
  const log = document.getElementById('log');
  const status = document.getElementById('status');
  const back = document.getElementById('back');

  btn.disabled = true;
  btn.textContent = 'Sincronizando…';
  log.style.display = 'block';
  log.textContent = '';
  status.textContent = '';
  back.style.display = 'none';

  const es = new EventSource('/sync');
  es.onmessage = function(e) {{
    if (e.data.startsWith('__DONE__')) {{
      es.close();
      const code = parseInt(e.data.replace('__DONE__', ''));
      if (code === 0) {{
        status.textContent = '✓ Sincronización completada.';
        status.className = 'ok';
        back.style.display = 'inline-block';
      }} else {{
        status.textContent = '✗ Error — revisá el log arriba.';
        status.className = 'err';
      }}
      btn.disabled = false;
      btn.textContent = 'Sincronizar de nuevo';
    }} else {{
      log.textContent += e.data + '\\n';
      log.scrollTop = log.scrollHeight;
    }}
  }};
  es.onerror = function() {{
    es.close();
    status.textContent = '✗ Conexión perdida con el servidor local.';
    status.className = 'err';
    btn.disabled = false;
    btn.textContent = 'Reintentar';
  }};
}}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self._html(PAGE)
        elif self.path == '/sync':
            self._stream_sync()
        elif self.path == '/ping':
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'ok')
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
        pass  # suppress request logs


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Goodvalley sync server → http://localhost:{PORT}')
    server.serve_forever()
