"""Telegram-бот «Фабрика»: Полина присылает видео → монтаж, субтитры, подпись → согласование → Instagram.

Запуск: python -m factory.bot  (настройки в переменных окружения, см. install.sh)
"""
import asyncio
import datetime as dt
import glob
import html
import logging
import os
import secrets
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

from . import captions, instagram, store
from .edit import make_cover, render
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
STATUS_NAMES = {"processing": "монтируется", "review": "ждёт решения", "scheduled": "в очереди",
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
    "Пришлите видео (вертикальное, со звуком). Я смонтирую его: вырежу паузы и оговорки, "
    "добавлю зумы, картинки, субтитры, обложку и подпись, и пришлю вам на проверку.\n\n"
    "В подписи к видео можно написать пожелание, например «плашка: 14-е лунные сутки» "
    "или «тема: руны на неделю».\n\n"
    "/plan очередь публикаций\n/stats статистика Instagram\n/style стиль по умолчанию: как у Полины, ночной или светлый\n"
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


def _music_file(on: bool):
    if not on:
        return None
    files = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    return files[0] if files else None


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


def _build(job_id: str, new_caption: bool, hint: str | None = None):
    """Тяжёлая часть, выполняется в отдельном потоке."""
    j = store.load()["jobs"][job_id]
    f = _job_files(job_id)
    words = load_or_transcribe(f["src"], f["words"])
    words = captions.remove_phrases(words, j.get("removed", []))
    if new_caption or not j.get("caption"):
        c = captions.make_caption(words, j.get("badge"), hint)
        cap = {"title": j.get("title_override") or c["title"], "caption": c["caption"],
               "hashtags": c["hashtags"], "rubric": c.get("rubric", ""), "caption_source": c["source"]}
    else:
        cap = {}
    title = cap.get("title") or j.get("title")
    tmp_out = f["out"] + ".tmp.mp4"
    info = render(f["src"], words, tmp_out, style=j["style"], music=_music_file(j.get("music")),
                  badge=j.get("badge"), title=title, workdir=f["dir"], cards=not j.get("no_cards"),
                  accents=j.get("accents"), auto_accents=j.get("auto_accents", True))
    os.replace(tmp_out, f["out"])
    make_cover(f["out"], title, f["cover"])

    def put(d):
        d["jobs"][job_id].update(cap)
        d["jobs"][job_id].update({"status": "review", "duration": info["duration"],
                                  "cut_seconds": info["cut_seconds"]})
    store.update(put)


def _kb(job_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [b(text="✅ В очередь", callback_data=f"q:{job_id}"), b(text="⚡ Выложить сейчас", callback_data=f"now:{job_id}")],
        [b(text="✏️ Поправить", callback_data=f"ed:{job_id}"), b(text="🎨 Другой стиль", callback_data=f"st:{job_id}")],
        [b(text="🗂 В архив", callback_data=f"ar:{job_id}")],
    ])


async def _send_preview(chat_id: int, job_id: str):
    j = store.load()["jobs"][job_id]
    f = _job_files(job_id)
    await bot.send_video(chat_id, FSInputFile(f["out"]), cover=FSInputFile(f["cover"]),
                         supports_streaming=True,
                         caption=f"{STYLE_NAMES[j['style']]} стиль · {j['duration']:.0f} с · "
                                 f"вырезано {j['cut_seconds']:.0f} с пауз и оговорок")
    text = (f"<b>{html.escape(j['title'])}</b>\n\n{html.escape(captions.full_caption(j))}")
    if j.get("caption_source") == "template":
        text += "\n\n<i>Подпись по шаблону: ключ Claude не подключён.</i>"
    await bot.send_message(chat_id, text[:4000], reply_markup=_kb(job_id))


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


@dp.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
async def on_video(m: Message):
    if not _allowed(m.from_user.id):
        return
    media = m.video or m.video_note or m.document
    if media.file_size and media.file_size > 20 * 1024 * 1024 and not LOCAL_API:
        await m.answer("Видео больше 20 МБ, а Telegram не отдаёт ботам такие файлы без локального сервера. "
                       "Пока сожмите видео или пришлите покороче; локальный сервер включается в установщике.")
        return
    job_id = dt.datetime.now(MSK).strftime("%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]
    f = _job_files(job_id)
    await bot.download(media, destination=f["src"])
    badge, hint = _parse_hint(m.caption)
    job = {"id": job_id, "chat_id": m.chat.id, "status": "processing", "style": store.load()["style"],
           "badge": badge, "music": bool(_music_file(True)), "removed": [], "key": secrets.token_urlsafe(12),
           "created": time.time()}
    store.update(lambda d: d["jobs"].__setitem__(job_id, job))
    await _process(m.chat.id, job_id, new_caption=True, hint=hint)


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


@dp.callback_query(F.data.contains(":"))
async def on_button(cq: CallbackQuery):
    if not _allowed(cq.from_user.id):
        await cq.answer()
        return
    action, job_id = cq.data.split(":", 1)
    j = store.load()["jobs"].get(job_id)
    if not j:
        await cq.answer("Ролик не найден")
        return
    if j["status"] in ("published", "publishing") and action != "ar":
        await cq.answer("Этот ролик уже опубликован")
        return
    await cq.answer()
    await cq.message.edit_reply_markup(reply_markup=None)

    if action == "q":
        when = _next_slot()
        store.update(lambda d: d["jobs"][job_id].update({"status": "scheduled", "publish_at": when}))
        extra = "" if instagram.connected() else "\nInstagram ещё не подключён: /instagram"
        await cq.message.answer(f"Поставила в очередь на {_fmt_time(when)} МСК ✅{extra}")
    elif action == "now":
        store.update(lambda d: d["jobs"][job_id].update({"status": "scheduled", "publish_at": time.time()}))
        await cq.message.answer("Публикую…")
        await _publish_due(force_id=job_id)
    elif action == "ed":
        _awaiting_edit[cq.message.chat.id] = job_id
        await cq.message.answer("Напишите или наговорите голосом, что поправить. Например: "
                                "«вырежи фразу про погоду», «заголовок: Руны на неделю», «светлый стиль», "
                                "«выдели слово „новолуние“ красным», "
                                "«без картинок», «перепиши подпись короче».")
    elif action == "st":
        new = {"polina": "night", "night": "paper", "paper": "polina"}.get(j["style"], "polina")
        store.update(lambda d: d["jobs"][job_id].update({"style": new, "status": "processing"}))
        await _process(cq.message.chat.id, job_id, new_caption=False)
    elif action == "ar":
        store.update(lambda d: d["jobs"][job_id].update({"status": "archived", "publish_at": None}))
        await cq.message.answer("Убрала в архив. Видео сохранено на сервере.")


@dp.message(F.text | F.voice)
async def on_text(m: Message):
    if not _allowed(m.from_user.id):
        return
    job_id = _awaiting_edit.pop(m.chat.id, None)
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
    e = await asyncio.to_thread(captions.parse_edit, request, words, j)

    def apply(d):
        jj = d["jobs"][job_id]
        if e.get("title"):
            jj["title"] = jj["title_override"] = e["title"]
        if e.get("caption"):
            jj["caption"] = e["caption"]
        for k in ("style", "badge"):
            if e.get(k):
                jj[k] = e[k]
        if e.get("music") is not None:
            jj["music"] = e["music"]
        if e.get("no_cards") is not None:
            jj["no_cards"] = e["no_cards"]
        if e.get("auto_accents") is not None:
            jj["auto_accents"] = e["auto_accents"]
        jj["accents"] = jj.get("accents", []) + e.get("accents", [])
        jj["removed"] = jj.get("removed", []) + e.get("remove_phrases", [])
        jj["status"] = "processing"
    store.update(apply)
    if e.get("note"):
        await m.answer(html.escape(e["note"]))
    only_text = not any(e.get(k) is not None and e.get(k) != [] for k in
                        ("title", "style", "badge", "music", "no_cards", "remove_phrases", "accents", "auto_accents"))
    if only_text and e.get("caption"):
        store.update(lambda d: d["jobs"][job_id].__setitem__("status", "review"))
        await _send_preview(m.chat.id, job_id)
    elif only_text:
        store.update(lambda d: d["jobs"][job_id].__setitem__("status", "review"))
        _awaiting_edit[m.chat.id] = job_id
        await m.answer("Не поняла, что поменять. Попробуйте сказать иначе.")
    else:
        await _process(m.chat.id, job_id, new_caption=False)


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

    app = web.Application()
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
