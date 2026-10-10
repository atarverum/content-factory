"""Автомонтаж: из сырого видео и слов с таймингами делает готовый Reels 1080x1920.

Шаги: вырезка пауз, оговорок и повторных дублей → чередование крупности (скрывает склейки)
→ цвет → карточки-картинки по ключевым словам → субтитры по словам → плашка в конце
→ музыка с приглушением под голос → громкость под Reels.
"""
import json
import os
import re
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .styles import ACCENT_AUTO, ACCENT_COLORS, END_CARD, NIGHT, PAPER, GOLD, STYLES, ass_color

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "assets", "fonts")
IMAGES = os.path.join(HERE, "assets", "images")

W, H, FPS = 1080, 1920, 30
MAX_GAP = 0.40       # паузы длиннее вырезаем
PAD_BEFORE = 0.12
PAD_AFTER = 0.22
RETAKE_GAP = 0.9     # пауза, после которой ищем повторный дубль
CARD_SECONDS = 2.2
END_SECONDS = 2.4
FILLERS = {"э", "ээ", "эээ", "эм", "эмм", "мм", "ммм", "хм", "а-а", "э-э"}


def _norm(word: str) -> str:
    return re.sub(r"[^\wё-]", "", word.lower())


def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


# ---------- 1. что оставить ----------

def _phrases(words):
    """Делим речь на фразы по паузам."""
    out, cur = [], []
    for w in words:
        if cur and w["start"] - cur[-1]["end"] > RETAKE_GAP:
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _drop_retakes(phrases):
    """Если фраза начинается теми же двумя словами, что и предыдущая, значит предыдущую Полина переговорила."""
    kept = []
    for ph in phrases:
        if kept:
            prev = [_norm(w["word"]) for w in kept[-1][:2]]
            cur = [_norm(w["word"]) for w in ph[:2]]
            if len(prev) == 2 and prev == cur and not any(w.get("keep") for w in kept[-1]):
                kept.pop()
        kept.append(ph)
    return kept


CUTS = {  # насколько плотно режем: (самая длинная пауза, запас до фразы, запас после)
    "tight": (0.25, 0.05, 0.10),     # «короче», «динамичнее»
    "normal": (MAX_GAP, PAD_BEFORE, PAD_AFTER),
    "gentle": (1.5, 0.3, 0.5),       # «подлиннее», «оставь паузы»
    "none": None,                    # не резать вообще: влог, зарисовка, «от начала до конца»
}


def plan_cuts(words, duration, gentle=False, cut=None, keep_retakes=False):
    """Возвращает (отрезки исходника [(start, end)], слова в новом времени).

    cut: tight / normal / gentle / none (см. CUTS); gentle=True — то же, что cut="gentle".
    keep_retakes: не выбрасывать повторные дубли.
    Слово с пометкой "nocut" не отрезаем от следующего: паузу после него оставляем (плавная склейка)."""
    cut = cut or ("gentle" if gentle else "normal")
    if cut == "none":
        return [(0.0, duration)], [dict(w, seg=0) for w in words if w["start"] < duration]
    max_gap, pad_before, pad_after = CUTS[cut]
    words = [w for w in words if w.get("keep") or _norm(w["word"]) not in FILLERS]
    if not keep_retakes:
        words = [w for ph in _drop_retakes(_phrases(words)) for w in ph]
    if not words:
        return [(0.0, duration)], []

    groups, cur = [], [words[0]]
    for w in words[1:]:
        if w["start"] - cur[-1]["end"] > max_gap and not cur[-1].get("nocut"):
            groups.append(cur)
            cur = []
        cur.append(w)
    groups.append(cur)

    segments, new_words, t_out, prev_end = [], [], 0.0, 0.0
    for gi, g in enumerate(groups):
        # «ты обрезал слово»: у такого слова запас больше (распознавание часто ставит границы слова неточно)
        s = max(prev_end, g[0]["start"] - pad_before - (0.25 if g[0].get("keep") else 0.0), 0.0)
        e = min(duration, g[-1]["end"] + pad_after + (0.35 if g[-1].get("keep") else 0.0))
        if gi + 1 < len(groups):
            e = min(e, groups[gi + 1][0]["start"] - 0.02)
        if e - s < 0.15:
            continue
        for w in g:
            new_words.append({
                "word": w["word"],
                "start": round(t_out + max(0.0, w["start"] - s), 3),
                "end": round(t_out + min(e, w["end"]) - s, 3),
                "seg": len(segments),
                **({"accent": w["accent"]} if w.get("accent") else {}),
                **({"i": w["i"]} if "i" in w else {}),
            })
        segments.append((round(s, 3), round(e, 3)))
        t_out += e - s
        prev_end = e
    return segments, new_words


def apply_plan(words, plan):
    """Ручной или сгенерированный Claude план правок по номерам слов исходной расшифровки.

    plan = {"delete": [[от, до], ...],      # вырезать слова (включительно): дубли, оговорки
            "merge": [[от, до], ...],       # склеить в одно слово: «юго» + «-востоке»
            "replace": {"номер": "текст"}}  # исправить написание в субтитрах
    Номер слова сохраняется в поле "i", чтобы картинки и выделения находили своё место.
    """
    out = [dict(w, i=w.get("i", k)) for k, w in enumerate(words)]
    rep = {int(k): v for k, v in (plan.get("replace") or {}).items()}
    for w in out:
        if w["i"] in rep:
            w["word"] = rep[w["i"]]
    for a, b in plan.get("merge") or []:
        grp = [w for w in out if a <= w["i"] <= b]
        if len(grp) < 2:
            continue
        text = grp[0]["word"]
        for w in grp[1:]:
            text += w["word"] if w["word"].startswith("-") or text.endswith("-") else " " + w["word"]
        grp[0]["word"], grp[0]["end"] = text, grp[-1]["end"]
        drop = {id(w) for w in grp[1:]}
        out = [w for w in out if id(w) not in drop]
    dels = plan.get("delete") or []
    return [w for w in out if not any(a <= w["i"] <= b for a, b in dels)]


def _match_key(word):
    k = _norm(word)
    return k[:max(3, len(k) - 2)]


def same_word(word, target):
    """Одно слово с точностью до окончания: «погоду» = «погода», но не «поговорим»."""
    w, t = _norm(word), _norm(target)
    return w.startswith(_match_key(t)) and abs(len(w) - len(t)) <= 2


def find_phrase(words, phrase):
    """(начало, конец) первого места, где звучит фраза; окончания слов не важны. None, если не нашли."""
    target = [t for t in phrase.split() if _norm(t)]
    n = len(target)
    for i in range(len(words) - n + 1):
        if n and all(same_word(words[i + k]["word"], target[k]) for k in range(n)):
            return words[i]["start"], words[i + n - 1]["end"]
    return None


def mark_smooth(words, phrases):
    """«После слов X резкая склейка» — помечаем последнее слово фразы, паузу после него не вырезаем."""
    out = [dict(w) for w in words]
    for ph in phrases or []:
        target = [t for t in ph.split() if _norm(t)]
        n = len(target)
        for i in range(len(out) - n + 1):
            if n and all(same_word(out[i + k]["word"], target[k]) for k in range(n)):
                out[i + n - 1]["nocut"] = True
                if i + n < len(out):  # и пауза перед следующей фразой тоже остаётся
                    out[i + n]["nocut_before"] = True
    return out


def mark_unclip(words, phrases):
    """«Ты обрезал слово X»: слово X не режем ни до, ни после, не считаем паразитом или дублем, оставляем запас."""
    out = [dict(w) for w in words]
    for ph in phrases or []:
        target = [t for t in re.findall(r"[\wё-]+", ph) if _norm(t)]
        n = len(target)
        for i in range(len(out) - n + 1):
            if n and all(same_word(out[i + k]["word"], target[k]) for k in range(n)):
                for k in range(n):
                    out[i + k]["keep"] = True
                    out[i + k]["nocut"] = True
                if i > 0:
                    out[i - 1]["nocut"] = True
    return out


def apply_fixes(words, fixes):
    """Исправляет написание в субтитрах: [{"from": "блаватская", "to": "Блаватская"}]."""
    out = [dict(w) for w in words]
    for fx in fixes or []:
        target = [_norm(t) for t in fx["from"].split() if _norm(t)]
        n = len(target)
        keys = [_norm(w["word"]) for w in out]
        for i in range(len(keys) - n + 1):
            if n and keys[i:i + n] == target:
                tail = re.search(r"[.,!?…:;]+$", out[i + n - 1]["word"])
                out[i]["word"] = fx["to"] + (tail.group(0) if tail else "")
                out[i]["end"] = out[i + n - 1]["end"]
                del out[i + 1:i + n]
                break
    return out


def crop_output(segments, words, a, b):
    """Оставляет кусок готового ролика от a до b секунд (обрезать начало или конец)."""
    new_segs, seg_map, t = [], {}, 0.0
    for k, (s, e) in enumerate(segments):
        d = e - s
        lo, hi = max(t, a), min(t + d, b)
        if hi - lo > 0.05:
            seg_map[k] = len(new_segs)
            new_segs.append((round(s + lo - t, 3), round(s + hi - t, 3)))
        t += d
    out = [dict(w, start=round(w["start"] - a, 3), end=round(min(w["end"], b) - a, 3), seg=seg_map[w["seg"]])
           for w in words if w["start"] >= a and w["start"] < b and w["seg"] in seg_map]
    return new_segs or segments, out


# ---------- 2. карточки-картинки ----------

def load_triggers():
    path = os.path.join(HERE, "assets", "triggers.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_cards(words, triggers, total, not_before=0.0, card_secs=CARD_SECONDS, end_secs=END_SECONDS):
    """По словам-ключам решаем, где показать картинку. Не чаще раза в 4 секунды.

    Порядок ключей в triggers.json = приоритет: в «лунный календарь» победит календарь,
    потому что смотрим на текущее слово и два следующих.
    """
    order = list(triggers)
    cards, last = [], -10.0
    for i, w in enumerate(words):
        t = w["start"]
        if t < not_before or t - last < 4.0 or t + card_secs > total - end_secs:
            continue
        if not any(_norm(w["word"]).startswith(k) for k in order):
            continue
        best = None
        for x in words[i:i + 3]:
            n = _norm(x["word"])
            for rank, k in enumerate(order):
                if n.startswith(k) and (best is None or rank < best):
                    best = rank
                    break
        path = os.path.join(IMAGES, triggers[order[best]])
        if os.path.exists(path):
            cards.append({"t": t, "image": path, "word": w["word"]})
            last = t
    return cards


def make_card(src, dst, border_hex, width=None, scale=1.0):
    """Картинка в рамке со скруглением и тенью, 840 px по большей стороне.

    border_hex=None: картинка как есть, без рамки (стиль Полины); width задаёт ширину в кадре.
    """
    im = Image.open(src).convert("RGBA")
    if border_hex is None:
        w = round((width or 600) * scale)
        im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
        im.save(dst)
        return
    side = round(840 * scale)
    if max(im.size) < side:  # маленькую картинку увеличиваем, а не оставляем крошечной
        k = side / max(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    im.thumbnail((side, side))
    r = 36
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], r, fill=255)
    im.putalpha(mask)
    pad = 40
    out = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("L", out.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle([pad, pad + 10, pad + im.width, pad + im.height + 10], r, fill=150)
    out.putalpha(shadow.filter(ImageFilter.GaussianBlur(16)))
    out.alpha_composite(im, (pad, pad))
    ImageDraw.Draw(out).rounded_rectangle(
        [pad - 3, pad - 3, pad + im.width + 2, pad + im.height + 2], r + 3, outline=border_hex, width=6)
    out.save(dst)


# ---------- 3. субтитры ----------

def mark_accents(words, accents=None, auto=True):
    """Помечает слова для выделения: {"phrase": "новолуние", "color": "red", "font": "Oranienbaum"}.

    auto=True дополнительно выделяет ключевые слова из ACCENT_AUTO цветом и шрифтом стиля.
    """
    out = [dict(w) for w in words]
    keys = [_norm(w["word"]) for w in out]
    for a in accents or []:
        target = [_norm(t) for t in a["phrase"].split() if _norm(t)]
        n = len(target)
        for i in range(len(keys) - n + 1):
            if n and all(keys[i + k].startswith(target[k][:max(3, len(target[k]) - 2)]) for k in range(n)):
                for k in range(n):
                    out[i + k]["accent"] = {"color": a.get("color"), "font": a.get("font")}
    if auto:
        for w, k in zip(out, keys):
            if "accent" not in w and any(k.startswith(p) for p in ACCENT_AUTO):
                w["accent"] = {"color": None, "font": None}
    return out


def _display(word, upper, lower=False):
    w = re.sub(r"[«»\"“”„.,:;…—–?!]", "", word).strip()
    return w.upper() if upper else w.lower() if lower else w


def build_ass(words, style, total, badge=None, title=None, end_seconds=END_SECONDS, subs=True):
    st = style if isinstance(style, dict) else STYLES[style]
    text_c, hi_c = ass_color(st["text"]), ass_color(st["highlight"])
    outline_c = ass_color(st["outline"])
    border_style = 3 if st["box"] else 1
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{st['font']},{st['font_size']},{text_c},{text_c},{outline_c},{ass_color(NIGHT, 0x90)},-1,0,0,0,100,100,1,0,"
        f"{border_style},{st['outline_px']},{st.get('shadow_px', 0)},{ {'top': 8, 'middle': 5}.get(st.get('align'), 2)},90,90,"
        f"{st.get('margin_v', 560)},204",
        f"Style: Badge,Jost,46,{ass_color(GOLD)},{ass_color(GOLD)},{ass_color(NIGHT, 0x40)},{ass_color(NIGHT, 0x40)},-1,0,0,0,100,100,2,0,3,16,0,7,70,70,120,204",
        f"Style: Title,Oranienbaum,96,{ass_color(PAPER)},{ass_color(PAPER)},{ass_color(NIGHT)},{ass_color(NIGHT, 0x30)},0,0,0,0,100,100,0,0,1,7,3,8,80,80,250,204",
        f"Style: End,Oranienbaum,88,{ass_color(PAPER)},{ass_color(PAPER)},{ass_color(NIGHT)},{ass_color(NIGHT)},0,0,0,0,100,100,0,0,1,5,0,5,80,80,0,204",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def ts(t):
        t = max(0.0, t)
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    end_at = total - end_seconds
    # группы по 2–3 слова, не через склейку и не через точку
    groups, cur = [], []
    for w in words if subs else []:
        if w["start"] >= end_at:
            break
        if cur and (len(cur) == st.get("max_words", 3) or w["seg"] != cur[-1]["seg"]
                    or sum(len(x["word"]) for x in cur) + len(w["word"]) > st.get("max_chars", 18)
                    or re.search(r"[.!?…]$", cur[-1]["word"])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    # «как у Полины»: всё строчными, кроме имён (слово с большой буквы не в начале фразы)
    proper = set()
    if st.get("lowercase"):
        for prev, w in zip(words, words[1:]):
            if w["word"][:1].isupper() and not re.search(r"[.!?…]$", prev["word"]):
                proper.add(_norm(w["word"]))
    lead = st.get("lead", 0.0)  # подпись появляется чуть раньше слова, сразу после предыдущей
    prev_end = 0.0
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0]["start"] - min(lead, max(0.0, groups[gi + 1][0]["start"] - g[-1]["end"])) \
            if gi + 1 < len(groups) else end_at
        g_end = min(g[-1]["end"] + 0.25 + lead, nxt, end_at)
        for i, w in enumerate(g):
            s = max(prev_end, w["start"] - lead) if i == 0 else w["start"]
            e = g[i + 1]["start"] if i + 1 < len(g) else g_end
            if e <= s:
                continue
            parts = []
            for j, x in enumerate(g):
                d = _display(x["word"], st["uppercase"], st.get("lowercase", False) and _norm(x["word"]) not in proper)
                if x.get("accent"):
                    a = x["accent"]
                    c = a.get("color")
                    col = ass_color(ACCENT_COLORS.get(c) or (c if c and c.startswith("#") else st["accent_color"]))
                    font = a.get("font") or st["accent_font"]
                    size = 112 if j == i else 104
                    parts.append(f"{{\\fn{font}\\c{col}\\fscx{size}\\fscy{size}}}{d}"
                                 f"{{\\fn{st['font']}\\c{text_c}\\fscx100\\fscy100}}")
                elif j == i:
                    parts.append("{\\c" + hi_c + "}" + d + "{\\c" + text_c + "}")
                else:
                    parts.append(d)
            pop = "{\\fscx108\\fscy108\\t(0,90,\\fscx100\\fscy100)}" if i == 0 and st.get("pop", True) else ""
            lines.append(f"Dialogue: 1,{ts(s)},{ts(e)},Sub,,0,0,0,,{pop}{' '.join(parts)}")
        prev_end = g_end

    if title:
        lines.append(f"Dialogue: 2,{ts(0)},{ts(min(2.8, end_at))},Title,,0,0,0,,{{\\fad(150,250)}}{title}")
    if badge:
        lines.append(f"Dialogue: 2,{ts(0)},{ts(end_at)},Badge,,0,0,0,,☾ {badge}")
    for k, part in enumerate(END_CARD.split("\\N") if end_seconds else []):
        lines.append(f"Dialogue: 3,{ts(end_at)},{ts(total)},End,,0,0,0,,{{\\fad(250,0)\\pos({W // 2},{H // 2 - 60 + k * 120})}}{part}")
    return "\n".join(lines) + "\n"


# ---------- 4. сборка ----------

# Цветовые правки поверх цвета стиля («ярче», «теплее», «чёрно-белое»…)
COLOR_FILTERS = {
    "bright": "eq=saturation=1.3:brightness=0.03:contrast=1.04",
    "light": "eq=brightness=0.06:gamma=1.05",
    "dark": "eq=brightness=-0.05:contrast=1.05",
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.06:rm=0.03:bm=-0.03",
    "cold": "colorbalance=rs=-0.05:bs=0.06:rm=-0.02:bm=0.03",
    "muted": "eq=saturation=0.75",
    "contrast": "eq=contrast=1.12",
    "bw": "hue=s=0",
}
SUB_COLORS = {"red": "#C8102E", "gold": "#CFA85F", "yellow": "#F5D547", "white": "#FFFFFF", "black": "#15122A",
              "pink": "#FB7D7E", "indigo": "#2B1E7A", "purple": "#8E5BD9", "green": "#1E3527", "orange": "#E2653A",
              "paper": "#F3EDE3", "night": "#15122A"}
SUB_POS = {"top": ("top", 205), "middle": ("middle", 0), "bottom": ("bottom", 380)}
# шрифты из assets/fonts (все с кириллицей); имя — как его знает libass
SUB_FONTS = ["Jost", "Oranienbaum", "Montserrat", "Rubik", "Comfortaa", "Russo One", "Unbounded", "Playfair Display",
             "Caveat", "Marck Script", "Lobster", "Pacifico", "Amatic SC"]


def style_with(style, opts):
    """Стиль ролика с правками субтитров: размер, место, регистр, цвет, шрифт, слов в строке."""
    st = dict(STYLES[style])
    if opts.get("sub_size"):
        st["font_size"] = round(st["font_size"] * opts["sub_size"])
    if opts.get("sub_pos") in SUB_POS:
        st["align"], st["margin_v"] = SUB_POS[opts["sub_pos"]]
    case = opts.get("sub_case")
    if case == "upper":
        st["uppercase"], st["lowercase"] = True, False
    elif case == "lower":
        st["uppercase"], st["lowercase"] = False, True
    elif case == "normal":
        st["uppercase"], st["lowercase"] = False, False
    c = opts.get("sub_color")
    if c:
        col = SUB_COLORS.get(c, c if str(c).startswith("#") else None)
        if col:
            if st["highlight"] == st["text"]:
                st["highlight"] = col
            st["text"] = col
            if col.upper() in ("#FFFFFF", "#F3EDE3", "#F5D547") and not st["outline_px"]:
                st["outline_px"] = 3  # светлый текст без обводки не читается на светлом фоне
    if opts.get("sub_font") in SUB_FONTS:
        st["font"] = opts["sub_font"]
    shift = int(opts.get("sub_shift") or 0)  # «выше» — плюс, «ниже» — минус, в пикселях
    if shift:
        align = st.get("align", "bottom")
        if align == "middle":  # от центра: переводим в отсчёт снизу
            st["align"], st["margin_v"] = "bottom", H // 2 - st["font_size"] // 2
            align = "bottom"
        mv = st.get("margin_v", 560)
        mv = mv - shift if align == "top" else mv + shift
        st["margin_v"] = max(40, min(H - 200, mv))
    if opts.get("sub_words"):
        n = max(1, min(5, int(opts["sub_words"])))
        st["max_words"], st["max_chars"] = n, 7 + 7 * n
    return st


def _zoom_expr2(seg_durs, base, punches):
    """Чередование крупности по склейкам (base) и наезды (punches: [(от, до, крупность в начале, в конце)])."""
    t, alt = 0.0, []
    for i, d in enumerate(seg_durs):
        if i % 2 == 1 and base > 1.0:
            alt.append(f"between(it,{t:.3f},{t + d:.3f})")
        t += d
    expr = f"if({'+'.join(alt)},{base},1)" if alt else "1"
    for a, b, z0, z1 in punches:
        val = f"{z0}" if z0 == z1 else f"{z0}+({z1 - z0:.3f})*(it-{a:.3f})/{max(0.1, b - a):.3f}"
        expr = f"if(between(it,{a:.3f},{b:.3f}),{val},{expr})"
    return expr


# наезды из монтажного листа: резкий на панчлайне, медленный на подводке, удержание на ремарке
ZOOMS = {"punch": (1.7, 1.7, 0.9), "push": (1.05, 1.45, 0.3), "hold": (1.3, 1.3, 0.4)}


def _zoom_marks(new_words, marks, total, strong=False):
    by_i = {w["i"]: w for w in new_words}
    out = []
    for m in marks or []:
        ws = [by_i[i] for i in range(m["from"], m["to"] + 1) if i in by_i]
        if not ws or m.get("kind") not in ZOOMS:
            continue
        z0, z1, tail = ZOOMS[m["kind"]]
        if strong:
            z0, z1 = round(z0 * 1.1, 2), round(z1 * 1.1, 2)
        a = max(0.0, ws[0]["start"] - 0.05)
        b = min(total, ws[-1]["end"] + tail)
        if m["kind"] == "push":
            b = min(total, max(b, a + 1.5))
        out.append((a, b, z0, z1))
    return out


def _no_overlap(punches):
    """Наезды не наслаиваются: следующий начинается не раньше, чем кончился предыдущий."""
    out = []
    for p in sorted(punches):
        if out and p[0] < out[-1][1]:
            continue
        out.append(p)
    return out


# ---------- замер кадра: лицо и цвет ----------

def _sample_times(segments, seek, n=6):
    total = sum(e - s for s, e in segments) or 1.0
    out, acc = [], 0.0
    targets = [total * (k + 0.5) / n for k in range(n)]
    for s, e in segments:
        for t in targets:
            if acc <= t < acc + (e - s):
                out.append(seek + s + (t - acc))
        acc += e - s
    return out


def _frame(src, t, w=None):
    """Кадр исходника, уже вписанный в 1080x1920, как в ролике (RGB, numpy-массив или PIL)."""
    vf = f"scale='trunc(iw*sar/2)*2':ih,setsar=1,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if w:
        vf += f",scale={w}:-2"
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                          "-vf", vf, "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    import io
    return Image.open(io.BytesIO(out)).convert("RGB")


def face_zone(src, segments, seek=0.0):
    """Где в кадре лицо: (верх, низ) в пикселях ролика 1080x1920, по нескольким кадрам. None — не нашли."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
        boxes = []
        for t in _sample_times(segments, seek, 5):
            im = _frame(src, t, w=360)
            gray = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                k = W / 360
                boxes.append((y * k, (y + h) * k))
        if not boxes:
            return None
        boxes.sort()
        mid = boxes[len(boxes) // 2]
        # с запасом: подбородок и волосы тоже лицо
        h = mid[1] - mid[0]
        return (float(max(0, mid[0] - 0.25 * h)), float(min(H, mid[1] + 0.35 * h)))
    except Exception:
        return None


def _free_spot(pref, face, card_h, st):
    """Куда поставить картинку высотой card_h, чтобы не закрыть лицо и субтитры. pref — куда хочется по стилю."""
    spots = {"top": (200, 200 + card_h), "middle": ((H - card_h) / 2, (H + card_h) / 2),
             "bottom": (H - card_h - 260, H - 260)}
    sub_top = st.get("align") == "top"
    sub = (st.get("margin_v", 205), st.get("margin_v", 205) + 260) if sub_top else (H - st.get("margin_v", 380) - 260, H - st.get("margin_v", 380))

    def clash(a, b):
        return max(0, min(a[1], b[1]) - max(a[0], b[0]))
    order = [pref] + [p for p in ("bottom", "top", "middle") if p != pref]
    best = min(order, key=lambda p: (clash(spots[p], face) > 40, clash(spots[p], sub) > 40, order.index(p)))
    return best


def measured_grade(src, segments, seek=0.0, limit=0.08):
    """Небольшая поправка по замеру кадров перед цветом стиля: яркость, насыщенность, баланс белого (±8 %)."""
    try:
        ys, ss, us, vs = [], [], [], []
        for t in _sample_times(segments, seek, 5):
            out = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                 "-vf", "scale=270:-2,signalstats,metadata=print:file=-", "-f", "null", "-"],
                capture_output=True, text=True, check=True).stdout
            vals = dict(re.findall(r"lavfi\.signalstats\.(\w+)=([\d.]+)", out))
            if "YAVG" in vals:
                ys.append(float(vals["YAVG"]))
                ss.append(float(vals.get("SATAVG", 0)))
                us.append(float(vals.get("UAVG", 128)))
                vs.append(float(vals.get("VAVG", 128)))
        if not ys:
            return ""
        n = len(ys)
        y, sat, u, v = sum(ys) / n, sum(ss) / n, sum(us) / n, sum(vs) / n

        def clamp(x):
            return max(-limit, min(limit, x))
        bright = clamp((118 - y) / 255 * 0.6)        # тёмное видео чуть светлее, пересвеченное чуть темнее
        satk = 1 + clamp((32 - sat) / 32 * 0.25)    # блёклое чуть сочнее, кислотное чуть спокойнее
        # баланс белого: V > 128 — краснит, U > 128 — синит; убираем треть сдвига и не больше 5 %,
        # чтобы кожа не ушла в серый или зелёный (тёплый образ стиля всё равно ляжет сверху)
        rs = max(-0.05, min(0.05, -(v - 128) / 128 * 0.33))
        bs = max(-0.05, min(0.05, -(u - 128) / 128 * 0.33))
        parts = []
        if abs(bright) > 0.01 or abs(satk - 1) > 0.01:
            parts.append(f"eq=brightness={bright:.3f}:saturation={satk:.3f}")
        if abs(rs) > 0.01 or abs(bs) > 0.01:
            parts.append(f"colorbalance=rm={rs:.3f}:bm={bs:.3f}")
        return ",".join(parts) + ("," if parts else "")
    except Exception:
        return ""


def render(src, words, out_path, style="night", music=None, badge=None, title=None, workdir=None, cards=True,
           accents=None, auto_accents=True, extra_cards=None, auto_cards=True, end_card=True, preset="medium", seek=0.0,
           bright=False, gentle=False, opts=None):
    """Монтаж ролика. opts — правки Полины (все необязательные):

    cut: tight/normal/gentle, keep_retakes, trim_start / trim_end (секунды с начала / конца),
    speed (1.1 = на 10 % быстрее), zoom: off/normal/strong, zoom_words [фразы], mirror,
    color [bright, light, dark, warm, cold, muted, contrast, bw, natural],
    subs (False = без субтитров), sub_size, sub_pos, sub_case, sub_color, sub_font, sub_words, fixes,
    card_scale, card_pos: top/middle/bottom, card_secs, cards_off [фразы],
    music_vol, voice_vol, denoise, sounds [{"phrase", "file", "vol"}],
    effects [{"phrase", "type": glow/bw/clip, "file", "secs"}], end_card, title_on.
    extra_cards: [{"word": номер слова в исходной расшифровке, "image": путь}] — картинки в заданные места.
    """
    o = dict(opts or {})
    if bright:
        o.setdefault("color", ["bright"])
    if gentle:
        o.setdefault("cut", "gentle")
    st = style_with(style, o)
    words = [dict(w, i=w.get("i", k)) for k, w in enumerate(words)]
    words = apply_fixes(words, o.get("fixes"))
    words = mark_smooth(words, o.get("smooth"))
    words = mark_unclip(words, o.get("unclip"))
    words = mark_accents(words, accents, auto_accents)
    work = workdir or tempfile.mkdtemp(prefix="factory_")
    os.makedirs(work, exist_ok=True)
    duration = ffprobe_duration(src) - seek
    if seek:
        # монтируем кусок длинного ролика: читаем исходник с отметки seek
        words = [dict(w, start=round(w["start"] - seek, 3), end=round(w["end"] - seek, 3))
                 for w in words if w["start"] >= seek]
    segments, new_words = plan_cuts(words, duration, cut=o.get("cut"), keep_retakes=bool(o.get("keep_retakes")))
    total = sum(e - s for s, e in segments)
    a, b = float(o.get("trim_start") or 0), total - float(o.get("trim_end") or 0)
    if (a > 0 or b < total) and b - a > 1.0:
        segments, new_words = crop_output(segments, new_words, a, b)
        total = sum(e - s for s, e in segments)
    speed = max(0.5, min(2.0, float(o.get("speed") or st.get("speed") or 1.0)))
    seg_durs = [(e - s) / speed for s, e in segments]
    if speed != 1.0:
        new_words = [dict(w, start=round(w["start"] / speed, 3), end=round(w["end"] / speed, 3)) for w in new_words]
    total = sum(seg_durs)
    show_end = o["end_card"] if o.get("end_card") is not None else (end_card and st.get("end_card", True))
    end_sec = END_SECONDS if show_end else 0.0
    if o.get("title_on") is False or (o.get("title_on") is None and not st.get("title_on", True)):
        title = None

    card_secs = float(o.get("card_secs") or CARD_SECONDS)
    if not cards:
        cards = []
    else:
        by_i = {w["i"]: w for w in new_words}
        off = [p for ph in o.get("cards_off") or [] for p in ph.split() if _norm(p)]
        manual = []
        for c in extra_cards or []:
            w = by_i.get(c["word"])
            if w is not None and os.path.exists(c["image"]) and not any(same_word(w["word"], k) for k in off):
                manual.append({"t": w["start"], "image": c["image"]})
        manual = [m for k, m in enumerate(sorted(manual, key=lambda m: m["t"]))
                  if k == 0 or m["t"] - sorted(manual, key=lambda m: m["t"])[k - 1]["t"] > 1.0]
        auto = find_cards(new_words, load_triggers(), total, not_before=2.9 if title else 0.0,
                          card_secs=card_secs, end_secs=end_sec) if auto_cards else []
        auto = [x for x in auto if not any(same_word(x["word"], k) for k in off)]
        # автоматические картинки не ставим ближе 3 с к заданным вручную
        auto = [x for x in auto if all(abs(x["t"] - m["t"]) > 3 for m in manual)]
        cards = sorted(manual + auto, key=lambda c: c["t"])
        for k, c in enumerate(cards):
            nxt = cards[k + 1]["t"] if k + 1 < len(cards) else total - end_sec
            c["dur"] = max(0.6, min(card_secs, nxt - c["t"] - 0.05))
    for i, c in enumerate(cards):
        c["png"] = os.path.join(work, f"card{i}.png")
        make_card(c["image"], c["png"], st["card_border"], scale=float(o.get("card_scale") or 1.0))
    card_pos = o.get("card_pos") or st.get("card_pos", "top")
    if not o.get("card_pos") and st.get("align") == card_pos and card_pos in ("top", "bottom"):
        card_pos = "top" if card_pos == "bottom" else "bottom"  # субтитры перенесли туда, где картинки: картинки уходят на другой край
    face = face_zone(src, segments, seek) if cards and not o.get("card_pos") else None
    for c in cards:
        c["pos"] = card_pos
        if face:
            c["pos"] = _free_spot(card_pos, face, Image.open(c["png"]).height, st)
    card_ys = {"top": "200", "middle": "(H-h)/2", "bottom": "H-h-260"}

    ass_path = os.path.join(work, "subs.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(build_ass(new_words, st, total, badge, title, end_sec, subs=o.get("subs", True) is not False))

    # звуки и эффекты на словах
    sounds = []
    for x in o.get("sounds") or []:
        at = find_phrase(new_words, x["phrase"]) if x.get("phrase") else (x.get("t", 0.0), 0)
        if at and os.path.exists(x["file"]):
            sounds.append({"t": at[0], "file": x["file"], "vol": float(x.get("vol") or 0.5)})
    effects = []
    for x in o.get("effects") or []:
        at = find_phrase(new_words, x["phrase"]) if x.get("phrase") else None
        if not at:
            continue
        t0 = max(0.0, at[0] - 0.1)
        t1 = min(total - end_sec, max(at[1] + 0.3, t0 + float(x.get("secs") or 2.0)))
        if x.get("secs") and float(x["secs"]) < 1:
            t1 = min(total, t0 + float(x["secs"]))
        if t1 - t0 > 0.1 and (x["type"] != "clip" or (x.get("file") and os.path.exists(x["file"]))):
            effects.append(dict(x, t0=t0, t1=t1))
    punches = []
    for ph in o.get("zoom_words") or []:
        at = find_phrase(new_words, ph)
        if at:
            punches.append((max(0.0, at[0] - 0.05), min(total, at[1] + 1.0), 1.45, 1.45))
    if o.get("zoom") != "off":
        punches += _zoom_marks(new_words, o.get("zoom_marks"), total, strong=o.get("zoom") == "strong")
    punches = _no_overlap(punches)

    inputs = (["-ss", f"{seek:.3f}"] if seek else []) + ["-i", src]
    for c in cards:
        inputs += ["-loop", "1", "-t", f"{c['dur']:.2f}", "-i", c["png"]]
    n_in = 1 + len(cards)
    music_idx = None
    if music:
        music_idx = n_in
        inputs += ["-stream_loop", "-1", "-i", music]
        n_in += 1
    fx_idx = {}
    for k, e in enumerate(effects):
        if e["type"] == "clip":
            fx_idx[k] = n_in
            inputs += ["-stream_loop", "-1", "-t", f"{e['t1'] - e['t0']:.3f}", "-i", e["file"]]
            n_in += 1
    snd_idx = []
    for x in sounds:
        snd_idx.append(n_in)
        inputs += ["-i", x["file"]]
        n_in += 1

    fc = []
    for i, (s, e) in enumerate(segments):
        d = e - s
        fc.append(f"[0:v]trim={s}:{e},setpts=PTS-STARTPTS[v{i}]")
        fc.append(f"[0:a]atrim={s}:{e},asetpts=PTS-STARTPTS,afade=t=in:d=0.012,afade=t=out:st={max(0, d - 0.025):.3f}:d=0.025[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=1:a=1[vc][ac]")
    base_zoom = {"off": 1.0, "strong": max(1.12, st["zoom"]) + 0.08}.get(o.get("zoom"), st["zoom"])
    colors = list(o.get("color") or [])
    grade = "null" if "natural" in colors else (measured_grade(src, segments, seek) + st["grade"])
    grade += "".join("," + COLOR_FILTERS[c] for c in colors if c in COLOR_FILTERS)
    fc.append(
        f"[vc]{f'setpts=PTS/{speed},' if speed != 1.0 else ''}fps={FPS},"
        # сначала квадратные пиксели (у некоторых телефонных видео они вытянутые), потом вписываем в 1080x1920
        f"scale='trunc(iw*sar/2)*2':ih,setsar=1,"
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,{'hflip,' if o.get('mirror') else ''}"
        f"zoompan=z='{_zoom_expr2(seg_durs, base_zoom, punches)}':x='iw/2-(iw/zoom/2)':y='ih*0.4-(ih/zoom*0.4)':d=1:s={W}x{H}:fps={FPS},"
        f"{grade}[v_base]"
    )
    last = "v_base"
    for k, e in enumerate(effects):
        on = f"enable='between(t,{e['t0']:.3f},{e['t1']:.3f})'"
        if e["type"] == "glow":
            fc.append(f"[{last}]split[g{k}a][g{k}b]")
            fc.append(f"[g{k}b]colorchannelmixer=rr=0.45:gg=1.25:bb=0.45,gblur=sigma=28,eq=brightness=0.06[g{k}c]")
            fc.append(f"[g{k}a][g{k}c]blend=all_mode=screen:all_opacity=0.65:{on}[g{k}]")
            last = f"g{k}"
        elif e["type"] == "bw":
            fc.append(f"[{last}]hue=s=0:{on}[bw{k}]")
            last = f"bw{k}"
        elif e["type"] == "clip":
            fc.append(f"[{fx_idx[k]}:v]scale={W}:{H},fps={FPS},format=yuva420p,"
                      f"lumakey=threshold=0:tolerance=0.12:softness=0.3,setpts=PTS-STARTPTS+{e['t0']:.3f}/TB[x{k}]")
            fc.append(f"[{last}][x{k}]overlay=eof_action=pass:{on}[xo{k}]")
            last = f"xo{k}"
    for i, c in enumerate(cards):
        t, dur = c["t"], c["dur"]
        fc.append(
            f"[{i + 1}:v]format=rgba,fade=t=in:st=0:d=0.2:alpha=1,fade=t=out:st={dur - 0.25:.2f}:d=0.25:alpha=1,"
            f"setpts=PTS-STARTPTS+{t:.3f}/TB[c{i}]"
        )
        fc.append(f"[{last}][c{i}]overlay=x=(W-w)/2:y={card_ys[c['pos']]}:eof_action=pass:enable='between(t,{t:.3f},{t + dur:.3f})'[o{i}]")
        last = f"o{i}"
    end_at = total - end_sec if end_sec else total + 1
    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts_esc = FONTS.replace(":", "\\:")
    fc.append(
        f"[{last}]drawbox=x=0:y=0:w=iw:h=ih:color=0x15122A@0.6:t=fill:enable='gte(t,{end_at:.3f})',"
        f"ass='{ass_esc}':fontsdir='{fonts_esc}',format=yuv420p[vout]"
    )
    tempo = f"atempo={speed}," if speed != 1.0 else ""
    denoise = "afftdn=nf=-25," if o.get("denoise") else ""
    voice_vol = float(o.get("voice_vol") or 1.0)
    fc.append(f"[ac]{tempo}highpass=f=80,{denoise}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
              f"{f',volume={voice_vol}' if voice_vol != 1.0 else ''}[voice]")
    mix = []
    if music_idx is not None:
        mv = 0.22 * float(o.get("music_vol") or 1.0)
        fc.append("[voice]asplit[v1][v2]")
        fc.append(f"[{music_idx}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,aresample=48000,volume={mv:.3f},"
                  f"afade=t=out:st={max(0, total - 1.5):.3f}:d=1.5[mus]")
        fc.append("[mus][v1]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[duck]")
        mix += ["[v2]", "[duck]"]
    else:
        mix.append("[voice]")
    for k, (x, idx) in enumerate(zip(sounds, snd_idx)):
        ms = int(x["t"] * 1000)
        fc.append(f"[{idx}:a]aformat=channel_layouts=stereo,aresample=48000,atrim=end=3,afade=t=out:st=2.6:d=0.4,"
                  f"volume={x['vol']:.2f},adelay={ms}|{ms}[s{k}]")
        mix.append(f"[s{k}]")
    if len(mix) > 1:
        fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[aout]")
    else:
        fc.append("[voice]anull[aout]")

    graph_path = os.path.join(work, "graph.txt")
    with open(graph_path, "w") as f:
        f.write(";\n".join(fc))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex_script", graph_path, "-map", "[vout]", "-map", "[aout]",
           "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", preset, "-crf", "20", "-r", str(FPS),
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    subprocess.run(cmd, check=True)
    return {"segments": segments, "words": new_words, "cards": [c["t"] for c in cards], "duration": round(total, 2),
            "cut_seconds": round(duration - total * speed, 2), "speed": speed, "source_seconds": round(duration, 2),
            "card_pos": [c["pos"] for c in cards], "face": face}


def make_cover(video_path, title, out_path, at=0.8):
    """Обложка: кадр из ролика + заголовок Oranienbaum на тёмной полосе."""
    frame = out_path + ".frame.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(at), "-i", video_path, "-frames:v", "1", frame],
                   check=True)
    im = Image.open(frame).convert("RGB")
    os.remove(frame)
    band = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(band)
    for y in range(900, H):
        a = int(min(1.0, (y - 900) / 500) * 215)
        d.line([(0, y), (W, y)], fill=(0x15, 0x12, 0x2A, a))
    im = Image.alpha_composite(im.convert("RGBA"), band)
    d = ImageDraw.Draw(im)
    font = ImageFont.truetype(os.path.join(FONTS, "Oranienbaum.ttf"), 112)
    words, lines, cur = title.split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font) > W - 160 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    lines.append(cur)
    y = 1500 - len(lines) * 120
    for ln in lines:
        tw = d.textlength(ln, font=font)
        d.text(((W - tw) / 2, y), ln, font=font, fill=PAPER)
        y += 124
    d.line([(W / 2 - 60, y + 30), (W / 2 + 60, y + 30)], fill=GOLD, width=4)
    im.convert("RGB").save(out_path, quality=92)
    return out_path
