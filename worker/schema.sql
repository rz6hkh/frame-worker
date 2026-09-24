-- База рамки. Ключ везде - id снимка в Immich (uuid): пути меняются
-- при переезде диска, id - нет.

-- Снимок и его признаки. Строка появляется, когда снимок хотя бы раз
-- прошёл через воркер; пустые поля - ещё не считалось.
create table if not exists photo (
    asset_id     uuid primary key,
    path         text not null,               -- /mnt/arhiv/... на момент последней синхронизации
    bytes        bigint,
    -- метрики качества (photoquality): только у снимков крупнее порога
    lap          real,                        -- дисперсия лапласиана, резкость
    mean         real,
    sd           real,
    dark         real,
    bright       real,
    small        boolean default false,       -- меньше 150 КБ или короче 1024 px
    -- разметка моделью (photovision v3)
    category     text,                        -- people travel cars animals things work document screenshot junk other
    people       smallint,
    kids         boolean, elderly boolean, alcohol boolean, smoke boolean,
    skin         boolean, posed boolean, party boolean,
    sensitive       text,                        -- none swimwear underwear nude (второй проход)
    model        text,
    prompt       text,
    marked_at    timestamptz,
    features_at  timestamptz
);

-- Тип человека. Имя живёт в Immich, здесь только роль по нашему списку.
create table if not exists person_type (
    person_id    uuid primary key,             -- person.personGroupId в Immich
    name         text not null,
    type         text not null,                -- из своего списка
    updated_at   timestamptz default now()
);

-- Что воркер положил в какой альбом. По этой таблице понимаем, что убрал
-- пользователь: было положено, а в Immich больше нет.
create table if not exists placed (
    album_id     uuid not null,
    album_name   text not null,
    asset_id     uuid not null,
    placed_at    timestamptz default now(),
    primary key (album_id, asset_id)
);

-- Ручные решения пользователя. Сильнее любого правила.
--   'removed'  - убрал из альбома: туда больше не класть
--   'added'    - положил сам: не трогать, даже если правило не подходит
create table if not exists manual (
    album_id     uuid not null,
    asset_id     uuid not null,
    action       text not null check (action in ('removed', 'added')),
    seen_at      timestamptz default now(),
    primary key (album_id, asset_id)
);

-- Журнал карантина: что вынесено с диска и куда, чтобы вернуть.
create table if not exists quarantine (
    id           bigserial primary key,
    asset_id     uuid,
    src          text not null,
    dst          text not null,
    bytes        bigint,
    reason       text,                        -- 'immich-trash' и т.п.
    moved_at     timestamptz default now(),
    purged_at    timestamptz,
    restored_at  timestamptz
);

-- Журнал запусков воркера: что запускали, чем кончилось и весь вывод.
create table if not exists run_log (
    id           bigserial primary key,
    started_at   timestamptz default now(),
    finished_at  timestamptz,
    command      text,
    summary      text
);
alter table run_log add column if not exists ok boolean;
alter table run_log add column if not exists error text;
