"""本地媒体准备：提取音频、计算指标和切分传输块。"""

from __future__ import annotations

import math
import subprocess
import sys
import wave
from array import array
from pathlib import Path
from typing import Any, Callable


def get_ffmpeg_executable(error_type: type[Exception]) -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise error_type("缺少 imageio-ffmpeg，请先安装 requirements.txt。") from exc
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    if not executable or not Path(executable).exists():
        raise error_type("imageio-ffmpeg 未找到可用的 FFmpeg 程序。")
    return executable


def prepare_audio(
    video_path: Path,
    work_dir: Path,
    chunk_seconds: int,
    *,
    error_type: type[Exception],
    safe_error_text: Callable[[str], str],
) -> dict[str, Any]:
    full_wav = work_dir / "full_audio.wav"
    full_wav.parent.mkdir(parents=True, exist_ok=True)
    command = [
        get_ffmpeg_executable(error_type), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a",
        "pcm_s16le", str(full_wav),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not full_wav.is_file():
        message = completed.stderr.strip() or "FFmpeg 未生成音频文件。"
        raise error_type(f"提取音频失败：{safe_error_text(message)}")
    metrics = read_wav_metrics(full_wav, error_type)
    chunks = split_wav(full_wav, work_dir / "chunks", chunk_seconds, error_type)
    return {"full_wav": full_wav, "metrics": metrics, "chunks": chunks}


def read_wav_metrics(wav_path: Path, error_type: type[Exception]) -> dict[str, Any]:
    with wave.open(str(wav_path), "rb") as reader:
        channels, sample_width, sample_rate, frame_count = (
            reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()
        )
        if channels != 1 or sample_width != 2:
            raise error_type("音频质量检查只支持16位单声道WAV。")
        sample_count = sum_squares = peak = clipped = silent = 0
        while raw := reader.readframes(16000):
            samples = array("h")
            samples.frombytes(raw)
            if sys.byteorder != "little":
                samples.byteswap()
            for sample in samples:
                absolute = abs(sample)
                sample_count += 1
                sum_squares += sample * sample
                peak = max(peak, absolute)
                clipped += absolute >= 32700
                silent += absolute <= 328
    rms = math.sqrt(sum_squares / sample_count) / 32768 if sample_count else 0.0
    return {
        "duration_seconds": round(frame_count / sample_rate, 3) if sample_rate else 0.0,
        "sample_rate": sample_rate, "channels": channels, "sample_width_bytes": sample_width,
        "rms_normalized": round(rms, 6), "peak_normalized": round(peak / 32768, 6),
        "silence_ratio": round(silent / sample_count, 6) if sample_count else 1.0,
        "clipping_ratio": round(clipped / sample_count, 6) if sample_count else 0.0,
    }


def split_wav(
    wav_path: Path,
    output_dir: Path,
    chunk_seconds: int,
    error_type: type[Exception],
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict[str, Any]] = []
    with wave.open(str(wav_path), "rb") as source:
        channels, sample_width, sample_rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        frames_per_chunk, offset_frames, index = max(1, sample_rate * chunk_seconds), 0, 1
        while offset_frames < source.getnframes():
            raw = source.readframes(frames_per_chunk)
            if not raw:
                break
            actual_frames = len(raw) // (channels * sample_width)
            chunk_path = output_dir / f"audio_{index:03d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setparams((channels, sample_width, sample_rate, actual_frames, source.getcomptype(), source.getcompname()))
                target.writeframes(raw)
            chunks.append({"index": index, "path": chunk_path, "offset_ms": int(offset_frames * 1000 / sample_rate), "duration_seconds": round(actual_frames / sample_rate, 3), "size_bytes": chunk_path.stat().st_size})
            offset_frames += actual_frames
            index += 1
    if not chunks:
        raise error_type("提取出的WAV没有可识别的音频帧。")
    return chunks
