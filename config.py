"""Настройки проекта. Меняй здесь, а не в коде."""

# Переход: Брест (BY) – Тересполь (PL)
CHECKPOINT_NAME = "Брест"
CHECKPOINT_NAME_ALIASES = ["брест", "brest", "варшавский мост"]

# Страница пункта пропуска на сайте Госпогранкомитета (резервный источник)
GPK_BREST_URL = "https://gpk.gov.by/en/situation-at-the-border/information-about-checkpoints/brest"
# На странице после "exit from RB" идут три числа без подписей (подписи — иконки).
# Порядок ниже — предположение, сверь с сайтом при первом запуске.
GPK_NUMBER_ORDER = ["car", "truck", "bus"]

# Электронная очередь Беларуси (выезд из РБ). Адреса взяты из открытых проектов,
# при первом запуске проверь, что они отвечают (см. README, шаг 4).
BELARUSBORDER_CHECKPOINTS_URL = "https://belarusborder.by/info/checkpoint?token=test"
BELARUSBORDER_QUEUE_URL = "https://belarusborder.by/info/monitoring-new?token=test&checkpointId={cid}"

# Nakordoni API (въезд в РБ, польская сторона). Ключ и id перехода кладутся
# в GitHub Secrets: NAKORDONI_API_KEY, NAKORDONI_PPID_ENTRY. Без них источник пропускается.
NAKORDONI_QUEUE_URL = "https://nakordoni.eu/api/v1/data/queue"

DATA_FILE = "data/queue.csv"
# Если приложение живёт отдельно от данных (например, данные на GitLab),
# укажи в переменной окружения DATA_URL raw-ссылку на queue.csv
import os as _os
DATA_URL = _os.environ.get("DATA_URL", "")
RAW_DIR = "data/raw"  # последние ответы источников, для отладки

# Сколько автобусов в час проходит пункт пропуска, если своей истории ещё мало.
# Это стартовая оценка, модель заменит её реальной по данным.
DEFAULT_BUSES_PER_HOUR = 2.0
DEFAULT_CARS_PER_HOUR = 2350 / 24  # пропускная способность Бреста по данным ГПК

# Время в пути до границы (часы), можно менять в приложении
TRAVEL_HOURS = {"Витебск → Брест": 9.5, "Прага → Тересполь": 10.0}

# Праздники, вокруг которых обычно пики (BY / PL / CZ)
HOLIDAYS = [
    "2026-12-24", "2026-12-25", "2026-12-26", "2026-12-31",
    "2027-01-01", "2027-01-02", "2027-01-06", "2027-01-07",
]
# Весь период праздничных поездок тоже считаем «особым»: модель сравнит его
# с обычными днями, как только накопит такие данные
HOLIDAY_SEASON = ("2026-12-20", "2027-01-10")

# Мои поездки: прогноз на них пересчитывается каждый запуск и пишется
# в data/forecast_log.csv, чтобы видеть, как он уточняется ближе к дате.
# direction: entry_by = въезд в Беларусь (Прага → Витебск), exit_by = выезд (Витебск → Прага)
# departure — время отправления автобуса (местное время города отправления)
MY_TRIPS = [
    {"name": "Домой 28.12", "direction": "entry_by", "vehicle": "bus",
     "departure": "2026-12-28T08:00", "departure_tz": "Europe/Prague", "travel_hours": 10.0},
    {"name": "Домой 29.12", "direction": "entry_by", "vehicle": "bus",
     "departure": "2026-12-29T08:00", "departure_tz": "Europe/Prague", "travel_hours": 10.0},
    # Обратно — впиши дату, когда определишься:
    # {"name": "Обратно 05.01", "direction": "exit_by", "vehicle": "bus",
    #  "departure": "2027-01-05T20:00", "departure_tz": "Europe/Minsk", "travel_hours": 9.5},
]
FORECAST_LOG = "data/forecast_log.csv"
MAX_HORIZON_DAYS = 60
