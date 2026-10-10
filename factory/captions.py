"""Заголовок, подпись и хештеги к ролику, а также разбор правок Полины.

Работает через Claude API (ключ ANTHROPIC_API_KEY). Если ключа нет или API недоступен,
используется простой шаблон, чтобы фабрика не останавливалась.
"""
import json
import os
import re

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Если основная модель откажется отвечать, Anthropic сам повторит запрос на запасной модели.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

STYLE_GUIDE = """Ты пишешь подписи к Reels для Полины (@icon___witch): лунные сутки, руны, расклады,
мировые религии и оккультные сообщества (Полина заканчивает РГГУ по направлению мистики и эзотерики).
Голос: тёплый, уверенный, по-дружески на «вы», без пафоса и без запугивания.
Короткие абзацы, 1–2 уместных эмодзи на всю подпись, никаких обещаний «гарантированно».
Время всегда указывай по МСК, если оно есть в речи.
Заголовок на обложку: 2–6 слов, интрига или польза, без кавычек и точки в конце.
Подпись: первая строка цепляет (видна до «ещё»), затем 2–4 коротких абзаца по смыслу ролика,
в конце призыв: сохранить, написать слово в комментарии или перейти в Telegram по ссылке в профиле.
Хештеги: 5–8 штук, русские и тематические, без мусорных #love #instagood."""

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

_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "caption": {"type": ["string", "null"]},
        "style": {"type": ["string", "null"], "enum": ["polina", "night", "paper", None]},
        "badge": {"type": ["string", "null"]},
        "music": {"type": ["boolean", "null"]},
        "remove_phrases": {"type": "array", "items": {"type": "string"}},
        "no_cards": {"type": ["boolean", "null"]},
        "accents": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "phrase": {"type": "string"},
                "color": {"type": ["string", "null"],
                          "enum": ["red", "gold", "indigo", "green", "orange", "paper", "night", None]},
                "font": {"type": ["string", "null"], "enum": ["Oranienbaum", "Jost", None]},
            },
            "required": ["phrase", "color", "font"],
            "additionalProperties": False,
        }},
        "auto_accents": {"type": ["boolean", "null"]},
        "note": {"type": "string"},
    },
    "required": ["title", "caption", "style", "badge", "music", "remove_phrases", "no_cards",
                 "accents", "auto_accents", "note"],
    "additionalProperties": False,
}


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


def _template(text: str, badge: str | None) -> dict:
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0] if text.strip() else "Новое видео"
    title_words = re.sub(r"[^\w\s-]", "", first).split()[:5]
    title = " ".join(title_words).capitalize() or "Лунный совет"
    lead = f"{badge}. " if badge else ""
    caption = (
        f"{lead}{first}\n\n"
        "Сохраните, чтобы не потерять 🌙\n"
        "Лунные сутки каждое утро в Telegram, ссылка в профиле."
    )
    return {
        "title": title,
        "caption": caption,
        "hashtags": ["#лунныесутки", "#луна", "#руны", "#эзотерика", "#лунныйкалендарь"],
        "rubric": "",
        "source": "template",
    }


def make_caption(words: list[dict], badge: str | None = None, hint: str | None = None) -> dict:
    """{"title", "caption", "hashtags", "rubric", "source"} по расшифровке ролика."""
    text = transcript_text(words)
    user = f"Расшифровка ролика:\n{text}\n"
    if badge:
        user += f"\nПлашка ролика: {badge}\n"
    if hint:
        user += f"\nПожелание Полины: {hint}\n"
    user += "\nПридумай заголовок на обложку, подпись, хештеги и рубрику (одним-двумя словами)."
    res = _ask(STYLE_GUIDE, user, _CAPTION_SCHEMA)
    if not res:
        return _template(text, badge)
    res["hashtags"] = [h if h.startswith("#") else "#" + h for h in res["hashtags"]]
    res["source"] = "claude"
    return res


def full_caption(c: dict) -> str:
    return c["caption"].strip() + "\n\n" + " ".join(c["hashtags"])


_EDIT_SYSTEM = """Полина присылает правку к смонтированному ролику своими словами (часто голосом).
Разбери её в структуру. Поля, которые правка не затрагивает, ставь null (remove_phrases пустой список).
style: "polina" (как у Полины: розовые строчные субтитры сверху), "night" (тёмный, золотые субтитры) или "paper" (светлый, красные акценты).
remove_phrases: точные фрагменты речи из расшифровки, которые нужно вырезать.
accents: слова или фразы из расшифровки, которые нужно выделить в субтитрах. Цвета: red (красный),
gold (золотой), indigo (синий/фиолетовый), green (зелёный), orange (оранжевый), paper (светлый), night (тёмный).
Шрифты: Oranienbaum (красивый, с засечками, как в заголовках), Jost (простой, рубленый). Не указано → null.
auto_accents: false, если просит не выделять слова автоматически; true, если вернуть; иначе null.
Если просит изменить подпись или заголовок, сразу напиши новый текст в её стиле.
note: одна короткая фраза, что будет сделано."""


def parse_edit(request: str, words: list[dict], current: dict) -> dict:
    user = (
        f"Расшифровка:\n{transcript_text(words)}\n\nСейчас заголовок: {current.get('title')}\n"
        f"Сейчас подпись:\n{current.get('caption')}\n\nПравка Полины: {request}"
    )
    res = _ask(_EDIT_SYSTEM + "\n\n" + STYLE_GUIDE, user, _EDIT_SCHEMA)
    if res:
        return res
    return _parse_edit_simple(request)


def _parse_edit_simple(request: str) -> dict:
    """Запасной разбор без Claude: понимает самые частые просьбы."""
    r = request.lower()
    res = {"title": None, "caption": None, "style": None, "badge": None, "music": None,
           "remove_phrases": [], "no_cards": None, "accents": [], "auto_accents": None, "note": ""}
    m = re.search(r"заголов\w*\s*[:—-]?\s*[«\"](.+?)[»\"]", request, re.I)
    if m:
        res["title"] = m.group(1)
    m = re.search(r"подпис\w*\s*[:—-]?\s*[«\"](.+?)[»\"]", request, re.I | re.S)
    if m:
        res["caption"] = m.group(1)
    if "полин" in r:
        res["style"] = "polina"
    elif "светл" in r or "бумаг" in r:
        res["style"] = "paper"
    elif "тёмн" in r or "темн" in r or "ночн" in r:
        res["style"] = "night"
    if "без музык" in r or "убери музык" in r:
        res["music"] = False
    elif "музык" in r:
        res["music"] = True
    if "без картин" in r or "убери картин" in r:
        res["no_cards"] = True
    for m in re.finditer(r"(?:вырежи|убери|удали)\s*[«\"](.+?)[»\"]", request, re.I):
        res["remove_phrases"].append(m.group(1))
    colors = {"красн": "red", "золот": "gold", "син": "indigo", "фиолет": "indigo", "зелен": "green",
              "зелён": "green", "оранж": "orange", "бел": "paper", "светл": "paper"}
    for m in re.finditer(r"выдел\w*\s*[«\"](.+?)[»\"]([^«\",.;]*)", request, re.I):
        tail = m.group(2).lower()
        color = next((v for k, v in colors.items() if k in tail), None)
        font = "Oranienbaum" if ("засеч" in tail or "красив" in tail) else "Jost" if "прост" in tail else None
        res["accents"].append({"phrase": m.group(1), "color": color, "font": font})
    if "не выделяй" in r or "без выделен" in r:
        res["auto_accents"] = False
    res["note"] = "Применяю правку" if any(v for k, v in res.items() if k != "note") else ""
    return res


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
