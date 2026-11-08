"""Strata stops: never end the engine mid-request, never report it gone early, no reload next to a render."""
import io
import json
import unittest
from unittest import mock

import coreengines as ce


def answer(body):
    return io.BytesIO(json.dumps(body).encode())


class Drain(unittest.TestCase):
    def setUp(self):
        for name, value in (("sleep", None),):
            patcher = mock.patch.object(ce.time, name, lambda *_: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_waits_past_the_old_20s_limit(self):
        replies = [answer({"busy": True, "queued": 0}) for _ in range(200)] + [answer({"busy": False, "queued": 0})]
        with mock.patch.object(ce.urllib.request, "urlopen", side_effect=replies) as call:
            self.assertTrue(ce.wait_strata_idle())
        self.assertEqual(call.call_count, 201)

    def test_one_slow_answer_is_not_stopped(self):
        replies = [OSError("timed out"), answer({"busy": True}), OSError("timed out"), answer({"busy": False})]
        with mock.patch.object(ce.urllib.request, "urlopen", side_effect=replies) as call:
            self.assertTrue(ce.wait_strata_idle())
        self.assertEqual(call.call_count, 4)

    def test_not_serving_returns(self):
        with mock.patch.object(ce.urllib.request, "urlopen", side_effect=OSError("refused")) as call:
            self.assertTrue(ce.wait_strata_idle())
        self.assertEqual(call.call_count, 3)

    def test_gives_up_at_the_limit(self):
        with mock.patch.object(ce.urllib.request, "urlopen", side_effect=lambda *a, **k: answer({"busy": True})):
            self.assertFalse(ce.wait_strata_idle(limit=0))


class Stop(unittest.TestCase):
    def test_drains_then_stops_then_waits_for_the_unit(self):
        order = []
        with mock.patch.object(ce, "wait_strata_idle", side_effect=lambda: order.append("drain") or True), \
                mock.patch.object(ce, "unit_active", lambda unit: unit == ce.SERVE_UNIT), \
                mock.patch.object(ce.subprocess, "run", side_effect=lambda *a, **k: order.append("stop")), \
                mock.patch.object(ce, "wait_unit_gone", side_effect=lambda unit: order.append("gone")):
            ce.stop_strata({"swap": True})
        self.assertEqual(order, ["drain", "stop", "gone"])


class Reload(unittest.TestCase):
    def queue(self, *entries):
        return mock.patch.object(ce.urllib.request, "urlopen", lambda *a, **k: answer({"entries": list(entries)}))

    def test_holder_found(self):
        with self.queue({"app": "Video", "state": "active", "uses_llm": False}):
            self.assertEqual(ce.render_holding_gpu(), "Video")

    def test_llm_jobs_waiting_jobs_and_old_entries_do_not_count(self):
        with self.queue({"app": "Agent", "state": "active", "uses_llm": True},
                        {"app": "Video", "state": "waiting", "uses_llm": False},
                        {"app": "Old", "state": "active"}):
            self.assertEqual(ce.render_holding_gpu(), "")

    def test_queue_down_allows_the_start(self):
        with mock.patch.object(ce.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertEqual(ce.render_holding_gpu(), "")

    def test_start_refused_during_render(self):
        with mock.patch.object(ce, "unit_active", return_value=False), mock.patch.object(ce, "health", return_value=False), \
                mock.patch.object(ce, "render_holding_gpu", return_value="Video"), \
                mock.patch.object(ce, "launch") as launch:
            with self.assertRaises(ce.Refused):
                ce.start_strata({})
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
