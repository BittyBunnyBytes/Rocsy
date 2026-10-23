"""Small host helper for the portable install's Settings uninstall flow."""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uninstall

SECRET = os.environ.get('HOSTCTL_SECRET', '')


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return bool(SECRET) and hmac.compare_digest(self.headers.get('X-Hostctl-Secret', ''), SECRET)

    def do_GET(self) -> None:
        if not self._authorized():
            return self._send(403, {'error': 'forbidden'})
        if self.path == '/uninstall/status':
            return self._send(*uninstall.status())
        return self._send(404, {'error': 'not found'})

    def do_POST(self) -> None:
        if not self._authorized():
            return self._send(403, {'error': 'forbidden'})
        if self.path != '/uninstall/start':
            return self._send(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, {'error': 'Invalid request.'})
        if not isinstance(body, dict) or type(body.get('delete_files')) is not bool:
            return self._send(400, {'error': 'Invalid uninstall choice.'})
        try:
            return self._send(*uninstall.start(body['delete_files']))
        except Exception:
            return self._send(503, {'error': 'Could not start uninstall.'})


if __name__ == '__main__':
    import dockeraccess  # the docker group in the session that installed Rocsy (see dockeraccess.py)
    dockeraccess.reexec_with_docker_group()
    if not SECRET:
        raise SystemExit('HOSTCTL_SECRET is required')
    ThreadingHTTPServer(('127.0.0.1', int(os.getenv('HOSTCTL_PORT', '8899'))), Handler).serve_forever()
