"""Audio read through ffmpeg and ffprobe, for the checks that look at the samples themselves: dedupe.py's comparisons
of copies and quality.py's analysis of a FLAC; and whether a file decodes at all, for quality.py's rewrites and
uploads."""
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .formats import suffix

COMPARE_RATE = 16000  # waveforms are compared mono at this rate: below 8 kHz, where lossy coding changes little


def check_decodes(path: Path, kind: str | None = None, timeout: int = 3600):
    """OSError unless every frame of the file decodes: flac -t for a FLAC when flac is installed, which also checks the
    MD5 its STREAMINFO holds, else a decode by ffmpeg that reports no error. FileNotFoundError when neither is installed.
    kind: the format, as its suffix names it ('flac'), of a file whose name doesn't say. timeout: seconds the check may take."""
    if (kind or suffix(path)) == "flac" and shutil.which("flac"):
        command = ["flac", "-t", "-s", str(path)]
    else:
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"]
    run = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    error = run.stderr.decode(errors="replace").strip()
    if run.returncode != 0 or command[0] == "ffmpeg" and error:
        raise OSError(f"{path.name} doesn't decode: {command[0]}: {error[-200:]}")


def stream_info(path: str) -> dict[str, str]:
    """ffprobe's codec_name, sample_rate, channels, bits_per_raw_sample, sample_fmt and bit_rate of the first audio
    stream, as text; one ffprobe doesn't know is missing or 'N/A'."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=codec_name,sample_rate,channels,bits_per_raw_sample,sample_fmt,bit_rate", "-of", "default=nw=1", path],
                         capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def decode(path: str, rate: int | None = None) -> np.ndarray:
    """Samples as int32 at the native rate with all channels (exact comparisons), or float32 mono at `rate`."""
    args, dtype = (["-ac", "1", "-ar", str(rate), "-f", "f32le"], np.float32) if rate else (["-f", "s32le"], np.int32)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", *args, "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype)


def chunks(path: str):
    """rate, channels, bits and the samples as int32 (left-aligned, whatever the file's depth), a chunk of frames at a
    time: for files too long to hold decoded at once."""
    info = stream_info(path)
    rate, channels = int(info["sample_rate"]), int(info["channels"])
    bits = int(info["bits_per_raw_sample"]) if info.get("bits_per_raw_sample", "").isdigit() else 16
    process = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0", "-f", "s32le", "-c:a", "pcm_s32le", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read():
        try:
            while data := process.stdout.read(channels * 4 << 18):
                yield np.frombuffer(data[:len(data) // (channels * 4) * channels * 4], np.int32).reshape(-1, channels)
        finally:
            process.stdout.close()
            error = process.stderr.read().decode(errors="replace").strip()
            if process.wait() != 0:
                raise OSError(f"can't decode {path}: {error[-200:]}")
    return rate, channels, bits, read()
