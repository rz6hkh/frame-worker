"""Общее: две базы (своя и Immich, Immich только чтение), API Immich, типы людей."""
import os
import re
import psycopg
import requests

FRAME_DB = os.environ["FRAME_DB"]
IMMICH_DB = os.environ["IMMICH_DB"]
IMMICH_URL = os.environ["IMMICH_URL"].rstrip("/")
ARHIV = os.environ.get("ARHIV", "/mnt/arhiv")
QUARANTINE = os.environ.get("QUARANTINE", "/mnt/arhiv_quarantine")

# Папки архива, из которых берём снимки (относительно ARHIV).
SOURCES = tuple(x for x in os.environ.get("SOURCES", "").split("|") if x)

NOSHOW = os.environ.get("NOSHOW_ALBUM", "Не показывать")

# Типы людей: тип пишется последним словом
# имени человека в Immich, скрипт его отрезает и запоминает у себя.
# Список свой у каждого - здесь он приходит из окружения.
PEOPLE_TYPES = [x for x in os.environ.get("PEOPLE_TYPES", "").split("|") if x]
PEOPLE_ALIASES = {}   # опечатки в именах -> правильный тип

# Человек с этим типом убирает снимок из показа целиком.
HIDE_TYPE = os.environ.get("HIDE_TYPE", "не показывать")


def frame():
    return psycopg.connect(FRAME_DB)


def immich():
    return psycopg.connect(IMMICH_DB, options="-c default_transaction_read_only=on")


def init_schema():
    with frame() as c, open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        c.execute(f.read())


# --- API Immich ---

def _key():
    with open(os.environ["IMMICH_KEY_FILE"], encoding="utf-8") as f:
        return f.read().strip()


def api(method, path, json=None, params=None):
    r = requests.request(method, f"{IMMICH_URL}/api{path}", json=json, params=params,
                         headers={"x-api-key": _key(), "Accept": "application/json"}, timeout=300)
    r.raise_for_status()
    return r.json() if r.content else None


def albums():
    return api("GET", "/albums")


def album_by_name(name):
    same = [a for a in albums() if a["albumName"] == name]
    if not same:
        return None
    return max(same, key=lambda a: a.get("assetCount", 0))


def album_create(name):
    return api("POST", "/albums", {"albumName": name})


def album_assets(album_id):
    """Полный состав альбома: GET /albums/{id} отдаёт один снимок, поиск - все."""
    ids, page = [], 1
    while True:
        r = api("POST", "/search/metadata", {"albumIds": [album_id], "size": 1000, "page": page})
        ids += [a["id"] for a in r["assets"]["items"]]
        if not r["assets"].get("nextPage"):
            return ids
        page = int(r["assets"]["nextPage"])


def album_add(album_id, ids):
    added = 0
    for i in range(0, len(ids), 500):
        r = api("PUT", f"/albums/{album_id}/assets", {"ids": ids[i:i + 500]})
        added += sum(1 for x in r if x.get("success"))
    return added


def album_remove(album_id, ids):
    removed = 0
    for i in range(0, len(ids), 500):
        r = api("DELETE", f"/albums/{album_id}/assets", {"ids": ids[i:i + 500]})
        removed += sum(1 for x in r if x.get("success"))
    return removed


# --- люди ---

def split_person_name(raw):
    """«Иван коллеги» -> («Иван», «коллеги»); без типа -> («…», None)."""
    n = raw.strip()
    for k in sorted(PEOPLE_ALIASES, key=len, reverse=True):
        if re.search(r"\s+" + re.escape(k) + r"$", n):
            return n[: -len(k)].rstrip(), PEOPLE_ALIASES[k]
    for t in sorted(PEOPLE_TYPES, key=len, reverse=True):
        if re.search(r"\s+" + re.escape(t) + r"$", n):
            return n[: -len(t)].rstrip(), t
    return n, None


def many(conn, sql, rows):
    """executemany есть у курсора, но не у соединения psycopg3."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


LINES = []          # весь вывод запуска, уезжает в run_log.summary


def log(msg):
    print(msg, flush=True)
    LINES.append(str(msg))
