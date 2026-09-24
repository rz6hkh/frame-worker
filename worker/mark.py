"""Разметка: метрики качества и признаки моделью для снимков, которых ещё нет в photo.

Перенос photoquality.ps1 + photovision.ps1 (промпт v3, второй проход по коже)
в контейнер. Модель смотрит на превью Immich (1440 px, есть у всех снимков),
метрики считаются по оригиналу с архива - как раньше.

Перед запуском проверяется видеокарта: в Ollama не должно быть чужих
моделей, Immich не должен индексировать. Иначе - отказ (см. грабли 14.09).
"""
import base64
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

import common as c

register_heif_opener()   # 6.5 тыс. HEIC с айфонов; Pillow сам их не открывает

OLLAMA = os.environ.get("OLLAMA", "http://ollama:11434")
MODEL = os.environ.get("MODEL", "qwen3.5:4b")
IMMICH_LIBRARY = os.environ.get("IMMICH_LIBRARY", "/mnt/immich_library")   # /data внутри Immich
PROMPT_VERSION = "v3"
SIDE = 512
MIN_BYTES = 150 * 1024
MIN_LONG = 1024
CATEGORIES = ("people", "travel", "cars", "animals", "things", "work", "document", "screenshot", "junk", "other")
FLAGS = ("kids", "elderly", "alcohol", "smoke", "skin", "posed", "party")
LEVELS = ("none", "mild", "strong")

PROMPT = """You are describing photos from a home archive so a script can sort them for a digital picture frame.
Look at the image and answer with JSON only, no other text:
{"category":"...","people":0,"kids":false,"elderly":false,"alcohol":false,"smoke":false,"skin":false,"posed":false,"party":false,"why":"..."}

category - exactly one of:
- people: people are the main subject: portrait, group, gathering, party, family table
- travel: nature, landscape, city, sights, sea, mountains, trip; people are small or absent
- cars: a car, motorcycle or truck is the main subject: exterior, interior, engine, wheels, repair, tuning, car meet
- animals: a pet or an animal is the main subject
- things: objects, food, interiors, purchases, decor, garage, tools, everyday stuff
- work: a monitor photographed with a camera, wires, cables, electrical panels, equipment, test benches, construction, engineering work
- document: paper document, scan, receipt, form, passport, printed text
- screenshot: a real screen capture: chat, app, web page, map, video frame; not a photo of a monitor
- junk: black or empty frame, accidental shot, blurred beyond recognition, nothing to look at
- other: cannot tell

people: number of people visible, 0 if none, 10 for a crowd.
kids: a child is visible.
elderly: an elderly person is visible.
alcohol: bottles, glasses of alcohol or drinking are visible.
smoke: smoking, vaping or hookah is visible.
skin: sensitive, underwear or topless people indoors; swimwear at a beach or pool is false.
posed: a deliberate portrait or photoshoot, the subject looks at the camera.
party: a celebration, party, feast or crowd having fun.
why: at most 5 words.
"""

SECOND_PROMPT = """Look at the image and answer with JSON only:
{"sensitive":"...","why":"..."}

sensitive - one of: none, mild, strong
why: at most 5 words.
"""
# Настоящий второй проход спрашивает конкретнее и с градациями,
# но это уже частность, а не устройство конвейера.


# ---------------------------------------------------------------- метрики (photoquality)

def metrics(path, size):
    """(lap, mean, sd, dark, bright, small). Как QualityMath: лапласиан на четырёх
    соседях по серому уменьшенному до 512 по длинной стороне.

    Фильтр уменьшения - HAMMING: у WPF свой (Fant), точно его не повторить,
    но яркость и контраст сходятся в пределах 1%, а резкость - ближе всего
    из доступных (13.7% медиана против 30% у BILINEAR). Абсолютные значения
    lap всё равно не сравнимы с кэшем v1: столбец пересчитан целиком."""
    with Image.open(path) as im:
        w, h = im.size
        if size < MIN_BYTES or max(w, h) < MIN_LONG:
            return None, None, None, None, None, True
        im.draft("L", (SIDE * 2, SIDE * 2))          # быстрый JPEG-декод не мельче нужного
        im = ImageOps.exif_transpose(im).convert("L")
        if w >= h:
            im = im.resize((SIDE, max(1, round(im.height * SIDE / im.width))), Image.HAMMING)
        else:
            im = im.resize((max(1, round(im.width * SIDE / im.height)), SIDE), Image.HAMMING)
        p = np.asarray(im, dtype=np.float64)
    lap = 4 * p[1:-1, 1:-1] - p[1:-1, :-2] - p[1:-1, 2:] - p[:-2, 1:-1] - p[2:, 1:-1]
    return (float(lap.var()), float(p.mean()), float(p.std()),
            float((p < 16).mean()), float((p > 240).mean()), False)


# ---------------------------------------------------------------- модель (photovision)

def jpeg_b64(path):
    with Image.open(path) as im:
        im.draft("RGB", (SIDE * 2, SIDE * 2))
        im = ImageOps.exif_transpose(im).convert("RGB")
        im = im.resize((SIDE, max(1, round(im.height * SIDE / im.width))), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def ask(prompt, b64, num_predict):
    r = requests.post(f"{OLLAMA}/api/generate", json={
        "model": MODEL, "prompt": prompt, "images": [b64], "stream": False, "format": "json",
        "think": False, "keep_alive": "30m", "options": {"temperature": 0, "num_predict": num_predict}},
        timeout=300).json()
    txt = (r.get("response") or "").strip()
    if not txt and str(r.get("thinking", "")).strip().startswith("{"):
        txt = r["thinking"]
    return json.loads(txt)


def describe(b64):
    j = ask(PROMPT, b64, 200)
    cat = str(j.get("category", "")).lower()
    if cat not in CATEGORIES:
        raise ValueError(f"категория вне списка: {cat!r}")
    out = {"category": cat, "people": int(j.get("people") or 0), "why": str(j.get("why", ""))[:80]}
    for f in FLAGS:
        out[f] = bool(j.get(f))
    return out


def sensitive(b64):
    n = str(ask(SECOND_PROMPT, b64, 80).get("sensitive", "")).lower()
    return n if n in LEVELS else None


# ---------------------------------------------------------------- проверка видеокарты

def gpu_free():
    ps = requests.get(f"{OLLAMA}/api/ps", timeout=10).json().get("models", [])
    others = [m["name"] for m in ps if m["name"] != MODEL]
    if others:
        return f"в Ollama чужие модели: {', '.join(others)} - сначала ollama stop"
    jobs = c.api("GET", "/jobs")
    busy = [k for k in ("smartSearch", "faceDetection", "facialRecognition", "ocr", "duplicateDetection")
            if jobs.get(k, {}).get("jobCounts", {}).get("active", 0)]
    if busy:
        return f"Immich индексирует: {', '.join(busy)} - подождать"
    return None


# ---------------------------------------------------------------- прогон

def todo():
    """Снимки Immich из папок SOURCES без метрик или без разметки."""
    with c.immich() as im:
        rows = im.execute("""
            select a.id, a."originalPath", f.path
            from asset a
            left join asset_file f on f."assetId" = a.id and f.type = 'preview' and not f."isEdited"
            where a."deletedAt" is null and a.type = 'IMAGE'""").fetchall()
    pref = tuple(f"{c.ARHIV}/{s}/" for s in c.SOURCES)
    rows = [(str(a), p, pv) for a, p, pv in rows if p.startswith(pref)]
    with c.frame() as fr:
        have = {r[0]: (r[1], r[2]) for r in fr.execute("select asset_id::text, features_at, marked_at from photo")}
    need_m = [(a, p, pv) for a, p, pv in rows if not have.get(a, (None, None))[0]]
    need_v = [(a, p, pv) for a, p, pv in rows if not have.get(a, (None, None))[1]]
    return len(rows), need_m, need_v


def run(limit=0, threads=3, no_model=False, force=False, all_metrics=False):
    total, need_m, need_v = todo()
    c.log(f"снимков Immich в архиве {total}: без метрик {len(need_m)}, без разметки {len(need_v)}")
    if all_metrics:
        with c.immich() as im:
            rows = im.execute("""select a.id, a."originalPath", null from asset a
                                 where a."deletedAt" is null and a.type = 'IMAGE'""").fetchall()
        pref = tuple(f"{c.ARHIV}/{s}/" for s in c.SOURCES)
        need_m = [(str(a), p, None) for a, p, _ in rows if p.startswith(pref)]
        c.log(f"пересчёт метрик целиком: {len(need_m)}")
    if limit:
        need_m, need_v = need_m[:limit], need_v[:limit]

    # метрики: оригинал с архива. Считаем в потоках (декодирование отпускает GIL),
    # пишем в базу из главного - соединение одно.
    def one_metric(item):
        aid, path = item[0], item[1]
        if not os.path.exists(path):
            return None
        try:
            size = os.path.getsize(path)
            return aid, path, size, metrics(path, size), None
        except Exception as e:  # noqa: BLE001 - один битый файл не должен ронять прогон
            return aid, path, 0, None, str(e)[:120]

    done = err = 0
    t0 = time.time()
    with c.frame() as fr, ThreadPoolExecutor(threads) as pool:
        for res in pool.map(one_metric, need_m):
            if res is None:
                continue
            aid, path, size, vals, e = res
            if e:
                err += 1
                c.log(f"  метрики: {path}: {e}")
            else:
                lap, mean, sd, dark, bright, small = vals
                fr.execute("""insert into photo (asset_id, path, bytes, lap, mean, sd, dark, bright, small, features_at)
                              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                              on conflict (asset_id) do update set path=excluded.path, bytes=excluded.bytes,
                              lap=excluded.lap, mean=excluded.mean, sd=excluded.sd, dark=excluded.dark,
                              bright=excluded.bright, small=excluded.small, features_at=now()""",
                           (aid, path, size, lap, mean, sd, dark, bright, small))
                done += 1
            if done % 500 == 0 and done:
                fr.commit()
                c.log(f"  метрики {done}/{len(need_m)}, {done / (time.time() - t0):.1f}/с")
    c.log(f"метрики: готово {done}, ошибок {err}, {time.time() - t0:.0f} с")
    if no_model or not need_v:
        return

    # модель: превью Immich
    why = None if force else gpu_free()
    if why:
        c.log(f"модель не запущена: {why}")
        return
    requests.post(f"{OLLAMA}/api/generate", json={"model": MODEL, "keep_alive": "30m"}, timeout=300)

    def one(item):
        aid, path, preview = item
        src = preview.replace("/data/", IMMICH_LIBRARY + "/", 1) if preview else None
        if not src or not os.path.exists(src):
            src = path if os.path.exists(path) else None
        if not src:
            return aid, None, "нет ни превью, ни оригинала"
        try:
            b64 = jpeg_b64(src)
            d = describe(b64)
            d["sensitive"] = sensitive(b64) if d["skin"] else "none"
            return aid, d, None
        except Exception as e:  # noqa: BLE001
            return aid, None, str(e)[:120]

    done = err = 0
    t0 = time.time()
    paths = {a: p for a, p, _ in need_v}
    with c.frame() as fr, ThreadPoolExecutor(threads) as pool:
        for aid, d, e in pool.map(one, need_v):
            if e:
                err += 1
                c.log(f"  модель: {aid}: {e}")
                continue
            fr.execute("""insert into photo (asset_id, path, category, people, kids, elderly, alcohol, smoke, skin, posed, party,
                                             sensitive, model, prompt, marked_at)
                          values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                          on conflict (asset_id) do update set category=excluded.category, people=excluded.people,
                          kids=excluded.kids, elderly=excluded.elderly, alcohol=excluded.alcohol, smoke=excluded.smoke,
                          skin=excluded.skin, posed=excluded.posed, party=excluded.party, sensitive=excluded.sensitive,
                          model=excluded.model, prompt=excluded.prompt, marked_at=now()""",
                       (aid, paths[aid], d["category"], d["people"], d["kids"], d["elderly"], d["alcohol"], d["smoke"],
                        d["skin"], d["posed"], d["party"], d["sensitive"], MODEL, PROMPT_VERSION))
            done += 1
            if done % 100 == 0:
                fr.commit()
                c.log(f"  модель {done}/{len(need_v)}, ошибок {err}, {done / (time.time() - t0):.2f}/с, "
                      f"осталось ~{(len(need_v) - done) * (time.time() - t0) / done / 60:.0f} мин")
    c.log(f"модель: готово {done}, ошибок {err}, {(time.time() - t0) / 60:.0f} мин")
