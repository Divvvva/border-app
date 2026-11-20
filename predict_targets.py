"""Пересчитывает прогноз на мои поездки (config.MY_TRIPS) и дописывает его
в data/forecast_log.csv. Запускается после collector.py.
Так видно, как прогноз на 28.12 менялся день за днём.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

import config
import model


def main() -> int:
    if not os.path.exists(config.DATA_FILE):
        print("Нет данных — пропуск")
        return 0
    df = model.load(config.DATA_FILE)
    made_at = pd.Timestamp.now(tz=model.TZ).floor("min")
    rows = []
    for trip in config.MY_TRIPS:
        s = model.series(df, trip["direction"], trip.get("vehicle", "bus"))
        if s.empty:
            # для автобусов данных может не быть — пробуем легковые как индикатор загрузки
            s = model.series(df, trip["direction"], "car")
            vehicle = "car"
        else:
            vehicle = trip.get("vehicle", "bus")
        if s.empty:
            print(f"{trip['name']}: по направлению {trip['direction']} данных пока нет")
            continue
        when = model.trip_border_time(trip)
        p = model.forecast_point(s, vehicle, when)
        if p is None:
            print(f"{trip['name']}: дата вне горизонта ({config.MAX_HORIZON_DAYS} дн.) или уже прошла")
            continue
        rows.append({"made_at": made_at.isoformat(), "trip": trip["name"], "vehicle": vehicle,
                     **{k: (v.isoformat() if isinstance(v, pd.Timestamp) else v) for k, v in p.items()}})
        print(f"{trip['name']}: на границе {when:%d.%m %H:%M}, очередь ≈ {p['queue_pred']:.0f} "
              f"({p['queue_lo']:.0f}–{p['queue_hi']:.0f}), ожидание ≈ {p['wait_h_pred']:.1f} ч, "
              f"уверенность: {p['confidence']}")
    if rows:
        new = pd.DataFrame(rows)
        header = not os.path.exists(config.FORECAST_LOG)
        new.to_csv(config.FORECAST_LOG, mode="a", header=header, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
