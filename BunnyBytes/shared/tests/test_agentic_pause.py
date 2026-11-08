"""Pause / Resume of framework Agentic sessions (2026-11-04)."""
import asyncio
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

PLAN = {'prompt': 'A red bicycle on a wet street at dusk'}
IMAGE = {'id': 'job1abcdefgh', 'images': [{'url': '/Image/one.png'}]}


class FakeStudio:
    """The Image studio: hands out job ids, records cancels, reports each job's state."""
    def __init__(self, states=None):
        self.generated, self.cancelled, self.states = [], [], dict(states or {})

    async def __call__(self, studio, method, path, payload=None):
        if method == 'POST' and path == '/api/generate':
            job_id = f'job{len(self.generated) + 1}abcdefgh'
            self.generated.append(job_id)
            return {'id': job_id}
        if method == 'POST' and path.endswith('/cancel'):
            job_id = path.split('/')[3]
            self.cancelled.append(job_id)
            self.states.setdefault(job_id, 'cancelled')
            return {}
        if method == 'GET' and path.startswith('/api/jobs/'):
            return {'state': self.states.get(path.split('/')[3], 'rendering')}
        raise AssertionError((method, path))


class PauseResumeTest(unittest.IsolatedAsyncioTestCase):
    def fresh(self, tmp):
        os.environ['FRAMEWORK_DATA_DIR'] = tmp
        import agentic
        return importlib.reload(agentic)

    async def wait_for(self, agentic, row, state):
        for _ in range(200):
            if row['state'] == state and row['id'] not in agentic.TASKS:
                return
            await asyncio.sleep(0.01)
        self.fail(f"state stayed {row['state']}, wanted {state}")

    async def test_pause_mid_render_cancels_it_and_resume_renders_the_same_plan_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            agentic = self.fresh(tmp)
            studio, rendering = FakeStudio(), asyncio.Event()

            async def wait_job(row, job_id):
                if job_id == 'job1abcdefgh':
                    rendering.set()
                    await asyncio.Event().wait()      # the first render runs until it is paused
                return {**IMAGE, 'id': job_id}
            with patch.object(agentic, 'hermes', new=AsyncMock(side_effect=[PLAN, {'pass': True}])) as llm, \
                 patch.object(agentic, 'studio_call', new=studio), \
                 patch.object(agentic, 'wait_job', new=wait_job), \
                 patch.object(agentic, 'review', new=AsyncMock(return_value=(True, 'ok'))):
                row = agentic.start('image', agentic.AgenticRequest(request='A red bicycle', settings={'model': 'krea2'}))
                await rendering.wait()
                agentic.pause(row)
                await self.wait_for(agentic, row, 'paused')
                self.assertEqual(studio.cancelled, ['job1abcdefgh'])
                self.assertEqual(row['paused']['job_id'], 'job1abcdefgh')
                session_id = row['id']
                await agentic.resume(row)
                await self.wait_for(agentic, row, 'awaiting_feedback')
            self.assertEqual(row['id'], session_id)                       # the same session, reloaded
            self.assertEqual(llm.await_count, 2)                         # no new plan: the same one rendered again
            self.assertEqual(studio.generated, ['job1abcdefgh', 'job2abcdefgh'])
            self.assertEqual(row['plan'], PLAN)
            self.assertTrue(any('interrupted by the pause' in entry['text'] for entry in row['log']))

    async def test_pause_while_planning_replans_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            agentic = self.fresh(tmp)
            studio, thinking = FakeStudio(), asyncio.Event()
            calls = []

            async def hermes(system, user, *args, **kwargs):
                calls.append(kwargs.get('purpose'))
                if len(calls) == 1:
                    thinking.set()
                    await asyncio.Event().wait()      # paused mid-generation
                return PLAN if kwargs.get('purpose') == 'planning' else {'pass': True}
            with patch.object(agentic, 'hermes', new=hermes), \
                 patch.object(agentic, 'studio_call', new=studio), \
                 patch.object(agentic, 'wait_job', new=AsyncMock(return_value=IMAGE)), \
                 patch.object(agentic, 'review', new=AsyncMock(return_value=(True, 'ok'))):
                row = agentic.start('image', agentic.AgenticRequest(request='A red bicycle', settings={'model': 'krea2'}))
                await thinking.wait()
                agentic.pause(row)
                await self.wait_for(agentic, row, 'paused')
                self.assertEqual(studio.generated, [])
                agentic.steer(row, 'make it rain harder')        # steering while paused is kept for the resume
                await agentic.resume(row)
                await self.wait_for(agentic, row, 'awaiting_feedback')
            self.assertEqual(calls, ['planning', 'planning', 'plan review'])
            self.assertEqual(studio.generated, ['job1abcdefgh'])
            self.assertIn('make it rain harder', row['steer'])

    async def test_render_that_kept_going_is_picked_up_not_rendered_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            agentic = self.fresh(tmp)
            studio, rendering, waits = FakeStudio(states={'job1abcdefgh': 'complete'}), asyncio.Event(), []

            async def wait_job(row, job_id):
                waits.append(job_id)
                if len(waits) == 1:
                    rendering.set()
                    await asyncio.Event().wait()
                return {**IMAGE, 'id': job_id}
            with patch.object(agentic, 'hermes', new=AsyncMock(side_effect=[PLAN, {'pass': True}])), \
                 patch.object(agentic, 'studio_call', new=studio), \
                 patch.object(agentic, 'wait_job', new=wait_job), \
                 patch.object(agentic, 'review', new=AsyncMock(return_value=(True, 'ok'))):
                row = agentic.start('image', agentic.AgenticRequest(request='A red bicycle', settings={'model': 'krea2'}))
                await rendering.wait()
                agentic.pause(row)
                await self.wait_for(agentic, row, 'paused')
                await agentic.resume(row)
                await self.wait_for(agentic, row, 'awaiting_feedback')
            self.assertEqual(studio.generated, ['job1abcdefgh'])          # finished during the pause: reviewed, not redone
            self.assertEqual(waits, ['job1abcdefgh', 'job1abcdefgh'])

    async def test_paused_session_survives_a_restart_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            agentic = self.fresh(tmp)
            studio, thinking = FakeStudio(), asyncio.Event()

            async def hermes(*args, **kwargs):
                thinking.set()
                await asyncio.Event().wait()
            with patch.object(agentic, 'hermes', new=hermes), patch.object(agentic, 'studio_call', new=studio):
                row = agentic.start('image', agentic.AgenticRequest(request='A red bicycle', settings={'model': 'krea2'}))
                await thinking.wait()
                agentic.pause(row)
                await self.wait_for(agentic, row, 'paused')
            agentic = self.fresh(tmp)                                   # framework restart
            saved = agentic.SESSIONS[row['id']]
            self.assertEqual(saved['state'], 'paused')
            with patch.object(agentic, 'hermes', new=AsyncMock(side_effect=[PLAN, {'pass': True}])), \
                 patch.object(agentic, 'studio_call', new=FakeStudio()), \
                 patch.object(agentic, 'wait_job', new=AsyncMock(return_value=IMAGE)), \
                 patch.object(agentic, 'review', new=AsyncMock(return_value=(True, 'ok'))):
                await agentic.resume(saved)
                await self.wait_for(agentic, saved, 'awaiting_feedback')
            stored = json.loads((Path(tmp) / 'agentic-sessions.json').read_text())
            self.assertEqual(stored[-1]['state'], 'awaiting_feedback')

    async def test_pause_needs_a_running_session_and_end_closes_a_paused_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            agentic = self.fresh(tmp)
            row = {'id': 'x' * 32, 'studio': 'image', 'state': 'awaiting_feedback', 'log': [], 'events': []}
            with self.assertRaises(ValueError):
                agentic.pause(row)
            with self.assertRaises(ValueError):
                await agentic.resume(row)
            row['state'] = 'paused'
            agentic.cancel(row)
            self.assertEqual(row['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
