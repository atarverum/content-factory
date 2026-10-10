"""Простое хранилище состояния в JSON-файле: кто допущен, задания, очередь публикаций, ключи."""
import json
import os
import threading

DATA = os.environ.get("FACTORY_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
_PATH = os.path.join(DATA, "state.json")
_LOCK = threading.RLock()

_DEFAULT = {
    "allowed": [],          # telegram id Полины и Александра
    "pair_code": None,      # одноразовый код привязки
    "style": "polina",
    "jobs": {},             # id → задание
    "secrets": {},          # meta_app_id, meta_app_secret, ig_token, ig_user_id, ig_token_expires
    "paused": False,
}


def load() -> dict:
    with _LOCK:
        if not os.path.exists(_PATH):
            return json.loads(json.dumps(_DEFAULT))
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _DEFAULT.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data


def save(data: dict) -> None:
    with _LOCK:
        os.makedirs(DATA, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _PATH)


def update(fn):
    """Прочитать, изменить функцией fn(data), сохранить. Возвращает результат fn."""
    with _LOCK:
        data = load()
        res = fn(data)
        save(data)
        return res


def job_dir(job_id: str) -> str:
    d = os.path.join(DATA, "jobs", job_id)
    os.makedirs(d, exist_ok=True)
    return d
