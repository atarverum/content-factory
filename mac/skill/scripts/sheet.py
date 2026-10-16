"""Лист кадров для самопроверки: python3 sheet.py <ролик.mp4> <лист.jpg> [время в секундах ...]

Без времён берёт 12 кадров равномерно. Claude смотрит лист и проверяет: картинка не закрывает лицо,
субтитры читаются и не налезают на картинки, наезд не режет голову, кадр не тёмный."""
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw


def main():
    video, out = sys.argv[1], sys.argv[2]
    dur = float(json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                                           video], capture_output=True, text=True).stdout)["format"]["duration"])
    times = [float(t) for t in sys.argv[3:]] or [dur * (k + 0.5) / 12 for k in range(12)]
    times = [min(max(0.0, t), dur - 0.05) for t in times][:24]
    tw, th, cols = 270, 480, 6
    rows = (len(times) + cols - 1) // cols
    sheet = Image.new("RGB", (tw * cols, (th + 24) * rows), "white")
    tmp = tempfile.mkdtemp()
    for k, t in enumerate(times):
        f = os.path.join(tmp, f"{k}.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                        "-vf", f"scale={tw}:{th}", f], check=True)
        x, y = (k % cols) * tw, (k // cols) * (th + 24)
        sheet.paste(Image.open(f), (x, y + 24))
        ImageDraw.Draw(sheet).text((x + 6, y + 5), f"{t:.1f} с", fill="black")
    sheet.save(out, quality=85)
    print(out)


if __name__ == "__main__":
    main()
