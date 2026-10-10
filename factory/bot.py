"""Telegram-бот «Фабрика»: Полина присылает видео → монтаж, субтитры, подпись → согласование → Instagram.

Запуск: python -m factory.bot  (настройки в переменных окружения, см. install.sh)
"""
import asyncio
import datetime as dt
import glob
import hashlib
import html
import json
import logging
import os
import re
import secrets
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiohttp import web

from . import captions, director, edits, instagram, library, store
from .edit import apply_plan, make_cover, render
from .transcribe import load_or_transcribe, transcribe

log = logging.getLogger("factory")
MSK = ZoneInfo("Europe/Moscow")
TOKEN = os.environ.get("BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")       # https://1-2-3-4.sslip.io
LOCAL_API = os.environ.get("TELEGRAM_API_URL", "")              # http://127.0.0.1:8081 (для видео > 20 МБ)
PUBLISH_TIME = os.environ.get("PUBLISH_TIME", "19:00")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "music")
STYLE_NAMES = {"polina": "как у Полины", "night": "ночной", "paper": "светлый"}
STATUS_NAMES = {"waiting": "ждёт выбора жанра", "processing": "монтируется", "review": "ждёт решения", "scheduled": "в очереди",
                "publishing": "публикуется", "published": "опубликован", "archived": "в архиве",
                "failed": "ошибка"}

dp = Dispatcher()
_work_lock = asyncio.Lock()
_awaiting_edit: dict[int, str] = {}      # chat_id → id ролика, к которому ждём правку
bot: Bot | None = None


# ───────────────────────── доступ ─────────────────────────

def _allowed(uid: int) -> bool:
    return uid in store.load()["allowed"]


def _new_pair_code() -> str:
    code = f"{secrets.randbelow(900000) + 100000}"
    store.update(lambda d: d.__setitem__("pair_code", code))
    with open(os.path.join(store.DATA, "pair_code.txt"), "w") as f:
        f.write(code + "\n")
    return code


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    if _allowed(m.from_user.id):
        await m.answer(HELP)
        return
    code = (command.args or "").strip()
    st = store.load()
    if not code or code != st.get("pair_code"):
        await m.answer("Это закрытый бот. Пришлите /start и код привязки.")
        return

    def add(d):
        d["allowed"].append(m.from_user.id)
        d["pair_code"] = None
    store.update(add)
    try:
        os.remove(os.path.join(store.DATA, "pair_code.txt"))
    except FileNotFoundError:
        pass
    await m.answer("Готово, вы подключены ✨\n\n" + HELP)


@dp.message(Command("invite"))
async def cmd_invite(m: Message):
    if not _allowed(m.from_user.id):
        return
    code = _new_pair_code()
    await m.answer(f"Код для второго человека: <code>{code}</code>\n"
                   f"Пусть откроет этого бота и отправит: <code>/start {code}</code>")


HELP = (
    "Пришлите видео (вертикальное, со звуком). Я спрошу, что это за ролик: рассказ (нарезка, картинки, "
    "выделения), влог или зарисовка (ролик целиком, только субтитры и цвет) или скетч (плотная нарезка), "
    "смонтирую и пришлю на проверку. Жанр можно сразу написать в подписи к видео: «влог», «скетч».\n\n"
    "В подписи к видео можно написать пожелание, например «плашка: 14-е лунные сутки» "
    "или «тема: руны на неделю».\n\n"
    "После монтажа правьте ролик текстом или голосом: «динамичнее и ярче», «субтитры снизу», "
    "«звук бум на слове „казнили“», «выложи завтра в 12». Свою картинку пришлите фото с подписью "
    "«на слово …», свою музыку пришлите mp3-файлом.\n\n"
    "В рассказах и скетчах Claude читает расшифровку как сценарий: убирает дубли, оговорки и вводные, "
    "ставит наезды и картинки, а перед отправкой сам проверяет кадры. Вернуть вырезанное: «оставь кусок про …», "
    "всё заново: «смонтируй заново». Если одну правку просите в разных роликах, я предложу её запомнить.\n\n"
    "Видео больше 20 МБ: /upload даст ссылку, по которой его можно загрузить прямо на сервер. /last пришлёт последний готовый ролик ещё раз.\n\n"
    "/edits все виды правок\n/forget забыть запомненные настройки\n/plan очередь публикаций\n/stats статистика Instagram\n/style стиль по умолчанию: как у Полины, ночной или светлый\n"
    "/pause и /resume остановить или возобновить публикации\n/instagram подключить Instagram\n"
    "/invite код для второго человека"
)


@dp.message(Command("help"))
async def cmd_help(m: Message):
    if _allowed(m.from_user.id):
        await m.answer(HELP)


# ───────────────────────── настройки ─────────────────────────

@dp.message(Command("meta"))
async def cmd_meta(m: Message, command: CommandObject):
    """/meta APP_ID APP_SECRET: ключи приложения Meta. Сообщение сразу удаляется."""
    if not _allowed(m.from_user.id):
        return
    try:
        await m.delete()
    except Exception:
        pass
    parts = (command.args or "").split()
    if len(parts) != 2:
        await m.answer("Формат: /meta ID_приложения Секрет_приложения")
        return

    def put(d):
        d["secrets"]["meta_app_id"], d["secrets"]["meta_app_secret"] = parts
    store.update(put)
    await m.answer("Ключи Meta сохранены, сообщение с ними удалено. Теперь /instagram")


@dp.message(Command("instagram"))
async def cmd_instagram(m: Message):
    if not _allowed(m.from_user.id):
        return
    s = store.load()["secrets"]
    if not s.get("meta_app_id"):
        await m.answer("Сначала пришлите ключи приложения Meta: /meta ID Секрет")
        return
    state = secrets.token_urlsafe(16)
    store.update(lambda d: d.__setitem__("oauth_state", {"state": state, "chat": m.chat.id}))
    url = instagram.auth_url(s["meta_app_id"], f"{PUBLIC_URL}/ig/callback", state)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Войти в Instagram", url=url)]])
    await m.answer("Нажмите кнопку, войдите в Instagram Полины и разрешите доступ. "
                   "Пароль остаётся у вас, мне приходит только разрешение на публикацию.", reply_markup=kb)


@dp.message(Command("style"))
async def cmd_style(m: Message, command: CommandObject):
    if not _allowed(m.from_user.id):
        return
    arg = (command.args or "").lower()
    style = ("polina" if ("полин" in arg or "polina" in arg) else "paper" if ("свет" in arg or "paper" in arg)
             else "night" if arg else None)
    if style:
        store.update(lambda d: d.__setitem__("style", style))
    cur = store.load()["style"]
    await m.answer(f"Стиль по умолчанию: {STYLE_NAMES[cur]}. Сменить: /style полина, /style светлый или /style ночной")


@dp.message(Command("pause"))
async def cmd_pause(m: Message):
    if _allowed(m.from_user.id):
        store.update(lambda d: d.__setitem__("paused", True))
        await m.answer("Публикации на паузе. Монтаж продолжает работать. Вернуть: /resume")


@dp.message(Command("resume"))
async def cmd_resume(m: Message):
    if _allowed(m.from_user.id):
        store.update(lambda d: d.__setitem__("paused", False))
        await m.answer("Публикации снова идут по расписанию.")


@dp.message(Command("plan"))
async def cmd_plan(m: Message):
    if not _allowed(m.from_user.id):
        return
    st = store.load()
    jobs = sorted((j for j in st["jobs"].values() if j["status"] in ("scheduled", "review", "processing")),
                  key=lambda j: j.get("publish_at") or 1e12)
    if not jobs:
        await m.answer("Очередь пуста. Пришлите видео 🙂")
        return
    lines = ["⏸ Публикации на паузе\n"] if st["paused"] else []
    for j in jobs:
        when = _fmt_time(j["publish_at"]) if j.get("publish_at") else STATUS_NAMES[j["status"]]
        lines.append(f"• {when}: {html.escape(j.get('title') or 'без названия')}")
    await m.answer("\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if not _allowed(m.from_user.id):
        return
    if not instagram.connected():
        await m.answer("Instagram ещё не подключён: /instagram")
        return
    await m.answer(await asyncio.to_thread(_stats_text))


def _stats_text() -> str:
    acc = instagram.account_stats()
    lines = [f"@{acc.get('username')}: {acc.get('followers_count', '?')} подписчиков, "
             f"цель 10 000 к 31.12.2026"]
    pub = sorted((j for j in store.load()["jobs"].values() if j.get("media_id")),
                 key=lambda j: j.get("published_at", 0), reverse=True)[:7]
    for j in pub:
        try:
            s = instagram.media_stats(j["media_id"])
            lines.append(f"• {j.get('title')}: 👁 {s.get('views', 0)}  ❤️ {s.get('likes', 0)}  "
                         f"💾 {s.get('saved', 0)}  ↗️ {s.get('shares', 0)}")
        except instagram.InstagramError as e:
            lines.append(f"• {j.get('title')}: нет данных ({e})")
    return "\n".join(lines)


# ───────────────────────── монтаж ─────────────────────────

def _job_files(job_id: str) -> dict:
    d = store.job_dir(job_id)
    return {"dir": d, "src": os.path.join(d, "source.mp4"), "words": os.path.join(d, "words.json"),
            "out": os.path.join(d, "final.mp4"), "cover": os.path.join(d, "cover.jpg")}


def _music_file(on: bool, job: dict | None = None):
    if not on:
        return None
    for f in ((job or {}).get("music_file"), store.load().get("music_file")):
        if f and os.path.exists(f):
            return f
    files = sorted(glob.glob(os.path.join(library.music_dir(), "*.mp3")) + glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    return files[0] if files else None


def _genre(j: dict) -> dict:
    return edits.GENRES.get(j.get("genre") or "talk", edits.GENRES["talk"])


def _job_flag(j: dict, key: str, default):
    """Настройка ролика: правка Полины, иначе жанр, иначе default."""
    if j.get(key) is not None:
        return j[key]
    v = _genre(j)["job"].get(key)
    if v is None:  # то, что автор попросил запомнить для всех роликов
        v = (store.load().get("defaults") or {}).get(key)
    return default if v is None else v


def _resolve_opts(j: dict) -> tuple[dict, list[str]]:
    """Настройки монтажа ролика с путями к звукам и эффектам (скачиваются при первой просьбе)."""
    # запомненное автором < настройки жанра < правки этого ролика
    remembered = {k: v for k, v in (store.load().get("defaults") or {}).items() if k in edits.OPT_KEYS}
    o = {**remembered, **_genre(j)["opts"], **(j.get("opts") or {})}
    warn = []
    snd = []
    for x in o.get("sounds") or []:
        try:
            snd.append({"phrase": x["phrase"], "file": library.sound_file(x["sound"]), "vol": 0.5})
        except Exception as e:
            log.exception("sound")
            warn.append(f"звук «{library.SOUNDS.get(x['sound'], ('', x['sound']))[1]}» не скачался ({e})")
    o["sounds"] = snd
    fx = []
    for x in o.get("effects") or []:
        name = x["effect"]
        secs = library.EFFECTS[name][3]
        if name in ("glow", "bw"):
            fx.append({"phrase": x["phrase"], "type": name, "secs": secs})
            continue
        try:
            fx.append({"phrase": x["phrase"], "type": "clip", "file": library.effect_file(name), "secs": secs})
        except Exception as e:
            log.exception("effect")
            warn.append(f"эффект «{library.EFFECTS[name][1]}» не скачался ({e})")
    o["effects"] = fx
    return o, warn


def _image_cards(j: dict, words: list[dict], warn: list[str]) -> list[dict]:
    """Картинки, которые попросили найти («картинку кота на слове кот»): скачиваем и ставим на слово."""
    from .edit import find_phrase
    out = []
    for x in j.get("images") or []:
        at = find_phrase(words, x["phrase"])
        if not at:
            warn.append(f"не нашла в ролике слово «{x['phrase']}» для картинки")
            continue
        idx = next(w["i"] for w in words if w["start"] >= at[0])
        try:
            out.append({"word": idx, "image": library.image_file(x.get("query") or x["phrase"])})
        except Exception as e:
            log.exception("image")
            warn.append(f"картинка «{x.get('query') or x['phrase']}» не нашлась ({e}); можно прислать фото "
                        f"с подписью «на слово {x['phrase']}»")
    return out


def _parse_hint(text: str | None) -> tuple[str | None, str | None]:
    """Из подписи к видео достаём «плашка: …», остальное идёт пожеланием к подписи."""
    if not text:
        return None, None
    badge = None
    rest = []
    for line in text.splitlines():
        low = line.lower().strip()
        if low.startswith("плашка"):
            badge = line.split(":", 1)[-1].strip() or None
        else:
            rest.append(line)
    return badge, "\n".join(rest).strip() or None


def _wants_plan(j: dict, opts: dict) -> bool:
    """Монтажный лист от Claude нужен там, где режем: рассказ и скетч; влог и «ничего не делай» идут целиком."""
    return opts.get("cut") != "none" and captions._client() is not None


def _plan_cards(plan: dict, words: list[dict], warn: list[str]) -> list[dict]:
    """Картинки из монтажного листа: ищем в фотобанке и ставим на слово."""
    have = {w["i"] for w in words}
    out = []
    for p in plan.get("pictures") or []:
        if p["word"] not in have:
            continue
        try:
            out.append({"word": p["word"], "image": library.image_file(p["query"])})
        except Exception as e:
            log.info("picture %s: %s", p["query"], e)
    if plan.get("pictures") and not out:
        warn.append("картинки из фотобанка не скачались")
    return out


def _build(job_id: str, new_caption: bool, hint: str | None = None):
    """Тяжёлая часть, выполняется в отдельном потоке."""
    j = store.load()["jobs"][job_id]
    f = _job_files(job_id)
    # номер слова в исходной расшифровке (i) сохраняем: по нему картинки встают на свои слова и после вырезаний
    words = [dict(w, i=k) for k, w in enumerate(load_or_transcribe(f["src"], f["words"]))]
    words = captions.remove_phrases(words, j.get("removed", []))
    if new_caption or not j.get("caption"):
        c = captions.make_caption(words, j.get("badge"), hint, cta=_job_flag(j, "cta", True), genre=j.get("genre"))
        cap = {"title": j.get("title_override") or c["title"], "caption": c["caption"],
               "hashtags": c["hashtags"], "rubric": c.get("rubric", ""), "caption_source": c["source"]}
    else:
        cap = {}
    title = cap.get("title") or j.get("title")
    opts, warn = _resolve_opts(j)

    # монтажный лист: что вырезать (дубли, оговорки), где наезды и картинки. Пишет Claude один раз на ролик,
    # правки потом меняют лист. Без ключа листа нет, и дубли ищет прежнее правило.
    plan = j.get("plan")
    if plan is None and _wants_plan(j, opts):
        plan = director.make_plan(words, j.get("genre") or "talk", j["style"], j.get("hint") or hint)
        if plan:
            store.update(lambda d: d["jobs"][job_id].__setitem__("plan", plan))
        else:
            warn.append("Claude не составил монтажный лист, дубли искала по простому правилу"
                        + (f" ({captions.LAST_ERROR})" if captions.LAST_ERROR else ""))
    use_plan = plan if plan and opts.get("cut") != "none" else None
    cut_words = words
    if use_plan:
        cuts = [c for c in use_plan.get("cuts") or [] if not (opts.get("keep_retakes") and c["kind"] == "retake")]
        cut_words = apply_plan(words, {"delete": [[c["from"], c["to"]] for c in cuts]})
        opts["keep_retakes"] = True  # дубли уже выбрал Claude
        opts["zoom_marks"] = use_plan.get("zooms") or []

    no_cards = _job_flag(j, "no_cards", False)
    extra = [c for c in j.get("extra_cards") or [] if os.path.exists(c["image"])] + _image_cards(j, words, warn)
    plan_pics = _plan_cards(use_plan, cut_words, warn) if use_plan and not no_cards else []
    if j.get("music") and not _music_file(True, j):
        warn.append("музыки пока нет: пришлите боту mp3-файл, и я поставлю его")
    tmp_out = f["out"] + ".tmp.mp4"

    def go(o):
        return render(f["src"], cut_words, tmp_out, style=j["style"], music=_music_file(j.get("music"), j),
                      badge=j.get("badge"), title=title, workdir=f["dir"], cards=bool(extra) or not no_cards,
                      auto_cards=not no_cards and not plan_pics,  # во влоге свои картинки показываем, а автоматические нет
                      accents=j.get("accents"), auto_accents=_job_flag(j, "auto_accents", True),
                      extra_cards=extra + plan_pics,
                      bright=bool(j.get("bright")), gentle=bool(j.get("gentle")), opts=o)
    info = go(opts)

    # самопроверка: Claude смотрит кадры у склеек и картинок; картинку на лице или на субтитрах переставляем
    issues = director.self_check(tmp_out, info, f["dir"])
    fixable = [x for x in issues if x["kind"] in ("card_on_face", "subs_under_card")]
    if fixable and info.get("card_pos") and not opts.get("card_pos"):
        cur = max(set(info["card_pos"]), key=info["card_pos"].count)
        o2 = dict(opts, card_pos="top" if cur != "top" else "bottom")
        info = go(o2)
        issues = [x for x in director.self_check(tmp_out, info, f["dir"]) if x["kind"] in director.ISSUE_KINDS]
    for x in issues:
        warn.append(f"проверка: {director.ISSUE_KINDS.get(x['kind'], x['kind'])} на {x['t']:.0f} с"
                    + (f" ({x['note']})" if x.get("note") else ""))
    os.replace(tmp_out, f["out"])
    _cover(j, f, title)
    director.save_edl(f["dir"], j, use_plan, info)
    line = director.plan_line(use_plan, info.get("source_seconds") or 0, info["duration"],
                              zooms=opts.get("zoom") != "off", pictures=len(plan_pics))

    def put(d):
        d["jobs"][job_id].update(cap)
        # ролик, стоявший в очереди, после правки остаётся в очереди
        status = "scheduled" if d["jobs"][job_id].get("publish_at") else "review"
        d["jobs"][job_id].update({"status": status, "duration": info["duration"],
                                  "cut_seconds": info["cut_seconds"], "warnings": warn, "plan_line": line})
    store.update(put)


def _cover(j: dict, f: dict, title: str | None):
    at = float(j.get("cover_at") or 0.8)
    try:
        make_cover(f["out"], j.get("cover_text") or title or "", f["cover"], at=at)
    except Exception:
        make_cover(f["out"], j.get("cover_text") or title or "", f["cover"])


def _kb(job_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="✅ В очередь", callback_data=f"q:{job_id}"), b(text="⚡ Выложить сейчас", callback_data=f"now:{job_id}")],
        [b(text="✏️ Поправить", callback_data=f"ed:{job_id}"), b(text="🎨 Другой стиль", callback_data=f"st:{job_id}")],
        [b(text="🎭 Другой жанр", callback_data=f"gq:{job_id}"), b(text="🗂 В архив", callback_data=f"ar:{job_id}")],
    ])


def _genre_kb(job_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="🎓 Рассказ", callback_data=f"g:talk:{job_id}")],
        [b(text="✈️ Влог, путешествие, зарисовка", callback_data=f"g:vlog:{job_id}")],
        [b(text="😂 Скетч, комедия", callback_data=f"g:sketch:{job_id}")],
    ])


GENRE_QUESTION = ("Что это за ролик?\n🎓 Рассказ: монтирую как ролики Полины про религии и историю, "
                  "с нарезкой, картинками и выделениями.\n✈️ Влог: ролик целиком, без вырезаний и картинок, "
                  "только субтитры и цвет.\n😂 Скетч: плотная нарезка, без картинок.")


TG_SEND_LIMIT = 48 * 1024 * 1024   # Telegram принимает от бота файлы до 50 МБ (свой сервер Bot API — до 2 ГБ)


def _small_copy(src: str, dst: str, seconds: float) -> str:
    """Лёгкая копия для показа в Telegram; полный ролик остаётся для Instagram и скачивания."""
    kbps = max(400, int(44 * 8 * 1024 / max(seconds, 1)) - 128)
    vf = [] if kbps >= 2500 else ["-vf", "scale=720:1280"]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, *vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst], check=True)
    return dst


async def _send_preview(chat_id: int, job_id: str):
    j = store.load()["jobs"][job_id]
    f = _job_files(job_id)
    video, full_link = f["out"], ""
    if not LOCAL_API and os.path.getsize(f["out"]) > TG_SEND_LIMIT:
        video = await asyncio.to_thread(_small_copy, f["out"], os.path.join(f["dir"], "preview.mp4"),
                                        float(j.get("duration") or 60))
        if PUBLIC_URL:
            full_link = f"\n\nВ Telegram ролик сжат, полное качество: {PUBLIC_URL}/media/{job_id}/{j['key']}/final.mp4"
    # без размеров Telegram иногда показывает вертикальный ролик квадратом
    await bot.send_video(chat_id, FSInputFile(video), cover=FSInputFile(f["cover"]),
                         supports_streaming=True, width=1080, height=1920, duration=int(round(j.get("duration") or 0)) or None,
                         caption=f"{_genre(j)['name']} · {STYLE_NAMES[j['style']]} стиль · "
                                 + (j.get("plan_line") or f"{j['duration']:.0f} с, вырезано {j['cut_seconds']:.0f} с пауз и оговорок"))
    text = (f"<b>{html.escape(j['title'])}</b>\n\n{html.escape(captions.full_caption(j))}")
    if j.get("caption_source") == "template":
        text += "\n\n<i>Подпись по шаблону: ключ Claude не подключён.</i>"
    for w in j.get("warnings") or []:
        text += f"\n⚠️ {html.escape(w)}"
    await bot.send_message(chat_id, text[:4000 - len(full_link)] + full_link, reply_markup=_kb(job_id))


@dp.message(Command("last"))
async def cmd_last(m: Message):
    """Прислать последний смонтированный ролик ещё раз (например, если отправка сорвалась)."""
    if not _allowed(m.from_user.id):
        return
    jobs = sorted((j for j in store.load()["jobs"].values() if j.get("chat_id") == m.chat.id
                   and os.path.exists(_job_files(j["id"])["out"]) and j.get("title")),
                  key=lambda j: j.get("created", 0), reverse=True)
    if not jobs:
        await m.answer("Готовых роликов пока нет.")
        return
    j = jobs[0]
    if j["status"] == "failed":
        store.update(lambda d: d["jobs"][j["id"]].__setitem__("status", "review"))
    await _send_preview(m.chat.id, j["id"])


async def _process(chat_id: int, job_id: str, new_caption: bool, hint: str | None = None):
    status = await bot.send_message(chat_id, "Монтирую… обычно это занимает несколько минут.")
    try:
        async with _work_lock:
            await asyncio.to_thread(_build, job_id, new_caption, hint)
        await _send_preview(chat_id, job_id)
    except Exception as e:
        log.exception("build failed")
        store.update(lambda d: d["jobs"][job_id].__setitem__("status", "failed"))
        await bot.send_message(chat_id, f"Не получилось смонтировать: {html.escape(str(e))[:500]}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


def _new_job_id() -> str:
    return dt.datetime.now(MSK).strftime("%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]


async def _start_job(chat_id: int, job_id: str, caption: str | None):
    """Исходник уже лежит в папке ролика: заводим задание, спрашиваем жанр или сразу монтируем."""
    badge, hint = _parse_hint(caption)
    job = {"id": job_id, "chat_id": chat_id, "status": "processing", "style": store.load()["style"],
           "badge": badge, "music": bool(_music_file(True)), "removed": [], "key": secrets.token_urlsafe(12),
           "created": time.time()}
    if "music" in (store.load().get("defaults") or {}):  # автор попросил запомнить «без музыки» / «с музыкой»
        job["music"] = bool(store.load()["defaults"]["music"]) and job["music"]
    job["hint"] = hint
    genre = next((g for g, pat in edits.GENRE_WORDS if hint and re.search(pat, hint.lower())), None)
    if genre:
        job["genre"] = genre
    else:
        job["status"] = "waiting"
    store.update(lambda d: d["jobs"].__setitem__(job_id, job))
    if not genre:
        await bot.send_message(chat_id, GENRE_QUESTION, reply_markup=_genre_kb(job_id))
        return
    await _process(chat_id, job_id, new_caption=True, hint=hint)


def _upload_link(chat_id: int) -> str | None:
    """Ссылка для загрузки большого видео прямо на сервер (работает сутки)."""
    if not PUBLIC_URL:
        return None
    token = secrets.token_urlsafe(16)

    def put(d):
        ups = d.setdefault("uploads", {})
        for t in [t for t, u in ups.items() if time.time() - u["created"] > 86400]:
            ups.pop(t)
        ups[token] = {"chat": chat_id, "created": time.time()}
    store.update(put)
    return f"{PUBLIC_URL}/up/{token}"


@dp.message(Command("upload"))
async def cmd_upload(m: Message):
    if not _allowed(m.from_user.id):
        return
    link = _upload_link(m.chat.id)
    if not link:
        await m.answer("Адрес сервера не настроен: запустите установку ещё раз.")
        return
    await m.answer(f"Загрузите видео по ссылке (без ограничения размера, ссылка работает сутки):\n{link}")


@dp.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
async def on_video(m: Message):
    if not _allowed(m.from_user.id):
        return
    media = m.video or m.video_note or m.document
    if media.file_size and media.file_size > 20 * 1024 * 1024 and not LOCAL_API:
        link = _upload_link(m.chat.id)
        await m.answer(f"Видео {media.file_size / 1048576:.0f} МБ, а Telegram отдаёт ботам файлы только до 20 МБ. "
                       + (f"Загрузите его по этой ссылке, там ограничения нет:\n{link}\n\n"
                          "Или пришлите видео сюда со сжатием (не файлом)." if link else
                          "Пришлите его со сжатием (не файлом) или покороче."))
        return
    job_id = _new_job_id()
    f = _job_files(job_id)
    await bot.download(media, destination=f["src"])
    await _start_job(m.chat.id, job_id, m.caption)


# ───────────────────────── кнопки ─────────────────────────

def _next_slot() -> float:
    hh, mm = map(int, PUBLISH_TIME.split(":"))
    taken = {dt.datetime.fromtimestamp(j["publish_at"], MSK).date()
             for j in store.load()["jobs"].values() if j["status"] == "scheduled" and j.get("publish_at")}
    now = dt.datetime.now(MSK)
    day = now.date()
    while True:
        slot = dt.datetime.combine(day, dt.time(hh, mm), MSK)
        if slot > now + dt.timedelta(minutes=10) and day not in taken:
            return slot.timestamp()
        day += dt.timedelta(days=1)


def _fmt_time(ts: float) -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    t = dt.datetime.fromtimestamp(ts, MSK)
    return f"{t.day} {months[t.month - 1]} в {t:%H:%M}"


# ───────────────────────── память стиля ─────────────────────────
# Если одну и ту же правку просят в разных роликах, бот предлагает запомнить её для всех следующих.
HABIT_OPT_KEYS = ["cut", "speed", "zoom", "color", "sub_size", "sub_pos", "sub_case", "sub_color", "sub_font", "sub_words",
                  "card_scale", "card_pos", "card_secs", "music_vol", "voice_vol", "denoise", "end_card", "title_on"]
HABIT_JOB_KEYS = ["music", "no_cards", "auto_accents", "cta"]


def _habits(job_id: str, e: dict) -> list[tuple[str, str]]:
    """Отметить правку; вернуть [(id, текст)] привычек, которые пора предложить запомнить (2+ разных ролика)."""
    offer = []

    def upd(d):
        hs = d.setdefault("habits", {})
        for k in HABIT_OPT_KEYS + HABIT_JOB_KEYS:
            v = e.get(k)
            if v is None or k in (d.get("defaults") or {}) and d["defaults"][k] == v:
                continue
            one = edits.new_edit()
            one[k] = v
            text = "; ".join(edits.summary(one))
            if not text:
                continue
            hid = hashlib.sha1(json.dumps([k, v], ensure_ascii=False).encode()).hexdigest()[:10]
            h = hs.setdefault(hid, {"key": k, "value": v, "text": text, "jobs": [], "state": None})
            if job_id not in h["jobs"]:
                h["jobs"].append(job_id)
            if len(h["jobs"]) >= 2 and h["state"] is None:
                h["state"] = "offered"
                offer.append((hid, text))
    store.update(upd)
    return offer


@dp.callback_query(F.data.startswith("rem:") | F.data.startswith("nrem:"))
async def on_remember(cq: CallbackQuery):
    if not _allowed(cq.from_user.id):
        await cq.answer()
        return
    action, hid = cq.data.split(":", 1)
    h = store.load().get("habits", {}).get(hid)
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=None)
    if not h:
        return
    if action == "nrem":
        store.update(lambda d: d["habits"][hid].__setitem__("state", "no"))
        await cq.message.answer("Хорошо, не запоминаю.")
        return

    def save(d):
        d["habits"][hid]["state"] = "saved"
        d.setdefault("defaults", {})[h["key"]] = h["value"]
    store.update(save)
    director.remember(f"Всегда: {h['text']}.")
    await cq.message.answer(f"Запомнила: {html.escape(h['text'])} — во всех следующих роликах. "
                            "Забыть всё запомненное: /forget")


@dp.message(Command("forget"))
async def cmd_forget(m: Message):
    if not _allowed(m.from_user.id):
        return
    store.update(lambda d: (d.__setitem__("defaults", {}), d.__setitem__("habits", {})))
    try:
        os.remove(director.notes_path())
    except FileNotFoundError:
        pass
    await m.answer("Забыла все запомненные настройки: новые ролики будут монтироваться по исходному стилю.")


@dp.callback_query(F.data.contains(":"))
async def on_button(cq: CallbackQuery):
    if not _allowed(cq.from_user.id):
        await cq.answer()
        return
    action, job_id = cq.data.split(":", 1)
    genre = None
    if action == "g":
        genre, job_id = job_id.split(":", 1)
    j = store.load()["jobs"].get(job_id)
    if not j:
        await cq.answer("Ролик не найден")
        return
    if j["status"] in ("published", "publishing") and action != "ar":
        await cq.answer("Этот ролик уже опубликован")
        return
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=None)

    if action == "g":
        first = j["status"] == "waiting"
        e = edits.new_edit()
        e["genre"] = genre

        def setg(d):
            jj = d["jobs"][job_id]
            edits.apply(jj, e)
            jj["genre"] = genre
            jj["status"] = "processing"
        store.update(setg)
        await cq.message.answer(f"Монтирую как {edits.GENRES[genre]['name']}.")
        await _process(cq.message.chat.id, job_id, new_caption=True, hint=j.get("hint") if first else None)
    elif action == "gq":
        await cq.message.answer(GENRE_QUESTION, reply_markup=_genre_kb(job_id))
        await cq.message.edit_reply_markup(reply_markup=_kb(job_id))
    elif action == "ed":
        _awaiting_edit[cq.message.chat.id] = job_id
        await cq.message.answer(EDIT_PROMPT)
    elif action == "st":
        new = {"polina": "night", "night": "paper", "paper": "polina"}.get(j["style"], "polina")
        store.update(lambda d: d["jobs"][job_id].update({"style": new, "status": "processing"}))
        await _process(cq.message.chat.id, job_id, new_caption=False)
    else:
        await _do_action(cq.message.chat.id, job_id, {"q": "queue", "now": "now", "ar": "archive"}[action])


EDIT_PROMPT = ("Напишите или наговорите голосом, что поправить, можно несколько правок сразу. Например: "
               "«сделай динамичнее и ярче», «вырежи фразу про погоду», «субтитры крупнее и снизу», "
               "«звук бум на слове „казнили“», «без музыки», «перепиши подпись короче», «выложи завтра в 12». "
               "Новую картинку пришлите фото с подписью «на слово …». Все варианты: /edits")


async def _do_action(chat_id: int, job_id: str, action: str, when: str | None = None):
    """В очередь / выложить сейчас / в архив / снять с публикации."""
    if action == "queue":
        ts = None
        if when:
            try:
                ts = dt.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=MSK).timestamp()
            except ValueError:
                ts = None
            if ts and ts < time.time():
                ts = None
        ts = ts or _next_slot()
        store.update(lambda d: d["jobs"][job_id].update({"status": "scheduled", "publish_at": ts}))
        extra = "" if instagram.connected() else "\nInstagram ещё не подключён: /instagram"
        await bot.send_message(chat_id, f"Поставила в очередь на {_fmt_time(ts)} МСК ✅{extra}")
    elif action == "now":
        store.update(lambda d: d["jobs"][job_id].update({"status": "scheduled", "publish_at": time.time()}))
        await bot.send_message(chat_id, "Публикую…")
        await _publish_due(force_id=job_id)
    elif action == "archive":
        store.update(lambda d: d["jobs"][job_id].update({"status": "archived", "publish_at": None}))
        await bot.send_message(chat_id, "Убрала в архив. Видео сохранено на сервере.")
    elif action == "cancel":
        store.update(lambda d: d["jobs"][job_id].update({"status": "review", "publish_at": None}))
        await bot.send_message(chat_id, "Сняла с публикации, ролик ждёт решения.", reply_markup=_kb(job_id))


def _target_job(chat_id: int) -> str | None:
    """Ролик, к которому относится правка: после «Поправить» или последний ждущий решения в этом чате."""
    job_id = _awaiting_edit.pop(chat_id, None)
    if job_id:
        return job_id
    jobs = [j for j in store.load()["jobs"].values()
            if j.get("chat_id") == chat_id and j["status"] in ("review", "scheduled", "failed", "waiting")
            and time.time() - j.get("created", 0) < 3 * 86400]
    return max(jobs, key=lambda j: j.get("created", 0))["id"] if jobs else None


@dp.message(Command("edits"))
async def cmd_edits(m: Message):
    if _allowed(m.from_user.id):
        await m.answer(html.escape(edits.CATALOG))


@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_photo(m: Message):
    """Своя картинка в ролик: фото с подписью «на слово луна»."""
    if not _allowed(m.from_user.id):
        return
    job_id = _target_job(m.chat.id)
    if not job_id:
        await m.answer("Сначала пришлите видео, потом картинку к нему с подписью «на слово …».")
        return
    phrase = edits._target(m.caption or "") or (m.caption or "").strip()
    if not phrase:
        _awaiting_edit[m.chat.id] = job_id
        await m.answer("Подпишите фото, на каком слове его показать, например «на слово луна», и пришлите ещё раз.")
        return
    f = _job_files(job_id)
    words = load_or_transcribe(f["src"], f["words"])
    from .edit import find_phrase
    indexed = [dict(w, i=k) for k, w in enumerate(words)]
    at = find_phrase(indexed, phrase)
    if not at:
        _awaiting_edit[m.chat.id] = job_id
        await m.answer(f"Не нашла в ролике слово «{html.escape(phrase)}». Напишите его так, как оно звучит, "
                       "и пришлите фото ещё раз.")
        return
    idx = next(w["i"] for w in indexed if w["start"] >= at[0])
    path = os.path.join(f["dir"], f"photo_{m.message_id}.jpg")
    await bot.download(m.photo[-1] if m.photo else m.document, destination=path)
    store.update(lambda d: d["jobs"][job_id].update(
        {"extra_cards": (d["jobs"][job_id].get("extra_cards") or []) + [{"word": idx, "image": path}],
         "status": "processing"}))
    await m.answer(f"Хорошо: покажу картинку на «{html.escape(phrase)}».")
    await _process(m.chat.id, job_id, new_caption=False)


@dp.message(F.audio | (F.document & F.document.mime_type.startswith("audio/")))
async def on_audio(m: Message):
    """Своя музыка: mp3-файлом. Ставится в последний ролик и запоминается для следующих."""
    if not _allowed(m.from_user.id):
        return
    media = m.audio or m.document
    name = library.safe_name(getattr(media, "file_name", None) or f"track_{m.message_id}.mp3")
    if not name.lower().endswith(".mp3"):
        name += ".mp3"
    path = os.path.join(library.music_dir(), name)
    await bot.download(media, destination=path)
    store.update(lambda d: d.__setitem__("music_file", path))
    job_id = _target_job(m.chat.id)
    if not job_id:
        await m.answer("Музыку сохранила, она будет в следующих роликах.")
        return
    store.update(lambda d: d["jobs"][job_id].update({"music_file": path, "music": True, "status": "processing"}))
    await m.answer("Хорошо: ставлю эту музыку в ролик и запоминаю для следующих.")
    await _process(m.chat.id, job_id, new_caption=False)


@dp.message(F.text | F.voice)
async def on_text(m: Message):
    if not _allowed(m.from_user.id):
        return
    if m.text and m.text.startswith("/"):
        await m.answer("Такой команды нет. Список: /help, все правки: /edits")
        return
    job_id = _target_job(m.chat.id)
    if not job_id:
        await m.answer("Пришлите видео, и я его смонтирую. Список команд: /help")
        return
    if m.voice:
        path = os.path.join(_job_files(job_id)["dir"], f"edit_{m.message_id}.ogg")
        await bot.download(m.voice, destination=path)
        async with _work_lock:
            words = await asyncio.to_thread(transcribe, path)
        request = " ".join(w["word"] for w in words)
        await m.answer(f"Поняла так: «{html.escape(request)}»")
    else:
        request = m.text
    f = _job_files(job_id)
    j = store.load()["jobs"][job_id]
    words = load_or_transcribe(f["src"], f["words"])
    words = captions.remove_phrases(words, j.get("removed", []))
    e = await asyncio.to_thread(captions.parse_edit, request, words, j)
    if e.get("fallback"):
        log.warning("Claude не разобрал правку: %s", e["fallback"])
        await m.answer("⚠️ Claude сейчас не ответил, разбираю правку упрощённо "
                       f"(причина: {html.escape(e['fallback'][:200])}).")
    flags = {}
    store.update(lambda d: flags.update(edits.apply(d["jobs"][job_id], e)))
    if e.get("restore_phrases") and j.get("plan"):
        # «оставь кусок про Якова»: убираем вырезку из монтажного листа
        indexed = [dict(w, i=k) for k, w in enumerate(load_or_transcribe(f["src"], f["words"]))]
        plan = j["plan"]
        if director.restore(plan, indexed, e["restore_phrases"]):
            store.update(lambda d: d["jobs"][job_id].__setitem__("plan", plan))
            flags["render"] = True

    said = edits.summary(e)
    if j["status"] == "waiting":
        # жанр ещё не выбран: из этого сообщения берём жанр и пожелания, остальное — рассказ
        store.update(lambda d: d["jobs"][job_id].update({"genre": d["jobs"][job_id].get("genre") or "talk",
                                                         "status": "processing"}))
        g = store.load()["jobs"][job_id]["genre"]
        await m.answer(f"Монтирую как {edits.GENRES[g]['name']}" + (": " + html.escape("; ".join(said)) if said else "") + ".")
        await _process(m.chat.id, job_id, new_caption=True, hint=" ".join(x for x in (j.get("hint"), request) if x))
        return
    if said:
        await m.answer("Хорошо: " + html.escape("; ".join(said)) + ".")
    if e.get("unsupported"):
        await m.answer("Пока не умею: " + html.escape("; ".join(e["unsupported"])) + ".")
    if not said:
        _awaiting_edit[m.chat.id] = job_id
        if not e.get("unsupported"):
            await m.answer("Не поняла, что поменять. Скажите иначе или посмотрите примеры: /edits")
        return

    if flags["render"]:
        store.update(lambda d: d["jobs"][job_id].__setitem__("status", "processing"))
        await _process(m.chat.id, job_id, new_caption=flags["recaption"], hint=request if flags["recaption"] else None)
    elif flags["recaption"]:
        jj = store.load()["jobs"][job_id]
        c = await asyncio.to_thread(captions.make_caption, words, jj.get("badge"), request,
                                    _job_flag(jj, "cta", True), jj.get("genre"))
        store.update(lambda d: d["jobs"][job_id].update({"caption": c["caption"], "hashtags": c["hashtags"],
                                                         "caption_source": c["source"]}))
        await _send_preview(m.chat.id, job_id)
    elif flags["cover"]:
        jj = store.load()["jobs"][job_id]
        await asyncio.to_thread(_cover, jj, f, jj.get("title"))
        await _send_preview(m.chat.id, job_id)
    elif not flags["publish"]:
        await _send_preview(m.chat.id, job_id)
    if flags["publish"] and store.load()["jobs"][job_id]["status"] != "failed":
        await _do_action(m.chat.id, job_id, flags["publish"], flags.get("when"))
    for hid, text in _habits(job_id, e):
        b = InlineKeyboardButton
        await m.answer(f"Вы уже не в первый раз просите: {html.escape(text)}. Запомнить для всех следующих роликов?",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                           b(text="📌 Запомнить", callback_data=f"rem:{hid}"),
                           b(text="Не надо", callback_data=f"nrem:{hid}")]]))


# ───────────────────────── публикация по расписанию ─────────────────────────

async def _notify(text: str):
    for uid in store.load()["allowed"]:
        try:
            await bot.send_message(uid, text)
        except Exception:
            log.exception("notify failed")


async def _publish_due(force_id: str | None = None):
    st = store.load()
    if not instagram.connected():
        if force_id:
            await _notify("Instagram ещё не подключён: /instagram")
        return
    if st["paused"] and not force_id:
        return
    now = time.time()
    due = [j for j in st["jobs"].values() if j["status"] == "scheduled" and j.get("publish_at")
           and (j["publish_at"] <= now or j["id"] == force_id)]
    for j in due:
        job_id = j["id"]
        store.update(lambda d: d["jobs"][job_id].__setitem__("status", "publishing"))
        base = f"{PUBLIC_URL}/media/{job_id}/{j['key']}"
        try:
            media_id, link = await asyncio.to_thread(
                instagram.publish_reel, f"{base}/final.mp4", captions.full_caption(j)[:2200], f"{base}/cover.jpg")
        except Exception as e:
            log.exception("publish failed")
            store.update(lambda d: d["jobs"][job_id].update({"status": "scheduled", "publish_at": now + 3600}))
            await _notify(f"Не удалось выложить «{html.escape(j['title'])}»: {html.escape(str(e))[:300]}\n"
                          f"Попробую ещё раз через час.")
            continue
        store.update(lambda d: d["jobs"][job_id].update(
            {"status": "published", "media_id": media_id, "permalink": link, "published_at": time.time()}))
        await _notify(f"Опубликовано: «{html.escape(j['title'])}» 🎉\n{link or ''}")


async def _housekeeping():
    st = store.load()
    s = st["secrets"]
    if s.get("ig_token") and s.get("ig_token_expires", 0) - time.time() < 30 * 86400 \
            and time.time() - st.get("last_refresh", 0) > 86400:
        store.update(lambda d: d.__setitem__("last_refresh", time.time()))
        try:
            await asyncio.to_thread(instagram.refresh_token)
        except Exception as e:
            await _notify(f"Не получилось продлить доступ к Instagram ({html.escape(str(e))}). Нажмите /instagram")
    now = dt.datetime.now(MSK)
    week = now.strftime("%G-%V")
    if now.weekday() == 6 and now.hour == 20 and st.get("last_report") != week and instagram.connected():
        store.update(lambda d: d.__setitem__("last_report", week))
        try:
            await _notify("Итоги недели\n" + await asyncio.to_thread(_stats_text))
        except Exception:
            log.exception("weekly report failed")
    # Исходники старше 60 дней удаляем, чтобы не забить диск; готовые ролики остаются.
    for j in st["jobs"].values():
        if j["status"] in ("published", "archived") and time.time() - j.get("created", 0) > 60 * 86400:
            src = _job_files(j["id"])["src"]
            if os.path.exists(src):
                os.remove(src)


async def scheduler():
    while True:
        try:
            await _publish_due()
            await _housekeeping()
        except Exception:
            log.exception("scheduler")
        await asyncio.sleep(60)


# ───────────────────────── веб: видео для Instagram и вход в Instagram ─────────────────────────

async def web_media(req: web.Request):
    job_id, key, name = req.match_info["job"], req.match_info["key"], req.match_info["name"]
    j = store.load()["jobs"].get(job_id)
    if not j or not secrets.compare_digest(j.get("key", ""), key) or name not in ("final.mp4", "cover.jpg"):
        raise web.HTTPNotFound()
    return web.FileResponse(os.path.join(_job_files(job_id)["dir"], name))


UPLOAD_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Видео для фабрики</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#15122A;color:#F3EDE3;margin:0;padding:24px;max-width:520px}
h1{font-size:22px;font-weight:600}label{display:block;margin:18px 0 6px}input,textarea,button{width:100%;box-sizing:border-box;
font-size:17px;border-radius:10px;border:1px solid #CFA85F;padding:12px;background:#221d3d;color:#F3EDE3}
button{background:#CFA85F;color:#15122A;font-weight:600;margin-top:20px}button:disabled{opacity:.5}
#bar{height:10px;background:#221d3d;border-radius:5px;margin-top:18px;overflow:hidden}#fill{height:100%;width:0;background:#CFA85F}
#msg{margin-top:12px;min-height:24px}</style></head><body>
<h1>Видео для фабрики</h1>
<label>Видео</label><input type="file" id="file" accept="video/*">
<label>Пожелания (необязательно): жанр, «плашка: …», что важно</label><textarea id="cap" rows="3"></textarea>
<button id="go">Отправить</button><div id="bar"><div id="fill"></div></div><div id="msg"></div>
<script>
const go=document.getElementById('go'),msg=document.getElementById('msg'),fill=document.getElementById('fill');
go.onclick=()=>{const f=document.getElementById('file').files[0];if(!f){msg.textContent='Выберите видео';return}
const fd=new FormData();fd.append('caption',document.getElementById('cap').value);fd.append('file',f);
const x=new XMLHttpRequest();x.open('POST',location.href);go.disabled=true;
x.upload.onprogress=e=>{if(e.lengthComputable){const p=Math.round(e.loaded/e.total*100);fill.style.width=p+'%';
msg.textContent='Загружаю… '+p+'%'}};
x.onload=()=>{msg.textContent=x.status==200?'Готово! Возвращайтесь в Telegram, бот уже взялся за ролик.':'Не получилось: '+x.responseText;
go.disabled=x.status==200};x.onerror=()=>{msg.textContent='Связь прервалась, попробуйте ещё раз';go.disabled=false};x.send(fd)};
</script></body></html>"""


def _upload_token(req: web.Request) -> dict | None:
    u = (store.load().get("uploads") or {}).get(req.match_info["token"])
    if not u or time.time() - u["created"] > 86400:
        return None
    return u


async def web_upload_page(req: web.Request):
    if not _upload_token(req):
        return web.Response(text="Ссылка устарела. Попросите новую в боте: /upload", content_type="text/plain", charset="utf-8")
    return web.Response(text=UPLOAD_PAGE, content_type="text/html", charset="utf-8")


async def web_upload(req: web.Request):
    """Приём большого видео по ссылке из бота: файл пишется на диск кусками, потом обычный монтаж."""
    u = _upload_token(req)
    if not u:
        return web.Response(status=403, text="Ссылка устарела. Попросите новую в боте: /upload")
    job_id = _new_job_id()
    f = _job_files(job_id)
    caption, size = None, 0
    reader = await req.multipart()
    async for part in reader:
        if part.name == "caption":
            caption = (await part.text()).strip() or None
        elif part.name == "file":
            with open(f["src"], "wb") as out:
                while True:
                    chunk = await part.read_chunk(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    size += len(chunk)
    if size < 1000:
        return web.Response(status=400, text="Файл не дошёл, попробуйте ещё раз")
    await bot.send_message(u["chat"], f"Видео получено по ссылке ({size / 1048576:.0f} МБ).")
    asyncio.create_task(_start_job(u["chat"], job_id, caption))
    return web.Response(text="ok")


async def web_callback(req: web.Request):
    st = store.load().get("oauth_state") or {}
    if not st or req.query.get("state") != st.get("state"):
        return web.Response(text="Ссылка устарела. Нажмите /instagram в боте ещё раз.", content_type="text/plain")
    store.update(lambda d: d.pop("oauth_state", None))
    if "code" not in req.query:
        await bot.send_message(st["chat"], "Вход в Instagram отменён.")
        return web.Response(text="Отменено.", content_type="text/plain")
    try:
        me = await asyncio.to_thread(instagram.exchange_code, req.query["code"], f"{PUBLIC_URL}/ig/callback")
    except Exception as e:
        await bot.send_message(st["chat"], f"Instagram не подключился: {html.escape(str(e))}")
        return web.Response(text="Не получилось, подробности в боте.", content_type="text/plain")
    await bot.send_message(st["chat"], f"Instagram @{me.get('username')} подключён ✅")
    return web.Response(text="Готово! Можно закрыть страницу и вернуться в Telegram.",
                        content_type="text/plain", charset="utf-8")


async def main():
    global bot
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API, is_local=True)) if LOCAL_API else None
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    os.makedirs(store.DATA, exist_ok=True)
    st = store.load()
    if not st["allowed"] and not st.get("pair_code"):
        code = _new_pair_code()
        log.info("Код привязки: %s", code)
    # Публикацию, прерванную перезапуском, не повторяем сами (вдруг она уже вышла): возвращаем на решение.
    stuck = [j["id"] for j in st["jobs"].values() if j["status"] in ("publishing", "processing")]
    store.update(lambda d: [d["jobs"][i].__setitem__("status", "review") for i in stuck
                            if d["jobs"][i]["status"] == "publishing"])

    app = web.Application(client_max_size=8 * 1024 ** 3)
    app.router.add_get("/up/{token}", web_upload_page)
    app.router.add_post("/up/{token}", web_upload)
    app.router.add_get("/media/{job}/{key}/{name}", web_media)
    app.router.add_get("/ig/callback", web_callback)
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", WEB_PORT).start()

    asyncio.create_task(scheduler())
    for i in stuck:
        j = store.load()["jobs"][i]
        if j["status"] == "processing":
            asyncio.create_task(_process(j["chat_id"], i, new_caption=not j.get("caption")))
        else:
            await bot.send_message(j["chat_id"], f"Сервер перезапускался во время публикации «{html.escape(j.get('title') or '')}». "
                                   "Проверьте Instagram: если ролика там нет, нажмите кнопку ещё раз.", reply_markup=_kb(i))
    me = await bot.get_me()
    log.info("Бот @%s запущен", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
