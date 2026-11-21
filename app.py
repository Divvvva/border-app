"""Моя граница: Брест – Тересполь. Когда ехать и сколько стоять.
Запуск локально: streamlit run app.py
"""
from __future__ import annotations

import os
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

import config
import insights as I
import model

st.set_page_config(page_title="Моя граница · Брест – Тересполь", page_icon="🚌", layout="centered")

# ---------------------------------------------------------------- стиль
st.markdown("""
<style>
.block-container {padding-top: 2rem; max-width: 860px;}
h1, h2, h3 {letter-spacing: -0.01em;}
.card {border-radius: 16px; padding: 18px 20px; margin-bottom: 12px;
       background: rgba(127,127,127,0.08); border: 1px solid rgba(127,127,127,0.18);}
.card .kicker {font-size: 0.8rem; opacity: 0.7; text-transform: uppercase; letter-spacing: 0.06em;}
.card .title {font-size: 1.15rem; font-weight: 600; margin: 2px 0 8px 0;}
.card .big {font-size: 1.9rem; font-weight: 700; line-height: 1.15;}
.card .sub {font-size: 0.92rem; opacity: 0.85; margin-top: 6px;}
.card .muted {font-size: 0.82rem; opacity: 0.6; margin-top: 8px;}
.tip {border-radius: 12px; padding: 10px 12px; margin-top: 10px;
      background: rgba(46,160,67,0.12); border: 1px solid rgba(46,160,67,0.35); font-size: 0.92rem;}
.warn {border-radius: 12px; padding: 8px 12px; margin-top: 8px;
       background: rgba(210,153,34,0.12); border: 1px solid rgba(210,153,34,0.35); font-size: 0.85rem;}
.block {border-radius: 12px; padding: 10px; text-align: center;
        background: rgba(127,127,127,0.08); border: 1px solid rgba(127,127,127,0.18);}
.block .n {font-size: 1.3rem; font-weight: 700;}
</style>
""", unsafe_allow_html=True)


def card(html: str):
    st.markdown(f'<div class="card">{html}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- данные
if not config.DATA_URL and not os.path.exists(config.DATA_FILE) and not os.path.exists(config.ARCHIVE_FILE):
    st.warning("Данных пока нет. Сборщик запускается каждые 15 минут — загляни чуть позже.")
    st.stop()


@st.cache_data(ttl=300, show_spinner=False)
def get_data():
    return model.load()


@st.cache_data(ttl=900, show_spinner=False)
def get_month(direction: str):
    d = get_data()
    h = I.hourly_forecast(d, direction, "bus", days=30)
    return h, I.daily_forecast(h, d, direction, "bus")


@st.cache_data(ttl=600, show_spinner=False)
def nakordoni_live(ppid: str):
    """Живые данные Nakordoni. Никуда не сохраняются (условия API), кэш 10 минут."""
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

    # По документации Nakordoni текущие значения лежат в data.snapshot
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    snap = data.get("snapshot") if isinstance(data, dict) and isinstance(data.get("snapshot"), dict) else None
    scope = snap if snap is not None else payload
    return {"queue": find(scope, ["queue_now", "queue", "vehicles", "count"]),
            "wait_min": find(scope, ["wait_min", "wait_minutes"]),
            "age": find(scope, ["updated_at", "observed_at", "last_observation", "timestamp", "data_age", "age"]),
            "from_snapshot": snap is not None,
            "raw": payload}


def fmt_age(age) -> str:
    """Время данных Nakordoni: метка времени или «N мин назад»."""
    if age is None:
        return ""
    if isinstance(age, (int, float)):
        return f"данные {I.fmt_h(age / 60)} назад" if age < 6000 else ""
    try:
        ts = pd.Timestamp(str(age))
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        return f"данные на {ts.tz_convert('Europe/Prague'):%d.%m %H:%M}"
    except Exception:  # noqa: BLE001
        return f"данные: {age}"


def nakordoni_safe(secret_name: str):
    try:
        return nakordoni_live(st.secrets[secret_name])
    except Exception:  # noqa: BLE001
        return None


df = get_data()

# ---------------------------------------------------------------- шапка
st.title("🚌 Моя граница")
st.caption("Брест – Тересполь · очередь автобусов и прогноз")

# ---------------------------------------------------------------- 1. мои поездки
st.subheader("Мои поездки")
DIR_TXT = {"entry_by": ("➡️ Туда", "въезд в Беларусь"), "exit_by": ("⬅️ Обратно", "выезд из Беларуси")}
cols = st.columns(len(config.MY_TRIPS)) if config.MY_TRIPS else []
for col, trip in zip(cols, config.MY_TRIPS):
    c = I.trip_card(df, trip)
    m = c["main"]
    head, sub = DIR_TXT[trip["direction"]]
    dep = pd.Timestamp(trip["departure"])
    when = m["when"]
    part = next(n for n, (a, b) in I.TIME_BLOCKS.items() if a <= when.hour < b).lower()
    html = (f'<div class="kicker">{head} · {sub}</div>'
            f'<div class="title">{I.ru_date(dep)} · через {max(c["days_left"], 0)} дн.</div>'
            f'<div class="sub">На границе ≈ {I.ru_date(when)}, {part} ({when:%H:%M})</div>')
    if m["known"]:
        html += (f'<div class="big">{m["icon"]} {m["label"]}</div>'
                 f'<div class="sub">Ожидание ≈ <b>{I.fmt_range(m["wait_lo"], m["wait_hi"])}</b> · '
                 f'автобусов впереди ≈ {m["buses"]:.0f}</div>')
        if m["past"]:
            yrs = ", ".join(f"{p['year']}: {p['value']:.0f}" for p in m["past"])
            html += f'<div class="muted">📚 В прошлые годы в это время: {yrs} автобусов</div>'
        html += f'<div class="muted">Уверенность: {m["confidence"]}</div>'
        if c["tip"]:
            html += f'<div class="tip">💡 {c["tip"]}</div>'
    else:
        html += ('<div class="big">⚪ пока без прогноза</div>'
                 '<div class="sub">По въезду в Беларусь нет своей истории — смотри «Сейчас на границе».</div>')
    with col:
        card(html)

# ---------------------------------------------------------------- 2. сейчас
st.subheader("Сейчас на границе")
c1, c2 = st.columns(2)

with c1:
    nk = nakordoni_safe("NAKORDONI_PPID_ENTRY")
    html = '<div class="kicker">➡️ Туда · Тересполь → Брест</div>'
    if nk and (nk["queue"] is not None or nk["wait_min"] is not None):
        w = nk["wait_min"] / 60 if isinstance(nk["wait_min"], (int, float)) else None
        html += (f'<div class="big">{I.level(w)[0]} {I.fmt_h(w)}</div>'
                 '<div class="sub">весь переход, оценка Nakordoni · автобусов в очереди: '
                 f'{nk["queue"] if nk["queue"] is not None else "—"}</div>'
                 f'<div class="muted">{fmt_age(nk["age"])} · Data by nakordoni.eu</div>')
    else:
        html += '<div class="big">—</div><div class="sub">Нет живых данных Nakordoni</div>'
    card(html)

with c2:
    ns = I.now_status(df, "exit_by")
    html = '<div class="kicker">⬅️ Обратно · Брест → Тересполь</div>'
    if ns:
        html += (f'<div class="big">{I.level(ns["wait"])[0]} {I.fmt_h(ns["wait"])}</div>'
                 f'<div class="sub">автобусов в очереди: {ns["buses"]:.0f}</div>'
                 '<div class="muted">Электронная очередь РБ</div>')
        if ns["age_h"] > 3:
            html += f'<div class="warn">⚠️ данные устарели ({ns["age_h"]:.0f} ч назад)</div>'
    else:
        html += '<div class="big">—</div>'
    card(html)

# ---------------------------------------------------------------- 3. прогноз на месяц
st.subheader("Прогноз на месяц")
dir_choice = st.segmented_control("Направление", ["⬅️ Обратно", "➡️ Туда"], default="⬅️ Обратно",
                                  label_visibility="collapsed") or "⬅️ Обратно"
direction = "exit_by" if dir_choice.startswith("⬅️") else "entry_by"
h, daily = get_month(direction)

if daily.empty:
    st.info("Для въезда в Беларусь пока нет истории, поэтому прогноза на месяц нет. "
            "Когда появится прогноз Nakordoni или своя история, график заработает.")
else:
    dd = daily.reset_index().rename(columns={"index": "day"})
    dd["day_txt"] = [I.ru_date(x) for x in dd["day"]]
    dd["wait_txt"] = [I.fmt_h(w) for w in dd["wait_typ_h"]]
    base = alt.Chart(dd).encode(x=alt.X("day:T", title=None, axis=alt.Axis(format="%d.%m", labelAngle=0)))
    layers = []
    hol = dd[dd["holiday"]].copy()
    if not hol.empty:
        hol["day_end"] = hol["day"] + pd.Timedelta(days=1)
        layers.append(alt.Chart(hol).mark_rect(opacity=0.10, color="#888").encode(x="day:T", x2="day_end:T"))
    layers += [
        base.mark_area(opacity=0.22, color="#e07b39").encode(y=alt.Y("calm:Q", title="автобусов в очереди"),
                                                             y2="peak:Q"),
        base.mark_line(color="#e07b39", strokeWidth=2.5, point=True).encode(
            y="typical:Q",
            tooltip=[alt.Tooltip("day_txt:N", title="день"),
                     alt.Tooltip("typical:Q", title="обычно автобусов", format=".0f"),
                     alt.Tooltip("peak:Q", title="в пик", format=".0f"),
                     alt.Tooltip("wait_txt:N", title="ожидание")]),
    ]
    ly = dd.dropna(subset=["last_year"])
    if not ly.empty:
        layers.append(alt.Chart(ly).mark_point(shape="diamond", size=60, color="#9aa4b2", filled=True).encode(
            x="day:T", y="last_year:Q",
            tooltip=[alt.Tooltip("day_txt:N", title="день"),
                     alt.Tooltip("last_year:Q", title="год назад", format=".0f")]))
    trips = [t for t in config.MY_TRIPS if t["direction"] == direction]
    if trips:
        tdf = pd.DataFrame([{"day": I.border_time(pd.Timestamp(t["departure"]), t.get("departure_tz", model.TZ),
                                                  t["travel_hours"]).tz_localize(None).normalize(),
                             "label": "✈ " + t["name"].split(" (")[0]} for t in trips])
        tdf = tdf[(tdf["day"] >= dd["day"].min()) & (tdf["day"] <= dd["day"].max())]
        if not tdf.empty:
            layers.append(alt.Chart(tdf).mark_rule(color="#2ea043", strokeDash=[4, 3]).encode(x="day:T"))
            layers.append(alt.Chart(tdf).mark_text(align="left", dx=4, dy=-6, color="#2ea043").encode(
                x="day:T", y=alt.value(10), text="label:N"))
    st.altair_chart(alt.layer(*layers).properties(height=280), use_container_width=True)
    st.caption("Линия — обычное число автобусов за день, полоса — от спокойного часа до пикового, "
               "◆ — тот же день год назад (архив ГПК), серый фон — праздничный период. "
               "Прогноз обновляется вместе со сбором данных.")

    # детализация дня
    days = list(dd["day"].dt.date)
    default_day = days[0]
    for t in trips:
        bd = I.border_time(pd.Timestamp(t["departure"]), t.get("departure_tz", model.TZ), t["travel_hours"]).date()
        if bd in days:
            default_day = bd
    pick = st.select_slider("День", options=days, value=default_day, format_func=I.ru_date)
    blocks = I.day_blocks(h, pick)
    bc = st.columns(4)
    for col, b in zip(bc, blocks):
        with col:
            n = "—" if b["buses"] is None else f"{b['buses']:.0f}"
            rng = I.fmt_range(b["wait_lo"], b["wait_hi"]) if b["buses"] is not None else ""
            st.markdown(f'<div class="block">{b["icon"]} <b>{b["name"]}</b><div class="n">{n}</div>'
                        f'<div class="muted">автобусов · {rng}</div></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------- 4. планировщик
st.divider()
st.subheader("🧭 Когда поехать в следующий раз")
st.caption("На будущее: подбирает даты внутри окна по прогнозу и прошлым годам. "
           "Оценивается обратный путь — по нему есть данные.")
occ = st.selectbox("Повод", list(I.OCCASIONS.keys()) + ["Свои даты…"])
if occ == "Свои даты…":
    rng = st.date_input("Окно", value=(date(2027, 3, 1), date(2027, 3, 31)))
    start, end = (rng if isinstance(rng, tuple) and len(rng) == 2 else (date(2027, 3, 1), date(2027, 3, 31)))
    anchor = None
else:
    start, end, anchor = I.OCCASIONS[occ]
    st.caption(f"Окно: {I.ru_date(start)} – {I.ru_date(end)}"
               + (" · сравнение с прошлыми годами по дням от Пасхи" if anchor == "easter" else ""))
pc1, pc2 = st.columns(2)
days_home = pc1.slider("Дней дома", 3, 14, 5)
max_wait = pc2.slider("Готова ждать на границе, не больше (ч)", 1, 12, 5)
res = I.plan(df, start, end, days_home=days_home, max_wait=max_wait, anchor=anchor)
if not res:
    st.info("Для этого окна пока нет данных о прошлых годах. Добавь в архив скриншоты ГПК за эти же даты "
            "прошлых лет (Брест, выезд) — и планировщик сможет сравнить варианты.")
for i, r in enumerate(res):
    medal = ["🥇", "🥈", "🥉"][i]
    b, t = r["back"], r["there"]
    yrs = ", ".join(f"{p['year']}: {p['value']:.0f}" for p in b["past"]) or "—"
    there_txt = (f"{t['icon']} {I.fmt_range(t['wait_lo'], t['wait_hi'])}" if t["known"] else "⚪ нет данных")
    card(f'<div class="title">{medal} {I.ru_date(r["there_date"])} → {I.ru_date(r["back_date"])}'
         f' · {r["days_home"]} дн. дома</div>'
         f'<div class="sub">Туда: {there_txt} · Обратно: {b["icon"]} <b>{I.fmt_range(b["wait_lo"], b["wait_hi"])}</b></div>'
         f'<div class="muted">📚 Прошлые годы на обратном пути: {yrs} автобусов · уверенность: {b["confidence"]}'
         + ("" if r["fits"] else " · ⚠️ дольше, чем ты готова ждать") + '</div>')
if res:
    with st.expander("Как следить за выбранным вариантом"):
        best = res[0]
        st.code(f'''{{"name": "Туда {best["there_date"]:%d.%m}", "direction": "entry_by", "vehicle": "bus",
 "departure": "{best["there_date"]}T20:50", "departure_tz": "Europe/Prague", "travel_hours": 11.5}},
{{"name": "Обратно {best["back_date"]:%d.%m}", "direction": "exit_by", "vehicle": "bus",
 "departure": "{best["back_date"]}T20:00", "departure_tz": "Europe/Minsk", "travel_hours": 5.0}},''',
                language="python")
        st.caption("Добавь эти строки в MY_TRIPS в config.py — поездка появится наверху.")

# ---------------------------------------------------------------- 5. подробнее
st.divider()
with st.expander("▸ Подробнее: почасовые данные, модель, источники"):
    vehicle = st.radio("Транспорт", ["bus", "car"], horizontal=True,
                       format_func={"bus": "Автобусы", "car": "Легковые"}.get)
    s = model.series(df, "exit_by", vehicle)
    if not s.empty:
        q = s["queue_count"].dropna()
        hist = q[q.index >= q.index.max() - pd.Timedelta(days=7)].resample("1h").mean().reset_index()
        hist.columns = ["ts", "value"]
        fc = model.forecast(s, vehicle, days=7).reset_index()
        st.altair_chart(alt.layer(
            alt.Chart(hist).mark_line(color="#1f77b4").encode(x=alt.X("ts:T", title=None),
                                                              y=alt.Y("value:Q", title="в очереди")),
            alt.Chart(fc).mark_line(color="#e07b39", strokeDash=[5, 3]).encode(x="ts:T", y="queue_pred:Q"),
        ).properties(height=220), use_container_width=True)
        st.caption("Выезд из Беларуси по часам: синяя — факт за 7 дней, оранжевая — прогноз на 7 дней.")
        bt = model.backtest(s, vehicle)
        if bt:
            st.write(f"Ошибка прогноза за последние {bt['hours']} ч: **{bt['mae_model']:.1f}** "
                     f"(простое правило «как сейчас»: {bt['mae_naive']:.1f}).")
        st.write(f"Пропускная способность (оценка): {model.throughput_per_hour(s, vehicle):.1f} в час.")
    nk = nakordoni_safe("NAKORDONI_PPID_ENTRY")
    if nk:
        st.caption("Ответ Nakordoni (въезд):")
        st.json(nk["raw"], expanded=False)
    st.caption("Источники: электронная очередь РБ (belarusborder.by), архив Госпогранкомитета (gpk.gov.by), "
               "Nakordoni (data by nakordoni.eu). Ожидание — оценка: очередь ÷ пропускная способность, "
               "без учёта времени самого контроля автобуса.")
