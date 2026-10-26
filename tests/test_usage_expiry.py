import asyncio
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
sys.modules["decky"] = types.SimpleNamespace(DECKY_PLUGIN_DIR=ROOT, DECKY_USER_HOME="/unused",
                                          DECKY_PLUGIN_SETTINGS_DIR="/unused", logger=logging.getLogger("usage"))
import main


class _Plugin(main.Plugin):
    def __getattr__(self, name):  # state fields other features add stay None here
        return None


class UsageExpiryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = _Plugin()
        self.plugin.usage_lock = asyncio.Lock()
        self.plugin.usage_checked_at = None
        self.plugin.usage_expiry = None
        self.plugin.usage = None
        self.plugin._save = Mock()
        self.plugin.history, self.plugin.running, self.plugin.asks = [], False, {}
        self.plugin.mode = self.plugin.model = self.plugin.theme = self.plugin.voice = "default"
        self.plugin.ptt, self.plugin.dictate, self.plugin.game_threads = "l4r4", "l5r5", True
        self.plugin.chat_meta, self.plugin.info, self.plugin.remote_url = {}, {}, None
        self.plugin.speaker = Mock(active=False)
        self.addCleanup(lambda: self.plugin.usage_expiry and self.plugin.usage_expiry.cancel())

    async def test_offline_resume_prunes_before_fetch_and_throttle(self):
        for checked_at in (None, 100):
            with self.subTest(checked_at=checked_at):
                self.plugin.usage = {"status": "rejected", "other": True,
                                     "unifiedWindows": {"five_hour": {"resetsAt": 150}}}
                self.plugin.usage_checked_at = checked_at
                def fetch():
                    self.assertEqual(self.plugin.usage, {"other": True})
                    return None
                with patch.object(main.time, "time", return_value=200), \
                        patch.object(main.time, "monotonic", return_value=100), \
                        patch.object(main, "_fetch_usage", side_effect=fetch) as fetch_usage, \
                        patch.object(main.decky, "emit", new=AsyncMock(), create=True):
                    await self.plugin._refresh_usage()
                    state = await self.plugin.get_state()
                self.assertEqual(state["usage"], {"other": True})
                self.assertEqual(fetch_usage.call_count, int(checked_at is None))

    async def test_state_prunes_window_that_expires_during_failed_fetch(self):
        now = 100
        self.plugin.usage = {"unifiedWindows": {"five_hour": {"resetsAt": 150}}}
        def fetch():
            nonlocal now
            now = 200
            return None
        with patch.object(main.time, "time", side_effect=lambda: now), \
                patch.object(main, "_fetch_usage", side_effect=fetch), \
                patch.object(main.decky, "emit", new=AsyncMock(), create=True):
            self.assertEqual((await self.plugin.get_state())["usage"], {})

    async def test_timer_rechecks_wall_clock(self):
        with patch.object(main.time, "time", side_effect=[100, 105, 110]), \
                patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep, \
                patch.object(self.plugin, "_set_usage", new=AsyncMock()) as set_usage:
            await self.plugin._expire_usage(110)
        self.assertEqual(sleep.call_args_list, [call(10), call(5)])
        set_usage.assert_awaited_once_with(self.plugin.usage)


if __name__ == "__main__":
    unittest.main()
