"""Ручной запуск монтажа: python -m factory.run видео.mp4 --style night [--words words.json] [--title ...]."""
import argparse
import json
import os

from .edit import make_cover, render


def main():
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--style", default="night", choices=["night", "paper"])
    p.add_argument("--words", help="готовые слова с таймингами (иначе распознаём речь)")
    p.add_argument("--music")
    p.add_argument("--badge", help="например «24-е лунные сутки»")
    p.add_argument("--title", help="заголовок на первые секунды и обложку")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    base = os.path.splitext(a.video)[0]
    if a.words:
        with open(a.words, encoding="utf-8") as f:
            words = json.load(f)
    else:
        from .transcribe import load_or_transcribe
        words = load_or_transcribe(a.video, base + ".words.json")

    out = a.out or f"{base}.{a.style}.mp4"
    info = render(a.video, words, out, style=a.style, music=a.music, badge=a.badge, title=a.title)
    if a.title:
        make_cover(out, a.title, os.path.splitext(out)[0] + ".cover.jpg")
    print(json.dumps({k: v for k, v in info.items() if k != "words"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
