"""«Режиссёр»: Claude читает расшифровку как сценарий и пишет монтажный лист.

Монтажный лист (job["plan"], копия в edl.json рядом с роликом) — что вырезать (дубли, оговорки, вводные,
мораль в конце) с причиной для каждого куска, где сделать наезд, где показать картинку. Сам Claude ничего
не режет: лист собирает ffmpeg (edit.render). Без ключа Claude листа нет, и работает прежнее правило.

Здесь же самопроверка: кадры готового ролика у склеек и картинок показываем Claude и спрашиваем, нет ли брака.
"""
import base64
import json
import os
import re
import subprocess

from . import captions, store

HERE = os.path.dirname(os.path.abspath(__file__))
STYLE_DIR = os.path.join(HERE, "style")
ZOOM_KINDS = {"punch": "резкий наезд на панчлайне", "push": "медленный наезд на длинной подводке",
              "hold": "удержание крупнее на ремарке"}


# ───────────────────────── паспорт стиля ─────────────────────────

def notes_path() -> str:
    return os.path.join(store.DATA, "style_notes.md")


def style_text(style: str = "polina") -> str:
    """Паспорт стиля из репозитория и то, что автор попросил запомнить (хранится на сервере)."""
    parts = []
    path = os.path.join(STYLE_DIR, f"{style}.md")
    if not os.path.exists(path):
        path = os.path.join(STYLE_DIR, "polina.md")
    with open(path, encoding="utf-8") as f:
        parts.append(f.read().strip())
    if os.path.exists(notes_path()):
        with open(notes_path(), encoding="utf-8") as f:
            extra = f.read().strip()
        if extra:
            parts.append("Автор попросил запомнить (важнее правил выше):\n" + extra)
    return "\n\n".join(parts)


def remember(line: str):
    """Дописать правило в память стиля."""
    os.makedirs(store.DATA, exist_ok=True)
    with open(notes_path(), "a", encoding="utf-8") as f:
        f.write(f"- {line.strip()}\n")


# ───────────────────────── монтажный лист ─────────────────────────

def script_text(words: list[dict], pause: float = 0.5) -> str:
    """Расшифровка как сценарий: фразы по паузам ≥ pause с временем, у каждого слова номер.

    [12.4–15.1] 34:Ну 35:собственно 36:ведьмы …"""
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


_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "cuts": {"type": "array", "items": {
            "type": "object",
            "properties": {"from": {"type": "integer"}, "to": {"type": "integer"},
                           "kind": {"type": "string", "enum": ["retake", "slip", "filler", "intro", "aside", "moral", "other"]},
                           "why": {"type": "string"}},
            "required": ["from", "to", "kind", "why"], "additionalProperties": False}},
        "zooms": {"type": "array", "items": {
            "type": "object",
            "properties": {"from": {"type": "integer"}, "to": {"type": "integer"},
                           "kind": {"type": "string", "enum": list(ZOOM_KINDS)}},
            "required": ["from", "to", "kind"], "additionalProperties": False}},
        "pictures": {"type": "array", "items": {
            "type": "object",
            "properties": {"word": {"type": "integer"}, "query": {"type": "string"}, "why": {"type": "string"}},
            "required": ["word", "query", "why"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["cuts", "zooms", "pictures", "summary"],
    "additionalProperties": False,
}

_PLAN_SYSTEM = """Ты монтажёр коротких вертикальных роликов (Reels) автора, описанного ниже. Тебе дают расшифровку
сырого видео: фразы с временем [начало–конец] в секундах, у каждого слова номер («34:слово»). Распознавание
речи иногда ошибается в словах, суди по смыслу.

Напиши монтажный лист. Сам ты ничего не режешь: по листу ролик соберёт программа.

cuts — что вырезать, диапазоны номеров слов включительно (from ≤ to), у каждого вид и короткая причина по-русски:
• retake — автор начал мысль, сбился и сказал её заново: из нескольких дублей одной мысли оставь последний
  законченный и чистый, остальные вырежи целиком (дубль бывает и внутри длинной фразы, и через несколько фраз);
• slip — оговорка, обрыв на полуслове, поправка себя («то есть», «вернее»), неудачное начало слова;
• filler — слова-паразиты и пустые вводные, если это в стиле автора;
• intro / aside / moral / other — вступление, боковая ремарка, мораль в конце, если стиль автора их выкидывает.
Не вырезай смысл: ролик должен остаться связным, каждая оставленная фраза понятна без вырезанной. Не режь
внутри слова. Короткие живые реакции («Вот.») оставляй, если так в стиле. Паузы не трогай: их программа уберёт сама.
Если сомневаешься, лучше оставь.

zooms — наезды камеры по смыслу (диапазон слов, на котором держится наезд): punch — резкий наезд на панчлайне
или неожиданном повороте (1–4 слова), push — медленный наезд на подводке к главному (фраза), hold — чуть крупнее
на ремарке. Примерно один наезд на 6–10 секунд ролика, не на каждой фразе.

pictures — картинки поверх видео: word — номер слова, на котором показать; query — что искать в фотобанке
Wikimedia Commons, коротко по-английски («Helena Blavatsky portrait», «Rider-Waite tarot», «medieval witch trial
engraving»). Подходят реальные люди по имени, книги, места, предметы, гравюры, карты таро. Мемы и персонажей
мультфильмов там не найти, их не предлагай. Не чаще одной картинки на 5–8 секунд, в первые 3 секунды не надо.

summary — одна строка по-русски, что сделано: «убрала 4 дубля и мораль в конце, 7 наездов, 3 картинки».
Учитывай жанр ролика и пожелания автора, если они есть."""


def make_plan(words: list[dict], genre: str = "talk", style: str = "polina", hint: str | None = None) -> dict | None:
    """Монтажный лист от Claude или None (нет ключа, ошибка, ответ не прошёл проверку)."""
    if not words or captions._client() is None:
        return None
    from .edits import GENRES
    user = (f"Жанр: {GENRES.get(genre, GENRES['talk'])['name']}.\n"
            + (f"Пожелания автора: {hint}\n" if hint else "")
            + f"Длительность исходника: {words[-1]['end']:.0f} с.\n\nРасшифровка:\n{script_text(words)}")
    res = captions._ask(_PLAN_SYSTEM + "\n\nСТИЛЬ АВТОРА\n" + style_text(style), user, _PLAN_SCHEMA, max_tokens=8000)
    if not res:
        return None
    return check_plan(res, words)


def check_plan(res: dict, words: list[dict]) -> dict | None:
    """Проверка листа: номера слов существуют, вырезано не больше 60 % речи. Иначе None — работает правило."""
    idx = {w["i"]: w for w in words}
    lo, hi = min(idx), max(idx)

    def ok(a, b):
        return isinstance(a, int) and isinstance(b, int) and lo <= a <= b <= hi

    cuts = [c for c in res.get("cuts") or [] if ok(c.get("from"), c.get("to"))]
    talk = sum(w["end"] - w["start"] for w in words) or 1.0
    cut_talk = sum(w["end"] - w["start"] for w in words if any(c["from"] <= w["i"] <= c["to"] for c in cuts))
    if cut_talk / talk > 0.6:
        return None
    zooms = [z for z in res.get("zooms") or [] if ok(z.get("from"), z.get("to")) and z.get("kind") in ZOOM_KINDS]
    pics = [p for p in res.get("pictures") or [] if ok(p.get("word"), p.get("word")) and (p.get("query") or "").strip()]
    return {"delete": [[c["from"], c["to"]] for c in cuts], "cuts": cuts, "zooms": zooms, "pictures": pics,
            "summary": (res.get("summary") or "").strip(), "source": "claude"}


def restore(plan: dict, words: list[dict], phrases: list[str]) -> int:
    """«Оставь кусок про Якова»: убрать из листа вырезки, которые задевают эти слова. Возвращает, сколько убрано."""
    from .edit import find_phrase
    if not plan:
        return 0
    hit = set()
    for ph in phrases:
        at = find_phrase(words, ph)
        if at:
            hit |= {w["i"] for w in words if at[0] <= w["start"] <= at[1]}
    keep = [c for c in plan.get("cuts") or [] if not any(c["from"] <= i <= c["to"] for i in hit)]
    n = len(plan.get("cuts") or []) - len(keep)
    plan["cuts"], plan["delete"] = keep, [[c["from"], c["to"]] for c in keep]
    return n


def _plural(n: int, one: str, few: str, many: str) -> str:
    k = n % 100
    form = many if 11 <= k <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
    return f"{n} {form}"


CUT_NAMES = {"retake": ("дубль", "дубля", "дублей"), "slip": ("оговорку", "оговорки", "оговорок"),
             "filler": ("вводное слово", "вводных слова", "вводных слов"), "intro": ("вступление", "вступления", "вступлений"),
             "aside": ("ремарку", "ремарки", "ремарок"), "moral": ("мораль в конце",) * 3, "other": ("кусок", "куска", "кусков")}


def plan_line(plan: dict | None, src_secs: float, out_secs: float, zooms: bool = True, pictures: int | None = None) -> str:
    """Одна строка для автора: «оставила 1:48 из 3:35; убрала 4 дубля и мораль; 9 наездов; 5 картинок»."""
    def mmss(s):
        return f"{int(s // 60)}:{int(s % 60):02d}"
    line = f"оставила {mmss(out_secs)} из {mmss(src_secs)}"
    if not plan:
        return line
    kinds = {}
    for c in plan.get("cuts") or []:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    parts = []
    for k, n in kinds.items():
        names = CUT_NAMES.get(k, CUT_NAMES["other"])
        parts.append(names[0] if k == "moral" else _plural(n, *names))
    if parts:
        line += "; убрала " + ", ".join(parts)
    nz = len(plan.get("zooms") or []) if zooms else 0
    if nz:
        line += "; " + _plural(nz, "наезд", "наезда", "наездов")
    np_ = len(plan.get("pictures") or []) if pictures is None else pictures
    if np_:
        line += "; " + _plural(np_, "картинка", "картинки", "картинок")
    return line


def save_edl(job_dir: str, job: dict, plan: dict | None, info: dict | None = None):
    """Монтажный лист целиком (лист Claude + настройки + что вышло) рядом с роликом: для проверки и отладки."""
    edl = {"plan": plan, "opts": job.get("opts"), "genre": job.get("genre"), "style": job.get("style"),
           "images": job.get("images"), "removed": job.get("removed")}
    if info:
        edl["segments"] = info.get("segments")
        edl["cards"] = info.get("cards")
        edl["duration"] = info.get("duration")
    with open(os.path.join(job_dir, "edl.json"), "w", encoding="utf-8") as f:
        json.dump(edl, f, ensure_ascii=False, indent=1)


# ───────────────────────── самопроверка ─────────────────────────

ISSUE_KINDS = {"card_on_face": "картинка закрывает лицо", "subs_under_card": "субтитры под картинкой",
               "subs_cut": "субтитры обрезаны краем кадра", "frozen": "застывший или чёрный кадр",
               "jump": "некрасивая склейка", "other": "другое"}

_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"frame": {"type": "integer"}, "kind": {"type": "string", "enum": list(ISSUE_KINDS)},
                           "note": {"type": "string"}},
            "required": ["frame", "kind", "note"], "additionalProperties": False}},
    },
    "required": ["issues"],
    "additionalProperties": False,
}

_CHECK_SYSTEM = """Ты проверяешь смонтированный вертикальный ролик перед отправкой автору. Тебе дают лист кадров:
у каждого кадра номер и время. Ищи только явный брак: картинка поверх лица человека (card_on_face), субтитры
перекрыты картинкой или налезают на неё (subs_under_card), субтитры обрезаны краем кадра (subs_cut), чёрный,
пустой или «застывший» кадр там, где должен идти человек (frozen), неприятная склейка, когда два соседних кадра
у склейки почти одинаковые с резким сдвигом (jump). Стиль, выбор картинок и цвет не оценивай. Если брака нет,
issues пустой. note — коротко по-русски, что не так."""


def contact_sheet(video: str, times: list[float], out_jpg: str, cols: int = 4) -> list[float]:
    """Лист кадров 270x480 с подписями «№ · время». Возвращает времена кадров по порядку."""
    from PIL import Image, ImageDraw
    from .edit import FONTS
    times = sorted({round(max(0.0, t), 2) for t in times})[:16]
    tiles = []
    tmp = out_jpg + ".f.jpg"
    for t in times:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                        "-vf", "scale=270:480", tmp], check=True)
        tiles.append(Image.open(tmp).convert("RGB"))
    if os.path.exists(tmp):
        os.remove(tmp)
    if not tiles:
        return []
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 270, rows * 510), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(os.path.join(FONTS, "Jost-Medium.ttf"), 22)
    except Exception:
        font = None
    for k, (im, t) in enumerate(zip(tiles, times)):
        x, y = (k % cols) * 270, (k // cols) * 510
        sheet.paste(im, (x, y + 30))
        d.text((x + 8, y + 4), f"{k + 1} · {t:.1f} с", fill=(255, 255, 255), font=font)
    sheet.save(out_jpg, quality=80)
    return times


def check_times(info: dict) -> list[float]:
    """Где смотреть: у склеек (до и после), на каждой картинке, в начале и в конце."""
    total = info.get("duration") or 0
    t, cuts = 0.0, []
    speed = info.get("speed") or 1.0
    for s, e in (info.get("segments") or [])[:-1]:
        t += (e - s) / speed
        cuts.append(t)
    times = [0.5, max(0.0, total - 0.6)]
    times += [c + 0.4 for c in info.get("cards") or []]
    step = max(1, len(cuts) // 5)
    for c in cuts[::step][:5]:
        times += [c - 0.15, c + 0.15]
    return [x for x in times if 0 <= x < total]


def self_check(video: str, info: dict, workdir: str) -> list[dict]:
    """Claude смотрит лист кадров. Возвращает [{"t", "kind", "note"}] (пусто — брака не нашёл или нет ключа)."""
    if captions._client() is None:
        return []
    sheet = os.path.join(workdir, "check.jpg")
    times = contact_sheet(video, check_times(info), sheet)
    if not times:
        return []
    with open(sheet, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
               {"type": "text", "text": f"Кадры: {', '.join(f'{k + 1} — {t:.1f} с' for k, t in enumerate(times))}. "
                                        f"Картинки поверх видео показаны в моменты: "
                                        f"{', '.join(f'{c:.1f}' for c in info.get('cards') or []) or 'нет'} с."}]
    res = captions._ask(_CHECK_SYSTEM, content, _CHECK_SCHEMA, max_tokens=1500)
    out = []
    for x in (res or {}).get("issues") or []:
        k = x.get("frame", 0) - 1
        if 0 <= k < len(times):
            out.append({"t": times[k], "kind": x.get("kind", "other"), "note": x.get("note", "")})
    return out
