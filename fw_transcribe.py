"""Run with the faster-whisper venv's Python, never the Decky backend's Python."""
import json
import os
import sys
import wave

import numpy as np
from faster_whisper import WhisperModel


def read_audio(path):
    # PyAV 19 breaks faster-whisper 1.2.0's file-path decoder.
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise ValueError("Expected 16-bit PCM WAV")
        rate, channels = wav.getframerate(), wav.getnchannels()
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000 and len(audio):
        audio = np.interp(np.arange(0, len(audio), rate / 16000),
                          np.arange(len(audio)), audio).astype(np.float32)
    return audio


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: fw_transcribe.py <model_dir> <wav>")
    audio = read_audio(sys.argv[2])
    model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8",
                         cpu_threads=min(8, os.cpu_count() or 4), local_files_only=True)
    segments, _ = model.transcribe(audio, beam_size=1, vad_filter=True, language="en")
    print(json.dumps({"text": " ".join(s.text.strip() for s in segments).strip()}))


if __name__ == "__main__":
    main()
