"""Прогноз очереди на Бресте.

Идея простая и прозрачная (для дипломного уровня объяснимо):
1. Профиль: медиана очереди по (день недели × час) за последние 4 недели.
   Если для какой-то ячейки мало данных — берём медиану по часу, потом общую.
2. Праздники: дни рядом с праздниками умножаем на коэффициент. Если в истории
   праздники уже были — коэффициент считается по данным, иначе 1.3.
3. Текущее отклонение: если сейчас очередь сильно выше/ниже обычного, это
   отклонение затухает в прогнозе с полупериодом 6 часов.
4. Ожидание (часы) = очередь / пропускная способность (машин в час),
   пропускная способность оценивается по тому, как быстро очередь убывает.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import config

TZ = "Europe/Minsk"
SOURCE_PRIORITY = {"belarusborder": 0, "nakordoni": 0, "gpk": 1}
HALF_LIFE_H = 6.0
DEFAULT_HOLIDAY_FACTOR = 1.3


def load(path: str | None = None) -> pd.DataFrame:
    path = path or config.DATA_URL or config.DATA_FILE
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True).dt.tz_convert(TZ)
    for c in ["queue_count", "wait_minutes", "oldest_wait_min", "median_wait_min"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["prio"] = df["source"].map(SOURCE_PRIORITY).fillna(9)
    return df


def series(df: pd.DataFrame, direction: str, vehicle: str) -> pd.DataFrame:
    """Ряд с шагом 15 минут, при дублях берём лучший источник."""
    d = df[(df.direction == direction) & (df.vehicle_type == vehicle)].copy()
    if d.empty:
        return d
    d = d.sort_values(["ts", "prio"]).drop_duplicates("ts", keep="first")
    d = d.set_index("ts")[["queue_count", "wait_minutes", "oldest_wait_min", "median_wait_min"]]
    return d.resample("15min").mean().interpolate(limit=4)


def holiday_flags(index: pd.DatetimeIndex) -> np.ndarray:
    hol = pd.to_datetime(config.HOLIDAYS).date
    near = set()
    for h in hol:
        for k in (-1, 0, 1):
            near.add(h + timedelta(days=k))
    s0, s1 = (pd.Timestamp(x).date() for x in getattr(config, "HOLIDAY_SEASON", ("1900-01-01", "1900-01-01")))
    return np.array([(ts.date() in near) or (s0 <= ts.date() <= s1) for ts in index])


def throughput_per_hour(s: pd.DataFrame, vehicle: str) -> float:
    """Сколько машин в час уходит из очереди (по положительным падениям)."""
    default = config.DEFAULT_BUSES_PER_HOUR if vehicle == "bus" else config.DEFAULT_CARS_PER_HOUR
    if s.empty or len(s) < 16:
        return default
    q = s["queue_count"].dropna()
    drops = (-q.diff()).clip(lower=0)
    hourly = drops.resample("1h").sum()
    hourly = hourly[q.resample("1h").mean() > 0]  # только часы, когда очередь была
    if len(hourly) < 6:
        return default
    est = float(hourly.quantile(0.75))  # падения занижены из-за новых приезжающих
    return est if est >= 0.5 else default


def wait_hours(queue: float, tp: float) -> float:
    return float(queue) / tp if tp > 0 else float("nan")


def build_profile(s: pd.DataFrame, weeks: int = 4):
    q = s["queue_count"].dropna()
    if q.empty:
        return None
    q = q[q.index >= q.index.max() - pd.Timedelta(weeks=weeks)]
    flags = holiday_flags(q.index)
    normal = q[~flags]
    base = normal if len(normal) > 0 else q
    frame = pd.DataFrame({"q": base, "wd": base.index.weekday, "h": base.index.hour})
    prof = {
        "wd_h": frame.groupby(["wd", "h"])["q"].agg(["median", "count",
                                                     lambda x: x.quantile(0.2),
                                                     lambda x: x.quantile(0.8)]),
        "h": frame.groupby("h")["q"].agg(["median", lambda x: x.quantile(0.2), lambda x: x.quantile(0.8)]),
        "all": (float(base.median()), float(base.quantile(0.2)), float(base.quantile(0.8))),
    }
    for p in ("wd_h", "h"):
        cols = list(prof[p].columns)
        cols[-2:] = ["p20", "p80"]
        prof[p].columns = cols
    # Коэффициент праздников по данным, если они уже были в истории
    hol = q[flags]
    if len(hol) >= 16 and len(normal) >= 16 and normal.median() > 0:
        prof["holiday_factor"] = float(np.clip(hol.median() / normal.median(), 0.5, 3.0))
    else:
        prof["holiday_factor"] = DEFAULT_HOLIDAY_FACTOR
    return prof


def profile_value(prof, ts: pd.Timestamp):
    key = (ts.weekday(), ts.hour)
    t = prof["wd_h"]
    if key in t.index and t.loc[key, "count"] >= 3:
        r = t.loc[key]
        return r["median"], r["p20"], r["p80"]
    if ts.hour in prof["h"].index:
        r = prof["h"].loc[ts.hour]
        return r["median"], r["p20"], r["p80"]
    return prof["all"]


def forecast(s: pd.DataFrame, vehicle: str, days: int = 7, now: pd.Timestamp | None = None) -> pd.DataFrame:
    prof = build_profile(s)
    if prof is None:
        return pd.DataFrame()
    q = s["queue_count"].dropna()
    now = now or q.index.max()
    start = now.floor("h") + pd.Timedelta(hours=1)
    idx = pd.date_range(start, periods=days * 24, freq="1h", tz=TZ)
    flags = holiday_flags(idx)
    cur_med, _, _ = profile_value(prof, now)
    deviation = float(q.iloc[-1]) - float(cur_med)
    tp = throughput_per_hour(s, vehicle)
    rows = []
    for ts, hol in zip(idx, flags):
        med, lo, hi = profile_value(prof, ts)
        f = prof["holiday_factor"] if hol else 1.0
        hours_ahead = (ts - now).total_seconds() / 3600
        decay = 0.5 ** (hours_ahead / HALF_LIFE_H)
        yhat = max(0.0, med * f + deviation * decay)
        rows.append({
            "ts": ts, "queue_pred": yhat,
            "queue_lo": max(0.0, lo * f + deviation * decay * 0.5),
            "queue_hi": max(0.0, hi * f + deviation * decay * 1.5),
            "wait_h_pred": wait_hours(yhat, tp), "holiday": bool(hol),
        })
    out = pd.DataFrame(rows).set_index("ts")
    out.attrs["throughput_per_hour"] = tp
    out.attrs["holiday_factor"] = prof["holiday_factor"]
    return out


def level_trend(s: pd.DataFrame) -> float:
    """Во сколько раз последняя неделя загруженнее предыдущих трёх (0.7–1.5)."""
    q = s["queue_count"].dropna()
    if q.empty or (q.index.max() - q.index.min()) < pd.Timedelta(days=14):
        return 1.0
    last = q[q.index > q.index.max() - pd.Timedelta(days=7)]
    prev = q[(q.index <= q.index.max() - pd.Timedelta(days=7)) &
             (q.index > q.index.max() - pd.Timedelta(days=28))]
    if prev.median() <= 0:
        return 1.0
    return float(np.clip(last.median() / prev.median(), 0.7, 1.5))


def forecast_point(s: pd.DataFrame, vehicle: str, when: pd.Timestamp) -> dict | None:
    """Прогноз на конкретный момент, в том числе через несколько недель.

    Ближе 7 дней — обычный почасовой прогноз (с учётом текущей ситуации).
    Дальше — профиль «день недели × час» за 8 недель × праздничный коэффициент ×
    тренд последней недели (затухает с горизонтом). Чем дальше дата и чем меньше
    истории, тем шире интервал и ниже уверенность.
    """
    q = s["queue_count"].dropna()
    if q.empty:
        return None
    when = when.tz_convert(TZ) if when.tzinfo else when.tz_localize(TZ)
    now = q.index.max()
    horizon_d = (when - now).total_seconds() / 86400
    if horizon_d < 0 or horizon_d > config.MAX_HORIZON_DAYS:
        return None
    history_d = (now - q.index.min()).total_seconds() / 86400
    tp = throughput_per_hour(s, vehicle)

    if horizon_d <= 7:
        fc = forecast(s, vehicle, days=8)
        row = at_time(fc, when)
        if row is None:
            return None
        pred, lo, hi = row.queue_pred, row.queue_lo, row.queue_hi
        hol = bool(row.holiday)
        hf = fc.attrs["holiday_factor"]
    else:
        prof = build_profile(s, weeks=8)
        med, lo, hi = profile_value(prof, when)
        hol = bool(holiday_flags(pd.DatetimeIndex([when]))[0])
        hf = prof["holiday_factor"]
        f = hf if hol else 1.0
        trend = 1 + (level_trend(s) - 1) * np.exp(-horizon_d / 21)
        pred = med * f * trend
        widen = 1 + horizon_d / 14
        lo = max(0.0, pred - (med - lo) * f * widen)
        hi = pred + (hi - med) * f * widen

    # Минимальная честная неопределённость: растёт с горизонтом
    rel = 0.15 + 0.02 * horizon_d
    lo = min(lo, pred * (1 - rel))
    hi = max(hi, pred * (1 + rel) + 0.5)
    lo = max(0.0, lo)

    if history_d < 7 or horizon_d > 30:
        conf = "низкая"
    elif history_d < 21 or horizon_d > 7:
        conf = "средняя"
    else:
        conf = "высокая"
    return {
        "target_ts": when, "queue_pred": float(pred), "queue_lo": float(lo), "queue_hi": float(hi),
        "wait_h_pred": wait_hours(pred, tp), "wait_h_lo": wait_hours(lo, tp), "wait_h_hi": wait_hours(hi, tp),
        "horizon_days": round(horizon_d, 1), "history_days": round(history_d, 1),
        "holiday": hol, "holiday_factor": float(hf), "confidence": conf,
        "holiday_factor_learned": bool(hf != DEFAULT_HOLIDAY_FACTOR),
    }


def trip_border_time(trip: dict) -> pd.Timestamp:
    dep = pd.Timestamp(trip["departure"]).tz_localize(trip.get("departure_tz", TZ))
    return (dep + pd.Timedelta(hours=float(trip["travel_hours"]))).tz_convert(TZ)


def best_times(fc: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    if fc.empty:
        return fc
    return fc.nsmallest(n, "queue_pred")[["queue_pred", "wait_h_pred"]]


def at_time(fc: pd.DataFrame, when: pd.Timestamp):
    if fc.empty:
        return None
    when = when.tz_convert(TZ) if when.tzinfo else when.tz_localize(TZ)
    if when < fc.index.min() or when > fc.index.max():
        return None
    return fc.iloc[fc.index.get_indexer([when], method="nearest")[0]]


def backtest(s: pd.DataFrame, vehicle: str, test_days: int = 3) -> dict | None:
    """MAE прогноза на последние test_days против «наивного» (последнее значение)."""
    q = s["queue_count"].dropna()
    if q.empty or (q.index.max() - q.index.min()) < pd.Timedelta(days=test_days + 7):
        return None
    cut = q.index.max() - pd.Timedelta(days=test_days)
    train, test = s[s.index <= cut], q[q.index > cut].resample("1h").mean().dropna()
    fc = forecast(train, vehicle, days=test_days + 1, now=train["queue_count"].dropna().index.max())
    joined = fc.join(test.rename("actual"), how="inner")
    if joined.empty:
        return None
    naive = float(train["queue_count"].dropna().iloc[-1])
    return {
        "mae_model": float((joined.queue_pred - joined.actual).abs().mean()),
        "mae_naive": float((naive - joined.actual).abs().mean()),
        "hours": len(joined),
    }
