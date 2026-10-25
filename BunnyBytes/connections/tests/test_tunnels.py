from __future__ import annotations

import asyncio
import json
import tempfile
import time
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


class _Validated:
    returncode = 0
    stderr = b""


class _Rejected:
    returncode = 1
    stderr = b"invalid ingress"


class _Running:
    def poll(self):
        return None


CREDENTIAL = json.dumps({'AccountTag': 'x', 'TunnelID': TUNNEL_ID, 'TunnelSecret': 'x'}).encode()
ROUTES = [{'hostname': 'demo.example.com', 'origin': 'http://127.0.0.1:8080'}]


class TunnelEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_validates_once_off_the_event_loop(self):
        calls = []

        def slow_validation(*args, **kwargs):
            calls.append(args)
            time.sleep(0.15)
            return _Validated()

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            manager = TunnelManager(root, root / 'absent.yml', root / 'absent.json')
            beats = []

            async def heartbeat():
                start = time.monotonic()
                await asyncio.sleep(0.01)
                beats.append(time.monotonic() - start)

            with patch('tunnels.subprocess.run', side_effect=slow_validation), \
                 patch('tunnels.subprocess.Popen', return_value=_Running()):
                await asyncio.gather(manager.add('demo', ROUTES, CREDENTIAL), heartbeat())
            self.assertEqual(len(calls), 1)
            self.assertLess(beats[0], 0.1)

    async def test_rejected_config_does_not_stop_supervision(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            manager = TunnelManager(root, root / 'absent.yml', root / 'absent.json')
            with patch('tunnels.subprocess.run', return_value=_Validated()), \
                 patch('tunnels.subprocess.Popen', return_value=_Running()):
                await manager.add('demo', ROUTES, CREDENTIAL)
            manager.processes.clear()
            with patch('tunnels.subprocess.run', return_value=_Rejected()), \
                 patch('tunnels.asyncio.sleep', side_effect=[None, asyncio.CancelledError()]):
                with self.assertRaises(asyncio.CancelledError):
                    await manager.supervise()    # two passes, then cancelled
            self.assertIn('invalid ingress', manager.start_errors[TUNNEL_ID])
            with patch('tunnels.read_status', return_value={'connected': False, 'active_connections': 0,
                                                            'total_requests': 0, 'request_errors': 0, 'error': ''}):
                self.assertIn('invalid ingress', manager.list_status()[0]['error'])
