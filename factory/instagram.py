"""Публикация Reels через официальный Instagram API (вход через Instagram, без пароля).

Порядок: Полина один раз нажимает ссылку входа, Instagram возвращает код на наш сервер,
мы меняем его на долгий токен (60 дней) и сами продлеваем его раз в неделю.
"""
import time
import urllib.parse

import httpx

from . import store

GRAPH = "https://graph.instagram.com/v23.0"
SCOPES = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights"


class InstagramError(Exception):
    pass


def _check(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise InstagramError(f"HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", data)
        msg = err.get("error_user_msg") or err.get("message") or err.get("error_message") or str(err)
        raise InstagramError(msg)
    return data


def auth_url(app_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPES, "state": state, "enable_fb_login": "0", "force_reauth": "true",
    })
    return f"https://www.instagram.com/oauth/authorize?{q}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Код из ссылки входа → долгий токен. Сохраняет всё в хранилище."""
    s = store.load()["secrets"]
    with httpx.Client(timeout=30) as c:
        short = _check(c.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": s["meta_app_id"], "client_secret": s["meta_app_secret"],
            "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code.split("#")[0],
        }))
        long = _check(c.get("https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": s["meta_app_secret"],
            "access_token": short["access_token"],
        }))
        me = _check(c.get(f"{GRAPH}/me", params={
            "fields": "user_id,username", "access_token": long["access_token"]}))

    def put(d):
        d["secrets"].update({
            "ig_token": long["access_token"],
            "ig_user_id": str(me.get("user_id") or short.get("user_id")),
            "ig_username": me.get("username"),
            "ig_token_expires": int(time.time()) + int(long.get("expires_in", 5184000)),
        })
    store.update(put)
    return me


def refresh_token() -> int:
    """Продлить токен ещё на 60 дней. Возвращает новую дату окончания (unix)."""
    s = store.load()["secrets"]
    with httpx.Client(timeout=30) as c:
        data = _check(c.get("https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": s["ig_token"]}))
    exp = int(time.time()) + int(data.get("expires_in", 5184000))

    def put(d):
        d["secrets"]["ig_token"] = data["access_token"]
        d["secrets"]["ig_token_expires"] = exp
    store.update(put)
    return exp


def connected() -> bool:
    s = store.load()["secrets"]
    return bool(s.get("ig_token") and s.get("ig_user_id"))


def publish_reel(video_url: str, caption: str, cover_url: str | None = None,
                 wait_seconds: int = 600, poll=time.sleep) -> tuple[str, str | None]:
    """Выложить Reels. Возвращает (id публикации, ссылка). video_url должен быть доступен из интернета по https."""
    s = store.load()["secrets"]
    token, uid = s["ig_token"], s["ig_user_id"]
    params = {"media_type": "REELS", "video_url": video_url, "caption": caption,
              "share_to_feed": "true", "access_token": token}
    if cover_url:
        params["cover_url"] = cover_url
    with httpx.Client(timeout=60) as c:
        container = _check(c.post(f"{GRAPH}/{uid}/media", data=params))["id"]
        waited = 0
        while True:
            st = _check(c.get(f"{GRAPH}/{container}", params={
                "fields": "status_code,status", "access_token": token}))
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Instagram не принял видео: {st.get('status')}")
            if waited >= wait_seconds:
                raise InstagramError("Instagram слишком долго обрабатывает видео")
            poll(10)
            waited += 10
        media = _check(c.post(f"{GRAPH}/{uid}/media_publish", data={
            "creation_id": container, "access_token": token}))["id"]
        info = _check(c.get(f"{GRAPH}/{media}", params={
            "fields": "permalink", "access_token": token}))
    return media, info.get("permalink")


def account_stats() -> dict:
    s = store.load()["secrets"]
    with httpx.Client(timeout=30) as c:
        return _check(c.get(f"{GRAPH}/me", params={
            "fields": "username,followers_count,media_count", "access_token": s["ig_token"]}))


def media_stats(media_id: str) -> dict:
    s = store.load()["secrets"]
    with httpx.Client(timeout=30) as c:
        data = _check(c.get(f"{GRAPH}/{media_id}/insights", params={
            "metric": "views,reach,likes,comments,saved,shares", "access_token": s["ig_token"]}))
    return {m["name"]: (m.get("values") or [{}])[0].get("value", 0) for m in data.get("data", [])}
