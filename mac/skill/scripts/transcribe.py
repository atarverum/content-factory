"""Расшифровка видео по словам: words.json (время каждого слова) и script.txt (сценарий с номерами слов).

python3 transcribe.py <видео> <папка ролика>
"""
import json
import os
import re
import sys


def transcribe(video: str) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    segments, _ = model.transcribe(video, language="ru", word_timestamps=True, vad_filter=True,
                                   initial_prompt="Лунные сутки, руны, расклад, Полина, ведьмы, Блаватская, таро.")
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"i": len(words), "start": round(w.start, 3), "end": round(w.end, 3), "word": text})
    return words


def script_text(words: list[dict], pause: float = 0.5) -> str:
    """Фразы по паузам с временем, у каждого слова номер: [12.4–15.1] 34:Ну 35:собственно 36:ведьмы"""
    lines, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] >= pause or re.search(r"[.!?…]$", cur[-1]["word"])):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return "\n".join(f"[{p[0]['start']:.1f}–{p[-1]['end']:.1f}] " + " ".join(f"{w['i']}:{w['word']}" for w in p)
                     for p in lines)


def main():
    video, job = sys.argv[1], sys.argv[2]
    os.makedirs(job, exist_ok=True)
    words_path = os.path.join(job, "words.json")
    if os.path.exists(words_path):
        words = json.load(open(words_path, encoding="utf-8"))
    else:
        words = transcribe(video)
        with open(words_path, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False, indent=1)
    with open(os.path.join(job, "script.txt"), "w", encoding="utf-8") as f:
        f.write(script_text(words) + "\n")
    print(f"слов: {len(words)}, длительность речи: {words[-1]['end'] if words else 0:.1f} с")
    print(f"сценарий: {os.path.join(job, 'script.txt')}")


if __name__ == "__main__":
    main()
