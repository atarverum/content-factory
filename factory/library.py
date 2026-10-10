"""Библиотека звуков и видеоэффектов для правок («звук бум на слове „казнили“», «молния на „мошенница“»).

Файлы бесплатные, с Mixkit (коммерческое использование без указания автора). Их нельзя выкладывать
отдельными файлами в открытый репозиторий, поэтому сервер скачивает каждый при первой просьбе
и хранит у себя в папке данных.
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

from . import store

# имя: (номер на Mixkit, как называем по-русски, слова, по которым узнаём в просьбе)
SOUNDS = {
    "boom": (563, "бум", ["бум", "вайн бум", "удар"]),
    "boom_deep": (2908, "кинобум", ["кинобум", "глубокий бум", "эпичный удар", "трейлер"]),
    "bruh_hit": (2150, "глухой удар", ["глухой удар", "бру", "bruh"]),
    "whoosh_fast": (1490, "свист", ["свист", "вжух", "whoosh", "вуш"]),
    "whoosh_slow": (1474, "медленный свист", ["медленный свист", "медленный вжух"]),
    "riser": (645, "нарастание", ["нарастан", "напряжен"]),
    "record_scratch": (704, "скретч пластинки", ["скретч", "пластинк", "царапин"]),
    "crickets": (1788, "сверчки", ["сверч", "тишина неловк", "неловк"]),
    "dramatic_sting": (546, "дун-дун-дуун", ["дун-дун", "дундун", "драматич", "драма"]),
    "drum_roll": (566, "барабанная дробь", ["барабанн", "дробь"]),
    "rimshot": (579, "ба-дум-тсс", ["ба-дум", "бадум", "тсс", "римшот", "шутк"]),
    "sad_trombone": (471, "грустный тромбон", ["тромбон", "грустн", "провал", "фейл"]),
    "ding": (586, "динь", ["динь", "дзинь", "колокольчик", "уведомлен"]),
    "correct_chime": (2870, "звук «верно»", ["верно", "правильн", "галочк"]),
    "wrong_buzzer": (950, "зуммер «неверно»", ["неверно", "зуммер", "бип", "ошибк"]),
    "choir_aaa": (654, "хор «ааа»", ["хор", "ангел", "аааа", "божествен"]),
    "evil_laugh_synth": (421, "злой смех", ["злой смех", "зловещ", "смех"]),
    "thunder": (1300, "гром", ["гром"]),
    "magic_sparkle": (871, "волшебное мерцание", ["волшеб", "магическ", "мерцан", "блеск"]),
    "church_bell": (619, "церковный колокол", ["колокол"]),
    "heartbeat": (493, "сердцебиение", ["сердц", "сердцебиен"]),
    "clock_ticking": (1063, "тиканье часов", ["тикан", "часы", "часов"]),
    "gong": (1066, "гонг", ["гонг"]),
    "glitch": (1457, "глитч", ["глитч", "сбой"]),
    "pop": (2358, "поп", ["поп", "чпок", "пук"]),
    "metal_clang": (833, "лязг трубы", ["лязг", "труба", "железк"]),
    "boing": (2894, "боинг", ["боинг", "пружин", "дзынь-боинг"]),
    "tada_fanfare": (722, "та-да", ["та-да", "тада", "фанфар", "праздн"]),
    "slide_whistle_down": (395, "свисток вниз", ["свисток"]),
    "bonk": (746, "бонк", ["бонк", "по голове"]),
    "horror_drone": (664, "хоррор-гул", ["хоррор", "жутк", "страшн", "гул"]),
    "wind_howl": (1157, "вой ветра", ["ветер", "ветр", "вой"]),
}

# видеоэффекты поверх кадра; "glow" и "bw" рисуются без файлов
EFFECTS = {
    "lightning": (47948, "молния", ["молни", "вспышк"], 0.4),
    "embers_rising": (3463, "летящие искры", ["искр", "угл", "угол"], 2.5),
    "fire_bottom": (52284, "огонь снизу", ["огонь", "огн", "пламя", "костер", "костёр", "горит"], 2.5),
    "smoke": (1960, "дым", ["дым", "туман"], 2.5),
    "light_leak_warm": (48011, "тёплый засвет", ["теплый засвет", "тёплый засвет", "засвет"], 2.0),
    "light_leak_purple": (48012, "фиолетовый засвет", ["фиолетовый засвет", "фиолет", "мистическ"], 2.0),
    "glitch": (4111, "глитч", ["глитч", "помех", "сбой"], 0.8),
    "vhs_static": (4135, "VHS-помехи", ["vhs", "вхс", "кассет", "старое тв", "телевизор"], 1.2),
    "magic_sparkles": (47961, "волшебные звёздочки", ["звездоч", "звёздоч", "блестк", "блёстк", "магия", "волшеб"], 2.0),
    "glow": (None, "свечение", ["свечен", "сиян", "светит", "ореол", "зелен свечен"], 2.0),
    "bw": (None, "чёрно-белая вставка", ["черно-бел", "чёрно-бел", "чб", "ч/б", "черно бел"], 2.0),
}


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def find_sound(text: str) -> str | None:
    t = _norm(text)
    best = None
    for name, (_, _, keys) in SOUNDS.items():
        for k in keys:
            if _norm(k) in t and (best is None or len(k) > best[1]):
                best = (name, len(k))
    return best[0] if best else None


def find_effect(text: str) -> str | None:
    t = _norm(text)
    best = None
    for name, (_, _, keys, _) in EFFECTS.items():
        for k in keys:
            if _norm(k) in t and (best is None or len(k) > best[1]):
                best = (name, len(k))
    return best[0] if best else None


def _dir(kind: str) -> str:
    d = os.path.join(store.DATA, "library", kind)
    os.makedirs(d, exist_ok=True)
    return d


UA = "IconWitchFactory/1.0 (https://github.com/atarverum/content-factory)"


def _download(urls: list[str], dst: str):
    last = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
                f.write(r.read())
            return
        except Exception as e:  # пробуем следующий адрес
            last = e
    raise RuntimeError(f"не удалось скачать {urls[0]}: {last}")


def sound_file(name: str) -> str:
    """mp3 звука (скачивается при первой просьбе, до 12 секунд)."""
    out = os.path.join(_dir("sounds"), name + ".mp3")
    if os.path.exists(out):
        return out
    num = SOUNDS[name][0]
    raw = out + ".wav"
    _download([f"https://assets.mixkit.co/active_storage/sfx/{num}/{num}.wav"], raw)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-t", "12", "-ac", "2", "-ar", "44100",
                    "-b:a", "192k", out], check=True)
    os.remove(raw)
    return out


def effect_file(name: str) -> str | None:
    """Вертикальный 1080x1920 ролик эффекта на чёрном фоне; None для эффектов без файла."""
    num = EFFECTS[name][0]
    if num is None:
        return None
    out = os.path.join(_dir("effects"), name + ".mp4")
    if os.path.exists(out):
        return out
    raw = out + ".src.mp4"
    _download([f"https://assets.mixkit.co/videos/{num}/{num}-{q}.mp4" for q in ("1080", "720", "360")], raw)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-t", "10", "-an",
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", out], check=True)
    os.remove(raw)
    return out


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def _commons_search(query: str, limit: int = 8) -> list[dict]:
    """Картинки из Wikimedia Commons (свободные лицензии) по запросу, лучшие первыми: [{"title", "thumb"}]."""
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": "6",
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|mime|size", "iiurlwidth": "1080",
    }
    req = urllib.request.Request(COMMONS_API + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        if min(info.get("width") or 0, info.get("height") or 0) < 300:
            continue  # значки и крошечные картинки не годятся
        out.append({"title": p.get("title", ""), "thumb": info.get("thumburl") or info.get("url")})
    return out


def image_file(query: str) -> str:
    """Картинка по запросу («black cat»): скачивается один раз и хранится в папке данных."""
    out = os.path.join(_dir("images"), safe_name(query.lower()) + ".jpg")
    if os.path.exists(out):
        return out
    found = _commons_search(query)
    if not found:
        raise RuntimeError(f"в фотобанке нет картинки «{query}»")
    raw = out + ".src"
    _download([x["thumb"] for x in found[:3]], raw)
    from PIL import Image
    with Image.open(raw) as im:
        im.convert("RGB").save(out, "JPEG", quality=92)
    os.remove(raw)
    return out


def music_dir() -> str:
    """Музыка, которую прислали боту файлом (папка данных, переживает обновления)."""
    return _dir("music")


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s)[:60] or "track"
