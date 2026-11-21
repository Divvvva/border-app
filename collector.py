"""Сборщик очередей на переходе Брест – Тересполь.

Запускается каждые 15 минут (GitHub Actions) и дописывает строки в data/queue.csv.
Каждый источник обёрнут в try: если один упал, остальные всё равно сохранятся.

Колонки CSV:
  ts_utc, direction (exit_by = выезд из Беларуси, entry_by = въезд),
  vehicle_type (bus / car / truck), queue_count,
  wait_minutes (оценка ожидания от источника, если есть),
  oldest_wait_min / median_wait_min (сколько уже стоят машины в очереди, если есть),
  source, note
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import config

MINSK = ZoneInfo("Europe/Minsk")
FIELDS = [
    "ts_utc", "direction", "vehicle_type", "queue_count", "wait_minutes",
    "oldest_wait_min", "median_wait_min", "source", "note",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (personal border-queue monitor; student project)"}
TIMEOUT = 25


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def save_raw(name: str, payload) -> None:
    os.makedirs(config.RAW_DIR, exist_ok=True)
    path = os.path.join(config.RAW_DIR, f"last_{name}")
    mode_json = not isinstance(payload, str)
    with open(path + (".json" if mode_json else ".html"), "w", encoding="utf-8") as f:
        if mode_json:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        else:
            f.write(payload[:300_000])


# ---------------------------------------------------------------- belarusborder
VEHICLE_KEYS = {
    "bus": ["busLiveQueue", "busQueue", "bus"],
    "car": ["carLiveQueue", "carQueue", "car"],
    "truck": ["truckLiveQueue", "truckQueue", "truck"],
}
DATE_FORMATS = ["%H:%M:%S %d.%m.%Y", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]


def parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=MINSK)
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=MINSK)
        except ValueError:
            continue
    return None


def find_checkpoint_id(checkpoints) -> str | None:
    items = checkpoints.get("result", checkpoints) if isinstance(checkpoints, dict) else checkpoints
    if not isinstance(items, list):
        return None
    for cp in items:
        if not isinstance(cp, dict):
            continue
        name = str(cp.get("name", "")).lower()
        if any(alias in name for alias in config.CHECKPOINT_NAME_ALIASES):
            return cp.get("id")
    return None


def summarize_queue(vehicles: list, ts: datetime) -> dict:
    """Количество машин и сколько они уже стоят (по дате регистрации в очереди)."""
    ages = []
    for v in vehicles:
        if not isinstance(v, dict):
            continue
        reg = parse_dt(v.get("registration_date") or v.get("registrationDate"))
        if reg:
            ages.append((ts - reg).total_seconds() / 60)
    ages = [a for a in ages if 0 <= a < 60 * 24 * 7]
    return {
        "queue_count": len(vehicles),
        "oldest_wait_min": round(max(ages)) if ages else "",
        "median_wait_min": round(statistics.median(ages)) if ages else "",
    }


def parse_belarusborder(queue_json: dict, ts: datetime) -> list[dict]:
    rows = []
    for vtype, keys in VEHICLE_KEYS.items():
        vehicles = None
        for k in keys:
            if isinstance(queue_json.get(k), list):
                vehicles = queue_json[k]
                break
        if vehicles is None:
            continue
        s = summarize_queue(vehicles, ts)
        statuses = {}
        for v in vehicles:
            if isinstance(v, dict) and "status" in v:
                statuses[str(v["status"])] = statuses.get(str(v["status"]), 0) + 1
        rows.append({
            "direction": "exit_by", "vehicle_type": vtype, "wait_minutes": "",
            "source": "belarusborder",
            "note": json.dumps({"status_counts": statuses}) if statuses else "",
            **s,
        })
    return rows


def parse_checkpoint_summary(checkpoints) -> list[dict]:
    """Формат таблицы «Зоны ожидания» (mon.declarant.by): у каждого пункта
    готовые счётчики countCar / countTruck / countBus."""
    items = checkpoints.get("result", checkpoints) if isinstance(checkpoints, dict) else checkpoints
    if not isinstance(items, list):
        return []
    for cp in items:
        if not isinstance(cp, dict):
            continue
        name = str(cp.get("name", "")).lower()
        if not any(alias in name for alias in config.CHECKPOINT_NAME_ALIASES):
            continue
        if "countBus" not in cp:
            return []
        note = json.dumps({k: cp.get(k) for k in ("countLiveQueue", "countBookings", "countPriority", "countAll")})
        rows = []
        for vtype, key in (("car", "countCar"), ("truck", "countTruck"), ("bus", "countBus")):
            rows.append({
                "direction": "exit_by", "vehicle_type": vtype, "queue_count": cp.get(key, ""),
                "wait_minutes": "", "oldest_wait_min": "", "median_wait_min": "",
                "source": "belarusborder", "note": note,
            })
        return rows
    return []


def collect_belarusborder(ts: datetime) -> list[dict]:
    r = requests.get(config.BELARUSBORDER_CHECKPOINTS_URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    checkpoints = r.json()
    save_raw("belarusborder_checkpoints", checkpoints)
    summary = parse_checkpoint_summary(checkpoints)
    if summary:
        # Дополнительно: список машин в очереди с временем регистрации —
        # даёт «сколько уже стоят». Если не получилось, хватит и счётчиков.
        try:
            cid = find_checkpoint_id(checkpoints)
            r2 = requests.get(config.BELARUSBORDER_QUEUE_URL.format(cid=cid), headers=HEADERS, timeout=TIMEOUT)
            r2.raise_for_status()
            detail = r2.json()
            save_raw("belarusborder_queue", detail)
            ages = {r["vehicle_type"]: r for r in parse_belarusborder(detail, ts)}
            for row in summary:
                a = ages.get(row["vehicle_type"])
                if a:
                    row["oldest_wait_min"] = a["oldest_wait_min"]
                    row["median_wait_min"] = a["median_wait_min"]
        except Exception as e:  # noqa: BLE001
            print(f"belarusborder: детали очереди недоступны ({e}), сохраняю только счётчики")
        return summary
    cid = find_checkpoint_id(checkpoints)
    if not cid:
        raise RuntimeError("Брест не найден в списке пунктов belarusborder")
    r = requests.get(config.BELARUSBORDER_QUEUE_URL.format(cid=cid), headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    save_raw("belarusborder_queue", data)
    rows = parse_belarusborder(data, ts)
    if not rows:
        raise RuntimeError("В ответе belarusborder нет списков очереди — посмотри data/raw/last_belarusborder_queue.json")
    return rows


# ---------------------------------------------------------------- gpk.gov.by (резерв)
def parse_gpk_html(html: str) -> list[dict]:
    """Берёт три числа сразу после 'exit from RB' (выезд из РБ)."""
    from bs4 import BeautifulSoup

    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = re.search(r"exit from RB\s+(\d+)\s+(\d+)\s+(\d+)", text, flags=re.IGNORECASE)
    if not m:
        return []
    rows = []
    for vtype, num in zip(config.GPK_NUMBER_ORDER, m.groups()):
        rows.append({
            "direction": "exit_by", "vehicle_type": vtype, "queue_count": int(num),
            "wait_minutes": "", "oldest_wait_min": "", "median_wait_min": "",
            "source": "gpk", "note": "order_unverified",
        })
    return rows


def collect_gpk(ts: datetime) -> list[dict]:
    r = requests.get(config.GPK_BREST_URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    save_raw("gpk_brest", r.text)
    rows = parse_gpk_html(r.text)
    if not rows:
        raise RuntimeError("Не нашла числа очереди на странице ГПК — посмотри data/raw/last_gpk_brest.html")
    return rows


# ---------------------------------------------------------------- nakordoni (въезд)
def parse_nakordoni(payload: dict, direction: str = "entry_by") -> list[dict]:
    data = payload.get("data", payload)
    snap = data.get("snapshot", data) if isinstance(data, dict) else {}
    rows = []
    # Если API отдаёт разбивку по типам транспорта — берём её, иначе общую очередь
    by_type = snap.get("by_vehicle") or snap.get("vehicles") or {}
    mapping = {"bus": ["bus", "buses"], "car": ["car", "cars"], "truck": ["truck", "trucks"]}
    for vtype, keys in mapping.items():
        for k in keys:
            if isinstance(by_type, dict) and k in by_type:
                v = by_type[k]
                q = v.get("queue_now", v.get("count")) if isinstance(v, dict) else v
                w = v.get("wait_min", "") if isinstance(v, dict) else ""
                rows.append({"vehicle_type": vtype, "queue_count": q, "wait_minutes": w})
                break
    if not rows and ("queue_now" in snap or "wait_min" in snap):
        rows.append({"vehicle_type": "all", "queue_count": snap.get("queue_now", ""),
                     "wait_minutes": snap.get("wait_min", "")})
    for r in rows:
        r.update({"direction": direction, "oldest_wait_min": "", "median_wait_min": "",
                  "source": "nakordoni", "note": ""})
    return rows


def collect_nakordoni(ts: datetime, direction: str = "entry_by") -> list[dict]:
    key = os.environ.get("NAKORDONI_API_KEY")
    env_name = "NAKORDONI_PPID_ENTRY" if direction == "entry_by" else "NAKORDONI_PPID_EXIT"
    ppid = os.environ.get(env_name)
    if not ppid:
        print(f"nakordoni ({direction}): пропуск (нет {env_name})")
        return []
    headers = dict(HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key
    r = requests.get(config.NAKORDONI_QUEUE_URL, params={"ppid": ppid}, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    save_raw(f"nakordoni_{direction}", payload)
    return parse_nakordoni(payload, direction)


# ---------------------------------------------------------------- main
def append_rows(rows: list[dict], ts: datetime, path: str = config.DATA_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({**{k: "" for k in FIELDS}, **r, "ts_utc": ts.isoformat()})


def main() -> int:
    ts = now_utc()
    all_rows, errors = [], []

    # Выезд из Беларуси: электронная очередь (отдельно автобусы), резерв — ГПК
    exit_rows = []
    sources = (
        ("belarusborder", collect_belarusborder),
        ("gpk", collect_gpk),
    )
    for name, fn in sources:
        try:
            exit_rows = fn(ts)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            continue
        if exit_rows:
            break
    all_rows += exit_rows

    # Nakordoni в архив НЕ пишем: их условия запрещают хранить отдельные значения.
    # Их данные показываются вживую в приложении (app.py), см. README.

    if all_rows:
        append_rows(all_rows, ts)
    for r in all_rows:
        print(f"{r['direction']:8} {r['vehicle_type']:6} очередь={r['queue_count']} "
              f"ждут_дольше_всех={r.get('oldest_wait_min')} мин  [{r['source']}]")
    for e in errors:
        print("ОШИБКА", e, file=sys.stderr)
    # Не роняем GitHub Action, если хоть что-то собрали
    return 0 if all_rows else 1


if __name__ == "__main__":
    sys.exit(main())
