"""Заголовок, подпись и хештеги к ролику, а также разбор правок Полины.

Работает через Claude API (ключ ANTHROPIC_API_KEY). Если ключа нет или API недоступен,
используется простой шаблон, чтобы фабрика не останавливалась.
"""
import datetime as dt
import json
import os
import re

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Если основная модель откажется отвечать, Anthropic сам повторит запрос на запасной модели.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

STYLE_GUIDE = """Ты пишешь подписи к Reels для Полины (@icon___witch). Темы у неё разные: лунные сутки, руны, расклады,
мировые религии, история оккультизма, просветительские и развлекательные ролики (Полина заканчивает РГГУ
по направлению мистики и эзотерики). Пиши строго про тему этого ролика: про лунные сутки только если ролик о них.
Голос: тёплый, уверенный, по-дружески на «вы», без пафоса и без запугивания.
Короткие абзацы, 1–2 уместных эмодзи на всю подпись, никаких обещаний «гарантированно».
Время всегда указывай по МСК, если оно есть в речи.
Заголовок на обложку: 2–6 слов, интрига или польза, без кавычек и точки в конце.
Подпись: первая строка цепляет (видна до «ещё»), затем 2–4 коротких абзаца по смыслу ролика,
в конце призыв: сохранить, написать слово в комментарии или перейти в Telegram по ссылке в профиле.
Хештеги: 5–8 штук, русские и по теме именно этого ролика, без мусорных #love #instagood."""

_CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "rubric": {"type": "string"},
    },
    "required": ["title", "caption", "hashtags", "rubric"],
    "additionalProperties": False,
}

# Строгая схема API допускает не больше 16 полей с типом «X или null» и не больше 24 необязательных полей.
# Поэтому в схеме все поля обязательные и без null: «не менять» — пустая строка "" (или пустой список),
# да/нет — строки "true"/"false", числа — строкой. _from_schema переводит ответ обратно в edits.EMPTY.
def _edit_spec() -> dict:
    """Поле → (вид, варианты): str, num, int, bool или list (тогда схема элемента)."""
    from . import edits, library
    from .edit import SUB_FONTS
    pos = ["top", "middle", "bottom"]
    s = {"type": "string"}

    def obj(props):
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    def en(vals):
        return {"type": "string", "enum": [""] + vals}

    spec = {
        "genre": ("str", list(edits.GENRES)), "minimal": ("bool", None),
        "cut": ("str", ["tight", "normal", "gentle", "none"]), "smooth": ("list", s), "unclip": ("list", s),
        "keep_retakes": ("bool", None), "trim_start": ("num", None), "trim_end": ("num", None),
        "remove_phrases": ("list", s), "restore_phrases": ("list", s),
        "speed": ("num", None), "zoom": ("str", ["off", "normal", "strong"]), "zoom_words": ("list", s),
        "mirror": ("bool", None),
        "color": ("list", {"type": "string", "enum": list(edits.LOOK_NAMES)}),
        "subs": ("bool", None), "sub_size": ("num", None), "sub_pos": ("str", pos), "sub_shift": ("int", None),
        "sub_case": ("str", ["upper", "lower", "normal"]), "sub_color": ("str", list(edits.COLOR_NAMES)),
        "sub_font": ("str", list(SUB_FONTS)), "sub_words": ("int", None),
        "fixes": ("list", obj({"from": s, "to": s})),
        "accents": ("list", obj({"phrase": s, "color": en(["red", "gold", "indigo", "green", "orange", "paper", "night"]),
                                 "font": en(["Oranienbaum", "Jost"])})),
        "auto_accents": ("bool", None),
        "no_cards": ("bool", None), "card_scale": ("num", None), "card_pos": ("str", pos), "card_secs": ("num", None),
        "cards_off": ("list", s), "images": ("list", obj({"phrase": s, "query": s})),
        "music": ("bool", None), "music_vol": ("num", None), "voice_vol": ("num", None), "denoise": ("bool", None),
        "sounds": ("list", obj({"phrase": s, "sound": {"type": "string", "enum": list(library.SOUNDS)}})),
        "effects": ("list", obj({"phrase": s, "effect": {"type": "string", "enum": list(library.EFFECTS)}})),
        "clear": ("list", {"type": "string", "enum": ["sounds", "effects", "zoom_words", "accents", "cards_off", "fixes",
                                                      "removed", "color", "images"]}),
        "style": ("str", ["polina", "night", "paper"]), "title": ("str", None), "title_on": ("bool", None),
        "badge": ("str", None), "end_card": ("bool", None),
        "caption": ("str", None), "recaption": ("bool", None), "cta": ("bool", None),
        "hashtags_add": ("list", s), "hashtags_remove": ("list", s),
        "cover_text": ("str", None), "cover_at": ("num", None),
        "publish": ("str", ["queue", "now", "archive", "cancel"]), "publish_when": ("str", None),
        "unsupported": ("list", s), "note": ("str", None),
    }
    assert set(spec) == set(edits.EMPTY), set(spec) ^ set(edits.EMPTY)
    return spec


def _edit_schema() -> dict:
    """Схема правки для Claude: те же поля, что в edits.EMPTY, без null и без необязательных полей."""
    props = {}
    for k, (kind, arg) in _edit_spec().items():
        if kind == "list":
            props[k] = {"type": "array", "items": arg}
        elif kind == "bool":
            props[k] = {"type": "string", "enum": ["", "true", "false"]}
        elif kind == "str" and arg:
            props[k] = {"type": "string", "enum": [""] + arg}
        else:
            props[k] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _from_schema(res: dict) -> dict:
    """Ответ Claude по схеме → правка edits.EMPTY ("" → None, "true" → True, "1.2" → 1.2)."""
    from . import edits
    e = edits.new_edit()
    for k, (kind, _) in _edit_spec().items():
        v = res.get(k)
        if kind == "list":
            v = list(v or [])
            if k == "accents":
                v = [dict(a, color=a.get("color") or None, font=a.get("font") or None) for a in v]
            if k == "color" and not v:
                v = None  # пустой список — цвет не трогаем; сбросить цвет — clear: ["color"]
            e[k] = v
            continue
        v = (v or "").strip() if isinstance(v, str) else v
        if v in ("", None):
            e[k] = "" if k == "note" else None
            continue
        if kind == "bool":
            e[k] = str(v).lower() in ("true", "1", "да", "yes")
        elif kind in ("num", "int"):
            try:
                x = float(str(v).replace(",", ".").replace("−", "-").replace("+", ""))
                e[k] = int(round(x)) if kind == "int" else x
            except ValueError:
                e[k] = None
        else:
            e[k] = v
    if e["badge"] == "-":  # «убери плашку»
        e["badge"] = ""
    return e


def _client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception:
        return None


LAST_ERROR = ""  # почему Claude не ответил в последний раз (бот показывает это при упрощённом разборе)


def _ask(system: str, user: str, schema: dict) -> dict | None:
    global LAST_ERROR
    LAST_ERROR = ""
    client = _client()
    if client is None:
        return None
    import anthropic
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as e:
        print("Claude API error:", e)
        LAST_ERROR = str(getattr(e, "message", "") or e)[:300]
        return None
    if resp.stop_reason == "refusal":
        LAST_ERROR = "Claude отказался отвечать"
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        LAST_ERROR = f"ответ оборвался ({resp.stop_reason})"
        return None


def transcript_text(words: list[dict]) -> str:
    return " ".join(w["word"] for w in words)


# Хештеги для подписи без Claude: тема ролика по словам из расшифровки (порядок = приоритет).
_TOPIC_TAGS = [
    (("путешеств", "поездк", "дорог", "зарисовк"), ["#путешествия", "#зарисовка", "#ведьмавпути"]),
    (("лунн", "луна", "луны", "новолун", "полнолун"), ["#лунныесутки", "#луна", "#лунныйкалендарь"]),
    (("рун",), ["#руны", "#рунология", "#расклад"]),
    (("таро", "аркан"), ["#таро", "#картытаро", "#расклад"]),
    (("ведьм", "инквизи", "колдов"), ["#ведьмы", "#история", "#магия"]),
    (("бог", "религи", "церк", "христиан", "ислам", "будд", "язычес"), ["#религия", "#религиоведение", "#история"]),
    (("оккульт", "теософ", "мистик", "общество"), ["#оккультизм", "#мистика", "#история"]),
]


def _topic_tags(text: str, hint: str = "", genre: str | None = None) -> list[str]:
    """Хештеги по словам ролика и пожеланию. Темы из частей пожелания с «убери», «не про» и т. п. не ставим вовсе."""
    norm = lambda x: x.lower().replace("ё", "е")
    neg = ("убер", "убр", "не из", "не про", "без ", "не надо", "не ну")
    clauses = re.split(r"[.,;!?]|\bно\b|\bа\b", norm(hint))
    banned = {i for i, (keys, _) in enumerate(_TOPIC_TAGS)
              for c in clauses if any(x in c for x in neg) and any(k in c for k in keys)}
    t = norm(text) + " " + " ".join(c for c in clauses if not any(x in c for x in neg))
    tags = []
    for i, (keys, add) in enumerate(_TOPIC_TAGS):
        if i not in banned and any(k in t for k in keys):
            tags += [h for h in add if h not in tags]
    base = {"vlog": ["#влог", "#icon_witch"], "sketch": ["#юмор", "#icon_witch"]}.get(genre, ["#эзотерика", "#icon_witch"])
    tags += [h for h in base if h not in tags]
    return tags[:8]


def _template(text: str, badge: str | None, hint: str | None = None, cta: bool = True, genre: str | None = None) -> dict:
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x]
    first = sentences[0] if sentences else ""
    title_words = re.sub(r"[^\w\s-]", "", first).split()[:5]
    title = " ".join(title_words).capitalize() or "Новое видео"
    # одна-две случайные фразы из влога — не подпись; тогда подпись короткая
    lead = first if len(first.split()) >= 4 else ""
    if badge:
        lead = f"{badge}. {lead}".strip()
    parts = [lead] if lead else []
    if cta:
        parts.append("Сохраните, чтобы не потерять ✨\nБольше в Telegram, ссылка в профиле.")
    caption = "\n\n".join(parts) or "✨"
    return {
        "title": title,
        "caption": caption,
        "hashtags": _topic_tags(text + " " + (badge or ""), hint or "", genre),
        "rubric": "",
        "source": "template",
    }


GENRE_HINTS = {
    "talk": "Жанр: рассказ Полины (просвещение, религии, история, эзотерика).",
    "vlog": "Жанр: влог, зарисовка из путешествия или повседневной жизни. Пиши легко и коротко, как личный пост, "
            "без эзотерики, если её нет в ролике.",
    "sketch": "Жанр: короткий комедийный скетч. Подпись короткая и смешная, без серьёзных пояснений.",
}


def make_caption(words: list[dict], badge: str | None = None, hint: str | None = None, cta: bool = True,
                 genre: str | None = None) -> dict:
    """{"title", "caption", "hashtags", "rubric", "source"} по расшифровке ролика."""
    text = transcript_text(words)
    user = f"Расшифровка ролика:\n{text}\n"
    if genre in GENRE_HINTS:
        user += f"\n{GENRE_HINTS[genre]}\n"
    if not cta:
        user += "\nБез призыва перейти в Telegram и без ссылок.\n"
    if badge:
        user += f"\nПлашка ролика: {badge}\n"
    if hint:
        user += f"\nПожелание Полины: {hint}\n"
    user += "\nПридумай заголовок на обложку, подпись, хештеги и рубрику (одним-двумя словами)."
    res = _ask(STYLE_GUIDE, user, _CAPTION_SCHEMA)
    if not res:
        return _template(text, badge, hint, cta, genre)
    res["hashtags"] = [h if h.startswith("#") else "#" + h for h in res["hashtags"]]
    res["source"] = "claude"
    return res


def full_caption(c: dict) -> str:
    return c["caption"].strip() + "\n\n" + " ".join(c["hashtags"])


_EDIT_SYSTEM = """Полина или Александр присылают правку к смонтированному ролику своими словами (часто голосом,
с ошибками распознавания). Разбери её в структуру; в одном сообщении может быть несколько правок, выполни все.
Поля, которые правка не затрагивает: пустая строка "", списки пустые. Да/нет пиши строкой "true"/"false",
числа — строкой с точкой ("1.44", "-150"). Числа пиши итоговые, с учётом текущих настроек ролика
(«ещё крупнее» при sub_size 1.2 → "1.44"). Если поле не изменилось, оставь его пустым, даже если знаешь значение.

Жанр. genre: talk — рассказ Полины (религии, история, эзотерика: нарезка, картинки, выделения), vlog — влог,
путешествие, зарисовка, повседневность (ролик целиком, без вырезаний и картинок, только субтитры), sketch —
комедийный скетч (плотная нарезка, без картинок). Ставь, если из правки понятно, что это за ролик.
minimal: true, если просят «больше ничего не делай», «только субтитры» — ролик целиком, только субтитры и цвет.
Нарезка. cut: tight («короче», «динамичнее», «убери все паузы»), gentle («подлиннее», «оставь паузы»,
«не режь так сильно»), normal (как обычно), none («от начала до конца», «ничего не вырезай», «целиком»).
smooth: фразы из расшифровки, после которых склейка резкая и паузу нужно оставить («после слов … перемонтируй»).
unclip: слова из расшифровки (дословно, 1–3 слова), которые обрезаны или «съедены» склейкой: «ты обрезал слово
море», «оборвалось на „привет“», «съел начало фразы „сегодня мы“». Вокруг них склейку не делаем и оставляем запас. keep_retakes: true — оставить повторные дубли.
trim_start / trim_end: сколько секунд срезать в начале / в конце готового ролика («начни с 3 секунды» → 3).
remove_phrases: точные куски речи из расшифровки, которые вырезать («вырежи, где я про погоду» → вся эта фраза
из расшифровки дословно). restore_phrases: вернуть вырезанное раньше.
Темп. speed: множитель (1.1 — на 10 % быстрее, 0.9 — медленнее, 1 — как в оригинале).
Кадр. zoom: off (без зумов), normal, strong (больше зумов). zoom_words: фразы из расшифровки, на которых сделать
наезд/приближение. mirror: отразить кадр.
Цвет. color — полный новый список поверх цвета стиля: bright (ярче, сочнее, подкрасить), light (светлее),
dark (темнее), warm (теплее), cold (холоднее), muted (приглушённее), contrast (контрастнее), bw (весь ролик
чёрно-белым), natural (без цветокоррекции, как снято). Пустой список — цвет не меняется; «цвет как было» → clear: ["color"]. Учитывай текущий список.
Субтитры. subs: false — убрать, true — вернуть или наложить. sub_size: множитель размера. sub_pos: top/middle/bottom
(«перенеси сверху вниз» → bottom: важно, куда переносят, а не откуда).
sub_shift: сдвиг в пикселях от текущего места, итоговый (выше +150, чуть выше +70, ниже −150).
«Чтобы субтитры были одни» — значит второй строкой мешает заголовок: title_on false.
sub_case: upper (заглавными), lower (строчными), normal. sub_color: цвет текста; если цвет не назван
(«поменяй цвет субтитров»), выбери сам заметно другой, хорошо читаемый, не текущий. sub_font: Jost (простой, как
у Полины), Oranienbaum (старинный с засечками), Montserrat (современный жирный), Rubik (округлый), Comfortaa (мягкий),
Russo One (спортивный), Unbounded (широкий модный), Playfair Display (элегантный журнальный), Caveat (рукописный),
Marck Script (каллиграфия), Lobster (ретро), Pacifico (летний), Amatic SC (тонкий рукописный).
Если шрифт не назван («поменяй шрифт»), выбери сам подходящий по теме ролика и не текущий. sub_words: сколько слов на экране сразу (1–4).
fixes: исправить написание слова в субтитрах (from — как в расшифровке, to — как надо).
accents: выделить слова в субтитрах цветом/шрифтом; auto_accents: false — не выделять автоматически.
Картинки. no_cards: true — без картинок, false — вернуть. cards_off: слова, на которых убрать картинку.
images: новая картинка из интернета: phrase — слово из расшифровки, на котором показать (дословно), query —
что искать в фотобанке Wikimedia Commons, коротко по-английски («black cat», «Helena Blavatsky portrait»,
«Eiffel Tower»). «Добавь картинку кота на слове кот» → images. Если место не назвали — выбери сам подходящее
слово из расшифровки. Мемы и персонажей из мультфильмов там не найти: тогда попроси прислать фото (unsupported).
card_scale: множитель размера. card_pos: где показывать. card_secs: сколько секунд показывать (обычно 2.2).
Звук. music: с музыкой или без. music_vol / voice_vol: множитель громкости. denoise: убрать шум.
sounds: звуковой эффект на фразе из расшифровки. effects: видеоэффект на фразе (bw — чёрно-белая вставка
только на этом месте). clear: что сбросить целиком («убери все звуки» → sounds).
Текст в кадре. style: polina (как у Полины), night (тёмный), paper (светлый). title: новый заголовок в начале
ролика, title_on: показывать ли заголовок. badge: текст плашки ("-" — убрать плашку). end_card: финальная заставка.
Подпись к посту. Если просят изменить подпись, сразу напиши новую в caption (по теме ролика, её пожеланию и
правилам ниже) и полный список хештегов в её конце не ставь — хештеги отдельно: hashtags_add / hashtags_remove.
recaption: true только если подпись нужно переписать, а ты её не написал. cta: false — без призыва и ссылки на
Telegram, true — с ним.
Обложка. cover_text: текст на обложке; cover_at: с какой секунды готового ролика взять кадр.
Публикация. publish: queue (в очередь на ближайший вечер), now (выложить сейчас), archive, cancel (снять с очереди).
publish_when: «ГГГГ-ММ-ДД ЧЧ:ММ» по Москве, если назвали время.
unsupported: по-русски коротко то, чего этими полями сделать нельзя (переставить куски, переходы, стоп-кадр,
замедленный повтор, мем или картинка, которой нет в фотобанке — попроси прислать фото с подписью «на слово …», другая музыка —
попроси прислать mp3).
note: одна короткая фраза по-русски, что будет сделано."""


def parse_edit(request: str, words: list[dict], current: dict) -> dict:
    """Правка в виде словаря edits.EMPTY: через Claude, а без ключа — простым разбором."""
    from . import edits
    now = dt.datetime.now(edits.MSK)
    user = (
        f"Сейчас {now:%Y-%m-%d %H:%M}, {['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'][now.weekday()]}.\n"
        f"Расшифровка:\n{transcript_text(words)}\n\nСтиль: {current.get('style')}. "
        f"Текущие настройки монтажа: {json.dumps(current.get('opts') or {}, ensure_ascii=False)}\n"
        f"Вырезано вручную: {current.get('removed') or []}\n"
        f"Сейчас заголовок: {current.get('title')}\nСейчас подпись:\n{current.get('caption')}\n"
        f"Хештеги: {' '.join(current.get('hashtags') or [])}\n\nПравка: {request}"
    )
    res = _ask(_EDIT_SYSTEM + "\n\n" + edits.catalog_for_claude() + "\n\n" + STYLE_GUIDE, user, _edit_schema())
    if res:
        return _from_schema(res)
    e = edits.parse_simple(request, words, current)
    if _client() is not None:
        e["fallback"] = LAST_ERROR or "нет ответа"
    return e


def remove_phrases(words: list[dict], phrases: list[str]) -> list[dict]:
    """Убирает из списка слов фрагменты речи (монтаж потом вырежет эти места)."""
    def norm(s):
        return re.sub(r"[^\w]", "", s.lower().replace("ё", "е"))
    out = list(words)
    for ph in phrases:
        target = [norm(t) for t in ph.split() if norm(t)]
        if not target:
            continue
        keys = [norm(w["word"]) for w in out]
        for i in range(len(keys) - len(target) + 1):
            if keys[i:i + len(target)] == target:
                out = out[:i] + out[i + len(target):]
                break
    return out
