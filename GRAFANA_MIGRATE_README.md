# Grafana Dashboard Migration Tool

Массовый перенос дашбордов между двумя инстансами Grafana v10+ через HTTP API.

## Возможности

- Перенос **всех** дашбордов из source в target (с пагинацией)
- **Сохранение структуры папок** — папки автоматически создаются в target
- **Фильтрация по папкам** — можно указать конкретные папки для переноса
- **Rate limiting** — конфигурируемый размер батча и пауза между батчами
- Замена datasource UID в панелях и templating-переменных (рекурсивно)
- Валидация маппинга: проверка существования datasource в target перед миграцией
- **dry-run** режим — посмотреть что будет сделано без реальных изменений
- Дамп трансформированных JSON на диск (`--dump-dir`)
- Подробное логирование и итоговый отчёт
- Все параметры задаются через CLI **или** `.env` файл

## Быстрый старт

### 1. Установка зависимостей

```bash
# Создаем виртуальное окружение, чтобы не путать зависимости
python -m venv .venv
# Linux/macOS:
# source .venv/bin/activate

# Активирует виртуальное окружение в текущей сессии терминала.
# Windows:
.venv\Scripts\activate

# Устанавливаем зависимости
pip install -r requirements.txt
```

python -m venv .venv

.venv\Scripts\activate

pip install -r requirements.txt

### 2. Настройка окружения

```bash
cp .env.example .env
# Отредактируйте .env — укажите URL и токены обоих инстансов
```

### 3. Получение API-токенов

В каждом инстансе Grafana:
1. **Administration → Service Accounts → Add service account**
2. Роль: `Admin` (нужен доступ к datasources и dashboards)
3. **Add service account token** → скопировать токен

### 4. Запуск

```bash
# Dry-run (ничего не создаёт, только показывает план):
python grafana.py --dry-run

# Реальная миграция:
python grafana.py

# С маппингом datasource (inline JSON):
python grafana.py --ds-mapping '{"abc123": "xyz789", "def456": "uvw012"}'

# С маппингом из файла:
python grafana.py --ds-mapping-file mapping.json

# С дампом JSON + verbose:
python grafana.py --dump-dir ./dump -v
```

## Параметры CLI

| Флаг                | Описание                                  | Источник по умолчанию       |
| ------------------- | ----------------------------------------- | --------------------------- |
| `--source-url`      | URL source Grafana                        | `SOURCE_GRAFANA_URL` env    |
| `--source-token`    | API token source                          | `SOURCE_GRAFANA_TOKEN` env  |
| `--target-url`      | URL target Grafana                        | `TARGET_GRAFANA_URL` env    |
| `--target-token`    | API token target                          | `TARGET_GRAFANA_TOKEN` env  |
| `--ds-mapping`      | JSON-строка маппинга datasource UID       | `DS_MAPPING` env            |
| `--ds-mapping-file` | Путь к JSON-файлу с маппингом             | —                           |
| `--dry-run`         | Не создавать дашборды, только логировать  | —                           |
| `--folders`         | Фильтр: переносить только указанные папки | `FOLDERS` env (JSON-массив) |
| `--max-folders`     | Максимальное кол-во папок для переноса    | `MAX_FOLDERS` env           |
| `--batch-size`      | Кол-во дашбордов в батче перед паузой     | `BATCH_SIZE` env (5)        |
| `--batch-delay`     | Пауза между батчами в секундах            | `BATCH_DELAY` env (1.0)     |
| `--target-folder`   | Корневая папка в target для всей структуры | `TARGET_FOLDER` env        |
| `--dump-dir`        | Папка для сохранения JSON                 | —                           |
| `--skip-existing`   | Пропускать дашборды, уже существующие в target по имени | —               |
| `--no-verify`       | Отключить проверку SSL-сертификатов       | —                           |
| `-v, --verbose`     | DEBUG-уровень логирования                 | —                           |

## Формат маппинга datasource

JSON-объект `{"source_uid": "target_uid"}`:

```json
{
  "prometheus-source-uid": "prometheus-target-uid",
  "loki-source-uid": "loki-target-uid"
}
```

Как узнать UID datasource:
```bash
# Source:
curl -H "Authorization: Bearer $SOURCE_GRAFANA_TOKEN" \
  https://source-grafana.local/api/datasources | jq '.[].uid'

# Target:
curl -H "Authorization: Bearer $TARGET_GRAFANA_TOKEN" \
  https://target-grafana.local/api/datasources | jq '.[].uid'
```

## Фильтрация по папкам

Скрипт автоматически сохраняет структуру папок из source в target.
Можно ограничить перенос конкретными папками:

```bash
# Только 2 папки
python grafana.py --folders "Infrastructure" "Databases"

# Первые 3 папки (по порядку из source)
python grafana.py --max-folders 3
```

Или через `.env`:
```
FOLDERS=["Infrastructure", "Databases", "Monitoring"]
MAX_FOLDERS=3
```

## Корневая папка в target (--target-folder)

По умолчанию папки из source создаются в корне target Grafana.
Флаг `--target-folder` помещает всю структуру внутрь указанной папки:

```bash
python grafana.py --target-folder "Migration-2026"
```

Или через `.env`:
```
TARGET_FOLDER=Migration-2026
```

Результат — вложенная структура:
```
Target Grafana:
└── Migration-2026/                ← --target-folder (создаётся автоматически)
    ├── Infrastructure/            ← папка из source
    │   ├── CPU Usage Overview
    │   └── Memory Usage
    ├── Databases/                 ← папка из source
    │   ├── Active Connections
    │   └── Query Rate
    └── Home Dashboard             ← дашборд из General (без вложенной папки)
```

Если папка `Migration-2026` уже существует — скрипт использует её, не создавая повторно.

## Rate limiting

Скрипт переносит дашборды порциями (batch). После каждой порции — пауза,
чтобы не перегружать API целевой Grafana.

```bash
# 3 дашборда за раз, пауза 2 секунды
python grafana.py --batch-size 3 --batch-delay 2.0
```

Или через `.env`:
```
BATCH_SIZE=5
BATCH_DELAY=1.0
```

Счётчик сквозной — он НЕ сбрасывается при переходе к следующей папке.
Например, при `--batch-size 5`: если в первой папке было 3 дашборда,
пауза сработает после 2-го дашборда из второй папки (3 + 2 = 5).

## Пример вывода

```
12:00:01 | INFO     | Source: https://source-grafana.local
12:00:01 | INFO     | Target: https://target-grafana.local
12:00:01 | INFO     | Datasource mapping: 1 записей
12:00:01 | INFO     | Dry-run: True
12:00:01 | INFO     | Rate limit: 5 / 1.0 сек пауза
12:00:01 | INFO     | Найдено папок: 5
12:00:01 | INFO     | Папок к обработке: 5
12:00:02 | INFO     | [Папка 1/5] === Infrastructure ===
12:00:02 | INFO     | [Папка 1/5] [1/5] Обработка: CPU Usage Overview (uid=infra-cpu)
12:00:02 | INFO     | [Папка 1/5] [1/5] [DRY-RUN] Пропущен: CPU Usage Overview
...
12:00:05 | INFO     | ============================================================
12:00:05 | INFO     | РЕЗУЛЬТАТ МИГРАЦИИ
12:00:05 | INFO     |   Папок обработано : 5
12:00:05 | INFO     |   Всего дашбордов : 25
12:00:05 | INFO     |   Создано          : 0
12:00:05 | INFO     |   Пропущено (dry)  : 25
12:00:05 | INFO     |   Ошибок           : 0
12:00:05 | INFO     | ============================================================
```

## SSL-сертификаты

Если Grafana использует самоподписанный или корпоративный сертификат, запросы упадут с ошибкой `SSL: CERTIFICATE_VERIFY_FAILED`. Решения:

```bash
# Быстрый способ — отключить проверку SSL:
python grafana.py --no-verify --dry-run

# Правильный способ — указать корневой CA-сертификат:
export REQUESTS_CA_BUNDLE=/path/to/corporate-ca.pem
python grafana.py --dry-run
```

## Требования

- Python 3.11+
- Grafana 10+
- Service Account с ролью Admin на обоих инстансах