"""Streamlit-приложение: очередь автобусов Брест – Тересполь и прогноз.
Запуск локально: streamlit run app.py
"""
from __future__ import annotations

import os
from datetime import datetime, time

import altair as alt
import pandas as pd
import streamlit as st

import config
import model

st.set_page_config(page_title="Граница Брест – Тересполь", page_icon="🚌", layout="wide")
st.title("🚌 Брест – Тересполь: очередь и прогноз")

if not config.DATA_URL and not os.path.exists(config.DATA_FILE) and not os.path.exists(config.ARCHIVE_FILE):
    st.warning("Данных пока нет. Сборщик запускается каждые 15 минут — загляни чуть позже.")
    st.stop()


@st.cache_data(ttl=300)
def get_data():
    return model.load()


df = get_data()
last_ts = df["ts"].max()
age_min = (pd.Timestamp.now(tz=model.TZ) - last_ts).total_seconds() / 60
st.caption(f"Последнее обновление: {last_ts:%d.%m %H:%M} (минское время)"
           + (f" — ⚠️ данные устарели на {age_min/60:.1f} ч" if age_min > 60 else ""))

VEHICLES = {"bus": "Автобусы", "car": "Легковые", "truck": "Грузовые", "all": "Все"}


def fmt_h(h):
    if h is None or pd.isna(h):
        return "—"
    return f"{int(h)} ч {int((h % 1) * 60):02d} мин"


CONF_ICON = {"низкая": "🟠", "средняя": "🟡", "высокая": "🟢"}


def show_point(p: dict, what: str):
    st.success(
        f"На границе ≈ **{p['target_ts']:%a %d.%m %H:%M}** (минское время). "
        f"Очередь ≈ **{p['queue_pred']:.0f}** {what} (обычно {p['queue_lo']:.0f}–{p['queue_hi']:.0f}), "
        f"ожидание ≈ **{fmt_h(p['wait_h_pred'])}** (от {fmt_h(p['wait_h_lo'])} до {fmt_h(p['wait_h_hi'])})."
    )
    note = (f"{CONF_ICON[p['confidence']]} Уверенность: {p['confidence']} — до даты {p['horizon_days']:.0f} дн., "
            f"истории {p['history_days']:.0f} дн.")
    if p["holiday"]:
        note += (f" · Праздничный период: ×{p['holiday_factor']:.2f} "
                 + ("(по данным)" if p["holiday_factor_learned"] else "(пока предположение, уточнится по данным)"))
    st.caption(note)
    an = p.get("analog")
    if an:
        yrs = ", ".join(f"{x['year']}: {x['value']:.0f}" for x in an["years"])
        st.caption(f"📚 В прошлые годы в этот день и час ({what}): {yrs} — по архиву ГПК")


def my_trips_block():
    st.subheader("🗓️ Мои поездки")
    st.caption("Даты задаются в config.py (MY_TRIPS). Прогноз пересчитывается при каждом сборе данных.")
    log = None
    if os.path.exists(config.FORECAST_LOG):
        log = pd.read_csv(config.FORECAST_LOG, parse_dates=["made_at", "target_ts"])
    for trip in config.MY_TRIPS:
        vehicle = trip.get("vehicle", "bus")
        s = model.series(df, trip["direction"], vehicle)
        if s.empty:
            vehicle, s = "car", model.series(df, trip["direction"], "car")
        with st.container(border=True):
            st.markdown(f"**{trip['name']}** · {'въезд в Беларусь' if trip['direction'] == 'entry_by' else 'выезд из Беларуси'}")
            when = model.trip_border_time(trip)
            an = model.analog(df, trip["direction"], vehicle, when) if not s.empty else None
            p = model.forecast_point(s, vehicle, when, an) if not s.empty else None
            if p is None:
                st.info("Для въезда в Беларусь пока нет своей истории: электронная очередь и архив ГПК "
                        "считают только выезд. Ориентируйся на живые данные Nakordoni выше."
                        if trip["direction"] == "entry_by" else
                        "Пока нет данных по этому направлению или дата дальше горизонта прогноза.")
            else:
                show_point(p, VEHICLES[vehicle].lower())
            if log is not None:
                h = log[log.trip == trip["name"]].copy()
                if len(h) > 1:
                    h = h.set_index("made_at").resample("6h").last().dropna(subset=["queue_pred"]).reset_index()
                    st.altair_chart(
                        (alt.Chart(h).mark_area(opacity=0.2, color="#e07b39").encode(
                            x=alt.X("made_at:T", title="когда сделан прогноз"), y="queue_lo:Q", y2="queue_hi:Q")
                         + alt.Chart(h).mark_line(point=True, color="#e07b39").encode(
                            x="made_at:T", y=alt.Y("queue_pred:Q", title="прогноз очереди"),
                            tooltip=[alt.Tooltip("made_at:T", format="%d.%m %H:%M"),
                                     alt.Tooltip("queue_pred:Q", format=".0f"),
                                     alt.Tooltip("wait_h_pred:Q", format=".1f", title="ожидание, ч"),
                                     "confidence"])).properties(height=180),
                        use_container_width=True)
                    st.caption("Как менялся прогноз на эту поездку: полоса сужается по мере приближения даты.")


def direction_tab(direction: str, title: str):
    available = [v for v in VEHICLES if not df[(df.direction == direction) & (df.vehicle_type == v)].empty]
    if not available:
        st.info("По этому направлению данных пока нет. "
                + ("Для въезда нужен ключ Nakordoni (см. README)." if direction == "entry_by" else ""))
        return
    vehicle = st.radio("Транспорт", available, format_func=VEHICLES.get, horizontal=True,
                       key=f"veh_{direction}", index=available.index("bus") if "bus" in available else 0)
    s = model.series(df, direction, vehicle)
    q = s["queue_count"].dropna()
    if q.empty:
        st.info("Пока нет чисел очереди для этого транспорта.")
        return
    fc = model.forecast(s, vehicle, days=7)
    tp = fc.attrs.get("throughput_per_hour", float("nan")) if not fc.empty else float("nan")

    # --- Сейчас
    now_q = float(q.iloc[-1])
    q6 = q[q.index >= q.index.max() - pd.Timedelta(hours=6)]
    trend = now_q - float(q6.iloc[0]) if len(q6) > 1 else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"{VEHICLES[vehicle]} в очереди", f"{now_q:.0f}", f"{trend:+.0f} за 6 ч", delta_color="inverse")
    src_wait = s["wait_minutes"].dropna()
    est_wait = (src_wait.iloc[-1] / 60) if not src_wait.empty else model.wait_hours(now_q, tp)
    c2.metric("Оценка ожидания", fmt_h(est_wait))
    oldest = s["oldest_wait_min"].dropna()
    c3.metric("Дольше всех стоят", fmt_h(oldest.iloc[-1] / 60) if not oldest.empty else "—")
    c4.metric("Пропускная (оценка)", f"{tp:.1f} в час" if pd.notna(tp) else "—")

    # --- График: 7 дней истории + 7 дней прогноза
    hist = q[q.index >= q.index.max() - pd.Timedelta(days=7)].resample("1h").mean().reset_index()
    hist.columns = ["ts", "value"]
    hist["kind"] = "Факт"
    layers = [alt.Chart(hist).mark_line(color="#1f4e79").encode(
        x=alt.X("ts:T", title=None), y=alt.Y("value:Q", title="машин в очереди"),
        tooltip=[alt.Tooltip("ts:T", format="%d.%m %H:%M"), alt.Tooltip("value:Q", format=".0f")])]
    if not fc.empty:
        f = fc.reset_index()
        layers.append(alt.Chart(f).mark_area(opacity=0.2, color="#e07b39").encode(
            x="ts:T", y="queue_lo:Q", y2="queue_hi:Q"))
        layers.append(alt.Chart(f).mark_line(strokeDash=[5, 3], color="#e07b39").encode(
            x="ts:T", y="queue_pred:Q",
            tooltip=[alt.Tooltip("ts:T", format="%a %d.%m %H:%M"),
                     alt.Tooltip("queue_pred:Q", format=".0f", title="прогноз"),
                     alt.Tooltip("wait_h_pred:Q", format=".1f", title="ожидание, ч")]))
    st.altair_chart(alt.layer(*layers).properties(height=320), use_container_width=True)
    st.caption("Синяя линия — факт, оранжевая — прогноз, полоса — обычный разброс (20–80%).")

    if fc.empty:
        return
    # --- Лучшее время
    st.subheader("Лучшее время, чтобы оказаться на границе (7 дней)")
    bt = model.best_times(fc, 8).reset_index()
    bt["Когда"] = bt["ts"].dt.strftime("%a %d.%m %H:00")
    bt["Очередь"] = bt["queue_pred"].round(0).astype(int)
    bt["Ожидание"] = bt["wait_h_pred"].map(fmt_h)
    st.dataframe(bt[["Когда", "Очередь", "Ожидание"]], hide_index=True, use_container_width=True)

    # --- Тепловая карта по дням и часам
    hm = fc.reset_index()
    hm["день"] = hm["ts"].dt.strftime("%a %d.%m")
    hm["час"] = hm["ts"].dt.hour
    st.altair_chart(alt.Chart(hm).mark_rect().encode(
        x=alt.X("час:O"), y=alt.Y("день:O", sort=None),
        color=alt.Color("queue_pred:Q", scale=alt.Scale(scheme="orangered"), title="очередь"),
        tooltip=["день", "час", alt.Tooltip("queue_pred:Q", format=".0f")]).properties(height=230),
        use_container_width=True)

    # --- Мой рейс
    st.subheader("🎫 Мой рейс")
    route = "Минск → Брест" if direction == "exit_by" else "Прага → Тересполь"
    my = next((tr for tr in config.MY_TRIPS if tr["direction"] == direction), None)
    dflt = pd.Timestamp(my["departure"]) if my else pd.Timestamp.now()
    a, b, c = st.columns(3)
    dep_date = a.date_input("Дата отправления", value=dflt.date(), key=f"d_{direction}")
    dep_time = b.time_input("Время отправления", value=dflt.time(), key=f"t_{direction}")
    hours = c.number_input(f"Часов в пути ({route})", 1.0, 30.0, config.TRAVEL_HOURS[route], 0.5, key=f"h_{direction}")
    dep_tz = "Europe/Minsk" if direction == "exit_by" else "Europe/Prague"
    dep = pd.Timestamp(datetime.combine(dep_date, dep_time)).tz_localize(dep_tz)
    arrival = (dep + pd.Timedelta(hours=hours)).tz_convert(model.TZ)
    p = model.forecast_point(s, vehicle, arrival, model.analog(df, direction, vehicle, arrival))
    if p is None:
        st.info(f"Автобус будет на границе {arrival:%d.%m %H:%M}. Прогноз строится максимум на "
                f"{config.MAX_HORIZON_DAYS} дней вперёд.")
    else:
        show_point(p, VEHICLES[vehicle].lower())

    with st.expander("Качество модели"):
        b = model.backtest(s, vehicle)
        if b:
            st.write(f"Ошибка прогноза за последние {b['hours']} ч: **{b['mae_model']:.1f}** машин "
                     f"(наивный прогноз «как сейчас»: {b['mae_naive']:.1f}).")
        else:
            st.write("Нужно минимум 10 дней данных, чтобы честно проверить модель.")
        st.write(f"Коэффициент праздников: {fc.attrs.get('holiday_factor', 1):.2f}")


@st.cache_data(ttl=600, show_spinner=False)
def nakordoni_live(ppid: str):
    """Живые данные Nakordoni. Не сохраняются никуда (условия API), кэш 10 минут."""
    import requests

    try:
        key = st.secrets["NAKORDONI_API_KEY"]
    except Exception:  # noqa: BLE001
        return None
    r = requests.get("https://nakordoni.eu/api/v1/data/queue", params={"ppid": ppid},
                     headers={"Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    payload = r.json()

    def find(obj, keys):
        """Ищем первое значение по любому из ключей на любой глубине ответа."""
        if isinstance(obj, dict):
            for k in keys:
                if k in obj and not isinstance(obj[k], (dict, list)):
                    return obj[k]
            for v in obj.values():
                f = find(v, keys)
                if f is not None:
                    return f
        elif isinstance(obj, list):
            for v in obj:
                f = find(v, keys)
                if f is not None:
                    return f
        return None

    return {"queue": find(payload, ["queue_now", "queue", "count"]),
            "wait_min": find(payload, ["wait_min", "wait_minutes", "wait"]),
            "age": find(payload, ["data_age", "age", "updated_at", "observed_at"]),
            "raw": payload}


def nakordoni_block():
    st.subheader("📡 Сейчас по данным Nakordoni (автобусы)")
    try:
        ppids = {"➡️ Туда: Тересполь → Брест": st.secrets["NAKORDONI_PPID_ENTRY"],
                 "⬅️ Обратно: Брест → Тересполь": st.secrets["NAKORDONI_PPID_EXIT"]}
    except Exception:  # noqa: BLE001
        st.caption("Не настроено: добавь NAKORDONI_API_KEY, NAKORDONI_PPID_ENTRY и "
                   "NAKORDONI_PPID_EXIT в Secrets приложения на Streamlit (см. README).")
        return
    cols = st.columns(len(ppids))
    for col, (label, ppid) in zip(cols, ppids.items()):
        with col:
            try:
                d = nakordoni_live(ppid)
            except Exception as e:  # noqa: BLE001
                st.warning(f"{label}: Nakordoni не ответил ({e})")
                continue
            if not d:
                continue
            w = d["wait_min"]
            st.metric(label, fmt_h(w / 60) if isinstance(w, (int, float)) else "—",
                      help="Оценка ожидания по данным Nakordoni")
            if d["queue"] is not None:
                st.caption(f"В очереди: {d['queue']}" + (f" · данные: {d['age']}" if d["age"] else ""))
            if d["wait_min"] is None and d["queue"] is None:
                with st.expander("Ответ API"):
                    st.json(d["raw"])
    st.caption("Data by [nakordoni.eu](https://nakordoni.eu) · показывается вживую, не сохраняется.")


nakordoni_block()
st.divider()
my_trips_block()
st.divider()

tab_back, tab_there = st.tabs(["⬅️ Обратно (выезд из Беларуси)", "➡️ Туда (въезд в Беларусь)"])
with tab_back:
    direction_tab("exit_by", "Обратно")
with tab_there:
    direction_tab("entry_by", "Туда")

st.divider()
st.caption("Источники: электронная очередь РБ (belarusborder), Госпогранкомитет (gpk.gov.by), Nakordoni. "
           "Рейсовые автобусы могут проходить вне электронной очереди — сверяйся с перевозчиком.")
