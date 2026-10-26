import importlib.util
import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "fw_transcribe.py"


class FasterWhisperScriptTests(unittest.TestCase):
    def setUp(self):
        self.numpy = MagicMock()
        self.whisper = Mock()
        spec = importlib.util.spec_from_file_location("fw_script", SCRIPT)
        self.script = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, numpy=self.numpy,
                        faster_whisper=SimpleNamespace(WhisperModel=self.whisper)):
            spec.loader.exec_module(self.script)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = str(Path(tmp.name) / "voice.wav")

    def write_wav(self, rate=16000, channels=1, width=2, frames=b"\0\0"):
        with wave.open(self.wav, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(rate)
            wav.writeframes(frames)

    def test_pcm_is_little_endian_float_normalized(self):
        frames = struct.pack("<hhh", -32768, 0, 32767)
        self.write_wav(frames=frames)
        audio = self.numpy.frombuffer.return_value.astype.return_value.__truediv__.return_value
        self.assertIs(self.script.read_audio(self.wav), audio)
        self.numpy.frombuffer.assert_called_once_with(frames, dtype="<i2")
        self.numpy.frombuffer.return_value.astype.assert_called_once_with(self.numpy.float32)
        self.numpy.frombuffer.return_value.astype.return_value.__truediv__.assert_called_once_with(32768.0)
        audio.reshape.assert_not_called()
        self.numpy.interp.assert_not_called()

    def test_stereo_is_mixed_before_resampling(self):
        frames = struct.pack("<hhhh", -32768, 32767, 0, 1000)
        self.write_wav(rate=48000, channels=2, frames=frames)
        audio = self.numpy.frombuffer.return_value.astype.return_value.__truediv__.return_value
        mono = audio.reshape.return_value.mean.return_value
        mono.__len__.return_value = 2
        self.numpy.arange.side_effect = ["sample positions", "original positions"]
        result = self.script.read_audio(self.wav)
        audio.reshape.assert_called_once_with(-1, 2)
        audio.reshape.return_value.mean.assert_called_once_with(axis=1)
        self.assertEqual(self.numpy.arange.call_args_list,
                         [unittest.mock.call(0, 2, 3.0), unittest.mock.call(2)])
        self.numpy.interp.assert_called_once_with("sample positions", "original positions", mono)
        self.numpy.interp.return_value.astype.assert_called_once_with(self.numpy.float32)
        self.assertIs(result, self.numpy.interp.return_value.astype.return_value)

    def test_empty_audio_does_not_call_interpolation(self):
        self.write_wav(rate=48000, frames=b"")
        self.script.read_audio(self.wav)
        self.numpy.interp.assert_not_called()

    def test_other_sample_widths_are_rejected(self):
        for width in (1, 3, 4):
            with self.subTest(width=width):
                self.write_wav(width=width, frames=b"\0" * width)
                with self.assertRaisesRegex(ValueError, "16-bit PCM"):
                    self.script.read_audio(self.wav)
        self.numpy.frombuffer.assert_not_called()

    def test_model_options_segments_and_json_line(self):
        self.whisper.return_value.transcribe.return_value = (
            iter([SimpleNamespace(text=" Open the "), SimpleNamespace(text=" library. ")]), None)
        audio = object()
        with patch.object(self.script, "read_audio", return_value=audio) as read, \
                patch.object(sys, "argv", [str(SCRIPT), "/models/medium", self.wav]), \
                patch.object(self.script.os, "cpu_count", return_value=16):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.script.main()
        read.assert_called_once_with(self.wav)
        self.whisper.assert_called_once_with(
            "/models/medium", device="cpu", compute_type="int8", cpu_threads=8,
            local_files_only=True)
        self.whisper.return_value.transcribe.assert_called_once_with(
            audio, beam_size=1, vad_filter=True, language="en")
        self.assertEqual(stdout.getvalue(), '{"text": "Open the library."}\n')

    def test_cpu_fallback_and_silence(self):
        for cpus, threads in ((None, 4), (2, 2)):
            with self.subTest(cpus=cpus), \
                    patch.object(self.script, "read_audio", return_value=object()), \
                    patch.object(sys, "argv", [str(SCRIPT), "/models/medium", self.wav]), \
                    patch.object(self.script.os, "cpu_count", return_value=cpus):
                self.whisper.return_value.transcribe.return_value = (iter([]), None)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.script.main()
                self.assertEqual(self.whisper.call_args.kwargs["cpu_threads"], threads)
                self.assertEqual(json.loads(stdout.getvalue()), {"text": ""})


if __name__ == "__main__":
    unittest.main()
