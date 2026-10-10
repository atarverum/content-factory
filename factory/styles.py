"""Стили роликов. Цвета и шрифты из брендбука ICON WITCH."""

NIGHT = "#15122A"
PAPER = "#F3EDE3"
LIPSTICK = "#C8102E"
GOLD = "#CFA85F"
INDIGO = "#2B1E7A"
FOREST = "#1E3527"
SUNSET = "#E2653A"

STYLES = {
    # «Ночь и золото»: крупные заглавные Jost, текущее слово золотом, контрастная картинка
    "night": {
        "title": "Ночь и золото",
        "font": "Jost",
        "font_size": 84,
        "uppercase": True,
        "text": PAPER,
        "highlight": GOLD,
        "outline": NIGHT,
        "outline_px": 6,
        "box": False,
        "grade": "eq=contrast=1.07:saturation=1.08:gamma=0.98,colorbalance=rs=0.03:gs=0.0:bs=-0.03:rm=0.02:bm=-0.02,vignette=angle=PI/5",
        "zoom": 1.12,
        "card_border": GOLD,
        "accent_color": LIPSTICK,
        "accent_font": "Oranienbaum",
    },
    # «Бумага»: Oranienbaum обычным регистром на плашке цвета бумаги, мягкий тёплый цвет
    "paper": {
        "title": "Бумага",
        "font": "Oranienbaum",
        "font_size": 80,
        "uppercase": False,
        "text": NIGHT,
        "highlight": LIPSTICK,
        "outline": PAPER,
        "outline_px": 14,
        "box": True,
        "grade": "eq=contrast=1.02:saturation=0.95:brightness=0.02,colorbalance=rs=0.04:gs=0.01:bs=-0.04",
        "zoom": 1.08,
        "card_border": PAPER,
        "accent_color": INDIGO,
        "accent_font": "Jost",
    },
}

# «Как у Полины»: повторяет её собственный монтаж (ролик про ведьм Норт-Бервика, октябрь 2026).
# Розовые строчные субтитры сверху, без обводки и без подсветки слова; картинки без рамок;
# мягкий тёплый цвет; речь ускорена в 1,1 раза; зумы по смыслу, а не через склейку.
STYLES["polina"] = {
    "title": "Как у Полины",
    "end_card": False,  # Полина не ставит финальную заставку
    "font": "Jost",
    "font_size": 124,
    "uppercase": False,
    "lowercase": True,
    "text": "#FB7D7E",
    "highlight": "#FB7D7E",
    "outline": "#15122A",
    "outline_px": 0,
    "shadow_px": 2,
    "box": False,
    "grade": "eq=contrast=1.08:saturation=1.03,colorbalance=rs=0.03:bs=-0.02,colorchannelmixer=rr=1.05:gg=1.02:bb=1.03",
    "zoom": 1.0,
    "card_border": None,
    "accent_color": "#C8102E",
    "accent_font": "Jost",
    "align": "top",
    "margin_v": 205,
    "max_words": 2,
    "max_chars": 12,
    "lead": 0.5,
    "pop": False,
    "speed": 1.1,
}

# Акценты: выделенные слова в субтитрах другим цветом и шрифтом.
ACCENT_COLORS = {"red": LIPSTICK, "gold": GOLD, "indigo": INDIGO, "green": FOREST, "orange": SUNSET,
                 "paper": PAPER, "night": NIGHT}
# Слова, которые выделяются сами (начало слова, без учёта регистра).
ACCENT_AUTO = ["новолун", "полнолун", "затмени", "календар", "расклад", "одина"]

END_CARD = "Больше в Telegram\\Nссылка в профиле"


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB → &HAABBGGRR для ASS."""
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()
