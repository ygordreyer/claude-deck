import importlib.util
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import deck_tools


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("voice_main", ROOT / "main.py")
main = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, decky=SimpleNamespace(
        DECKY_PLUGIN_DIR=str(ROOT), DECKY_USER_HOME=str(ROOT),
        DECKY_PLUGIN_SETTINGS_DIR=str(ROOT), emit=AsyncMock(), logger=Mock())):
    spec.loader.exec_module(main)


class VoiceInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        paths = {
            "FW_PYTHON": str(self.root / "faster-whisper/venv/bin/python3"),
            "FW_MODEL": str(self.root / "faster-whisper/medium"),
            "WHISPER_BIN": str(self.root / "whisper/whisper-cli"),
            "WHISPER_MODEL": str(self.root / "whisper/ggml-base.en.bin"),
        }
        for name, value in paths.items():
            self.enterContext(patch.object(deck_tools, name, value))
        self.runner = self.enterContext(patch.object(deck_tools, "_run", new_callable=AsyncMock))
        self.env = {"HOME": "/clean-home", "PATH": "/usr/bin"}
        self.enterContext(patch.object(deck_tools, "clean_env", return_value=self.env))
        self.wav = str(self.root / "voice.wav")

    def install_faster(self):
        python = Path(deck_tools.FW_PYTHON)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.touch()
        model = Path(deck_tools.FW_MODEL)
        model.mkdir(parents=True, exist_ok=True)
        (model / "model.bin").touch()

    def install_whisper(self):
        for name in (deck_tools.WHISPER_BIN, deck_tools.WHISPER_MODEL):
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def write_wav(self, seconds):
        with wave.open(self.wav, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * int(16000 * seconds))

    async def test_faster_runtime_works_without_whisper_cpp(self):
        self.install_faster()
        self.runner.return_value = (0, '{"text": "Open the library."}\n', "")
        self.assertEqual(await deck_tools.transcribe(self.wav), "Open the library.")
        self.runner.assert_awaited_once_with(
            deck_tools.FW_PYTHON, str(ROOT / "fw_transcribe.py"), deck_tools.FW_MODEL,
            self.wav, env=self.env, timeout=120)

    async def test_faster_runtime_takes_priority(self):
        self.install_faster()
        self.install_whisper()
        self.runner.return_value = (0, '{"text": "Launch the game"}', "")
        self.assertEqual(await deck_tools.transcribe(self.wav), "Launch the game")
        self.assertEqual(self.runner.await_args.args[0], deck_tools.FW_PYTHON)

    async def test_whisper_cpp_arguments_and_context_are_unchanged(self):
        self.install_whisper()
        self.runner.return_value = (0, "  Launch the\n\n game. \n", "")
        for seconds, context in ((0.1, "256"), (5, "384"), (60, "1500")):
            with self.subTest(seconds=seconds):
                self.write_wav(seconds)
                self.runner.reset_mock()
                self.assertEqual(await deck_tools.transcribe(self.wav), "Launch the game.")
                self.runner.assert_awaited_once_with(
                    deck_tools.WHISPER_BIN, "-m", deck_tools.WHISPER_MODEL,
                    "-f", self.wav, "-l", "en", "-nt", "-np", "-t", "4",
                    "-ac", context, timeout=120)

    async def test_whisper_cpp_unreadable_wav_keeps_default_context(self):
        self.install_whisper()
        self.runner.return_value = (0, "Open library", "")
        self.assertEqual(await deck_tools.transcribe(self.wav), "Open library")
        self.assertEqual(self.runner.await_args.args[-2:], ("-ac", "1500"))

    async def test_incomplete_faster_runtime_falls_back(self):
        self.install_whisper()
        self.runner.return_value = (0, "Open library", "")
        for missing in ("python", "model", "weights"):
            with self.subTest(missing=missing):
                self.install_faster()
                if missing == "python":
                    Path(deck_tools.FW_PYTHON).unlink()
                else:
                    (Path(deck_tools.FW_MODEL) / "model.bin").unlink()
                    if missing == "model":
                        Path(deck_tools.FW_MODEL).rmdir()
                self.assertEqual(await deck_tools.transcribe(self.wav), "Open library")
                self.assertEqual(self.runner.await_args.args[0], deck_tools.WHISPER_BIN)

    async def test_json_text_and_newlines(self):
        self.install_faster()
        self.runner.return_value = (0, json.dumps({"text": "  Open café\n\nsettings.  "}) + "\n", "")
        self.assertEqual(await deck_tools.transcribe(self.wav), "Open café settings.")

    async def test_invalid_json_is_reported(self):
        self.install_faster()
        for payload in ("", "not JSON", "{}", "[]", "null", '{"text": null}',
                        '{"text": 12}', '{"text": ["hello"]}'):
            with self.subTest(payload=payload):
                self.runner.return_value = (0, payload, "")
                with self.assertRaisesRegex(RuntimeError, "invalid transcript JSON"):
                    await deck_tools.transcribe(self.wav)

    async def test_filters_apply_to_both_engines(self):
        self.install_whisper()
        cases = {
            "": "", " [BLANK_AUDIO] ": "", "(silence)": "", "MUSIC": "",
            "You.": "", "Thank you!": "", "Thanks for watching.": "", "Bye!": "",
            "[MUSIC PLAYING]": "", "(footsteps) [music] *gunshot* ♪♪": "",
            "123 !": "", "A": "",
            "[music] Open (footsteps) the *gunshot* library ♪": "Open the library",
            "Please save the game.": "Please save the game.",
        }
        for engine in ("whisper.cpp", "faster-whisper"):
            if engine == "faster-whisper":
                self.install_faster()
            for text, expected in cases.items():
                with self.subTest(engine=engine, text=text):
                    out = json.dumps({"text": text}) if engine == "faster-whisper" else text
                    self.runner.return_value = (0, out, "")
                    self.assertEqual(await deck_tools.transcribe(self.wav), expected)

    async def test_failed_runtime_is_reported_without_fallback(self):
        self.install_faster()
        self.install_whisper()
        self.runner.return_value = (1, "", "could not load model\n")
        with self.assertRaisesRegex(RuntimeError, "faster-whisper failed: could not load model"):
            await deck_tools.transcribe(self.wav)
        self.runner.assert_awaited_once()

    async def test_whisper_cpp_failure_is_unchanged(self):
        self.install_whisper()
        self.runner.return_value = (1, "", "could not load model\n")
        with self.assertRaisesRegex(RuntimeError, "whisper failed: could not load model"):
            await deck_tools.transcribe(self.wav)

    async def test_missing_engines_do_not_run_subprocess(self):
        self.assertFalse(deck_tools.voice_input_available())
        with self.assertRaisesRegex(RuntimeError, "Voice input isn't installed"):
            await deck_tools.transcribe(self.wav)
        self.runner.assert_not_awaited()

    def test_availability_requires_a_complete_engine(self):
        self.assertFalse(deck_tools.voice_input_available())
        self.install_faster()
        self.assertTrue(deck_tools.voice_input_available())
        Path(deck_tools.FW_PYTHON).unlink()
        self.assertFalse(deck_tools.voice_input_available())
        self.install_faster()
        (Path(deck_tools.FW_MODEL) / "model.bin").unlink()
        self.assertFalse(deck_tools.voice_input_available())
        self.install_whisper()
        self.assertTrue(deck_tools.voice_input_available())
        Path(deck_tools.WHISPER_MODEL).unlink()
        self.assertFalse(deck_tools.voice_input_available())
        self.install_whisper()
        Path(deck_tools.WHISPER_BIN).unlink()
        self.assertFalse(deck_tools.voice_input_available())

    async def test_plugin_setup_and_dictation_accept_faster_only(self):
        self.install_faster()
        plugin = main.Plugin()
        plugin.speaker = SimpleNamespace(available=lambda: False)
        with patch.object(main, "CLAUDE", str(self.root / "missing-claude")), \
                patch.object(deck_tools, "find_controller", return_value=None):
            self.assertTrue(await plugin.voice_available())
            self.assertTrue((await plugin.setup_status())["voice"])
        plugin.caption_up, plugin.ptt_t0 = False, 0
        plugin.recorder = SimpleNamespace(active=False, start=AsyncMock())
        plugin.stop_speaking = AsyncMock()
        with patch.object(main.asyncio, "sleep", AsyncMock()):
            await plugin._ptt_down("dictate")
        plugin.recorder.start.assert_awaited_once()
        main.decky.emit.assert_awaited_with("claude_ptt", {"phase": "dictating"})


if __name__ == "__main__":
    unittest.main()
