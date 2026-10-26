import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
decky = SimpleNamespace(DECKY_PLUGIN_DIR=str(ROOT), DECKY_USER_HOME="/tmp",
                        DECKY_PLUGIN_SETTINGS_DIR="/tmp", logger=Mock(), emit=AsyncMock())
with patch.dict(sys.modules, {"decky": decky}):
    import main


class EffortTests(unittest.TestCase):
    def setUp(self):
        env = patch.object(main, "_env", return_value={})
        env.start()
        self.addCleanup(env.stop)
        self.plugin = main.Plugin()
        self.plugin.__dict__.update(proc=None, mode="ask", model="haiku", effort="default",
                                    session_id="saved-chat", spoken=[], history=[], usage=None,
                                    theme="claude", voice="off", ptt="l4r4", dictate="l5r5",
                                    chats={}, chat_meta={}, game_threads=True)

    def test_startup_args(self):
        for effort in main.EFFORTS:
            with self.subTest(effort=effort):
                self.plugin.effort = effort
                with patch.object(main.asyncio, "create_subprocess_exec", side_effect=RuntimeError("spawn")) as spawn:
                    with self.assertRaisesRegex(RuntimeError, "spawn"):
                        asyncio.run(self.plugin._ensure())
                args = spawn.call_args.args
                self.assertEqual(args[args.index("--model") + 1], "haiku")
                self.assertEqual(args[args.index("--resume") + 1], "saved-chat")
                if effort == "default":
                    self.assertNotIn("--effort", args)
                else:
                    self.assertEqual(args[args.index("--effort") + 1], effort)
                flag = list(args[args.index("--effort"):args.index("--effort") + 2]) if "--effort" in args else []
                print(effort + ": " + repr(flag))

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "STATE_FILE", str(Path(tmp) / "state.json")):
            for effort in main.EFFORTS:
                self.plugin.effort = effort
                self.plugin._save()
                self.plugin.effort = "default"
                self.plugin._load()
                self.assertEqual(self.plugin.effort, effort)
            for data in ({}, {"effort": "invalid"}, {"effort": None}):
                Path(main.STATE_FILE).write_text(json.dumps(data))
                self.plugin._load()
                self.assertEqual(self.plugin.effort, "default")

    def test_set_effort(self):
        self.plugin._save = Mock()
        self.plugin._control = AsyncMock()
        self.assertFalse(asyncio.run(self.plugin.set_effort("invalid")))
        self.plugin._save.assert_not_called()
        self.assertTrue(asyncio.run(self.plugin.set_effort("low")))
        self.plugin._control.assert_not_called()
        self.plugin.proc = SimpleNamespace(returncode=None)
        for effort in main.EFFORTS:
            self.assertTrue(asyncio.run(self.plugin.set_effort(effort)))
            self.plugin._control.assert_awaited_with({"subtype": "apply_flag_settings",
                "settings": {"effortLevel": None if effort == "default" else effort}})
            self.assertEqual(self.plugin.effort, effort)
        self.plugin._control.side_effect = RuntimeError("rejected")
        self.plugin._save.reset_mock()
        self.assertFalse(asyncio.run(self.plugin.set_effort("low")))
        self.assertEqual(self.plugin.effort, "max")
        self.plugin._save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
