"""Правки к смонтированному ролику: что можно попросить, как это понять и как применить.

Правка — словарь EMPTY с заполненными полями. Её собирает Claude (captions.parse_edit) или,
если ключа нет, простой разбор по словам (parse_simple). Несколько правок в одном сообщении
выполняются разом. apply() переносит правку в ролик, summary() пересказывает её по-человечески.
"""
import datetime as dt
import re
from zoneinfo import ZoneInfo

from . import library

MSK = ZoneInfo("Europe/Moscow")

EMPTY = {
    # жанр ролика: talk (рассказ), vlog (влог, путешествие, зарисовка), sketch (комедийный скетч)
    "genre": None,
    # «больше ничего не делай»: только субтитры и цвет, без нарезки, картинок, музыки, заголовка, заставки
    "minimal": None,
    # нарезка
    "cut": None, "smooth": [], "unclip": [], "keep_retakes": None, "trim_start": None, "trim_end": None,
    "remove_phrases": [], "restore_phrases": [],
    # темп и кадр
    "speed": None, "zoom": None, "zoom_words": [], "mirror": None,
    # цвет
    "color": None,
    # субтитры
    "subs": None, "sub_size": None, "sub_pos": None, "sub_shift": None, "sub_case": None, "sub_color": None, "sub_font": None,
    "sub_words": None, "fixes": [], "accents": [], "auto_accents": None,
    # картинки
    "no_cards": None, "card_scale": None, "card_pos": None, "card_secs": None, "cards_off": [],
    # новые картинки из фотобанка: [{"phrase": слово из речи, "query": что искать}]
    "images": [],
    # звук
    "music": None, "music_vol": None, "voice_vol": None, "denoise": None, "sounds": [],
    # эффекты
    "effects": [],
    # что очистить целиком: sounds, effects, zoom_words, accents, cards_off, fixes, removed, color, images
    "clear": [],
    # текст в кадре
    "style": None, "title": None, "title_on": None, "badge": None, "end_card": None,
    # подпись к посту
    "caption": None, "recaption": None, "cta": None, "hashtags_add": [], "hashtags_remove": [],
    # обложка
    "cover_text": None, "cover_at": None,
    # публикация: queue / now / archive / cancel; время «ГГГГ-ММ-ДД ЧЧ:ММ» по МСК
    "publish": None, "publish_when": None,
    # чего бот пока не умеет (пересказ просьбы)
    "unsupported": [],
    "note": "",
}

# какие поля хранятся в настройках монтажа ролика (job["opts"])
OPT_KEYS = ["sub_shift", "cut", "keep_retakes", "trim_start", "trim_end", "speed", "zoom", "mirror", "color", "subs", "sub_size",
            "sub_pos", "sub_case", "sub_color", "sub_font", "sub_words", "card_scale", "card_pos", "card_secs",
            "music_vol", "voice_vol", "denoise", "end_card", "title_on"]
LIST_OPTS = ["zoom_words", "cards_off", "fixes", "sounds", "effects", "smooth", "unclip"]

# Жанры: с чего начинается монтаж. Правки поверх жанра сохраняются, смена жанра сбрасывает его настройки.
GENRES = {
    "talk": {"name": "рассказ", "desc": "нарезка, картинки, выделения, как ролики Полины", "opts": {}, "job": {"no_cards": None, "auto_accents": None, "cta": True}},
    "vlog": {"name": "влог / зарисовка", "desc": "ролик целиком, без вырезаний, картинок и заголовка, только субтитры",
             "opts": {"cut": "none", "zoom": "off", "title_on": False, "end_card": False},
             "job": {"no_cards": True, "auto_accents": False, "cta": False}},
    "sketch": {"name": "скетч / комедия", "desc": "плотная нарезка, без картинок и заголовка",
               "opts": {"cut": "tight", "title_on": False, "end_card": False},
               "job": {"no_cards": True, "auto_accents": False, "cta": False}},
}
GENRE_WORDS = [
    ("sketch", r"комеди|скетч|юмор|смешн|прикол|сценк|пародия|шутк\w* ролик"),
    ("vlog", r"путешеств|влог|блог|зарисовк|поездк|прогулк|отпуск|трип|из поезд|из отпуск|повседнев|лайфстайл"),
    ("talk", r"рассказ|лекци|просвет|образоват|познават|религиовед|про истори|научн"),
]
FONT_WORDS = [  # как могут назвать шрифт → имя шрифта
    (r"рукопис|от руки|почерк", "Caveat"), (r"каллиграф|пропис|витиеват", "Marck Script"),
    (r"лобстер|ретро|винтаж", "Lobster"), (r"пасифик|летн|пляжн|курортн", "Pacifico"),
    (r"амати|тонк\w* рукопис|узк", "Amatic SC"), (r"элегантн|журнальн|плейфер|playfair", "Playfair Display"),
    (r"монтсерр|montserrat|современн|жирн", "Montserrat"), (r"рубик|округл", "Rubik"),
    (r"комфорта|мягк|кругл|милый|мил", "Comfortaa"), (r"руссо|спортивн|квадратн|грубоват|брутальн", "Russo One"),
    (r"анбаунд|unbounded|широк|модн", "Unbounded"), (r"засеч|красив|старинн|ораниенбаум|oranienbaum", "Oranienbaum"),
    (r"прост|рублен|без засеч|jost|джост|как у полин|обычн", "Jost"),
]

COLOR_WORDS = {"красн": "red", "золот": "gold", "желт": "yellow", "бел": "white", "черн": "black", "розов": "pink",
               "коралл": "pink", "син": "indigo", "фиолет": "purple", "сирен": "purple", "зелен": "green",
               "оранж": "orange", "бежев": "paper"}
COLOR_NAMES = {"red": "красный", "gold": "золотой", "yellow": "жёлтый", "white": "белый", "black": "чёрный",
               "pink": "розовый", "indigo": "синий", "purple": "фиолетовый", "green": "зелёный", "orange": "оранжевый",
               "paper": "бежевый", "night": "тёмный"}
LOOK_NAMES = {"bright": "ярче", "light": "светлее", "dark": "темнее", "warm": "теплее", "cold": "холоднее",
              "muted": "приглушённее", "contrast": "контрастнее", "bw": "чёрно-белое", "natural": "без цветокоррекции"}
OPPOSITE = {"warm": "cold", "cold": "warm", "light": "dark", "dark": "light", "bright": "muted", "muted": "bright"}
POS_NAMES = {"top": "сверху", "middle": "посередине", "bottom": "снизу"}
STYLE_NAMES = {"polina": "как у Полины", "night": "ночной", "paper": "светлый"}


def new_edit() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY.items()}


def _n(s: str) -> str:
    return s.lower().replace("ё", "е")


def _num(text: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if m:
        return float(m.group(1).replace(",", "."))
    words = {"одн": 1, "полторы": 1.5, "полсекунд": 0.5, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5,
             "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
    for k, v in words.items():
        if re.search(r"\b" + k, text):
            return v
    return None


def _quoted(text: str) -> list[str]:
    return [q.strip() for q in re.findall(r"[«\"“„']([^»\"”“']+)[»\"”“']", text) if q.strip()]


def _target(clause: str, loose: bool = False) -> str | None:
    """На какую фразу правка: «в кавычках» или после «на слове…», «где я говорю…», «когда говорю…».

    loose: годится и просто «на X» («молния на мошенница»)."""
    q = _quoted(clause)
    if q:
        return q[0]
    if loose:
        m = re.search(r"\bна\s+(?!слов|фраз|моменте|экран|обложк)(.+)$", clause)
        if m and not re.search(r"(?:на слов|на фраз|где |когда |после слов)", clause):
            tail = re.split(r"\b(?:и|а|потом|ещё|еще|тоже|пожалуйста)\b", m.group(1))[0]
            words = re.findall(r"[\wё-]+", tail)[:3]
            return " ".join(words) or None
    m = re.search(r"(?:на слов\w*|на фраз\w*|\bслов[оа]\b|\bфразу\b|где (?:я |она )?(?:говор\w*|произнош\w*)|когда (?:я |она )?говор\w*|"
                  r"после слов\w*|после фраз\w*|после того,? как (?:я |она )?(?:говор\w*|сказа\w*)|на моменте|в момент\w*,? где|про)\s+(.+)$", clause)
    if not m:
        return None
    tail = re.split(r"\b(?:и|а|потом|ещё|еще|тоже|пожалуйста|резк\w*|склейк\w*|сделай|перемонтир\w*|"
                    r"получ\w*|плавн\w*|поставь|добавь|убери)\b", m.group(1), flags=re.I)[0]
    words = [w for w in re.findall(r"[\wё-]+", tail)]
    while words and _n(words[0]) in ("про", "о", "об", "где", "я", "она", "говорю", "говорит"):
        words = words[1:]
    return " ".join(words[:3]) or None


NEG = ("убер", "убр", "без ", "не надо", "не нуж", "выключ", "отключ", "удали", "не став", "не добавл", "уйди", "сним", "долой")


def _neg(c: str) -> bool:
    return any(k in c for k in NEG) or c.startswith("без")


def _clauses(text: str) -> list[str]:
    # запятые и точки внутри кавычек и чисел («1.2») не режем
    prot = re.sub(r"[«\"“„][^»\"”“]*[»\"”“]", lambda m: m.group(0).replace(",", "\x00").replace(".", "\x01"), text)
    prot = re.sub(r"(\d)[.,](\d)", "\\1\x01\\2", prot)
    parts = re.split(r"[.;!?\n]+|,|\s+(?:а также|и ещё|и еще|плюс|потом)\s+", prot)
    parts = [p.replace("\x00", ",").replace("\x01", ".") for p in parts]
    out = []
    for p in parts:
        # «… и …» делим, только если обе части похожи на просьбы (а не «руны и луна»)
        bits = re.split(r"\s+и\s+(?=(?:сделай|сделал|добавь|добавил|убери|убрал|поставь|вырежи|верни|выдели|смени|"
                        r"поменяй|перепиши|выложи|увеличь|уменьши|подними|опусти|ускорь|замедли|обрежь|наложи|"
                        r"включи|выключи|перемонтир|подкрас|измени|отрази)\w*)", p, flags=re.I)
        out += [b.strip() for b in bits if b.strip()]
    return out


def parse_simple(request: str, words: list[dict], job: dict) -> dict:
    """Разбор правки без Claude: понимает самые частые просьбы во всех разделах, по нескольку за раз."""
    e = new_edit()
    o = job.get("opts") or {}
    whole = _n(request)
    # жанр, «больше ничего не делай», «от начала до конца», ссылка на Telegram — смотрим на всю просьбу целиком
    for raw in _clauses(request):
        c = _n(raw)
        if re.search(r"\bне (?:про|из|в) |\bне (?:влог|скетч|рассказ)", c):
            continue
        for g, pat in GENRE_WORDS:
            if re.search(pat, c) and not re.search(r"подпис|хе?ш-?тег|хэш", c):
                e["genre"] = e["genre"] or g
    if re.search(r"больше ничего не делай|ничего больше не делай|только субтитр|ничего лишнего|ничего не добавляй|"
                 r"ничего не меняй кроме|ничего другого не", whole):
        e["minimal"] = True
    if re.search(r"от начала (?:и )?до конца|целиком|не обрезай|ничего не вырезай|без нарезк|не режь|как есть|"
                 r"полностью весь|весь ролик целиком", whole):
        e["cut"] = "none"
    if re.search(r"(?:не надо|без|убери|убрал|не нужн\w*|не став\w*)\s(?:\w+\s){0,3}(?:ссылк|телеграм|призыв)", whole):
        e["cta"] = False
    elif re.search(r"(?:добавь|верни|постав\w*)\s(?:\w+\s){0,3}(?:ссылк|телеграм|призыв)", whole):
        e["cta"] = True
    request = _pull_fixes(request, e)
    for raw in _clauses(request):
        c = _n(raw)
        q = _quoted(raw)
        tgt = _target(raw)
        neg = _neg(c)
        add = any(k in c for k in ("добав", "верни", "включ", "постав", "наложи", "вставь", "сделай", "нужн", "хочу"))
        handled = False
        if re.search(r"ссылк|телеграм|призыв", c) and e["cta"] is not None and not re.search(r"подпис\w*\s*[:—-]?\s*[«\"]", raw):
            continue
        if re.search(r"больше ничего не делай|ничего больше не делай|ничего лишнего|от начала (?:и )?до конца", c):
            continue

        # ── публикация
        if re.search(r"выклад|выложи|опублику|публику|в очередь|в архив|отмени публик|сними с публ|запланир", c):
            if "архив" in c:
                e["publish"] = "archive"
            elif "отмен" in c or "сними" in c:
                e["publish"] = "cancel"
            elif "сейчас" in c or "сразу" in c:
                e["publish"] = "now"
            else:
                e["publish"] = "queue"
                when = _when(c)
                if when:
                    e["publish_when"] = when
            continue

        # ── подпись к посту и хештеги
        if re.search(r"подпис|описани|текст пост|хе?ш-?тег|хэш-?тег|#", c) and "субтит" not in c:
            tags = re.findall(r"#[\wё]+", raw)
            if tags or "тег" in c:
                if neg:
                    e["hashtags_remove"] += tags
                else:
                    e["hashtags_add"] += tags
                if not tags:
                    e["recaption"] = True
            elif q and re.search(r"подпис\w*\s*[:—-]?\s*[«\"]", raw, re.I):
                e["caption"] = q[0]
            else:
                e["recaption"] = True
            continue

        # ── обложка
        if "обложк" in c or "превью" in c:
            if q:
                e["cover_text"] = q[0]
            sec = _num(c)
            if sec is not None and "секунд" in c:
                e["cover_at"] = sec
            elif not q:
                e["cover_at"] = float(job.get("cover_at") or 0.8) + 2.5
            continue

        # ── заголовок, плашка, заставка, стиль
        if "заголов" in c or "название" in c:
            if q:
                e["title"], e["title_on"] = q[0], True
            elif neg:
                e["title_on"] = False
            else:
                e["title_on"] = True
            continue
        if "плашк" in c:
            e["badge"] = q[0] if q else ("" if neg else None)
            if not q and not neg:
                e["unsupported"].append("плашка без текста: напишите «плашка: „текст“»")
            continue
        if re.search(r"заставк|концовк|финальн\w* (?:кадр|титр|экран)", c):
            e["end_card"] = not neg
            continue
        if "стил" in c or "бумаг" in c:
            if "полин" in c:
                e["style"] = "polina"
            elif "светл" in c or "бумаг" in c:
                e["style"] = "paper"
            elif "темн" in c or "ночн" in c:
                e["style"] = "night"
            if e["style"]:
                continue

        # ── субтитры
        if re.search(r"субтит|титр|текст на экран|надпис|букв|шрифт|слов\w* на экране|строчк", c):
            handled = True
            if re.search(r"исправ|ошибк|неправильн|пишется|опечат", c) or (len(q) >= 2 and " на " in c):
                if len(q) >= 2:
                    e["fixes"].append({"from": q[0], "to": q[1]})
                else:
                    e["unsupported"].append("исправить слово: напишите «исправь „как сейчас“ на „как надо“»")
                continue
            if re.search(r"были одни|одни субтитр|только одни|двойн|дублир|два раза|дважды|две строк", c):
                e["title_on"] = False  # второй строкой шёл заголовок: убираем его, субтитры остаются одни
                continue
            if neg and not re.search(r"больш|мельч|меньш|крупн|выше|ниже", c):
                e["subs"] = False
                continue
            if add or re.search(r"наложи|нужны|включи", c):
                e["subs"] = True
            step = 70 if re.search(r"чуть|немн|слегка|капельк", c) else 150
            if re.search(r"выше|подними|повыше|поднять", c):
                e["sub_shift"] = int(o.get("sub_shift") or 0) + step
            elif re.search(r"ниже|опусти|пониже|опустить", c):
                e["sub_shift"] = int(o.get("sub_shift") or 0) - step
            if re.search(r"заглавн|капс|больш\w* букв|прописн", c):
                e["sub_case"] = "upper"
            elif re.search(r"строчн|маленьк\w* букв", c):
                e["sub_case"] = "lower"
            elif "обычн" in c and ("регистр" in c or "букв" in c):
                e["sub_case"] = "normal"
            elif re.search(r"крупн|больш|увелич", c):
                e["sub_size"] = round(float(o.get("sub_size") or 1.0) * 1.2, 2)
            elif re.search(r"мельч|меньш|уменьш", c):
                e["sub_size"] = round(float(o.get("sub_size") or 1.0) / 1.2, 2)
            pos = _pos(c)
            if pos:
                e["sub_pos"] = pos
            col = _color(c)
            if col:
                e["sub_color"] = col
            elif re.search(r"цвет", c) and re.search(r"друг|смени|поменя|измени", c):
                cycle = ["white", "yellow", "pink", "gold", "red"]
                cur = o.get("sub_color")
                e["sub_color"] = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle else "white"
            if "шрифт" in c or re.search(r"рукопис|каллиграф|засеч", c):
                font = next((f for pat, f in FONT_WORDS if re.search(pat, c)), None)
                if not font and re.search(r"друг|смени|поменя|измени|ин[оа]", c):
                    from .edit import SUB_FONTS
                    cur = o.get("sub_font") or "Jost"
                    font = SUB_FONTS[(SUB_FONTS.index(cur) + 1) % len(SUB_FONTS)] if cur in SUB_FONTS else "Montserrat"
                if font:
                    e["sub_font"] = font
            m = re.search(r"по (\d|одн|дв|тр|четыр)\w*\s*слов", c)
            if m:
                e["sub_words"] = {"одн": 1, "дв": 2, "тр": 3, "четыр": 4}.get(m.group(1), None) or int(m.group(1))
            elif "меньше слов" in c:
                e["sub_words"] = 1
            elif "больше слов" in c:
                e["sub_words"] = 3
            continue
        if re.search(r"исправ\w*|опечат", c) and len(q) >= 2:
            e["fixes"].append({"from": q[0], "to": q[1]})
            continue
        if "выдел" in c:
            if "не выделяй" in c or "без выделен" in c or (neg and not q):
                e["auto_accents"] = False
                e["clear"].append("accents")
            elif q:
                col = _color(c[c.find(_n(q[0])) + len(q[0]):] if _n(q[0]) in c else c)
                font = ("Oranienbaum" if re.search(r"засеч|красив", c) else "Jost" if "прост" in c else None)
                e["accents"].append({"phrase": q[0], "color": col, "font": font})
            elif tgt:
                e["accents"].append({"phrase": tgt, "color": _color(c), "font": None})
            continue

        # ── картинки и мемы
        if re.search(r"картин|мем|фото|изображ|карточк|иллюстрац", c):
            if neg and tgt:
                e["cards_off"].append(tgt)
            elif neg and re.search(r"нов|добавленн|найденн", c):
                e["clear"].append("images")
            elif neg:
                e["no_cards"] = True
            elif "верни" in c:
                e["no_cards"] = False
            elif re.search(r"чаще|побольше картин|больше картин|больше мем|добав|встав|постав|найди|покажи", c):
                at = _target(raw)
                m2 = re.search(r"(?:картинк\w*|фото\w*|изображени\w*|иллюстрац\w*)\s+(?:с\s+|со\s+)?([\wё-]+(?:\s+[\wё-]+)?)", c)
                what = m2.group(1) if m2 else None
                if what and re.match(r"(?:на|в|где|когда|после|про)\b", what):
                    what = None
                if what:
                    what = re.split(r"\s+(?:на|в|где|когда|после)\b", what)[0]
                if at or what:
                    e["images"].append({"phrase": at or what, "query": what or at})
                else:
                    e["unsupported"].append("новая картинка: скажите, что на ней и на каком слове, или пришлите фото "
                                            "с подписью «на слово …»")
            if re.search(r"крупн|увелич|больше размер|побольше размер", c) or (re.search(r"\bбольше\b", c) and not add):
                e["card_scale"] = round(float(o.get("card_scale") or 1.0) * 1.25, 2)
            elif re.search(r"мельч|меньш|уменьш", c):
                e["card_scale"] = round(float(o.get("card_scale") or 1.0) / 1.25, 2)
            pos = _pos(c)
            if pos:
                e["card_pos"] = pos
            if re.search(r"дольше|подольше|длиннее|подлиннее", c):
                e["card_secs"] = 3.2
            elif re.search(r"короче|быстрее|покороче|мельком", c):
                e["card_secs"] = 1.4
            continue

        # ── музыка
        if "музык" in c or "трек" in c or "мелоди" in c:
            if re.search(r"громч|погромч|сильнее", c):
                e["music_vol"] = round(float(o.get("music_vol") or 1.0) * 1.5, 2)
                e["music"] = True
            elif re.search(r"тише|потиш|тихо|приглуш", c):
                e["music_vol"] = round(float(o.get("music_vol") or 1.0) / 1.6, 2)
            elif neg:
                e["music"] = False
            elif re.search(r"друг|смени|поменя|весел|грустн|спокойн|бодр|энергичн|таинствен", c):
                e["unsupported"].append("другая музыка: пришлите боту mp3-файл, и я поставлю его в этот ролик")
            else:
                e["music"] = True
            continue

        # ── голос и шум
        if re.search(r"голос|речь|меня не слышно|слышно плохо", c) and not re.search(r"голосов\w* сообщ", c):
            if re.search(r"громч|погромч|не слышно|плохо слышно", c):
                e["voice_vol"] = round(float(o.get("voice_vol") or 1.0) * 1.3, 2)
                handled = True
            elif re.search(r"тише|потиш", c):
                e["voice_vol"] = round(float(o.get("voice_vol") or 1.0) / 1.3, 2)
                handled = True
            if re.search(r"шум|шип|эхо|чище", c):
                e["denoise"] = True
                handled = True
            if handled:
                continue
        if re.search(r"шум|шипен|фон\w* звук|гул на фоне|ветер в микрофон|почисти звук", c) and \
                not library.find_sound(c.replace("шум", "")):
            e["denoise"] = not ("верни" in c)
            continue

        # ── звуковые эффекты
        snd = library.find_sound(c) if re.search(r"звук|эффект|саунд|бум|дзинь|динь|свист|сверч|ба-дум|бадум|тромбон|"
                                                 r"гонг|гром|фанфар|хор|скретч|колокол|сердц|поп|бонк|боинг", c) else None
        if re.search(r"звук", c) and neg and not tgt:
            e["clear"].append("sounds")
            continue
        if snd and not ("эффект" in c and not "звук" in c and library.find_effect(c)):
            tgt = _target(raw, loose=True)
            if tgt:
                e["sounds"].append({"phrase": tgt, "sound": snd})
            else:
                e["unsupported"].append(f"звук «{library.SOUNDS[snd][1]}»: скажите, на каком слове, "
                                        f"например «звук {library.SOUNDS[snd][1]} на слове „казнили“»")
            continue

        # ── видеоэффекты
        fx = library.find_effect(c)
        if re.search(r"эффект", c) and neg and not tgt and not fx:
            e["clear"].append("effects")
            continue
        if fx:
            tgt = _target(raw, loose=True)
        if fx and not re.search(r"(?:цвет|картинк|видео|ролик|всё|все)\w* .*черно-бел|черно-бел\w* (?:весь|все|всё)", c) \
                or (fx == "bw" and tgt):
            if tgt:
                e["effects"].append({"phrase": tgt, "effect": fx})
            elif fx not in ("bw",):
                e["unsupported"].append(f"эффект «{library.EFFECTS[fx][1]}»: скажите, на каком слове")
            else:
                fx = None
            if fx:
                continue

        # ── кадр: отражение и то, чего пока нет
        if re.search(r"отрази|зеркал", c):
            e["mirror"] = not neg and "верни" not in c
            continue
        if re.search(r"поверн|переверн|кадрир|сдвинь кадр|смести кадр|обрежь кадр", c):
            e["unsupported"].append("поворот, сдвиг и кадрирование картинки")
            continue

        # ── нарезка: начало, конец, дубли, конкретные фразы
        if re.search(r"дубл|оговорк|запинк|повтор", c) and not re.search(r"замедленн\w* повтор", c):
            if re.search(r"остав|верни|не убир|не выреза", c):
                e["keep_retakes"] = True
            else:
                e["keep_retakes"] = False
            continue
        if re.search(r"начал|первые|в начале|начни|старт", c) and re.search(r"обреж|убер|вырежи|отрежь|срежь|начни с|лишн", c):
            sec = _num(c)
            e["trim_start"] = sec if sec is not None and sec < 60 else 2.0
            continue
        if re.search(r"конец|концовк|последн|в конце|финал|закончи", c) and re.search(r"обреж|убер|вырежи|отрежь|срежь|лишн|раньше", c):
            sec = _num(c)
            e["trim_end"] = sec if sec is not None and sec < 60 else 2.0
            continue
        if re.search(r"обрез\w*|оборва\w*|обрыва\w*|съел\w*|съеда\w*|отрезал\w*|не договар|недоговор|проглот", c) and \
                re.search(r"слов|фраз|начал|конец|окончан|букв|на\s|[«\"]", c) and not re.search(r"секунд|кадр|видео в начал", c):
            ut = q[0] if q else _target(raw, loose=True)
            if not ut:
                m2 = re.search(r"(?:слово|фразу|слова)\s+([\wё-]+(?:\s+[\wё-]+){0,2})", c)
                ut = m2.group(1) if m2 else None
            if ut:
                e["unclip"].append(ut)
            else:
                e["cut"] = e["cut"] or "gentle"  # слово не назвали — режем мягче везде
            continue
        if re.search(r"склейк|перемонтир|плавн|резк\w* (?:переход|обрыв)|дерга|обрыв", c):
            st = _target(raw)
            if st:
                e["smooth"].append(st)
            elif not e["smooth"] and not re.search(r"после слов", whole):
                e["cut"] = e["cut"] or "gentle"
            continue
        if re.search(r"верни\w*", c) and (q or re.search(r"фраз|кусок|слов|момент", c)):
            if q or tgt:
                e["restore_phrases"].append(q[0] if q else tgt)
            else:
                e["clear"].append("removed")
            continue
        if re.search(r"вырежи|вырезать|убери|удали|выкинь|отрежь|срежь", c) and (q or re.search(r"фраз|кусок|слов|момент|где я|про ", c)) \
                and not re.search(r"зум|наезд|приближ|выдел|цвет", c):
            if q:
                e["remove_phrases"] += q
            elif tgt:
                # «вырежи фразу про погоду» — целиком фразу, где звучит это слово
                e["remove_phrases"].append(_sentence_with(words, tgt) if re.search(r"фраз|кусок|момент|про ", c) else tgt)
            else:
                e["unsupported"].append("что вырезать: напишите фразу в кавычках, например «вырежи „ну короче“»")
            continue

        # ── общие ручки, их часто просят вместе («динамичнее и ярче», «подкрась и сделай подлиннее»)
        hit = False
        looks = []
        for pat, look in ((r"ярч|ярк|сочн|насыщ|подкрас|красочн", "bright"), (r"светлее|посветл|высветл", "light"),
                          (r"темнее|затемн|потемн|мрачн", "dark"), (r"тепл|желтее", "warm"),
                          (r"холодн|синее", "cold"), (r"приглуш|бледн|блекл|пастел", "muted"),
                          (r"контраст", "contrast"), (r"черно-бел|чб\b|ч/б|без цвета|монохром", "bw"),
                          (r"без цветокор|естествен|натурал|как в оригинал|как снято|как на телефон|без фильтр", "natural")):
            if re.search(pat, c):
                looks.append(look)
        if looks or (re.search(r"цвет", c) and re.search(r"как было|верни|сбрось|обычн", c)):
            hit = True
            if not looks:
                e["color"] = []
            else:
                cur = list(e["color"] if e["color"] is not None else (o.get("color") or []))
                for lk in looks:
                    cur = [x for x in cur if x != OPPOSITE.get(lk)]
                    if lk == "natural":
                        cur = [x for x in cur if x == "bw"]
                    if lk != "natural" and re.search(r"убер|не надо|не нуж|верни", c):
                        cur = [x for x in cur if x != lk]
                    elif lk not in cur:
                        cur.append(lk)
                e["color"] = cur

        if re.search(r"зум|наезд|приближ|приблиз|крупн\w* план|укрупн", c):
            hit = True
            ztgt = _target(raw, loose=True)
            if neg and ztgt:
                e["zoom_words"] = [z for z in o.get("zoom_words", []) if _n(z) != _n(ztgt)]
                e["clear"].append("zoom_words")
            elif neg:
                e["zoom"] = "off"
                e["clear"].append("zoom_words")
            elif ztgt:
                e["zoom_words"].append(ztgt)
            elif re.search(r"меньше|слабее|реже", c):
                e["zoom"] = "normal" if o.get("zoom") == "strong" else "off"
            else:
                e["zoom"] = "strong"

        m = re.search(r"в (\d+(?:[.,]\d+)?) раз", c)
        if re.search(r"ускор|быстрее|побыстрее|темп\b|темпа|бодрее|динамичн|живее|медленн|замедл|спокойнее|скорост", c) \
                and not re.search(r"режь|резать|нарез", c):
            hit = True
            sp = float(o.get("speed") or 1.0)
            if m:
                sp = float(m.group(1).replace(",", "."))
            elif re.search(r"как было|обычн|нормальн", c):
                sp = 1.0
            elif re.search(r"медленн|замедл|спокойнее", c):
                sp = sp / 1.1
            else:
                sp = sp * 1.1
            e["speed"] = round(max(0.7, min(1.6, sp)), 2)

        if re.search(r"длинн|длиньше|подольше|меньше режь|меньше вырез|оставь пауз|помягче|воздух|не так рез|"
                     r"увелич\w* (?:длин|ролик|видео|хрономет)", c):
            if e["cut"] != "none":
                e["cut"] = "gentle"
            hit = True
        elif re.search(r"короче|покороче|плотнее|динамичн|бодрее|живее|убери (?:все )?пауз|режь сильнее|больше режь|туже", c):
            if e["cut"] != "none":
                e["cut"] = "tight"
            hit = True
        elif re.search(r"нарезк|паузы", c) and re.search(r"как было|обычн|верни", c):
            e["cut"], hit = "normal", True
        if hit:
            continue

        # ── то, чего фабрика пока не умеет
        for pat, what in ((r"местами|переставь|порядок|в начало перенеси|перенеси в начало", "переставить куски местами"),
                          (r"переход", "переходы между кусками"),
                          (r"стоп-?кадр|замри|заморо", "стоп-кадр"),
                          (r"замедленн\w* повтор|повтор\w* замедл|слоу-?мо|slow", "замедленный повтор"),
                          (r"\bлого|водян\w* знак|ватермарк", "логотип в кадре")):
            if re.search(pat, c):
                e["unsupported"].append(what)
                handled = True
                break
        if not handled and re.search(r"переделай|не нравит|плохо|ужасн|по-другому", c):
            e["unsupported"].append("общая переделка: скажите, что именно поменять (цвет, темп, субтитры, картинки…)")

    # ролик и так целиком (влог) — «подлиннее» не должно включать нарезку
    g = e["genre"] or job.get("genre") or "talk"
    cur_cut = o.get("cut") if (o.get("cut") and not e["genre"]) else GENRES[g]["opts"].get("cut")
    if e["cut"] == "gentle" and cur_cut == "none":
        e["cut"] = None
    _dedup(e)
    if summary(e):  # «плохо, переделай» рядом с понятными правками — не повод говорить «не умею»
        e["unsupported"] = [u for u in e["unsupported"] if not u.startswith("общая переделка")]
    return e


_W = r"[«\"“„']?([\wё-]+(?:\s+[\wё-]+){0,2}?)[»\"”“']?"
_FIX_PATTERNS = [
    # «исправь слово блаватская на Блаватская», «замени „таро“ на „Таро“»
    r"(?:исправ\w*|замени\w*|поменяй|перепиши|напиши)\s+(?:в\s+субтитрах\s+)?(?:слово\s+|написание\s+)?" + _W + r"\s+на\s+" + _W + r"(?=$|[\s.,;!?])",
    # «там написано блаватская, а надо Блаватская», «написано X, а правильно Y»
    r"(?:написан\w*|пишется|в субтитрах)\s+" + _W + r"\s*,?\s+а\s+(?:надо|нужно|правильно|должно быть)\s+" + _W + r"(?=$|[\s.,;!?])",
    # «не блаватская, а Блаватская»
    r"\bне\s+" + _W + r"\s*,\s*а\s+" + _W + r"(?=$|[.,;!?])",
]


def _pull_fixes(request: str, e: dict) -> str:
    """Исправления написания в субтитрах без кавычек. Возвращает просьбу без этих кусков."""
    for pat in _FIX_PATTERNS:
        for m in list(re.finditer(pat, request, flags=re.I)):
            a, b = m.group(1).strip(), m.group(2).strip()
            if _n(a) == _n(b) and a == b:
                continue
            if re.search(r"шрифт|цвет|субтитр|титр|картин|фото|музык|трек|жанр|стил|сверху|снизу|подпис|заголов|обложк|плашк|темп|зум|звук|эффект|хе?ш|время|завтра|сегодня|\d", _n(a + " " + b)):
                continue  # «поменяй шрифт на рукописный» — это не исправление слова
            e["fixes"].append({"from": a, "to": b})
            request = request.replace(m.group(0), " ")
    return request


def _sentence_with(words: list[dict], phrase: str) -> str:
    """Фраза из расшифровки (от паузы или точки до паузы или точки), где звучит phrase."""
    from .edit import find_phrase
    at = find_phrase(words, phrase)
    if not at:
        return phrase
    i = next(k for k, w in enumerate(words) if w["start"] >= at[0])
    a = i
    while a > 0 and not re.search(r"[.!?…]$", words[a - 1]["word"]) and words[a]["start"] - words[a - 1]["end"] < 0.6:
        a -= 1
    b = i
    while b + 1 < len(words) and not re.search(r"[.!?…]$", words[b]["word"]) and words[b + 1]["start"] - words[b]["end"] < 0.6:
        b += 1
    return " ".join(w["word"] for w in words[a:b + 1])


def _dedup(e: dict):
    for k in ("clear", "remove_phrases", "restore_phrases", "zoom_words", "cards_off", "unsupported", "unclip",
              "hashtags_add", "hashtags_remove"):
        seen, out = set(), []
        for x in e[k]:
            if x not in seen:
                seen.add(x)
                out.append(x)
        e[k] = out


def _pos(c: str):
    # «перенеси сверху вниз» — важно, куда, а не откуда
    m = re.search(r"\b(?:с|со)\s+(?:верх\w*|сверху|низ\w*|снизу|центр\w*|середин\w*)\s+(?:на\s+|в\s+)?(\w+)", c) or \
        re.search(r"\b(?:сверху|снизу)\s+(?:на\s+|в\s+)?(вниз|низ\w*|наверх|вверх|верх\w*|центр\w*|середин\w*)", c)
    if m:
        c = m.group(1)
    if re.search(r"сверху|наверх|вверх|верх", c):
        return "top"
    if re.search(r"снизу|вниз|низ", c):
        return "bottom"
    if re.search(r"середин|центр|посередин", c):
        return "middle"
    return None


def _color(c: str):
    for k, v in COLOR_WORDS.items():
        if k in c:
            return v
    return None


def _when(c: str) -> str | None:
    """«завтра в 12», «сегодня в 18:30», «в пятницу в 19» → «ГГГГ-ММ-ДД ЧЧ:ММ» по МСК."""
    now = dt.datetime.now(MSK)
    day = None
    if "послезавтра" in c:
        day = now.date() + dt.timedelta(days=2)
    elif "завтра" in c:
        day = now.date() + dt.timedelta(days=1)
    elif "сегодня" in c:
        day = now.date()
    else:
        days = ["понедельн", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресен"]
        for i, d in enumerate(days):
            if re.search(r"\bв?о? ?" + d, c):
                delta = (i - now.weekday()) % 7 or 7
                day = now.date() + dt.timedelta(days=delta)
                break
    m = re.search(r"в (\d{1,2})(?:[:.](\d{2}))?", c)
    if not day and not m:
        return None
    day = day or now.date()
    hh, mm = (int(m.group(1)), int(m.group(2) or 0)) if m else (19, 0)
    if m and hh < 9 and "утр" not in c and "ноч" not in c:
        hh += 12  # «в 7» без уточнения — это вечер
    return f"{day:%Y-%m-%d} {hh:02d}:{mm:02d}"


def apply(job: dict, e: dict) -> dict:
    """Переносит правку в ролик. Возвращает, что делать дальше: render / recaption / cover / publish."""
    o = job.setdefault("opts", {})
    render = False
    recaption = False
    if e.get("genre") in GENRES and e["genre"] != job.get("genre"):
        g = GENRES[e["genre"]]
        job["genre"] = e["genre"]
        for k in g["opts"]:
            o.pop(k, None)  # настройки жанра заменяют прежние
        for k in ("no_cards", "auto_accents", "cta"):
            job.pop(k, None)
        render = recaption = True
    if e.get("minimal"):
        o.update({"cut": "none", "zoom": "off", "title_on": False, "end_card": False, "sounds": [], "effects": [],
                  "zoom_words": []})
        job.update({"no_cards": True, "music": False, "auto_accents": False, "cta": False, "badge": None})
        render = recaption = True
    if e.get("cta") is not None:
        job["cta"] = e["cta"]
        recaption = True
    for k in e.get("clear") or []:
        if k == "removed":
            job["removed"] = []
            render = True
        elif k == "accents":
            job["accents"] = []
            render = True
        elif k == "color":
            o["color"] = []
            render = True
        elif k == "images":
            job["images"] = []
            render = True
        elif k in LIST_OPTS:
            o[k] = []
            render = True
    for k in OPT_KEYS:
        if e.get(k) is not None:
            o[k] = e[k]
            render = True
    # совместимость со старыми роликами
    if e.get("color") is not None:
        job.pop("bright", None)
    if e.get("cut") is not None:
        job.pop("gentle", None)
    for k in ("zoom_words", "cards_off", "smooth", "unclip"):
        if e.get(k):
            o[k] = list(dict.fromkeys((o.get(k) or []) + e[k]))
            render = True
    if e.get("fixes"):
        o["fixes"] = (o.get("fixes") or []) + e["fixes"]
        render = True
    if e.get("sounds"):
        o["sounds"] = (o.get("sounds") or []) + [{"phrase": s["phrase"], "sound": s["sound"]} for s in e["sounds"]
                                                  if s.get("sound") in library.SOUNDS]
        render = True
    if e.get("effects"):
        o["effects"] = (o.get("effects") or []) + [{"phrase": x["phrase"], "effect": x["effect"]} for x in e["effects"]
                                                   if x.get("effect") in library.EFFECTS]
        render = True
    if e.get("remove_phrases"):
        job["removed"] = (job.get("removed") or []) + e["remove_phrases"]
        render = True
    if e.get("restore_phrases"):
        from .edit import same_word
        back = [w for p in e["restore_phrases"] for w in re.findall(r"[\wё-]+", p) if len(w) >= 4]

        def hit(r):
            return any(same_word(x, b) for x in re.findall(r"[\wё-]+", r) for b in back)
        job["removed"] = [r for r in job.get("removed") or [] if not hit(r)]
        render = True
    if e.get("accents"):
        job["accents"] = (job.get("accents") or []) + e["accents"]
        render = True
    if e.get("images"):
        new = [{"phrase": x["phrase"], "query": x.get("query") or x["phrase"]} for x in e["images"] if x.get("phrase")]
        same = {_n(x["phrase"]) for x in new}  # на том же слове новая картинка заменяет прежнюю
        job["images"] = [x for x in job.get("images") or [] if _n(x["phrase"]) not in same] + new
        render = True
    for k in ("style", "music", "no_cards", "auto_accents"):
        if e.get(k) is not None:
            job[k] = e[k]
            render = True
    if e.get("badge") is not None:
        job["badge"] = e["badge"] or None
        render = True
    if e.get("title"):
        job["title"] = job["title_override"] = e["title"]
        render = True
    if e.get("caption"):
        job["caption"] = e["caption"]
    tags = list(job.get("hashtags") or [])
    for t in e.get("hashtags_add") or []:
        t = t if t.startswith("#") else "#" + t
        if t not in tags:
            tags.append(t)
    rm = {_n(t if t.startswith("#") else "#" + t) for t in e.get("hashtags_remove") or []}
    job["hashtags"] = [t for t in tags if _n(t) not in rm]
    cover = False
    if e.get("cover_text"):
        job["cover_text"] = e["cover_text"]
        cover = True
    if e.get("cover_at") is not None:
        job["cover_at"] = max(0.0, float(e["cover_at"]))
        cover = True
    return {"render": render, "recaption": (recaption or bool(e.get("recaption"))) and not e.get("caption"),
            "cover": cover or render, "publish": e.get("publish"), "when": e.get("publish_when")}


def summary(e: dict) -> list[str]:
    """Что поняли, по-человечески: для ответа «Хорошо: …»."""
    s = []
    if e.get("genre") in GENRES:
        s.append(f"жанр: {GENRES[e['genre']]['name']} ({GENRES[e['genre']]['desc']})")
    if e.get("minimal"):
        s.append("только субтитры и цвет, больше ничего не добавляю")
    cut = {"tight": "режу плотнее, ролик станет короче", "gentle": "режу мягче, оставлю паузы, ролик станет длиннее",
           "normal": "нарезка как обычно", "none": "ничего не вырезаю, ролик целиком от начала до конца"}
    if e.get("cut"):
        s.append(cut[e["cut"]])
    for p in e.get("smooth") or []:
        s.append(f"после «{p}» склейка плавнее, паузу оставлю")
    for p in e.get("unclip") or []:
        s.append(f"не буду обрезать «{p}»")
    if e.get("keep_retakes") is True:
        s.append("оставлю повторные дубли")
    if e.get("trim_start"):
        s.append(f"обрежу {e['trim_start']:g} с в начале")
    if e.get("trim_end"):
        s.append(f"обрежу {e['trim_end']:g} с в конце")
    for p in e.get("remove_phrases") or []:
        s.append(f"вырежу «{p}»")
    for p in e.get("restore_phrases") or []:
        s.append(f"верну «{p}»")
    if e.get("speed"):
        s.append("темп как в оригинале" if e["speed"] == 1 else f"темп ×{e['speed']:g}")
    z = {"off": "без зумов", "strong": "зумы сильнее", "normal": "зумы как обычно"}
    if e.get("zoom"):
        s.append(z[e["zoom"]])
    for p in e.get("zoom_words") or []:
        s.append(f"наезд на «{p}»")
    if e.get("mirror") is not None:
        s.append("отражу кадр" if e["mirror"] else "уберу отражение")
    if e.get("color") is not None:
        s.append("цвет: " + (", ".join(LOOK_NAMES.get(c, c) for c in e["color"]) or "как в стиле"))
    if e.get("subs") is False:
        s.append("без субтитров")
    elif e.get("subs") is True:
        s.append("с субтитрами")
    if e.get("sub_size"):
        s.append(f"субтитры {'крупнее' if e['sub_size'] > 1 else 'мельче'}")
    if e.get("sub_pos"):
        s.append(f"субтитры {POS_NAMES[e['sub_pos']]}")
    if e.get("sub_shift") is not None:
        s.append("субтитры выше" if e["sub_shift"] > 0 else "субтитры ниже" if e["sub_shift"] < 0 else "субтитры на место")
    if e.get("sub_case"):
        s.append({"upper": "субтитры заглавными", "lower": "субтитры строчными", "normal": "субтитры обычным регистром"}[e["sub_case"]])
    if e.get("sub_color"):
        s.append(f"цвет субтитров: {COLOR_NAMES.get(e['sub_color'], e['sub_color'])}")
    if e.get("sub_font"):
        s.append(f"шрифт {e['sub_font']}")
    if e.get("sub_words"):
        s.append(f"по {int(e['sub_words'])} сл. на экране")
    for f in e.get("fixes") or []:
        s.append(f"исправлю «{f['from']}» на «{f['to']}»")
    for a in e.get("accents") or []:
        s.append(f"выделю «{a['phrase']}»" + (f" ({COLOR_NAMES.get(a.get('color'), '')})" if a.get("color") else ""))
    if e.get("auto_accents") is False:
        s.append("без выделения слов")
    if e.get("no_cards") is True:
        s.append("без картинок")
    elif e.get("no_cards") is False:
        s.append("верну картинки")
    for p in e.get("cards_off") or []:
        s.append(f"уберу картинку на «{p}»")
    for x in e.get("images") or []:
        s.append(f"найду картинку «{x.get('query') or x['phrase']}» и покажу на «{x['phrase']}»")
    if e.get("card_scale"):
        s.append(f"картинки {'крупнее' if e['card_scale'] > 1 else 'мельче'}")
    if e.get("card_pos"):
        s.append(f"картинки {POS_NAMES[e['card_pos']]}")
    if e.get("card_secs"):
        s.append(f"картинки {'дольше' if e['card_secs'] > 2.2 else 'короче'}")
    if e.get("music") is True:
        s.append("с музыкой")
    elif e.get("music") is False:
        s.append("без музыки")
    if e.get("music_vol"):
        s.append(f"музыка {'громче' if e['music_vol'] > 1 else 'тише'}")
    if e.get("voice_vol"):
        s.append(f"голос {'громче' if e['voice_vol'] > 1 else 'тише'}")
    if e.get("denoise"):
        s.append("уберу шум")
    for x in e.get("sounds") or []:
        if x.get("sound") in library.SOUNDS:
            s.append(f"звук «{library.SOUNDS[x['sound']][1]}» на «{x['phrase']}»")
    for x in e.get("effects") or []:
        if x.get("effect") in library.EFFECTS:
            s.append(f"эффект «{library.EFFECTS[x['effect']][1]}» на «{x['phrase']}»")
    names = {"sounds": "звуки", "effects": "эффекты", "zoom_words": "наезды", "cards_off": "убранные картинки",
             "accents": "выделения", "fixes": "исправления", "removed": "вырезанные фразы", "color": "цвет",
             "images": "найденные картинки"}
    for k in e.get("clear") or []:
        if k == "removed":
            s.append("верну всё вырезанное вручную")
        elif k in names and k != "accents":
            s.append(f"сброшу: {names[k]}")
    if e.get("style"):
        s.append(f"стиль {STYLE_NAMES.get(e['style'], e['style'])}")
    if e.get("title"):
        s.append(f"заголовок «{e['title']}»")
    elif e.get("title_on") is False and not e.get("minimal"):
        s.append("без заголовка в кадре, субтитры будут одни")
    elif e.get("title_on") is True:
        s.append("с заголовком в кадре")
    if e.get("badge") == "":
        s.append("без плашки")
    elif e.get("badge"):
        s.append(f"плашка «{e['badge']}»")
    if e.get("end_card") is not None:
        s.append("с финальной заставкой" if e["end_card"] else "без финальной заставки")
    if e.get("cta") is False:
        s.append("подпись без ссылки на Telegram")
    elif e.get("cta") is True:
        s.append("в подписи будет ссылка на Telegram")
    if e.get("caption"):
        s.append("новая подпись")
    elif e.get("recaption"):
        s.append("перепишу подпись")
    if e.get("hashtags_add"):
        s.append("добавлю " + " ".join(e["hashtags_add"]))
    if e.get("hashtags_remove"):
        s.append("уберу " + " ".join(e["hashtags_remove"]))
    if e.get("cover_text"):
        s.append(f"текст обложки «{e['cover_text']}»")
    if e.get("cover_at") is not None:
        s.append(f"обложка с {e['cover_at']:g} с")
    pub = {"now": "выложу сейчас", "archive": "уберу в архив", "cancel": "сниму с публикации",
           "queue": "поставлю в очередь"}
    if e.get("publish"):
        s.append(pub[e["publish"]] + (f" на {e['publish_when']} МСК" if e.get("publish_when") else ""))
    return s


CATALOG = """Что можно попросить (текстом или голосом, можно несколько правок сразу):

🎭 Жанр: «это рассказ» (как ролики Полины про религии и историю: нарезка, картинки, выделения), «это влог из путешествия» / «зарисовка» (ролик целиком, без вырезаний и картинок, только субтитры), «это скетч» / «комедия» (плотная нарезка, без картинок). «Больше ничего не делай, только субтитры» — ролик целиком, только субтитры и цвет.

✂️ Нарезка: «ты обрезал слово „море“» (склейку у этого слова уберу), «сделай короче / динамичнее», «подлиннее, оставь паузы», «от начала до конца, ничего не вырезай», «после слов „смотрите какой вид“ резкая склейка, сделай плавнее», «вырежи „ну короче“», «верни фразу „про луну“», «оставь дубли», «обрежь 3 секунды в начале», «убери последние 2 секунды»
⏩ Темп: «быстрее», «медленнее», «ускорь в 1.2 раза», «темп как было»
🔍 Кадр: «без зумов», «больше зумов», «наезд на слове „мошенница“», «отрази кадр»
🎨 Цвет: «ярче», «темнее», «светлее», «теплее», «холоднее», «приглуши», «контрастнее», «чёрно-белое», «без цветокоррекции», «цвет как было»
💬 Субтитры: «убери субтитры», «субтитры крупнее / мельче», «субтитры снизу / сверху / по центру», «субтитры выше / чуть ниже», «чтобы субтитры были одни», «заглавными / строчными», «субтитры белым», «шрифт рукописный / с засечками / современный / мягкий / спортивный / элегантный / ретро», «поменяй шрифт», «поменяй цвет субтитров», «по одному слову», «исправь „блаватская“ на „Блаватская“», «выдели „новолуние“ красным», «не выделяй слова»
🖼 Картинки: «без картинок», «убери картинку на слове „луна“», «картинки крупнее / мельче», «картинки снизу / сверху», «показывай картинки дольше», «добавь картинку Эйфелевой башни на слове „Париж“» (найду в фотобанке); свою картинку пришлите фото с подписью «на слово …»
🔊 Звук: «без музыки», «музыка тише / громче», «голос громче», «убери шум», «звук бум на слове „казнили“», «ба-дум-тсс на „шутка“», «убери звуки»; свою музыку пришлите mp3-файлом
✨ Эффекты: «молния на „мошенница“», «искры на слове „огонь“», «дым», «засвет», «глитч», «звёздочки», «свечение», «чёрно-белая вставка на „скучно“», «убери эффекты»
🏷 Текст в кадре: «заголовок: „Руны на неделю“», «без заголовка», «плашка: „14-е лунные сутки“», «убери плашку», «добавь финальную заставку», «стиль как у Полины / светлый / ночной»
📝 Подпись: «перепиши подпись короче / веселее / про путешествия», «подпись: „свой текст“», «без ссылки на Телеграм», «добавь хештег #таро», «убери хештег #луна»
🖼 Обложка: «обложка с 5 секунды», «другой кадр на обложку», «текст на обложке „Кто такая Блаватская“»
📅 Публикация: «в очередь», «выложи сейчас», «выложи завтра в 12», «в пятницу в 19:00», «в архив», «отмени публикацию»

Звуки: """ + ", ".join(v[1] for v in library.SOUNDS.values()) + """.
Эффекты: """ + ", ".join(v[1] for v in library.EFFECTS.values()) + "."


def catalog_for_claude() -> str:
    """Справка для Claude: названия звуков и эффектов, которые можно ставить в sounds/effects."""
    snd = "; ".join(f"{k} ({v[1]})" for k, v in library.SOUNDS.items())
    fx = "; ".join(f"{k} ({v[1]})" for k, v in library.EFFECTS.items())
    return f"Звуки (поле sound): {snd}.\nЭффекты (поле effect): {fx}."
