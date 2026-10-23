import asyncio
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
from downloads import Downloads
from library import Library


class VerifiedUpdateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-update-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'models'
        self.data = Path(self.temp.name) / 'data'
        self.root.mkdir()
        self.data.mkdir()
        modelhub_app.ROOT = self.root
        modelhub_app.library = Library(self.data, self.root, lambda: {})
        self.downloads = Downloads(self.data, self.root, lambda: {}, modelhub_app.download_done)
        modelhub_app.downloads = self.downloads
        (self.root / 'loras').mkdir()

    def old(self, name='old.safetensors', folder='loras'):
        (self.root / folder).mkdir(exist_ok=True)
        path = self.root / folder / name
        path.write_bytes(b'old weights')
        stat = path.stat()
        identity = {field: getattr(stat, field)
                    for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}
        return path, {'path': f'{folder}/{name}', 'identity': identity}

    def add(self, name, content, batch, expected, old=None, replace=False, sha=True, folder='loras'):
        digest = hashlib.sha256(content).hexdigest() if sha else ''
        return self.downloads.add(url=f'https://example.test/{name}', dest=folder, name=name,
                                  size=len(content), sha256=digest, source='hf', group=batch,
                                  link={}, replace=replace, update_batch=batch, update_expected=expected,
                                  supersede=old['path'] if old else '',
                                  supersede_identity=old['identity'] if old and replace else None,
                                  supersede_files=[old] if old else None)

    async def run_download(self, job, content):
        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            transport = httpx.MockTransport(lambda request: httpx.Response(
                200, content=content, headers={'content-type': 'application/octet-stream',
                                                'content-length': str(len(content))}))
            return real_client(*args, transport=transport, **kwargs)

        with mock.patch.object(download_module.httpx, 'AsyncClient', side_effect=client):
            await self.downloads._download(job)

    async def test_same_name_removed_only_after_verified_download(self):
        old_path, old = self.old()
        job = self.add(old_path.name, b'new weights', uuid.uuid4().hex, 1, old, replace=True)
        await self.run_download(job, b'new weights')
        self.assertEqual(job['state'], 'complete')
        self.assertTrue(job['retired'])
        self.assertEqual(old_path.read_bytes(), b'new weights')
        self.assertFalse((old_path.parent / (old_path.name + '.old')).exists())

    async def test_renamed_update_removes_original(self):
        old_path, old = self.old()
        job = self.add('new.safetensors', b'new weights', uuid.uuid4().hex, 1, old)
        await self.run_download(job, b'new weights')
        self.assertTrue(job['retired'])
        self.assertFalse(old_path.exists())
        self.assertEqual((self.root / 'loras/new.safetensors').read_bytes(), b'new weights')

    async def test_model_update_removes_original(self):
        old_path, old = self.old('old.ckpt', folder='checkpoints')
        job = self.add('new.ckpt', b'new model', uuid.uuid4().hex, 1, old, folder='checkpoints')
        await self.run_download(job, b'new model')
        self.assertTrue(job['retired'])
        self.assertFalse(old_path.exists())
        self.assertEqual((self.root / 'checkpoints/new.ckpt').read_bytes(), b'new model')

    async def test_renamed_update_removes_only_unchanged_checksum_sidecar(self):
        old_path, old = self.old()
        sidecar = old_path.with_name(old_path.name + '.sha256')
        sidecar.write_text('old checksum')
        stat = sidecar.stat()
        old['sidecar_identity'] = {field: getattr(stat, field)
                                   for field in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}
        job = self.add('new.safetensors', b'new weights', uuid.uuid4().hex, 1, old)
        await self.run_download(job, b'new weights')
        self.assertFalse(old_path.exists())
        self.assertFalse(sidecar.exists())

    async def test_preexisting_backup_is_preserved(self):
        old_path, old = self.old()
        backup = old_path.with_name(old_path.name + '.old')
        backup.write_bytes(b'older user backup')
        job = self.add(old_path.name, b'new weights', uuid.uuid4().hex, 1, old, replace=True)
        await self.run_download(job, b'new weights')
        self.assertTrue(job['retired'])
        self.assertEqual(backup.read_bytes(), b'older user backup')

    async def test_checksum_failure_keeps_original(self):
        old_path, old = self.old()
        job = self.add('new.safetensors', b'expected', uuid.uuid4().hex, 1, old)
        await self.run_download(job, b'corrupted')
        self.assertNotEqual(job['state'], 'complete')
        self.assertEqual(old_path.read_bytes(), b'old weights')

    async def test_missing_source_hash_keeps_backup(self):
        old_path, old = self.old()
        job = self.add(old_path.name, b'new weights', uuid.uuid4().hex, 1, old, replace=True, sha=False)
        await self.run_download(job, b'new weights')
        self.assertEqual(job['state'], 'complete')
        self.assertFalse(job.get('retired'))
        self.assertTrue((old_path.parent / (old_path.name + '.old')).exists())

    async def test_multifile_update_waits_for_all_files(self):
        old_path, old = self.old()
        batch = uuid.uuid4().hex
        first = self.add('new-1.safetensors', b'new one', batch, 2, old)
        second = self.add('new-2.safetensors', b'new two', batch, 2)
        await self.run_download(first, b'new one')
        self.assertTrue(old_path.exists())
        await self.run_download(second, b'new two')
        self.assertFalse(old_path.exists())
        self.assertTrue(first['retired'])

    async def test_multifile_update_keeps_old_if_one_file_lacks_hash(self):
        old_path, old = self.old()
        batch = uuid.uuid4().hex
        first = self.add('new-1.safetensors', b'new one', batch, 2, old)
        second = self.add('new-2.safetensors', b'new two', batch, 2, sha=False)
        await self.run_download(first, b'new one')
        await self.run_download(second, b'new two')
        self.assertTrue(old_path.exists())
        self.assertFalse(first.get('retired'))

    async def test_sharded_same_name_update_removes_all_old_shards(self):
        first_path, old_first = self.old('weights-00001-of-00002.safetensors')
        second_path, old_second = self.old('weights-00002-of-00002.safetensors')
        batch = uuid.uuid4().hex
        first = self.add(first_path.name, b'new shard one', batch, 2, old_first, replace=True)
        second = self.add(second_path.name, b'new shard two', batch, 2, replace=True)
        second['supersede_identity'] = old_second['identity']
        first['supersede_files'] = [old_first, old_second]
        await self.run_download(first, b'new shard one')
        self.assertTrue((first_path.parent / (first_path.name + '.old')).exists())
        await self.run_download(second, b'new shard two')
        self.assertTrue(first['retired'])
        self.assertEqual(first_path.read_bytes(), b'new shard one')
        self.assertEqual(second_path.read_bytes(), b'new shard two')
        self.assertFalse((first_path.parent / (first_path.name + '.old')).exists())
        self.assertFalse((second_path.parent / (second_path.name + '.old')).exists())

    async def test_registration_failure_keeps_old(self):
        old_path, old = self.old()
        job = self.add('new.safetensors', b'new weights', uuid.uuid4().hex, 1, old)
        job['after'] = {'type': 'gallery'}
        with mock.patch.object(modelhub_app.integrations, 'register_lora',
                               new=mock.AsyncMock(side_effect=RuntimeError('gallery unavailable'))):
            await self.run_download(job, b'new weights')
        self.assertTrue(old_path.exists())
        self.assertFalse(job.get('retired'))

    async def test_changed_original_is_not_deleted(self):
        old_path, old = self.old()
        job = self.add('new.safetensors', b'new weights', uuid.uuid4().hex, 1, old)
        old_path.write_bytes(b'user changed this')
        await self.run_download(job, b'new weights')
        self.assertEqual(old_path.read_bytes(), b'user changed this')
        self.assertFalse(job.get('retired'))

    async def test_library_update_queues_retirement_only_for_update_action(self):
        old_path, old = self.old('old.ckpt', folder='checkpoints')
        modelhub_app.library.links[old['path']] = {
            'source': 'civitai', 'model_id': 'model-1',
            'update': {'available': True, 'version_id': 'version-2'}}
        detail = {'kind': 'model', 'id': 'model-1', 'name': 'Test model', 'url': 'https://example.test/model',
                  'versions': [{'id': 'version-2', 'name': 'New', 'base': 'Other', 'category': 'model',
                                'variants': [{'key': 'full', 'primary': True,
                                              'files': [{'name': 'new.ckpt', 'dest': 'checkpoints',
                                                         'size': 9, 'sha256': hashlib.sha256(b'new model').hexdigest(),
                                                         'url': 'https://example.test/new.ckpt'}]}]}]}
        with mock.patch.object(modelhub_app, 'fetch_detail', new=mock.AsyncMock(return_value=detail)):
            result = await modelhub_app.library_update(modelhub_app.UpdateBody(path=old['path'], keep_name=False))
            update_job = result['jobs'][0]
            self.assertEqual(update_job['supersede'], old['path'])
            self.assertEqual(update_job['supersede_files'], [old])
            self.assertFalse(update_job['replace'])
            ordinary = await modelhub_app.download(modelhub_app.DownloadBody(
                source='civitai', model_id='model-1', version_id='version-2', variant='full'))
            self.assertNotIn('update_batch', ordinary['jobs'][0])
        self.assertTrue(old_path.exists())

    async def test_library_update_queues_all_old_shards_for_replacement(self):
        first_path, first_old = self.old('weights-00001-of-00002.safetensors')
        second_path, second_old = self.old('weights-00002-of-00002.safetensors')
        modelhub_app.library.links[first_old['path']] = {
            'source': 'civitai', 'model_id': 'model-1',
            'update': {'available': True, 'version_id': 'version-2'}}
        files = [{'name': path.name, 'dest': 'loras', 'size': 10,
                  'sha256': hashlib.sha256(path.name.encode()).hexdigest(),
                  'url': f'https://example.test/{path.name}'}
                 for path in (first_path, second_path)]
        detail = {'kind': 'lora', 'id': 'model-1', 'name': 'Test LoRA', 'url': 'https://example.test/model',
                  'versions': [{'id': 'version-2', 'name': 'New', 'base': 'Other', 'category': 'lora',
                                'variants': [{'key': 'full', 'primary': True, 'files': files}]}]}
        with mock.patch.object(modelhub_app, 'fetch_detail', new=mock.AsyncMock(return_value=detail)):
            result = await modelhub_app.library_update(modelhub_app.UpdateBody(path=first_old['path'], keep_name=True))
        queued = result['jobs']
        self.assertEqual(len(queued), 2)
        self.assertTrue(all(job['replace'] for job in queued))
        self.assertEqual(queued[0]['supersede_files'], [first_old, second_old])


if __name__ == '__main__':
    unittest.main()
