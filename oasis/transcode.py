"""Streams in another format or at a lower bitrate, as clients ask with format and maxBitRate: ffmpeg reads the file or
YouTube Music's stream and its output is sent as it comes."""
import shutil
import subprocess

from starlette.responses import StreamingResponse

FORMATS = {  # format: ffmpeg codec, container, content type
    "opus": ("libopus", "webm", "audio/webm"), "mp3": ("libmp3lame", "mp3", "audio/mpeg"), "aac": ("aac", "adts", "audio/aac"),
}


def target(fmt: str | None, max_bitrate: int, suffix: str, bitrate: int | None) -> tuple[str, int] | None:
    """The (format, kbit/s) to transcode to, or None to send the file as it is. Needs ffmpeg."""
    if not shutil.which("ffmpeg") or fmt == "raw":
        return None
    if fmt in FORMATS and fmt != suffix:
        return fmt, min(max_bitrate or 192, 320)
    if max_bitrate and bitrate and max_bitrate < bitrate:
        return "opus", max_bitrate
    return None


def stream(source: str, fmt: str, kbps: int, offset: int, headers: dict | None = None) -> StreamingResponse:
    """source (a path or a YouTube Music stream URL, asked for with headers) from offset seconds on, transcoded as it plays."""
    codec, container, content_type = FORMATS[fmt]
    sent = ["-headers", "".join(f"{key}: {value}\r\n" for key, value in headers.items())] if headers else []
    command = ["ffmpeg", "-v", "error", "-ss", str(offset), *sent, "-i", source,
               "-map", "0:a:0", "-c:a", codec, "-b:a", f"{kbps}k", "-f", container, "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def body():
        try:
            while data := process.stdout.read(1 << 16):
                yield data
        finally:
            process.kill()
            process.wait()
    return StreamingResponse(body(), media_type=content_type)
