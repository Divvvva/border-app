"""Расчёты для интерфейса: прогноз по дням, «светофор», сводка по поездке,
прошлые годы и планировщик поездок. Без Streamlit — можно проверять отдельно."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

import config
import model

TZ = model.TZ

# Католическая Пасха (по ней едут в Польшу/ЕС) — для сравнения «по дням от Пасхи»
EASTER = {2024: date(2024, 3, 31), 2025: date(2025, 4, 20), 2026: date(2026, 4, 5),
          2027: date(2027, 3, 28), 2028: date(2028, 4, 16)}

# Окна для планировщика (учебный год 2026/27, ВШЭ). Сессию зимой уточни в InSIS.
OCCASIONS = {
    "Перерыв между семестрами": (date(2027, 1, 25), date(2027, 2, 14), None),
    "Пасха + Инновационная неделя": (date(2027, 3, 26), date(2027, 4, 4), "easter"),
    "ДР мамы (18.05)": (date(2027, 5, 14), date(2027, 5, 23), None),
    "Летние каникулы": (date(2027, 7, 1), date(2027, 8, 31), None),
}

TIME_BLOCKS = {"Ночь": (0, 6), "Утро": (6, 12), "День": (12, 18), "Вечер": (18, 24)}


# ---------------------------------------------------------------- светофор
def level(wait_h: float | None) -> tuple[str, str]:
    if wait_h is None or pd.isna(wait_h):
        return "⚪", "нет данных"
    if wait_h < 2:
        return "🟢", "спокойно"
    if wait_h < 5:
        return "🟡", "средне"
    return "🔴", "пик"


def tp(s: pd.DataFrame, vehicle: str) -> float:
    return model.throughput_per_hour(s, vehicle)


# ---------------------------------------------------------------- прошлые годы
def past_value(df: pd.DataFrame, direction: str, vehicle: str, when: pd.Timestamp,
               anchor: str | None = None, hour_window: float = 2.0) -> list[dict]:
    """Значения из архива за прошлые годы в «тот же» момент.
    anchor='easter' — тот же сдвиг от Пасхи, иначе тот же календарный день."""
    d = df[(df.direction == direction) & (df.vehicle_type == vehicle)
           & (df.source == "gpk_archive")].dropna(subset=["queue_count"])
    if d.empty:
        return []
    when = when.tz_convert(TZ) if when.tzinfo else when.tz_localize(TZ)
    out = []
    for y in sorted(set(d.ts.dt.year)):
        if y >= when.year:
            continue
        try:
            if anchor == "easter" and when.year in EASTER and y in EASTER:
                shift = when.normalize().date() - EASTER[when.year]
                day = EASTER[y] + shift
                center = pd.Timestamp(day).tz_localize(TZ) + pd.Timedelta(hours=when.hour, minutes=when.minute)
            else:
                center = when.replace(year=y)
        except ValueError:
            continue
        near = d[(d.ts - center).abs() <= pd.Timedelta(hours=hour_window)]
        if not near.empty:
            out.append({"year": int(y), "value": float(near.queue_count.mean()), "date": center.date()})
    return out


def past_day_mean(df: pd.DataFrame, direction: str, vehicle: str, day: date) -> float | None:
    """Среднее за тот же календарный день год назад (для пунктира на графике)."""
    d = df[(df.direction == direction) & (df.vehicle_type == vehicle)
           & (df.source == "gpk_archive")].dropna(subset=["queue_count"])
    try:
        prev = day.replace(year=day.year - 1)
    except ValueError:
        return None
    v = d[d.ts.dt.date == prev].queue_count
    return float(v.mean()) if len(v) else None


# ---------------------------------------------------------------- почасовой прогноз на месяц
def hourly_forecast(df: pd.DataFrame, direction: str, vehicle: str = "bus", days: int = 30) -> pd.DataFrame:
    s = model.series(df, direction, vehicle)
    q = s["queue_count"].dropna() if not s.empty else pd.Series(dtype=float)
    if q.empty:
        return pd.DataFrame()
    now = q.index.max()
    near = model.forecast(s, vehicle, days=8)
    prof = model.build_profile(s, weeks=8)
    trend = model.level_trend(s)
    rate = tp(s, vehicle)
    start = now.floor("h") + pd.Timedelta(hours=1)
    idx = pd.date_range(start, periods=days * 24, freq="1h", tz=TZ)
    flags = model.holiday_flags(idx)
    rows = []
    for ts, hol in zip(idx, flags):
        h_ahead = (ts - now).total_seconds() / 86400
        if ts in near.index:
            r = near.loc[ts]
            pred, lo, hi = r.queue_pred, r.queue_lo, r.queue_hi
        else:
            med, lo0, hi0 = model.profile_value(prof, ts)
            f = prof["holiday_factor"] if hol else 1.0
            tr = 1 + (trend - 1) * np.exp(-h_ahead / 21)
            pred = med * f * tr
            widen = 1 + h_ahead / 14
            lo = max(0.0, pred - (med - lo0) * f * widen)
            hi = pred + (hi0 - med) * f * widen
        past = []
        if hol:
            past = past_value(df, direction, vehicle, ts)
            if past:
                pv = [p["value"] for p in past]
                pred = max(pred, float(np.median(pv)))
                hi = max(hi, max(pv))
        rel = 0.15 + 0.02 * h_ahead
        lo = max(0.0, min(lo, pred * (1 - rel)))
        hi = max(hi, pred * (1 + rel) + 0.5)
        rows.append({"ts": ts, "pred": float(pred), "lo": float(lo), "hi": float(hi), "holiday": bool(hol)})
    out = pd.DataFrame(rows).set_index("ts")
    out.attrs["rate"] = rate
    return out


def daily_forecast(h: pd.DataFrame, df: pd.DataFrame, direction: str, vehicle: str = "bus") -> pd.DataFrame:
    """Одна строка на день: типичное (среднее), спокойный и пиковый час, ожидание."""
    if h.empty:
        return h
    g = h.groupby(h.index.date)
    d = pd.DataFrame({
        "typical": g["pred"].mean(),
        "calm": g["pred"].min(),
        "peak": g["pred"].max(),
        "peak_hi": g["hi"].max(),
        "holiday": g["holiday"].any(),
    })
    rate = h.attrs.get("rate", config.DEFAULT_BUSES_PER_HOUR)
    d["wait_typ_h"] = d["typical"] / rate
    d["wait_peak_h"] = d["peak"] / rate
    d["level"] = [level(w)[0] for w in d["wait_typ_h"]]
    d["last_year"] = [past_day_mean(df, direction, vehicle, x) for x in d.index]
    d.index = pd.to_datetime(d.index)
    return d


def day_blocks(h: pd.DataFrame, day: date) -> list[dict]:
    """Утро / день / вечер / ночь для выбранного дня."""
    x = h[h.index.date == day]
    rate = h.attrs.get("rate", config.DEFAULT_BUSES_PER_HOUR)
    out = []
    for name, (a, b) in TIME_BLOCKS.items():
        part = x[(x.index.hour >= a) & (x.index.hour < b)]
        if part.empty:
            out.append({"name": name, "buses": None, "wait_lo": None, "wait_hi": None, "icon": "⚪"})
            continue
        p = part["pred"].mean()
        out.append({"name": name, "buses": p,
                    "wait_lo": part["lo"].mean() / rate, "wait_hi": part["hi"].mean() / rate,
                    "icon": level(p / rate)[0]})
    return out


# ---------------------------------------------------------------- поездка
def border_time(departure: pd.Timestamp, dep_tz: str, hours: float) -> pd.Timestamp:
    dep = departure.tz_localize(dep_tz) if departure.tzinfo is None else departure
    return (dep + pd.Timedelta(hours=hours)).tz_convert(TZ)


def leg_estimate(df: pd.DataFrame, direction: str, when: pd.Timestamp, vehicle: str = "bus",
                 anchor: str | None = None) -> dict:
    """Оценка одного пересечения: модель (до 60 дней) + прошлые годы.
    Дальше горизонта — только по прошлым годам."""
    s = model.series(df, direction, vehicle)
    past = past_value(df, direction, vehicle, when, anchor=anchor)
    rate = tp(s, vehicle) if not s.empty else config.DEFAULT_BUSES_PER_HOUR
    p = None
    if not s.empty:
        an = model.analog(df, direction, vehicle, when)
        p = model.forecast_point(s, vehicle, when, an)
    if p is not None:
        pred, lo, hi = p["queue_pred"], p["queue_lo"], p["queue_hi"]
        conf, src = p["confidence"], "model"
    elif past:
        v = [x["value"] for x in past]
        pred, lo, hi = float(np.median(v)), float(min(v)) * 0.7, float(max(v)) * 1.3 + 1
        conf, src = "низкая", "past"
    else:
        return {"when": when, "known": False, "icon": "⚪", "label": "нет данных", "past": past}
    w, wlo, whi = pred / rate, lo / rate, hi / rate
    icon, label = level(w)
    return {"when": when, "known": True, "buses": pred, "buses_lo": lo, "buses_hi": hi,
            "wait": w, "wait_lo": wlo, "wait_hi": whi, "icon": icon, "label": label,
            "confidence": conf, "source": src, "past": past}


def trip_card(df: pd.DataFrame, trip: dict) -> dict:
    """Карточка поездки + сравнение с выездом на день раньше/позже."""
    dep = pd.Timestamp(trip["departure"])
    tz = trip.get("departure_tz", TZ)
    main = leg_estimate(df, trip["direction"], border_time(dep, tz, trip["travel_hours"]))
    alts = []
    for shift in (-1, 1):
        e = leg_estimate(df, trip["direction"], border_time(dep + pd.Timedelta(days=shift), tz, trip["travel_hours"]))
        if e["known"]:
            alts.append({"shift": shift, "departure": dep + pd.Timedelta(days=shift), **e})
    tip = None
    if main["known"] and alts:
        best = min(alts, key=lambda a: a["wait"])
        if best["wait"] < main["wait"] * 0.6 and main["wait"] - best["wait"] > 0.75:
            word = "раньше" if best["shift"] < 0 else "позже"
            tip = (f"Выезд на день {word} ({best['departure']:%d.%m}) спокойнее: "
                   f"{best['icon']} ≈ {fmt_range(best['wait_lo'], best['wait_hi'])}")
    return {"trip": trip, "main": main, "alts": alts, "tip": tip,
            "days_left": (dep.normalize() - pd.Timestamp.now().normalize()).days}


# ---------------------------------------------------------------- «сейчас»
def now_status(df: pd.DataFrame, direction: str, vehicle: str = "bus") -> dict | None:
    d = df[(df.direction == direction) & (df.vehicle_type == vehicle)
           & (df.source != "gpk_archive")].dropna(subset=["queue_count"])
    if d.empty:
        return None
    last = d.sort_values("ts").iloc[-1]
    s = model.series(df, direction, vehicle)
    rate = tp(s, vehicle)
    age_h = (pd.Timestamp.now(tz=TZ) - last.ts).total_seconds() / 3600
    return {"buses": float(last.queue_count), "wait": float(last.queue_count) / rate,
            "age_h": age_h, "ts": last.ts}


# ---------------------------------------------------------------- планировщик
def plan(df: pd.DataFrame, start: date, end: date, days_home: int = 5, max_wait: float = 5.0,
         anchor: str | None = None, there_time: str = "20:50", back_time: str = "20:00",
         there_hours: float = 11.5, back_hours: float = 5.0, top: int = 3) -> list[dict]:
    """Перебирает поездки длиной days_home внутри окна и ранжирует по ожиданию на
    обратном пути (по нему есть данные). «Туда» оценивается, если появятся данные."""
    results = []
    d1 = start
    while d1 + timedelta(days=days_home) <= end:
        d2 = d1 + timedelta(days=days_home)
        back = leg_estimate(df, "exit_by",
                            border_time(pd.Timestamp(f"{d2} {back_time}"), "Europe/Minsk", back_hours), anchor=anchor)
        there = leg_estimate(df, "entry_by",
                             border_time(pd.Timestamp(f"{d1} {there_time}"), "Europe/Prague", there_hours), anchor=anchor)
        if back["known"]:
            results.append({"there_date": d1, "back_date": d2, "days_home": days_home,
                            "there": there, "back": back, "fits": back["wait_hi"] <= max_wait})
        d1 += timedelta(days=1)
    results.sort(key=lambda r: (not r["fits"], round(r["back"]["wait"], 1), r["there_date"]))
    return results[:top]


# ---------------------------------------------------------------- форматирование
def fmt_h(h):
    if h is None or pd.isna(h):
        return "—"
    if h < 1:
        return f"{int(round(h * 60))} мин"
    return f"{h:.1f} ч".replace(".0 ч", " ч")


def fmt_range(lo, hi):
    if lo is None or pd.isna(lo):
        return "—"
    lo_r, hi_r = max(0, round(lo)), max(1, round(hi))
    if hi_r <= lo_r:
        hi_r = lo_r + 1
    return f"{lo_r}–{hi_r} ч"


RU_DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def ru_date(d) -> str:
    d = pd.Timestamp(d)
    return f"{RU_DAYS[d.weekday()]} {d:%d.%m}"
