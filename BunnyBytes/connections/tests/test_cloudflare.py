from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cloudflare import metric_value, parse_connections, read_config


class CloudflareTest(TestCase):
    def test_existing_tunnel_route_and_secret_redaction(self):
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / 'config.yml'
            path.write_text('''tunnel: test-id
credentials-file: /secret/path.json
ingress:
  - hostname: rocsy.example.com
    service: http://user:password@127.0.0.1:8080/private?key=secret
  - service: http_status:404
''')
            data = read_config(path)
        self.assertEqual(data['tunnel_id'], 'test-id')
        self.assertEqual(data['routes'], [{'hostname': 'rocsy.example.com', 'origin': 'http://127.0.0.1:8080'}])
        self.assertNotIn('secret', repr(data))

    def test_metrics_connections(self):
        self.assertEqual(parse_connections('# HELP x\ncloudflared_tunnel_ha_connections 4\n'), 4)
        self.assertIsNone(parse_connections('other_metric 1\n'))
        self.assertEqual(metric_value('cloudflared_tunnel_total_requests 123\n', 'cloudflared_tunnel_total_requests'), 123)
