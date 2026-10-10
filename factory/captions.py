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

def _edit_schema() -> dict:
    """Схема правки для Claude: те же поля, что в edits.EMPTY."""
    from . import edits, library

    def nul(t, enum=None):
        x = {"type": [t, "null"]}
        if enum:
            x["enum"] = enum + [None]
        return x

    def arr(items):
        return {"type": "array", "items": items}

    def obj(props):
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    pos = ["top", "middle", "bottom"]
    from .edit import SUB_FONTS
    props = {
        "genre": nul("string", list(edits.GENRES)), "minimal": nul("boolean"),
        "cut": nul("string", ["tight", "normal", "gentle", "none"]), "smooth": arr({"type": "string"}),
        "keep_retakes": nul("boolean"),
        "trim_start": nul("number"), "trim_end": nul("number"),
        "remove_phrases": arr({"type": "string"}), "restore_phrases": arr({"type": "string"}),
        "speed": nul("number"), "zoom": nul("string", ["off", "normal", "strong"]),
        "zoom_words": arr({"type": "string"}), "mirror": nul("boolean"),
        "color": {"type": ["array", "null"], "items": {"type": "string", "enum": list(edits.LOOK_NAMES)}},
        "subs": nul("boolean"), "sub_size": nul("number"), "sub_pos": nul("string", pos), "sub_shift": nul("integer"),
        "sub_case": nul("string", ["upper", "lower", "normal"]), "sub_color": nul("string", list(edits.COLOR_NAMES)),
        "sub_font": nul("string", SUB_FONTS), "sub_words": nul("integer"),
        "fixes": arr(obj({"from": {"type": "string"}, "to": {"type": "string"}})),
        "accents": arr(obj({"phrase": {"type": "string"},
                            "color": nul("string", ["red", "gold", "indigo", "green", "orange", "paper", "night"]),
                            "font": nul("string", ["Oranienbaum", "Jost"])})),
        "auto_accents": nul("boolean"),
        "no_cards": nul("boolean"), "card_scale": nul("number"), "card_pos": nul("string", pos),
        "card_secs": nul("number"), "cards_off": arr({"type": "string"}),
        "music": nul("boolean"), "music_vol": nul("number"), "voice_vol": nul("number"), "denoise": nul("boolean"),
        "sounds": arr(obj({"phrase": {"type": "string"}, "sound": {"type": "string", "enum": list(library.SOUNDS)}})),
        "effects": arr(obj({"phrase": {"type": "string"}, "effect": {"type": "string", "enum": list(library.EFFECTS)}})),
        "clear": arr({"type": "string", "enum": ["sounds", "effects", "zoom_words", "accents", "cards_off", "fixes",
                                                 "removed", "color"]}),
        "style": nul("string", ["polina", "night", "paper"]), "title": nul("string"), "title_on": nul("boolean"),
        "badge": nul("string"), "end_card": nul("boolean"),
        "caption": nul("string"), "recaption": nul("boolean"), "cta": nul("boolean"),
        "hashtags_add": arr({"type": "string"}), "hashtags_remove": arr({"type": "string"}),
        "cover_text": nul("string"), "cover_at": nul("number"),
        "publish": nul("string", ["queue", "now", "archive", "cancel"]), "publish_when": nul("string"),
        "unsupported": arr({"type": "string"}), "note": {"type": "string"},
    }
    assert set(props) == set(edits.EMPTY), set(props) ^ set(edits.EMPTY)
    return obj(props)


def _client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        return anthropic.Anthropic()
    except Exception:
        return None


def _ask(system: str, user: str, schema: dict) -> dict | None:
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
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
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
Поля, которые правка не затрагивает: null, списки пустые. Числа пиши итоговые, с учётом текущих настроек ролика
(«ещё крупнее» при sub_size 1.2 → 1.44).

Жанр. genre: talk — рассказ Полины (религии, история, эзотерика: нарезка, картинки, выделения), vlog — влог,
путешествие, зарисовка, повседневность (ролик целиком, без вырезаний и картинок, только субтитры), sketch —
комедийный скетч (плотная нарезка, без картинок). Ставь, если из правки понятно, что это за ролик.
minimal: true, если просят «больше ничего не делай», «только субтитры» — ролик целиком, только субтитры и цвет.
Нарезка. cut: tight («короче», «динамичнее», «убери все паузы»), gentle («подлиннее», «оставь паузы»,
«не режь так сильно»), normal (как обычно), none («от начала до конца», «ничего не вырезай», «целиком»).
smooth: фразы из расшифровки, после которых склейка резкая и паузу нужно оставить («после слов … перемонтируй»). keep_retakes: true — оставить повторные дубли.
trim_start / trim_end: сколько секунд срезать в начале / в конце готового ролика («начни с 3 секунды» → 3).
remove_phrases: точные куски речи из расшифровки, которые вырезать («вырежи, где я про погоду» → вся эта фраза
из расшифровки дословно). restore_phrases: вернуть вырезанное раньше.
Темп. speed: множитель (1.1 — на 10 % быстрее, 0.9 — медленнее, 1 — как в оригинале).
Кадр. zoom: off (без зумов), normal, strong (больше зумов). zoom_words: фразы из расшифровки, на которых сделать
наезд/приближение. mirror: отразить кадр.
Цвет. color — полный новый список поверх цвета стиля: bright (ярче, сочнее, подкрасить), light (светлее),
dark (темнее), warm (теплее), cold (холоднее), muted (приглушённее), contrast (контрастнее), bw (весь ролик
чёрно-белым), natural (без цветокоррекции, как снято). «Цвет как было» → []. Учитывай текущий список.
Субтитры. subs: false — убрать, true — вернуть или наложить. sub_size: множитель размера. sub_pos: top/middle/bottom.
sub_shift: сдвиг в пикселях от текущего места, итоговый (выше +150, чуть выше +70, ниже −150).
«Чтобы субтитры были одни» — значит второй строкой мешает заголовок: title_on false.
sub_case: upper (заглавными), lower (строчными), normal. sub_color: цвет текста. sub_font: Jost (простой, как
у Полины), Oranienbaum (старинный с засечками), Montserrat (современный жирный), Rubik (округлый), Comfortaa (мягкий),
Russo One (спортивный), Unbounded (широкий модный), Playfair Display (элегантный журнальный), Caveat (рукописный),
Marck Script (каллиграфия), Lobster (ретро), Pacifico (летний), Amatic SC (тонкий рукописный). sub_words: сколько слов на экране сразу (1–4).
fixes: исправить написание слова в субтитрах (from — как в расшифровке, to — как надо).
accents: выделить слова в субтитрах цветом/шрифтом; auto_accents: false — не выделять автоматически.
Картинки. no_cards: true — без картинок, false — вернуть. cards_off: слова, на которых убрать картинку.
card_scale: множитель размера. card_pos: где показывать. card_secs: сколько секунд показывать (обычно 2.2).
Звук. music: с музыкой или без. music_vol / voice_vol: множитель громкости. denoise: убрать шум.
sounds: звуковой эффект на фразе из расшифровки. effects: видеоэффект на фразе (bw — чёрно-белая вставка
только на этом месте). clear: что сбросить целиком («убери все звуки» → sounds).
Текст в кадре. style: polina (как у Полины), night (тёмный), paper (светлый). title: новый заголовок в начале
ролика, title_on: показывать ли заголовок. badge: текст плашки ("" — убрать плашку). end_card: финальная заставка.
Подпись к посту. Если просят изменить подпись, сразу напиши новую в caption (по теме ролика, её пожеланию и
правилам ниже) и полный список хештегов в её конце не ставь — хештеги отдельно: hashtags_add / hashtags_remove.
recaption: true только если подпись нужно переписать, а ты её не написал. cta: false — без призыва и ссылки на
Telegram, true — с ним.
Обложка. cover_text: текст на обложке; cover_at: с какой секунды готового ролика взять кадр.
Публикация. publish: queue (в очередь на ближайший вечер), now (выложить сейчас), archive, cancel (снять с очереди).
publish_when: «ГГГГ-ММ-ДД ЧЧ:ММ» по Москве, если назвали время.
unsupported: по-русски коротко то, чего этими полями сделать нельзя (переставить куски, переходы, стоп-кадр,
замедленный повтор, новая картинка без файла — попроси прислать фото с подписью «на слово …», другая музыка —
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
        e = edits.new_edit()
        e.update(res)
        return e
    return edits.parse_simple(request, words, current)


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
