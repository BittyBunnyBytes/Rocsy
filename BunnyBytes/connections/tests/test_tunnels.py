from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tunnels import TunnelError, TunnelManager, validate_credential, validate_routes
from cloudflare_api import create_local_tunnel, create_dns_routes

TUNNEL_ID = '00000000-0000-4000-8000-000000000001'


class TunnelTests(unittest.TestCase):
    def test_import_existing_config_without_starting_a_duplicate(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            config = root / 'config.yml'
            config.write_text(f'''tunnel: {TUNNEL_ID}\ncredentials-file: /secret.json\ningress:\n  - hostname: rocsy.example.com\n    service: http://127.0.0.1:8080\n  - service: http_status:404\n''')
            manager = TunnelManager(root / 'data', config, root / 'secret.json')
            item = manager.records[TUNNEL_ID]
            self.assertFalse(item['managed'])
            self.assertFalse(item['enabled'])
            self.assertEqual(item['routes'][0]['hostname'], 'rocsy.example.com')
            self.assertTrue((root / 'data/tunnels.json').is_file())

    def test_route_and_credential_validation(self):
        self.assertEqual(validate_routes([{'hostname':'Example.com', 'origin':'http://127.0.0.1:8080/'}]),
                         [{'hostname':'example.com','origin':'http://127.0.0.1:8080'}])
        for routes in ([], [{'hostname':'bad', 'origin':'http://localhost:80'}],
                       [{'hostname':'example.com', 'origin':'http://user:pass@localhost:80'}]):
            with self.assertRaises(TunnelError):
                validate_routes(routes)
        raw = json.dumps({'AccountTag':'account','TunnelID':TUNNEL_ID,'TunnelSecret':'secret'}).encode()
        self.assertEqual(validate_credential(raw)[0], TUNNEL_ID)
        with self.assertRaises(TunnelError):
            validate_credential(b'{}')

    @patch('cloudflare_api._call')
    def test_api_create_uses_local_config_and_dns_cname(self, call):
        call.side_effect = [{'id':TUNNEL_ID,'account_tag':'a'*32}, {'id':'dns'}]
        tunnel_id, credential = create_local_tunnel('token','a'*32,'example')
        self.assertEqual(tunnel_id,TUNNEL_ID)
        self.assertEqual(json.loads(credential)['TunnelID'],TUNNEL_ID)
        self.assertEqual(call.call_args_list[0].args[3]['config_src'],'local')
        errors = create_dns_routes('token','b'*32,TUNNEL_ID,[{'hostname':'example.com','origin':'http://localhost:8000'}])
        self.assertFalse(errors)
        self.assertEqual(call.call_args_list[1].args[3]['content'],TUNNEL_ID+'.cfargotunnel.com')
