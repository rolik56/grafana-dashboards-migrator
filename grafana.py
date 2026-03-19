"""
Grafana Dashboard Migration Tool

Массовый перенос дашбордов между двумя инстансами Grafana (v10+)
через HTTP API с поддержкой:
  - сохранения структуры папок
  - замены datasource UID
  - фильтрации по именам папок
  - конфигурируемого rate-limit (batch size + delay)
  - dry-run режима

Python 3.11+ | Grafana 10+
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
import os

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

load_dotenv()

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
logger = logging.getLogger("grafana-migrate")


@dataclass(frozen=True)
class GrafanaInstance:
    """Параметры подключения к одному инстансу Grafana."""

    url: str
    token: str
    verify: bool = True

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def api(self, path: str) -> str:
        """Полный URL для заданного API-пути."""
        return f"{self.url.rstrip('/')}{path}"


@dataclass
class MigrationStats:
    """Счётчики результатов миграции."""

    total: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    folders_processed: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Работа с Grafana API
# ---------------------------------------------------------------------------


def search_all(
    instance: GrafanaInstance,
    search_type: str = "dash-db",
) -> list[dict[str, Any]]:
    """
    Получить список объектов через /api/search с пагинацией.
    search_type: "dash-db" для дашбордов, "dash-folder" для папок.
    """
    page = 1
    limit = 1000
    all_results: list[dict[str, Any]] = []

    while True:
        resp = requests.get(
            instance.api("/api/search"),
            headers=instance.headers,
            params={"type": search_type, "limit": limit, "page": page},
            timeout=30,
            verify=instance.verify,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_results.extend(batch)
        if len(batch) < limit:
            break
        page += 1

    return all_results


def get_folders(instance: GrafanaInstance) -> list[dict[str, Any]]:
    """Получить список всех папок из source."""
    folders = search_all(instance, search_type="dash-folder")
    logger.info("Найдено папок: %d", len(folders))
    return folders


def get_dashboards_in_folder(
    instance: GrafanaInstance, folder_id: int
) -> list[dict[str, Any]]:
    """Получить дашборды в конкретной папке."""
    page = 1
    limit = 1000 # Сколько бордов получаем за раз (возможно, стоит уменьшить для стабильности)
    results: list[dict[str, Any]] = []

    while True:
        resp = requests.get(
            instance.api("/api/search"),
            headers=instance.headers,
            params={
                "type": "dash-db",
                "folderIds": folder_id,
                "limit": limit,
                "page": page,
            },
            timeout=30,
            verify=instance.verify,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        results.extend(batch)
        if len(batch) < limit:
            break
        page += 1

    return results


def get_dashboard(instance: GrafanaInstance, uid: str) -> dict[str, Any]:
    """Скачать полный JSON дашборда по UID."""
    resp = requests.get(
        instance.api(f"/api/dashboards/uid/{uid}"),
        headers=instance.headers,
        timeout=30,
        verify=instance.verify,
    )
    resp.raise_for_status()
    return resp.json()


def get_target_datasources(instance: GrafanaInstance) -> dict[str, str]:
    """
    Получить словарь {uid: name} всех datasource в target-инстансе.
    Используется для валидации маппинга.
    """
    resp = requests.get(
        instance.api("/api/datasources"),
        headers=instance.headers,
        timeout=30,
        verify=instance.verify,
    )
    resp.raise_for_status()
    return {ds["uid"]: ds["name"] for ds in resp.json()}


def create_folder(
    instance: GrafanaInstance, title: str, parent_uid: str | None = None
) -> dict[str, Any]:
    """Создать папку в target. Возвращает JSON с uid и id."""
    body: dict[str, Any] = {"title": title, "uid": uuid.uuid4().hex[:12]}
    if parent_uid:
        body["parentUid"] = parent_uid
    resp = requests.post(
        instance.api("/api/folders"),
        headers=instance.headers,
        json=body,
        timeout=30,
        verify=instance.verify,
    )
    resp.raise_for_status()
    return resp.json()


def find_folder_by_title(
    instance: GrafanaInstance,
    title: str,
    parent_uid: str | None = None,
) -> dict[str, Any] | None:
    """
    Найти папку в target по имени.
    Если parent_uid задан — ищет только среди дочерних папок этого родителя.
    Возвращает None если не найдена.
    """
    folders = search_all(instance, search_type="dash-folder")
    for f in folders:
        if f.get("title") != title:
            continue
        # Если ищем вложенную папку — проверяем родителя
        if parent_uid is not None:
            folder_detail = requests.get(
                instance.api(f"/api/folders/{f['uid']}"),
                headers=instance.headers,
                timeout=30,
                verify=instance.verify,
            )
            if folder_detail.ok:
                detail = folder_detail.json()
                if detail.get("parentUid") == parent_uid:
                    return f
        else:
            # Ищем папку верхнего уровня (без родителя)
            return f
    return None


def get_existing_dashboard_titles(
    instance: GrafanaInstance, folder_id: int
) -> set[str]:
    """Получить множество имён дашбордов в папке target для проверки дубликатов."""
    dashboards = get_dashboards_in_folder(instance, folder_id)
    return {d.get("title", "") for d in dashboards}


def create_dashboard(
    instance: GrafanaInstance, payload: dict[str, Any]
) -> dict[str, Any]:
    """Создать / обновить дашборд в target через POST /api/dashboards/db."""
    resp = requests.post(
        instance.api("/api/dashboards/db"),
        headers=instance.headers,
        json=payload,
        timeout=60,
        verify=instance.verify,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Трансформация JSON
# ---------------------------------------------------------------------------


def _replace_ds_uid(obj: Any, ds_mapping: dict[str, str]) -> Any:
    """
    Рекурсивно обходит произвольную структуру и заменяет datasource UID
    по маппинг-таблице. Обрабатывает:
      - {"datasource": {"uid": "...", "type": "..."}}
      - {"datasource": "uid_string"}
      - Вложенные списки / словари (targets, panels, templating и т.д.)
    """
    if isinstance(obj, dict):
        if "datasource" in obj:
            ds = obj["datasource"]
            if isinstance(ds, dict) and "uid" in ds:
                old_uid = ds["uid"]
                if old_uid in ds_mapping:
                    ds["uid"] = ds_mapping[old_uid]
            elif isinstance(ds, str) and ds in ds_mapping:
                obj["datasource"] = ds_mapping[ds]

        for value in obj.values():
            _replace_ds_uid(value, ds_mapping)

    elif isinstance(obj, list):
        for item in obj:
            _replace_ds_uid(item, ds_mapping)

    return obj


def transform_dashboard(
    dashboard_json: dict[str, Any],
    ds_mapping: dict[str, str],
    folder_uid: str | None,
) -> dict[str, Any]:
    """
    Подготовить JSON дашборда для импорта в target:
      1. Удалить id, version
      2. Сгенерировать новый UID
      3. Заменить datasource UID в панелях и templating
      4. Обернуть в payload для /api/dashboards/db
    """
    dash = copy.deepcopy(dashboard_json["dashboard"])

    dash.pop("id", None)
    dash.pop("version", None)
    dash["uid"] = uuid.uuid4().hex[:12]

    _replace_ds_uid(dash, ds_mapping)

    payload: dict[str, Any] = {
        "dashboard": dash,
        "overwrite": False,
        "message": "Migrated by grafana-migrate tool",
    }

    # Размещение в папку по folderUid (Grafana 10+ API)
    if folder_uid:
        payload["folderUid"] = folder_uid

    return payload


# ---------------------------------------------------------------------------
# Валидация маппинга
# ---------------------------------------------------------------------------


def validate_ds_mapping(
    ds_mapping: dict[str, str], target: GrafanaInstance
) -> bool:
    """
    Проверить, что все целевые UID из маппинга существуют в target.
    Возвращает True если всё ОК, False при ошибках.
    """
    target_ds = get_target_datasources(target)
    ok = True
    for src_uid, tgt_uid in ds_mapping.items():
        if tgt_uid not in target_ds:
            logger.error(
                "Datasource UID '%s' (маппинг для '%s') не найден в target. "
                "Доступные: %s",
                tgt_uid,
                src_uid,
                ", ".join(f"{u} ({n})" for u, n in target_ds.items()),
            )
            ok = False
        else:
            logger.info(
                "Маппинг OK: %s -> %s (%s)",
                src_uid,
                tgt_uid,
                target_ds[tgt_uid],
            )
    return ok


# ---------------------------------------------------------------------------
# Основной процесс миграции
# ---------------------------------------------------------------------------


def ensure_target_folder(
    target: GrafanaInstance,
    folder_title: str,
    folder_cache: dict[str, str],
    parent_uid: str | None = None,
) -> str:
    """
    Получить или создать папку в target по имени.
    Если parent_uid задан — создаёт/ищет вложенную папку.
    Возвращает folderUid. Кеширует результат.
    """
    cache_key = f"{parent_uid or ''}::{folder_title}"
    if cache_key in folder_cache:
        return folder_cache[cache_key]

    existing = find_folder_by_title(target, folder_title, parent_uid=parent_uid)
    if existing:
        uid = existing["uid"]
        logger.info("Папка '%s' уже существует в target (uid=%s)", folder_title, uid)
    else:
        created = create_folder(target, folder_title, parent_uid=parent_uid)
        uid = created["uid"]
        parent_info = f" (parent={parent_uid})" if parent_uid else ""
        logger.info("Создана папка '%s' в target (uid=%s)%s", folder_title, uid, parent_info)

    folder_cache[cache_key] = uid
    return uid


def migrate(
    source: GrafanaInstance,
    target: GrafanaInstance,
    ds_mapping: dict[str, str],
    *,
    dry_run: bool = False,
    dump_dir: Path | None = None,
    folder_filter: list[str] | None = None,
    max_folders: int | None = None,
    batch_size: int = 5,
    batch_delay: float = 1.0,
    skip_existing: bool = False,
    target_root_folder: str | None = None,
) -> MigrationStats:
    """
    Полный цикл миграции с поддержкой папок и rate-limit:
      1. Получить папки из source (+ General)
      2. Отфильтровать по --folders / --max-folders
      3. Для каждой папки — получить дашборды, перенести батчами

    Если target_root_folder задан, вся структура source помещается внутрь
    этой папки в target:
      target_root_folder / source_folder / dashboard
    """
    stats = MigrationStats()

    if ds_mapping and not validate_ds_mapping(ds_mapping, target):
        logger.error("Маппинг datasource невалиден — прерываем миграцию")
        sys.exit(1)

    # --- Корневая папка в target ---
    root_folder_uid: str | None = None
    if target_root_folder and not dry_run:
        folder_cache_root: dict[str, str] = {}
        root_folder_uid = ensure_target_folder(
            target, target_root_folder, folder_cache_root
        )
        logger.info(
            "Корневая папка в target: '%s' (uid=%s)",
            target_root_folder, root_folder_uid,
        )
    elif target_root_folder and dry_run:
        logger.info("[DRY-RUN] Корневая папка в target: '%s'", target_root_folder)

    # --- Собираем папки ---
    source_folders = get_folders(source)

    # General (id=0) — дашборды без папки
    folder_list: list[dict[str, Any]] = [
        {"id": 0, "title": "General", "uid": ""}
    ]
    folder_list.extend(source_folders) 

    # Фильтр по именам папок
    if folder_filter:
        filter_set = set(folder_filter)
        folder_list = [f for f in folder_list if f["title"] in filter_set]
        logger.info(
            "Фильтр папок: %s -> найдено %d из %d",
            folder_filter, len(folder_list), len(filter_set),
        )
        not_found = filter_set - {f["title"] for f in folder_list}
        if not_found:
            logger.warning("Папки не найдены в source: %s", ", ".join(not_found))

    # Лимит количества папок
    if max_folders and len(folder_list) > max_folders:
        logger.info(
            "Ограничение: обрабатываем %d из %d папок",
            max_folders, len(folder_list),
        )
        folder_list = folder_list[:max_folders]

    logger.info("Папок к обработке: %d", len(folder_list))

    # Кеш folderUid в target, чтобы не создавать повторно
    folder_cache: dict[str, str] = {}
    dashboards_migrated_in_batch = 0

    # --- Цикл по папкам ---
    for folder_idx, folder_meta in enumerate(folder_list, start=1):
        folder_title = folder_meta["title"]
        folder_id = folder_meta["id"]
        folder_prefix = f"[Папка {folder_idx}/{len(folder_list)}]"

        logger.info("%s === %s ===", folder_prefix, folder_title)

        dashboards = get_dashboards_in_folder(source, folder_id)
        if not dashboards:
            logger.info("%s Нет дашбордов, пропускаем", folder_prefix)
            continue

        stats.folders_processed += 1
        stats.total += len(dashboards)
        logger.info("%s Дашбордов: %d", folder_prefix, len(dashboards))

        # Определяем target folder UID
        # Если задан target_root_folder:
        #   General -> дашборды попадают прямо в root_folder
        #   Другая папка -> создаётся как дочерняя внутри root_folder
        target_folder_uid: str | None = None
        if folder_title == "General":
            # General: если есть корневая папка — кладём туда, иначе в корень target
            target_folder_uid = root_folder_uid
        else:
            if not dry_run:
                target_folder_uid = ensure_target_folder(
                    target, folder_title, folder_cache,
                    parent_uid=root_folder_uid,
                )
            else:
                parent_info = f"{target_root_folder}/" if target_root_folder else ""
                target_folder_uid = f"<dry-run:{parent_info}{folder_title}>"

        # Получаем существующие дашборды в target для проверки дубликатов
        existing_titles: set[str] = set()
        if skip_existing and not dry_run:
            target_folder = find_folder_by_title(target, folder_title)
            target_fid = target_folder["id"] if target_folder else 0
            existing_titles = get_existing_dashboard_titles(target, target_fid)
            if existing_titles:
                logger.info(
                    "%s В target уже есть %d дашбордов",
                    folder_prefix, len(existing_titles),
                )

        # --- Цикл по дашбордам внутри папки ---
        for dash_idx, meta in enumerate(dashboards, start=1):
            uid = meta["uid"]
            title = meta.get("title", "<no title>")
            prefix = f"{folder_prefix} [{dash_idx}/{len(dashboards)}]"

            logger.info("%s Обработка: %s (uid=%s)", prefix, title, uid)

            # Пропуск дубликатов
            if skip_existing and title in existing_titles:
                logger.info("%s [SKIP] Уже существует в target: %s", prefix, title)
                stats.skipped += 1
                continue

            try:
                full = get_dashboard(source, uid)
            except requests.HTTPError as exc:
                msg = f"{prefix} Ошибка загрузки {title}: {exc}"
                logger.error(msg)
                stats.failed += 1
                stats.errors.append(msg)
                continue

            payload = transform_dashboard(full, ds_mapping, target_folder_uid)

            if dump_dir:
                dump_dir.mkdir(parents=True, exist_ok=True)
                dump_file = dump_dir / f"{uid}.json"
                dump_file.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.debug("%s JSON сохранён: %s", prefix, dump_file)

            if dry_run:
                logger.info("%s [DRY-RUN] Пропущен: %s", prefix, title)
                stats.skipped += 1
                continue

            try: # остановился тут
                result = create_dashboard(target, payload)
                logger.info(
                    "%s Создан: %s -> %s (status=%s)",
                    prefix, title,
                    result.get("uid", "?"),
                    result.get("status", "?"),
                )
                stats.created += 1
            except requests.HTTPError as exc:
                body = ""
                if exc.response is not None:
                    body = exc.response.text[:500]
                msg = f"{prefix} Ошибка создания {title}: {exc} | {body}"
                logger.error(msg)
                stats.failed += 1
                stats.errors.append(msg)
                continue

            # --- Rate limit ---
            dashboards_migrated_in_batch += 1
            if dashboards_migrated_in_batch >= batch_size:
                logger.info(
                    "Rate limit: перенесено %d дашбордов, пауза %.1f сек...",
                    dashboards_migrated_in_batch, batch_delay,
                )
                time.sleep(batch_delay)
                dashboards_migrated_in_batch = 0

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_ds_mapping(raw: str | None, file: str | None) -> dict[str, str]:
    """
    Загрузить маппинг datasource UID.
    Приоритет: --ds-mapping-file > --ds-mapping > DS_MAPPING env.
    Формат: JSON-объект {"old_uid": "new_uid", ...}
    """
    if file:
        text = Path(file).read_text(encoding="utf-8")
        return json.loads(text)

    src = raw or os.getenv("DS_MAPPING", "")
    if not src.strip():
        return {}
    return json.loads(src)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Grafana Dashboard Migration Tool — "
        "массовый перенос дашбордов между инстансами Grafana v10+",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Примеры:
  # Dry-run:
  python grafana.py --dry-run

  # Только определённые папки:
  python grafana.py --folders "Infrastructure" "Databases" --dry-run

  # Лимит папок + batch по 3 дашборда с паузой 2 сек:
  python grafana.py --max-folders 5 --batch-size 3 --batch-delay 2.0

  # Миграция с маппингом из файла + дамп JSON:
  python grafana.py --ds-mapping-file mapping.json --dump-dir ./dump
        """,
    )
    p.add_argument(
        "--source-url",
        default=os.getenv("SOURCE_GRAFANA_URL", "https://source-grafana.local"),
        help="URL исходной Grafana (default: env SOURCE_GRAFANA_URL)",
    )
    p.add_argument(
        "--source-token",
        default=os.getenv("SOURCE_GRAFANA_TOKEN", ""),
        help="API token исходной Grafana (default: env SOURCE_GRAFANA_TOKEN)",
    )
    p.add_argument(
        "--target-url",
        default=os.getenv("TARGET_GRAFANA_URL", "https://target-grafana.local"),
        help="URL целевой Grafana (default: env TARGET_GRAFANA_URL)",
    )
    p.add_argument(
        "--target-token",
        default=os.getenv("TARGET_GRAFANA_TOKEN", ""),
        help="API token целевой Grafana (default: env TARGET_GRAFANA_TOKEN)",
    )
    p.add_argument(
        "--ds-mapping",
        default=None,
        help='JSON-строка маппинга datasource: \'{"old": "new"}\'',
    )
    p.add_argument(
        "--ds-mapping-file",
        default=None,
        help="Путь к JSON-файлу с маппингом datasource",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Не создавать дашборды в target — только показать план",
    )
    # FOLDERS env: JSON-массив, например: ["Infrastructure", "Databases"]
    env_folders = None
    env_folders_raw = os.getenv("FOLDERS", "")
    if env_folders_raw.strip():
        env_folders = json.loads(env_folders_raw)

    p.add_argument(
        "--folders",
        nargs="+",
        default=env_folders,
        help='Фильтр папок по имени (env FOLDERS — JSON-массив)',
    )
    p.add_argument(
        "--max-folders",
        type=int,
        default=int(os.getenv("MAX_FOLDERS", "0")) or None,
        help="Максимальное кол-во папок (default: env MAX_FOLDERS)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("BATCH_SIZE", "5")),
        help="Кол-во дашбордов в одном батче перед паузой (default: env BATCH_SIZE или 5)",
    )
    p.add_argument(
        "--batch-delay",
        type=float,
        default=float(os.getenv("BATCH_DELAY", "1.0")),
        help="Пауза между батчами в секундах (default: env BATCH_DELAY или 1.0)",
    )
    p.add_argument(
        "--dump-dir",
        default=None,
        help="Директория для сохранения трансформированных JSON",
    )
    p.add_argument(
        "--target-folder",
        default=os.getenv("TARGET_FOLDER", ""),
        help="Корневая папка в target, внутри которой создаётся вся структура "
             "(default: env TARGET_FOLDER). Если не задана — папки создаются в корне target",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Пропускать дашборды, если в target уже есть дашборд с таким именем",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Отключить проверку SSL-сертификатов (для самоподписанных/корпоративных сертификатов)",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Подробный вывод (DEBUG)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not args.source_token:
        logger.error(
            "Не задан токен source Grafana. "
            "Укажите SOURCE_GRAFANA_TOKEN в .env или --source-token"
        )
        sys.exit(1)
    if not args.target_token:
        logger.error(
            "Не задан токен target Grafana. "
            "Укажите TARGET_GRAFANA_TOKEN в .env или --target-token"
        )
        sys.exit(1)

    verify_ssl = not args.no_verify
    if not verify_ssl:
        logger.warning("SSL-верификация отключена (--no-verify)")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    source = GrafanaInstance(url=args.source_url, token=args.source_token, verify=verify_ssl)
    target = GrafanaInstance(url=args.target_url, token=args.target_token, verify=verify_ssl)

    ds_mapping = load_ds_mapping(args.ds_mapping, args.ds_mapping_file)

    logger.info("Source: %s", source.url)
    logger.info("Target: %s", target.url)
    logger.info("Datasource mapping: %d записей", len(ds_mapping))
    logger.info("Dry-run: %s", args.dry_run)
    logger.info("Rate limit: %d дашбордов / %.1f сек пауза",
                args.batch_size, args.batch_delay)
    if args.folders:
        logger.info("Фильтр папок: %s", args.folders)
    if args.max_folders:
        logger.info("Лимит папок: %d", args.max_folders)

    target_root = args.target_folder.strip() or None
    if target_root:
        logger.info("Корневая папка в target: '%s'", target_root)

    dump_dir = Path(args.dump_dir) if args.dump_dir else None

    stats = migrate(
        source,
        target,
        ds_mapping,
        dry_run=args.dry_run,
        dump_dir=dump_dir,
        folder_filter=args.folders,
        max_folders=args.max_folders,
        batch_size=args.batch_size,
        batch_delay=args.batch_delay,
        skip_existing=args.skip_existing,
        target_root_folder=target_root,
    )

    # Итоговый отчёт
    logger.info("=" * 60)
    logger.info("РЕЗУЛЬТАТ МИГРАЦИИ")
    logger.info("  Папок обработано : %d", stats.folders_processed)
    logger.info("  Всего дашбордов : %d", stats.total)
    logger.info("  Создано          : %d", stats.created)
    logger.info("  Пропущено (dry)  : %d", stats.skipped)
    logger.info("  Ошибок           : %d", stats.failed)
    if stats.errors:
        logger.info("ОШИБКИ:")
        for err in stats.errors:
            logger.info("  - %s", err)
    logger.info("=" * 60)

    if stats.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
