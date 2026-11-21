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
SOURCE_PRIORITY = {"belarusborder": 0, "nakordoni": 0, "gpk": 1, "gpk_archive": 2}
HALF_LIFE_H = 6.0
DEFAULT_HOLIDAY_FACTOR = 1.3


def load(path: str | None = None, with_archive: bool = True) -> pd.DataFrame:
    path = path or config.DATA_URL or config.DATA_FILE
    import os
    if path == config.DATA_FILE and not os.path.exists(path):
        df = pd.DataFrame(columns=["ts_utc", "direction", "vehicle_type", "queue_count", "wait_minutes",
                                   "oldest_wait_min", "median_wait_min", "source", "note"])
    else:
        df = pd.read_csv(path)
    arch = getattr(config, "ARCHIVE_FILE", "")
    if with_archive and arch and path == config.DATA_FILE:
        import os
        if os.path.exists(arch):
            df = pd.concat([pd.read_csv(arch), df], ignore_index=True)
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
    d["is_archive"] = (d["source"] == "gpk_archive").astype(float)
    d = d.set_index("ts")[["queue_count", "wait_minutes", "oldest_wait_min", "median_wait_min", "is_archive"]]
    return d.resample("15min").mean().interpolate(limit=8, limit_area="inside")


def holiday_flags(index: pd.DatetimeIndex) -> np.ndarray:
    hol = pd.to_datetime(config.HOLIDAYS).date
    near = set()
    for h in hol:
        for k in (-1, 0, 1):
            near.add(h + timedelta(days=k))
    s0, s1 = (pd.Timestamp(x).date() for x in getattr(config, "HOLIDAY_SEASON", ("1900-01-01", "1900-01-01")))
    return np.array([(ts.date() in near) or (s0 <= ts.date() <= s1) for ts in index])


def throughput_per_hour(s: pd.DataFrame, vehicle: str) -> float:
    """Сколько машин в час уходит из очереди.
    Считаем только по плотным собственным данным (последние 14 дней, шаг 15 мин,
    заполнено ≥70%). Архив снят раз в 2 часа — по нему убывание очереди занижено."""
    default = config.DEFAULT_BUSES_PER_HOUR if vehicle == "bus" else config.DEFAULT_CARS_PER_HOUR
    if s.empty:
        return default
    own = s[s["is_archive"] < 0.5] if "is_archive" in s else s
    if own.empty:
        return default
    recent = own[own.index >= own.index.max() - pd.Timedelta(days=14)]
    q = recent["queue_count"]
    if len(q) < 96 * 7 or q.notna().mean() < 0.7:
        return default
    q = q.dropna()
    drops = (-q.diff()).clip(lower=0)
    hourly = drops.resample("1h").sum()
    hourly = hourly[q.resample("1h").mean() > 0]
    if len(hourly) < 24:
        return default
    est = float(hourly.quantile(0.75))
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


def forecast_point(s: pd.DataFrame, vehicle: str, when: pd.Timestamp, an: dict | None = None) -> dict | None:
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
    own = s[s["is_archive"] < 0.5]["queue_count"].dropna() if "is_archive" in s else q
    history_d = (now - own.index.min()).total_seconds() / 86400 if not own.empty else 0.0
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

    # Праздник и есть архив прошлых лет на эту дату → не ниже, чем бывало раньше
    if an and hol:
        pred = max(pred, an["median"])
        hi = max(hi, an["max"])

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
        "analog": an,
    }


def analog(df: pd.DataFrame, direction: str, vehicle: str, when: pd.Timestamp,
           hour_window: float = 2.0) -> dict | None:
    """Что было в ПРОШЛЫЕ годы в тот же календарный день и час (архив ГПК).
    Нужен для праздников: своей истории праздников у сборщика ещё нет."""
    d = df[(df.direction == direction) & (df.vehicle_type == vehicle)
           & (df.source == "gpk_archive")].dropna(subset=["queue_count"])
    if d.empty:
        return None
    when = when.tz_convert(TZ) if when.tzinfo else when.tz_localize(TZ)
    vals = []
    for y in sorted(set(d.ts.dt.year)):
        if y >= when.year:
            continue
        try:
            center = when.replace(year=y)
        except ValueError:
            continue
        near = d[(d.ts - center).abs() <= pd.Timedelta(hours=hour_window)]
        if not near.empty:
            vals.append({"year": int(y), "value": float(near.queue_count.mean())})
    if not vals:
        return None
    v = [x["value"] for x in vals]
    return {"median": float(np.median(v)), "max": float(max(v)), "years": vals}


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


# ---------------------------------------------------------------- прогноз на месяц (для графика)
def horizon_hourly(df: pd.DataFrame, direction: str, vehicle: str, days: int = 30) -> pd.DataFrame:
    """Почасовой прогноз на `days` дней: первые 7 дней — с учётом текущей ситуации,
    дальше — профиль × праздники × тренд; в праздники не ниже архива прошлых лет."""
    s = series(df, direction, vehicle)
    q = s["queue_count"].dropna() if not s.empty else pd.Series(dtype=float)
    if q.empty:
        return pd.DataFrame()
    now = q.index.max()
    idx = pd.date_range(now.floor("h") + pd.Timedelta(hours=1), periods=days * 24, freq="1h", tz=TZ)
    prof = build_profile(s, weeks=8)
    flags = holiday_flags(idx)
    trend0 = level_trend(s)
    hf = prof["holiday_factor"]
    rows = []
    for ts, hol in zip(idx, flags):
        h = (ts - now).total_seconds() / 86400
        med, lo, hi = profile_value(prof, ts)
        f = hf if hol else 1.0
        tr = 1 + (trend0 - 1) * np.exp(-h / 21)
        pred = med * f * tr
        widen = 1 + h / 14
        rows.append({"ts": ts, "pred": pred, "lo": max(0.0, pred - (med - lo) * f * widen),
                     "hi": pred + (hi - med) * f * widen, "holiday": bool(hol)})
    out = pd.DataFrame(rows).set_index("ts")
    short = forecast(s, vehicle, days=7)
    if not short.empty:
        common = out.index.intersection(short.index)
        out.loc[common, "pred"] = short.loc[common, "queue_pred"]
        out.loc[common, "lo"] = short.loc[common, "queue_lo"]
        out.loc[common, "hi"] = short.loc[common, "queue_hi"]
    # праздники: не ниже того, что было в эти дни в прошлые годы
    for ts in out.index[out["holiday"]]:
        an = analog(df, direction, vehicle, ts, hour_window=1.0)
        if an:
            out.loc[ts, "pred"] = max(out.loc[ts, "pred"], an["median"])
            out.loc[ts, "hi"] = max(out.loc[ts, "hi"], an["max"])
    out["lo"] = out["lo"].clip(lower=0)
    out.attrs["throughput"] = throughput_per_hour(s, vehicle)
    return out


def daily_summary(h: pd.DataFrame) -> pd.DataFrame:
    """Один ряд на день: типичная очередь (среднее за сутки), спокойный и пиковый час."""
    if h.empty:
        return h
    g = h.groupby(h.index.date)
    d = pd.DataFrame({"typical": g["pred"].mean(), "quiet": g["pred"].min(),
                      "peak": g["pred"].max(), "holiday": g["holiday"].max()})
    d.index = pd.to_datetime(d.index)
    tp = h.attrs.get("throughput", config.DEFAULT_BUSES_PER_HOUR)
    d["wait_typical_h"] = d["typical"] / tp
    return d


def last_year_daily(df: pd.DataFrame, direction: str, vehicle: str, dates) -> pd.Series:
    """Средняя очередь за те же календарные дни год назад (архив ГПК)."""
    a = df[(df.direction == direction) & (df.vehicle_type == vehicle) & (df.source == "gpk_archive")]
    a = a.dropna(subset=["queue_count"])
    if a.empty:
        return pd.Series(dtype=float)
    daily = a.groupby(a.ts.dt.date)["queue_count"].mean()
    vals = {}
    for d in dates:
        try:
            prev = (pd.Timestamp(d) - pd.DateOffset(years=1)).date()
        except Exception:  # noqa: BLE001
            continue
        if prev in daily.index:
            vals[pd.Timestamp(d)] = float(daily[prev])
    return pd.Series(vals, dtype=float)


def day_parts(h: pd.DataFrame, day) -> pd.DataFrame:
    """Утро / день / вечер / ночь для выбранного дня."""
    parts = {"🌅 Утро (6–12)": (6, 12), "☀️ День (12–18)": (12, 18),
             "🌆 Вечер (18–24)": (18, 24), "🌙 Ночь (0–6)": (0, 6)}
    x = h[h.index.date == pd.Timestamp(day).date()]
    rows = []
    for name, (a, b) in parts.items():
        y = x[(x.index.hour >= a) & (x.index.hour < b)]
        if not y.empty:
            rows.append({"part": name, "pred": float(y["pred"].mean()), "hi": float(y["hi"].mean())})
    return pd.DataFrame(rows)


def level(wait_h: float) -> tuple[str, str]:
    """Светофор по ожиданию (часы)."""
    if wait_h is None or pd.isna(wait_h):
        return "⚪", "нет данных"
    if wait_h < config.GREEN_MAX_H:
        return "🟢", "спокойно"
    if wait_h < config.YELLOW_MAX_H:
        return "🟡", "средняя загрузка"
    return "🔴", "пик"
