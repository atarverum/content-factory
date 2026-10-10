"""Распознавание речи с таймингом каждого слова (faster-whisper, работает на сервере без интернета)."""
import json
import os

_MODEL = None


def transcribe(video_path: str, model_size: str | None = None) -> list[dict]:
    """Список слов [{"start": 1.23, "end": 1.5, "word": "Луна"}, ...] в секундах исходного видео."""
    global _MODEL
    from faster_whisper import WhisperModel

    if _MODEL is None:
        size = model_size or os.environ.get("WHISPER_MODEL", "small")
        _MODEL = WhisperModel(size, device="cpu", compute_type="int8")
    segments, _ = _MODEL.transcribe(
        video_path,
        language="ru",
        word_timestamps=True,
        vad_filter=True,
        initial_prompt="Лунные сутки, руны, расклад, Полина, новолуние, полнолуние.",
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"start": round(w.start, 3), "end": round(w.end, 3), "word": text})
    return words


def load_or_transcribe(video_path: str, cache_path: str) -> list[dict]:
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    words = transcribe(video_path)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=1)
    return words
