from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any


@dataclass(frozen=True)
class TranscriptionResult:
    path: Path
    cached: bool
    elapsed_seconds: float
    sentence_count: int
    language: str
    model: str


def source_fingerprint(path: str | Path) -> str:
    source = Path(path).resolve()
    stat = source.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode())
    digest.update(str(stat.st_mtime_ns).encode())
    with source.open("rb") as stream:
        digest.update(stream.read(1024 * 1024))
        if stat.st_size > 1024 * 1024:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            digest.update(stream.read(1024 * 1024))
    return digest.hexdigest()[:24]


def _model_source(model: str, explicit: str | Path | None) -> str:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    configured = os.getenv("FULIN_WHISPER_MODEL", "").strip()
    if configured:
        candidates.append(Path(configured))
    project_root = Path(__file__).resolve().parents[2]
    candidates.extend(
        [
            project_root / "models" / f"faster-whisper-{model}",
            Path.home() / "Desktop" / "suchen-便携版-1.2.3" / "_internal" / "models" / f"faster-whisper-{model}",
            Path.home() / "Desktop" / "suchen-U盘完整版-内置浏览器" / "_internal" / "models" / f"faster-whisper-{model}",
        ]
    )
    for candidate in candidates:
        if (candidate / "model.bin").is_file():
            return str(candidate.resolve())
    return model


def _to_simplified(text: str) -> str:
    try:
        from opencc import OpenCC

        return OpenCC("t2s").convert(text)
    except (ImportError, OSError, RuntimeError):
        return text


def _sentence_payload(segment: Any) -> dict[str, Any] | None:
    text = _to_simplified(str(segment.text).strip())
    start = round(float(segment.start), 3)
    end = round(float(segment.end), 3)
    if not text or end <= start:
        return None
    words = []
    for word in segment.words or ():
        word_start = float(word.start if word.start is not None else start)
        word_end = float(word.end if word.end is not None else end)
        words.append(
            {
                "start": round(word_start, 3),
                "end": round(max(word_start, word_end), 3),
                "text": _to_simplified(str(word.word).strip()),
                "confidence": round(float(word.probability or 0.0), 4),
            }
        )
    confidence = sum(word["confidence"] for word in words) / len(words) if words else 0.5
    return {
        "start": start,
        "end": end,
        "text": text,
        "confidence": round(confidence, 4),
        "words": words,
    }


@lru_cache(maxsize=2)
def _load_whisper(selected_model: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("缺少 faster-whisper，无法自动识别口播") from exc
    return WhisperModel(selected_model, device="cpu", compute_type="int8")


def transcribe_video(
    video: str | Path,
    cache_dir: str | Path,
    *,
    model: str = "small",
    model_path: str | Path | None = None,
    language: str = "zh",
    force: bool = False,
) -> TranscriptionResult:
    source = Path(video).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    cache_root = Path(cache_dir).resolve() / "asr"
    cache_root.mkdir(parents=True, exist_ok=True)
    fingerprint = source_fingerprint(source)
    output = cache_root / f"{source.stem}_{fingerprint}_{model}.json"
    if output.is_file() and not force:
        payload = json.loads(output.read_text(encoding="utf-8"))
        if payload.get("source_fingerprint") == fingerprint and payload.get("sentences"):
            return TranscriptionResult(
                path=output,
                cached=True,
                elapsed_seconds=0.0,
                sentence_count=len(payload["sentences"]),
                language=str(payload.get("language", language)),
                model=str(payload.get("model", model)),
            )

    selected_model = _model_source(model, model_path)
    started = perf_counter()
    whisper = _load_whisper(selected_model)
    segments, info = whisper.transcribe(
        str(source),
        language=language,
        beam_size=1,
        best_of=1,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    sentences = []
    for segment in segments:
        item = _sentence_payload(segment)
        if item:
            sentences.append(item)
    if not sentences:
        raise RuntimeError("自动转写完成，但没有识别到可用于剪辑的中文口播")
    elapsed = round(perf_counter() - started, 3)
    payload = {
        "source": str(source),
        "source_fingerprint": fingerprint,
        "language": getattr(info, "language", language),
        "language_probability": round(float(getattr(info, "language_probability", 0.0)), 4),
        "model": model,
        "model_source": "local" if Path(selected_model).is_dir() else selected_model,
        "elapsed_seconds": elapsed,
        "sentences": sentences,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return TranscriptionResult(
        path=output,
        cached=False,
        elapsed_seconds=elapsed,
        sentence_count=len(sentences),
        language=str(payload["language"]),
        model=model,
    )
