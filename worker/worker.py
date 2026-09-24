"""frame_worker - все манипуляции рамки одним контейнером.

  python worker.py init                 создать схему базы
  python worker.py people               люди из Immich: типы из имён -> person_type, имена вычистить
  python worker.py mark [--limit N] [--threads N] [--no-model] [--force]
                                        метрики + модель для снимков, которых ещё нет в photo
  python worker.py sync                 запомнить ручные правки: что убрано/добавлено в альбомах
  python worker.py albums [--probe] [--only NAME] [--count N --suffix S]
                                        правила -> альбомы (сначала sync)
  python worker.py log [--n 10] [--run N]   последние запуски; --run - весь вывод одного
  python worker.py trash [--apply]      корзина Immich -> карантин на диске (-> purge отдельно)
  python worker.py purge [--apply]      удалить карантин насовсем
  python worker.py restore              вернуть из карантина

Ничего не удаляет ни с диска, ни из Immich без --apply. Разметка (метрики и
модель) - mark.py, туда же перенесены photoquality/photovision.
"""
import os
import random
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime

import common as c
import rules


@dataclass
class Photo:
    asset_id: str
    path: str
    year: str | None
    city: str | None
    types: list = field(default_factory=list)
    f: dict | None = None


# ---------------------------------------------------------------- init / import

def cmd_init():
    c.init_schema()
    c.log("схема готова")


def cmd_people():
    """Типы из имён Immich -> person_type; имена в Immich вычищаются (через API, не в базе)."""
    with c.immich() as im:
        # в v3 у person нет своего id: ключ (ownerId, personGroupId), в API - personGroupId
        rows = im.execute('select "personGroupId", "personGroupId", name from person where name <> \'\'').fetchall()
    with c.frame() as fr:
        known = {str(k): v for k, v in fr.execute("select person_id, type from person_type").fetchall()}
        cleaned = 0
        for person_id, group_id, raw in rows:
            name, t = c.split_person_name(raw)
            if not t:
                t = known.get(str(group_id))
            if t:
                fr.execute("""insert into person_type (person_id, name, type) values (%s,%s,%s)
                              on conflict (person_id) do update set name=excluded.name, type=excluded.type, updated_at=now()""",
                           (group_id, name, t))
            if name != raw:                              # имя чистится через API, в базу Immich не пишем
                c.api("PUT", f"/people/{person_id}", {"name": name})
                cleaned += 1
        n = fr.execute("select count(*) from person_type").fetchone()[0]
    c.log(f"люди: с типом {n}, имён вычищено в Immich {cleaned}")


# ---------------------------------------------------------------- photos

def load_photos():
    with c.immich() as im:
        rows = im.execute("""
            select a.id, a."originalPath", to_char(e."dateTimeOriginal", 'YYYY'), e.city,
                   (e.make is not null and e.make <> '') as cam
            from asset a left join asset_exif e on e."assetId" = a.id
            where a."deletedAt" is null and a.type = 'IMAGE'""").fetchall()
        faces = im.execute("""
            select f."assetId", f."personGroupId" from asset_face f
            where f."deletedAt" is null""").fetchall()
        # Сколько кусков текста нашёл OCR Immich - признак скриншота, которого
        # модель не разглядела (реклама, переписка, объявление с машиной).
        ocr = dict(im.execute('select "assetId", count(*) from asset_ocr group by 1').fetchall())
    pref = tuple(f"{c.ARHIV}/{s}/" for s in c.SOURCES)
    photos = {str(aid): Photo(str(aid), p, y, city) for aid, p, y, city, _ in rows if p.startswith(pref)}
    cam = {str(aid): bool(k) for aid, _, _, _, k in rows}
    ocr = {str(k): v for k, v in ocr.items()}
    with c.frame() as fr:
        types = dict(fr.execute("select person_id::text, type from person_type").fetchall())
        feats = {}
        for r in fr.execute("""select asset_id::text, category, people, kids, elderly, alcohol, smoke, skin, posed, party,
                                      sensitive, lap, mean, sd, dark, bright, small from photo""").fetchall():
            feats[r[0]] = dict(zip(["category", "people", "kids", "elderly", "alcohol", "smoke", "skin", "posed", "party",
                                    "sensitive", "lap", "mean", "sd", "dark", "bright", "small"], r[1:]))
    for aid, pid in faces:
        p = photos.get(str(aid))
        if p and str(pid) in types:
            p.types.append(types[str(pid)])
    for aid, p in photos.items():
        f = dict(feats.get(aid) or {})
        f["ocr"] = ocr.get(aid, 0)
        f["cam"] = cam.get(aid, False)
        p.f = f
    return photos


# ---------------------------------------------------------------- sync (ручные правки)

def cmd_sync():
    """Сравнить, что воркер клал, с тем, что в альбомах сейчас: разница - решения пользователя."""
    live = {a["id"]: a["albumName"] for a in c.albums()}
    with c.frame() as fr:
        placed = {}
        for album_id, album_name, asset_id in fr.execute("select album_id::text, album_name, asset_id::text from placed"):
            placed.setdefault((album_id, album_name), set()).add(asset_id)

        gone = [(i, n) for (i, n) in placed if i not in live]
        for album_id, album_name in gone:                # альбом удалён руками в Immich
            fr.execute("delete from placed where album_id = %s", (album_id,))
            fr.execute("delete from manual where album_id = %s", (album_id,))
            del placed[(album_id, album_name)]
            c.log(f"альбом «{album_name}» удалён в Immich - забываю о нём")

        removed = added = 0
        for (album_id, album_name), ours in placed.items():
            now = set(c.album_assets(album_id))
            for aid in ours - now:                       # клали мы, в Immich нет -> убрал пользователь
                fr.execute("""insert into manual (album_id, asset_id, action) values (%s,%s,'removed')
                              on conflict (album_id, asset_id) do update set action='removed', seen_at=now()""", (album_id, aid))
                fr.execute("delete from placed where album_id=%s and asset_id=%s", (album_id, aid))
                removed += 1
            for aid in now - ours:                       # есть в Immich, мы не клали -> положил пользователь
                fr.execute("""insert into manual (album_id, asset_id, action) values (%s,%s,'added')
                              on conflict (album_id, asset_id) do update set action='added', seen_at=now()""", (album_id, aid))
                added += 1

        # Альбом, о котором мы ещё ничего не знаем (собран прежним скриптом или
        # создан руками), принимаем как есть: его содержимое - точка отсчёта,
        # а не «пользователь добавил три тысячи снимков».
        base = 0
        for album_id, album_name in live.items():
            if any(i == album_id for i, _ in placed):
                continue
            ids = c.album_assets(album_id)
            if not ids:
                continue
            c.many(fr, "insert into placed (album_id, album_name, asset_id) values (%s,%s,%s) on conflict do nothing",
                   [(album_id, album_name, a) for a in ids])
            base += len(ids)
            c.log(f"альбом «{album_name}» принят как есть: {len(ids)}")
        c.log(f"правки пользователя: убрано {removed}, добавлено {added}" + (f", принято без разбора {base}" if base else ""))


# ---------------------------------------------------------------- albums

def cmd_albums(probe=False, only=None, count=0, suffix=""):
    cmd_sync()
    photos = load_photos()
    c.log(f"снимков: {len(photos)}, с людьми {sum(1 for p in photos.values() if p.types)}, "
          f"с признаками модели {sum(1 for p in photos.values() if p.f and p.f.get('category'))}")

    noshow = c.album_by_name(c.NOSHOW) or (None if probe else c.album_create(c.NOSHOW))
    noshow_ids = set(c.album_assets(noshow["id"])) if noshow else set()
    with c.frame() as fr:
        manual = {}
        for album_id, asset_id, action in fr.execute("select album_id::text, asset_id::text, action from manual"):
            manual.setdefault(album_id, {})[asset_id] = action

    # общие вычеты -> «Не показывать» (кроме того, что пользователь оттуда убрал)
    ns_manual = manual.get(noshow["id"], {}) if noshow else {}
    auto_noshow, eligible = [], []
    for aid, p in photos.items():
        if aid in noshow_ids:
            continue
        # Признак «не всем» сюда не идёт: его вычитают те правила,
        # которые сочтут нужным. Иначе в мусорку уезжают семейные
        # снимки, которым место в показе для своих.
        auto = (c.HIDE_TYPE in p.types) or rules.junk(p.f)
        if auto and ns_manual.get(aid) != "removed":
            auto_noshow.append(aid)
            continue
        eligible.append(p)
    c.log(f"«{c.NOSHOW}»: сейчас {len(noshow_ids)}, автоматика добавит {len(auto_noshow)}; для правил {len(eligible)}")
    if not probe and auto_noshow:
        c.album_add(noshow["id"], auto_noshow)
        with c.frame() as fr:
            c.many(fr, "insert into placed (album_id, album_name, asset_id) values (%s,%s,%s) on conflict do nothing",
                   [(noshow["id"], c.NOSHOW, a) for a in auto_noshow])
        # чтобы вычистить их из показа уже на этом прогоне, а не на следующем
        noshow_ids |= set(auto_noshow)

    names = [only] if only else list(rules.RULES)
    rnd = random.Random(21)
    for name in names:
        rule = rules.RULES[name]
        hit = [p for p in eligible if rule(p)]
        title = f"{name} {suffix.strip()}" if suffix.strip() else name
        c.log(f"{name:<12} подходит {len(hit):6}")
        if probe:
            continue
        pick = hit if not count or len(hit) <= count else rnd.sample(hit, count)
        alb = c.album_by_name(title) or c.album_create(title)
        m = manual.get(alb["id"], {})
        have = set(c.album_assets(alb["id"]))

        # Снимок, отправленный пользователем в «Не показывать», убираем из
        # показа - это его же решение, а не откат его правок.
        banned = list(have & noshow_ids)
        if banned:
            c.album_remove(alb["id"], banned)
            with c.frame() as fr:
                c.many(fr, "delete from placed where album_id = %s and asset_id = %s",
                       [(alb["id"], a) for a in banned])
                c.many(fr, """insert into manual (album_id, asset_id, action) values (%s,%s,'removed')
                              on conflict (album_id, asset_id) do update set action='removed', seen_at=now()""",
                       [(alb["id"], a) for a in banned])
            have -= set(banned)

        new = [p.asset_id for p in pick if p.asset_id not in have and m.get(p.asset_id) != "removed"]
        added = c.album_add(alb["id"], new) if new else 0
        with c.frame() as fr:
            c.many(fr, "insert into placed (album_id, album_name, asset_id) values (%s,%s,%s) on conflict do nothing",
                   [(alb["id"], title, a) for a in new])
        c.log(f"  «{title}»: было {len(have) + len(banned)}, добавлено {added}, "
              f"убрано как «не показывать» {len(banned)}, "
              f"пропущено как убранное рукой {sum(1 for p in pick if m.get(p.asset_id) == 'removed')}")


# ---------------------------------------------------------------- trash -> quarantine

def cmd_trash(apply=False):
    """Всё, что пользователь удалил в Immich (корзина), вынести с диска в карантин."""
    with c.immich() as im:
        rows = im.execute('select id, "originalPath" from asset where "deletedAt" is not null').fetchall()
    with c.frame() as fr:
        done = {r[0] for r in fr.execute("select src from quarantine where restored_at is null")}
    todo = [(str(a), p) for a, p in rows if p.startswith(c.ARHIV + "/") and p not in done and os.path.exists(p)]
    size = sum(os.path.getsize(p) for _, p in todo)
    c.log(f"в корзине Immich {len(rows)}, есть на диске и ещё не в карантине {len(todo)}, {size / 2**30:.2f} ГБ")
    if not apply or not todo:
        return
    moved = 0
    with c.frame() as fr:
        for aid, src in todo:
            dst = os.path.join(c.QUARANTINE, src[len(c.ARHIV) + 1:])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            b = os.path.getsize(src)
            shutil.move(src, dst)
            fr.execute("insert into quarantine (asset_id, src, dst, bytes, reason) values (%s,%s,%s,%s,'immich-trash')",
                       (aid, src, dst, b))
            moved += 1
    c.log(f"перемещено в карантин: {moved}")
    forget_in_immich([a for a, _ in todo])


def forget_in_immich(ids):
    """Убрать из Immich записи о снимках, которых больше нет на диске.

    Без этого они висят в корзине ещё 30 дней, остаются в альбомах и рамка
    продолжает их показывать: превью лежит отдельно от оригинала и никуда
    не делось. Файл к этому моменту уже в карантине, удалять Immich нечего -
    архив у него примонтирован только на чтение.
    """
    if not ids:
        return
    for i in range(0, len(ids), 500):
        c.api("DELETE", "/assets", {"ids": ids[i:i + 500], "force": True})
    with c.frame() as fr:
        c.many(fr, "delete from placed where asset_id = %s", [(a,) for a in ids])
        c.many(fr, "delete from manual where asset_id = %s", [(a,) for a in ids])
        c.many(fr, "delete from photo where asset_id = %s", [(a,) for a in ids])
    c.log(f"убрано из Immich и из своей базы: {len(ids)}")


def cmd_purge(apply=False):
    with c.frame() as fr:
        rows = fr.execute("select id, dst, bytes from quarantine where purged_at is null and restored_at is null").fetchall()
        present = [(i, d, b) for i, d, b in rows if os.path.exists(d)]
        c.log(f"в журнале {len(rows)}, в карантине найдено {len(present)}, {sum(b or 0 for _, _, b in present) / 2**30:.2f} ГБ")
        if len(present) != len(rows):
            c.log("журнал и карантин не сходятся - сначала разберись")
            return
        if not apply:
            return
        for i, d, _ in present:
            os.remove(d)
            fr.execute("update quarantine set purged_at=now() where id=%s", (i,))
        c.log(f"удалено {len(present)}")


def cmd_restore():
    with c.frame() as fr:
        rows = fr.execute("select id, src, dst from quarantine where purged_at is null and restored_at is null").fetchall()
        back = 0
        for i, src, dst in rows:
            if os.path.exists(dst):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
                fr.execute("update quarantine set restored_at=now() where id=%s", (i,))
                back += 1
        c.log(f"возвращено {back}")


# ---------------------------------------------------------------- main

def cmd_log(n=10, full=None):
    """Последние запуски; с номером - весь вывод того запуска."""
    with c.frame() as fr:
        if full:
            for cmdline, started, out, err in fr.execute(
                    "select command, started_at, summary, error from run_log where id = %s", (full,)):
                print(f"#{full}  {started:%d.%m %H:%M}  {cmdline}\n")
                print(out or "(пусто)")
                if err:
                    print("\nОШИБКА:\n" + err)
            return
        print(f"{'№':>5}  {'когда':<12} {'сколько':>8}  {'итог':<6} команда")
        for i, cmdline, st, fin, ok in fr.execute(
                """select id, command, started_at, finished_at, ok from run_log order by id desc limit %s""", (n,)):
            took = f"{(fin - st).total_seconds():.0f} с" if fin else "-"
            print(f"{i:>5}  {st:%d.%m %H:%M}  {took:>8}  {'ок' if ok else 'СБОЙ' if ok is False else '?':<6} {cmdline}")


def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    flag = lambda n: n in args
    opt = lambda n, d=None: args[args.index(n) + 1] if n in args else d
    if cmd == "log":                       # сам себя в журнал не пишет
        cmd_log(n=int(opt("--n", 10)), full=opt("--run"))
        return
    started = datetime.now()
    err = None
    try:
        if cmd == "init":
            cmd_init()
        elif cmd == "people":
            cmd_people()
        elif cmd == "mark":
            import mark
            mark.run(limit=int(opt("--limit", 0)), threads=int(opt("--threads", 3)),
                     no_model=flag("--no-model"), force=flag("--force"), all_metrics=flag("--all-metrics"))
        elif cmd == "sync":
            cmd_sync()
        elif cmd == "albums":
            cmd_albums(probe=flag("--probe"), only=opt("--only"), count=int(opt("--count", 0)), suffix=opt("--suffix", ""))
        elif cmd == "trash":
            cmd_trash(apply=flag("--apply"))
        elif cmd == "purge":
            cmd_purge(apply=flag("--apply"))
        elif cmd == "restore":
            cmd_restore()
        else:
            print(__doc__)
            return
    except BaseException as e:             # включая Ctrl+C: запуск всё равно попадёт в журнал
        err = traceback.format_exc()
        c.log(f"СБОЙ: {type(e).__name__}: {e}")
        raise
    finally:
        try:
            with c.frame() as fr:
                fr.execute("""insert into run_log (started_at, finished_at, command, summary, ok, error)
                              values (%s, now(), %s, %s, %s, %s)""",
                           (started, " ".join(argv), "\n".join(c.LINES), err is None, err))
        except Exception as e:  # noqa: BLE001 - журнал не должен прятать исходную ошибку
            print(f"журнал не записан: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
