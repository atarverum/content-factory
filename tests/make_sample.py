"""Синтетическое «говорящее» видео для проверки монтажа без настоящей съёмки.

Звук: тональные всплески на месте слов, между ними паузы, оговорка «э» и повторный дубль.
"""
import json
import subprocess
import sys

SCRIPT = [
    (0.6, "Сегодня"), (1.1, "новолуние."),
    (3.0, "э"),
    (4.4, "Это"), (4.8, "время"), (5.3, "загадывать"), (6.1, "желания."),
    (8.0, "Руны"), (8.4, "на"), (8.6, "неделю."),               # неудачный дубль
    (10.4, "Руны"), (10.8, "на"), (11.0, "эту"), (11.3, "неделю"), (11.8, "говорят:"),
    (12.6, "не"), (12.8, "бойтесь"), (13.3, "перемен."),
    (15.2, "Лунный"), (15.7, "календарь"), (16.4, "на"), (16.6, "двадцать"), (17.1, "седьмой"), (17.6, "год"),
    (18.0, "уже"), (18.3, "скоро."),
    (20.0, "Подписывайтесь,"), (20.8, "чтобы"), (21.1, "не"), (21.3, "пропустить."),
]


def main(photo, out_video, out_words):
    words = []
    for i, (s, w) in enumerate(SCRIPT):
        dur = min(0.08 * len(w) + 0.12, (SCRIPT[i + 1][0] - s - 0.05) if i + 1 < len(SCRIPT) else 1.0)
        words.append({"start": s, "end": round(s + dur, 3), "word": w})
    total = words[-1]["end"] + 1.5
    amp = "+".join(f"between(t,{w['start']},{w['end']})" for w in words)
    audio = f"aevalsrc='0.3*({amp})*sin(2*PI*(180+40*sin(2*PI*3*t))*t)':s=48000:d={total}"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", photo, "-f", "lavfi", "-i", audio,
        "-filter_complex",
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "zoompan=z='1+0.02*sin(on/40)':d=1:s=1080x1920:fps=30[v]",
        "-map", "[v]", "-map", "1:a", "-t", str(total), "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac", "-pix_fmt", "yuv420p", out_video,
    ], check=True)
    with open(out_words, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:4])
