# TEMP speech bench: mocked engines only; never load a real speech model.
import asyncio
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
decky = SimpleNamespace(DECKY_PLUGIN_DIR=str(ROOT), DECKY_USER_HOME="/tmp",
                        DECKY_PLUGIN_SETTINGS_DIR="/tmp", logger=Mock(), emit=AsyncMock())
with patch.dict(sys.modules, {"decky": decky}):
    import main


def write_wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000)


class SpeechBenchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bench = self.root / "whisper/bench"
        self.bench.mkdir(parents=True)
        self.python = self.root / "voice-venv/bin/python3"
        self.models = self.root / "models"
        self.wav = self.root / "voice.wav"
        write_wav(self.wav)
        for name, value in (("HOME", str(self.root)), ("WHISPER_DIR", str(self.bench.parent))):
            patcher = patch.object(deck_tools, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def model(self, name, size):
        path = self.bench / f"ggml-{name}.bin"
        path.write_bytes(b"x" * size)
        return path

    def discover(self):
        return deck_tools._speech_bench_engines(str(self.bench), str(self.python), str(self.models))

    def snapshot(self, name):
        path = self.models / f"models--Systran--faster-whisper-{name}/snapshots/local"
        path.mkdir(parents=True)
        for file in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (path / file).write_bytes(b"model")
        return path

    def test_discovery_size_order_including_symlink(self):
        self.model("medium.en", 30)
        self.model("small.en", 20)
        base = self.root / "base.bin"
        base.write_bytes(b"x" * 10)
        (self.bench / "ggml-base.en.bin").symlink_to(base)
        (self.bench / "ggml-directory.bin").mkdir()
        (self.bench / "ggml-incomplete.bin.part").write_bytes(b"x")
        (self.bench / "ggml-broken.bin").symlink_to(self.root / "missing")
        self.assertEqual([e["model"] for e in self.discover()], ["base.en", "small.en", "medium.en"])

    def test_faster_whisper_skipped_without_venv(self):
        self.model("base.en", 10)
        self.snapshot("small")
        self.snapshot("medium")
        self.assertEqual([e["engine"] for e in self.discover()], ["whisper.cpp"])

    def test_faster_whisper_appended_small_then_medium(self):
        self.model("base.en", 10)
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.snapshot("medium")
        self.assertEqual([e["model"] for e in self.discover()], ["base.en", "medium"])
        self.snapshot("small")
        engines = self.discover()
        self.assertEqual([e["model"] for e in engines], ["base.en", "small", "medium"])
        self.assertEqual([e["engine"] for e in engines], ["whisper.cpp", "faster-whisper", "faster-whisper"])

    async def test_failures_continue_raw_text_flags_and_evidence(self):
        for i, name in enumerate(("base.en", "small.en", "medium.en", "large-v3-turbo"), 1):
            self.model(name, i)
        runner = AsyncMock(side_effect=[(1, "", "bad model"), RuntimeError("timed out"),
                                       (0, " [BLANK_AUDIO]\n thank you \n", ""), (0, "last result", "")])
        progress = Mock()
        with patch.object(deck_tools, "_speech_bench_run", runner), \
                patch.object(deck_tools, "_speech_bench_engines", return_value=self.discover()):
            results = await deck_tools.speech_bench(str(self.wav), progress)
        self.assertEqual(len(results), 4)
        self.assertEqual([r["error"] for r in results], ["bad model", "timed out", None, None])
        self.assertEqual(results[2]["text"], "[BLANK_AUDIO] thank you")
        self.assertEqual(results[3]["text"], "last result")
        self.assertEqual(progress.call_args_list[0].args, (1, 4, "base.en"))
        self.assertEqual(progress.call_args_list[-1].args, (4, 4, "large-v3-turbo"))
        saved, = (self.root / "speech-bench").glob("*.wav")
        self.assertEqual(saved.read_bytes(), self.wav.read_bytes())
        for call in runner.call_args_list:
            self.assertEqual(call.args[3:], ("-f", str(saved), "-l", "en", "-nt", "-np", "-t", "4", "-ac", "256"))
        log = (self.root / "speech-bench/speech-bench.log").read_text()
        self.assertIn(f"wav: {saved}\tclip seconds: 1.0", log)
        self.assertIn("bad model", log)
        self.assertIn("[BLANK_AUDIO] thank you", log)
        self.assertIn("last result", log)
        self.assertTrue(all(isinstance(r["seconds"], float) for r in results))

    async def test_faster_whisper_subprocess_and_async_progress(self):
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        snapshot = self.snapshot("medium")
        runner = AsyncMock(return_value=(0, json.dumps({"text": "Thank you."}), ""))
        progress = AsyncMock()
        with patch.object(deck_tools, "_speech_bench_run", runner), \
                patch.object(deck_tools, "_speech_bench_engines", return_value=self.discover()):
            results = await deck_tools.speech_bench(str(self.wav), progress)
        args = runner.call_args.args
        self.assertEqual(args[:2], (str(self.python), "-c"))
        self.assertEqual(args[3], str(snapshot))
        compile(args[2], "faster-whisper-bench", "exec")
        self.assertEqual(results[0]["text"], "Thank you.")
        progress.assert_awaited_once_with(1, 1, "medium")

    async def test_no_models_keeps_evidence_and_reports_error(self):
        with patch.object(deck_tools, "_speech_bench_engines", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "No speech benchmark models found"):
                await deck_tools.speech_bench(str(self.wav))
        saved, = (self.root / "speech-bench").glob("*.wav")
        self.assertEqual(saved.read_bytes(), self.wav.read_bytes())
        self.assertIn("No speech benchmark models found", (self.root / "speech-bench/speech-bench.log").read_text())

    async def test_timeout_reaps_subprocess(self):
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(), kill=Mock())
        with patch.object(deck_tools.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)), \
                patch.object(deck_tools.asyncio, "wait_for", side_effect=asyncio.TimeoutError) as wait:
            with self.assertRaisesRegex(RuntimeError, "300 seconds"):
                await deck_tools._speech_bench_run("fake")
        self.assertEqual(wait.call_args.args[1], 300)
        wait.call_args.args[0].close()
        process.kill.assert_called_once()
        process.communicate.assert_awaited_once()

    async def test_transcribe_keeps_flags_and_filters(self):
        model = self.model("base.en", 1)
        runner = AsyncMock(return_value=(0, "thank you", ""))
        with patch.object(deck_tools, "WHISPER_BIN", str(model)), \
                patch.object(deck_tools, "WHISPER_MODEL", str(model)), patch.object(deck_tools, "_run", runner):
            self.assertEqual(await deck_tools.transcribe(str(self.wav)), "")
        self.assertEqual(runner.call_args.args[-8:], ("-l", "en", "-nt", "-np", "-t", "4", "-ac", "256"))
        self.assertEqual(deck_tools._whisper_context(str(self.root / "absent.wav")), 1500)


class SpeechBenchPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.Plugin()
        self.plugin.bench_state = {"phase": "idle", "results": []}
        self.plugin.bench_task = None
        self.plugin.ptt_t0 = 0
        self.plugin.stop_speaking = AsyncMock()
        self.plugin.recorder = SimpleNamespace(active=False, start=AsyncMock(), cancel=AsyncMock(return_value=True),
                                               stop=AsyncMock())
        emitter = patch.object(main.decky, "emit", AsyncMock())
        self.emit = emitter.start()
        self.addCleanup(emitter.stop)

    async def test_busy_start_does_not_steal_recorder(self):
        for phase, active, starting in (("running", False, False), ("recording", False, False),
                                       ("idle", True, False), ("idle", False, True)):
            self.plugin.bench_state["phase"] = phase
            self.plugin.recorder.active = active
            self.plugin.bench_mic_starting = starting
            self.assertIn("error", await self.plugin.bench_start())
        self.plugin.recorder.start.assert_not_awaited()

    async def test_stop_returns_before_benchmark_finishes_and_retains_results(self):
        entered, release = asyncio.Event(), asyncio.Event()
        results = [{"model": "base.en", "text": "hello"}]
        async def benchmark(wav, progress):
            await progress(1, 1, "base.en")
            entered.set()
            await release.wait()
            return results
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin.recorder.path = str(Path(tmp) / "voice.wav")
            write_wav(self.plugin.recorder.path)
            with patch.object(main.deck_tools, "speech_bench", benchmark):
                self.assertEqual(await self.plugin.bench_start(), {"ok": True})
                self.plugin.stop_speaking.assert_awaited_once()
                self.assertEqual(await self.plugin.bench_stop(), {"ok": True})
                self.plugin.recorder.cancel.assert_awaited_once()
                self.plugin.recorder.stop.assert_not_awaited()
                await entered.wait()
                self.assertFalse(self.plugin.bench_task.done())
                self.assertEqual((await self.plugin.bench_last())["phase"], "running")
                release.set()
                await self.plugin.bench_task
        state = await self.plugin.bench_last()
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["results"], results)
        self.assertEqual(state["clip_seconds"], 1.0)
        self.assertEqual([c.args[1]["phase"] for c in self.emit.call_args_list],
                         ["recording", "running", "running", "done"])

    async def test_background_failure_emits_error(self):
        self.plugin.recorder.path = "/missing/benchmark.wav"
        self.plugin.bench_state["phase"] = "recording"
        self.assertEqual(await self.plugin.bench_stop(), {"ok": True})
        await self.plugin.bench_task
        self.assertEqual(self.plugin.bench_state["phase"], "error")
        self.assertIn("benchmark.wav", self.plugin.bench_state["message"])

    async def test_normal_mic_cannot_interrupt_benchmark(self):
        self.plugin.bench_state["phase"] = "recording"
        self.assertIn("error", await self.plugin.mic_start())
        self.assertIn("error", await self.plugin.mic_stop())
        self.assertFalse(await self.plugin.mic_cancel())
        await self.plugin._ptt_down()
        self.plugin.recorder.start.assert_not_awaited()
        self.plugin.recorder.stop.assert_not_awaited()
        self.plugin.recorder.cancel.assert_not_awaited()
