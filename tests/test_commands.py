import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deck_main", ROOT / "main.py")
main = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, decky=SimpleNamespace(DECKY_PLUGIN_DIR=str(ROOT),
                DECKY_USER_HOME=str(ROOT), DECKY_PLUGIN_SETTINGS_DIR=str(ROOT))):
    spec.loader.exec_module(main)


class CommandsTest(unittest.IsolatedAsyncioTestCase):
    def test_empty_init(self):
        self.assertEqual(main._commands_from_init({}), [])
        self.assertEqual(main._commands_from_init({"skills": None}), [])
        self.assertEqual(main._commands_from_init({"skills": "not-a-list"}), [])

    def test_names_are_merged_sorted_and_deduplicated(self):
        ev = {"slash_commands": ["compact", "plugin:check", "check"],
              "skills": ["check", "plugin:check", "project-check"],
              "plugins": [{"name": "not-a-command"}]}
        self.assertEqual([c["name"] for c in main._commands_from_init(ev)],
                         ["check", "compact", "plugin:check", "project-check"])

    def test_descriptions_and_leading_slashes(self):
        ev = {"slash_commands": ["review", {"name": "/check", "description": "Check files"}],
              "skills": [{"name": "review", "description": "Review files"}, "check"]}
        self.assertEqual(main._commands_from_init(ev), [
            {"name": "check", "description": "Check files"},
            {"name": "review", "description": "Review files"}])

    def test_terminal_and_internal_commands_are_hidden(self):
        hidden = {"login", "logout", "exit", "clear", "config", "terminal-setup", "vim", "ide",
                  "color", "heapdump", "__remote-workflow", "workflow-launch-exec", "plugin-types"}
        self.assertEqual(main.HIDDEN_COMMANDS, hidden)
        ev = {"slash_commands": list(hidden) + ["compact", "context", "plugin:clear"],
              "skills": [{"name": "/config"}, "login"]}
        self.assertEqual([c["name"] for c in main._commands_from_init(ev)],
                         ["compact", "context", "plugin:clear"])

    def test_terminal_commands_from_init_are_hidden(self):
        ev = {"slash_commands": ["statusline", "compact"], "terminal_slash_commands": ["statusline"]}
        self.assertEqual(main._commands_from_init(ev), [{"name": "compact", "description": ""}])

    def test_invalid_entries_are_ignored(self):
        ev = {"skills": [None, 3, {}, {"name": None}, "", "/", "two words",
                         {"name": "check", "description": None}]}
        self.assertEqual(main._commands_from_init(ev), [{"name": "check", "description": ""}])

    def test_command_arguments_are_not_changed_by_context(self):
        prompt = "/plugin:check some arguments"
        self.assertEqual(main.Plugin()._with_context(prompt, {"Running game": "Test"}), prompt)

    async def test_latest_init_replaces_commands(self):
        p = main.Plugin()
        with patch.object(main.decky, "emit", AsyncMock(), create=True) as emit:
            await p._handle({"type": "system", "subtype": "init", "skills": ["check"]})
            self.assertEqual(p.commands, [{"name": "check", "description": ""}])
            await p._handle({"type": "system", "subtype": "init"})
            self.assertEqual(p.commands, [])
            emit.assert_awaited_with("claude_commands", [])

    async def test_local_command_finishes_without_echo(self):
        for queued in (1, 2):
            p = main.Plugin()
            p.inflight, p.spoken, p.tts_on = queued, [False] * queued, False
            p._push, p._set_running = AsyncMock(), AsyncMock()
            await p._handle({"type": "result", "subtype": "success", "local_command": "context"})
            self.assertEqual(p.inflight, queued - 1)
            self.assertEqual(len(p.spoken), queued - 1)
            if queued == 1:
                p._set_running.assert_awaited_once_with(False)
            else:
                p._set_running.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
