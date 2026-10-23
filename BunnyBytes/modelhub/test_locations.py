"""Storage locations in the Models hub: downloads into a location, updates and uninstall of moved
files, unplugged drives. Throwaway folders only; run in the image with --network none."""
import hashlib
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ.get('MODELHUB_CODE_DIR', str(Path(__file__).resolve().parent)))

import httpx
import app as modelhub_app
import downloads as download_module
import locations
from downloads import Downloads
from library import Library


class LocationsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-locations-test-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.data, self.drive = base / 'models', base / 'data', base / 'drive'
        for folder in (self.root / 'loras', self.data, self.drive):
            folder.mkdir(parents=True)
        modelhub_app.ROOT = self.root
        modelhub_app.library = Library(self.data, self.root, lambda: {})
        self.downloads = Downloads(self.data, self.root, lambda: {}, modelhub_app.download_done)
        self.downloads.store_for = modelhub_app.download_store
        modelhub_app.downloads = self.downloads
        locations.snapshot.update(locations=[{'id': 'drive', 'label': 'Drive', 'path': str(self.drive)}],
                                  defaults={'models': 'comfy', 'loras': 'drive', 'llm': 'comfy'}, links={}, jobs=[])
        self.addCleanup(locations.snapshot.update, locations=[], defaults={}, links={}, jobs=[])

    async def run_download(self, job, content):
        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            transport = httpx.MockTransport(lambda request: httpx.Response(
                200, content=content, headers={'content-type': 'application/octet-stream',
                                                'content-length': str(len(content))}))
            return real_client(*args, transport=transport, **kwargs)

        with mock.patch.object(download_module.httpx, 'AsyncClient', side_effect=client):
            await self.downloads._download(job)

    def add(self, name, content, **extra):
        return self.downloads.add(url=f'https://example.test/{name}', dest='loras', name=name, size=len(content),
                                  sha256=hashlib.sha256(content).hexdigest(), source='hf', group=uuid.uuid4().hex,
                                  link={}, **extra)

    async def test_download_goes_to_default_location(self):
        job = self.add('a.safetensors', b'weights')
        await self.run_download(job, b'weights')
        link = self.root / 'loras/a.safetensors'
        self.assertEqual(job['state'], 'complete', job['error'])
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), self.drive / 'loras/a.safetensors')
        self.assertEqual(link.read_bytes(), b'weights')
        self.assertTrue((self.root / 'loras/a.safetensors.sha256').is_file())    # sidecar next to the link
        self.assertEqual(job['path'], 'loras/a.safetensors')

    async def test_unreachable_default_falls_back_to_models_folder(self):
        locations.snapshot['locations'][0]['path'] = str(self.drive / 'gone')
        job = self.add('b.safetensors', b'weights')
        await self.run_download(job, b'weights')
        self.assertFalse((self.root / 'loras/b.safetensors').is_symlink())
        self.assertEqual((self.root / 'loras/b.safetensors').read_bytes(), b'weights')

    async def test_update_of_moved_file_stays_on_its_drive_and_retires_old(self):
        real = self.drive / 'loras/old.safetensors'
        real.parent.mkdir(parents=True)
        real.write_bytes(b'old weights')
        link = self.root / 'loras/old.safetensors'
        link.symlink_to(real)
        locations.snapshot['defaults'] = {'loras': 'comfy'}                    # the update follows the file, not the default
        stat = os.stat(link)
        old = {'path': 'loras/old.safetensors',
               'identity': {f: getattr(stat, f) for f in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}}
        batch = uuid.uuid4().hex
        job = self.add('old.safetensors', b'new weights', replace=True, update_batch=batch, update_expected=1,
                       supersede=old['path'], supersede_identity=old['identity'], supersede_files=[old])
        await self.run_download(job, b'new weights')
        self.assertEqual(job['state'], 'complete', job['error'])
        self.assertTrue(job.get('retired'), job.get('note'))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), b'new weights')
        self.assertTrue(str(os.readlink(link)).startswith(str(self.drive)))
        self.assertFalse(real.exists())                                         # old file on the drive removed
        self.assertFalse((self.root / 'loras/old.safetensors.old').exists())   # and its backup link

    async def test_uninstall_removes_file_on_the_drive(self):
        real = self.drive / 'loras/c.safetensors'
        real.parent.mkdir(parents=True)
        real.write_bytes(b'x' * (2 * 1024 * 1024))
        (self.root / 'loras/c.safetensors').symlink_to(real)
        modelhub_app.library.scan()
        with mock.patch.object(modelhub_app.integrations, 'lora_usage', new=mock.AsyncMock(return_value={})):
            result = await modelhub_app.uninstall(modelhub_app.UninstallBody(path='loras/c.safetensors'))
        self.assertFalse(real.exists())
        self.assertFalse((self.root / 'loras/c.safetensors').is_symlink())
        self.assertEqual(result['freed'], 2 * 1024 * 1024)

    def test_scan_keeps_files_on_an_unplugged_drive(self):
        real = self.drive / 'loras/d.safetensors'
        real.parent.mkdir(parents=True)
        real.write_bytes(b'x' * (2 * 1024 * 1024))
        (self.root / 'loras/d.safetensors').symlink_to(real)
        library = modelhub_app.library
        library.scan()
        library.links['loras/d.safetensors'] = {'source': 'civitai', 'model_id': '1'}
        real.unlink()                                                          # drive unplugged
        items = library.scan()
        item = next(i for i in items if i['path'] == 'loras/d.safetensors')
        self.assertTrue(item['offline'])
        self.assertEqual(item['size'], 2 * 1024 * 1024)
        self.assertIn('loras/d.safetensors', library.links)                    # Civitai link info survives


if __name__ == '__main__':
    unittest.main()
