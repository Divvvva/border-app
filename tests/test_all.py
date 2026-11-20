"""Проверка без интернета: парсеры на образцах и модель на синтетике.
Запуск: python -m tests.test_all
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector  # noqa: E402
import model  # noqa: E402


def test_belarusborder_parser():
    ts = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)  # 18:00 Минск
    sample = {
        "busLiveQueue": [
            {"regnum": "AB1234-1", "status": 2, "registration_date": "14:00:00 20.11.2026"},
            {"regnum": "AB5678-1", "status": 2, "registration_date": "16:30:00 20.11.2026"},
        ],
        "carLiveQueue": [{"regnum": f"X{i}", "status": 2, "registration_date": "17:00:00 20.11.2026"} for i in range(40)],
        "truckLiveQueue": [],
    }
    rows = {r["vehicle_type"]: r for r in collector.parse_belarusborder(sample, ts)}
    assert rows["bus"]["queue_count"] == 2
    assert rows["bus"]["oldest_wait_min"] == 240, rows["bus"]
    assert rows["car"]["queue_count"] == 40
    assert collector.find_checkpoint_id({"result": [{"id": "x1", "name": "Каменный Лог"},
                                                    {"id": "b2", "name": "Брест (Варшавский мост)"}]}) == "b2"
    print("ok belarusborder parser")


def test_gpk_parser():
    html = "<div>Information about the queues at the checkpoint</div><div>exit from RB</div>" \
           "<span>120</span><span>0</span><span>7</span><div>Passanger cars - 2350</div>"
    rows = {r["vehicle_type"]: r["queue_count"] for r in collector.parse_gpk_html(html)}
    assert rows == {"car": 120, "truck": 0, "bus": 7}, rows
    print("ok gpk parser")


def test_nakordoni_parser():
    rows = collector.parse_nakordoni({"data": {"snapshot": {"queue_now": 35, "wait_min": 180}}})
    assert rows[0]["queue_count"] == 35 and rows[0]["direction"] == "entry_by"
    print("ok nakordoni parser")


def synthetic_csv(path, days=24, direction="exit_by", end=None):
    """Автобусы: пик вечером пятницы и воскресенья, минимум в будни днём."""
    rng = np.random.default_rng(1)
    end = end or datetime(2026, 11, 20, tzinfo=timezone.utc)
    start = end - timedelta(days=days)
    rows = []
    q = 5.0
    for i in range(days * 96):
        ts = start + timedelta(minutes=15 * i)
        local = ts + timedelta(hours=3)
        base = 4 + 3 * np.sin((local.hour - 8) / 24 * 2 * np.pi)
        if local.weekday() in (4, 6) and local.hour >= 16:
            base += 10
        q = 0.8 * q + 0.2 * base + rng.normal(0, 0.6)
        rows.append({"ts_utc": ts.isoformat(), "direction": direction, "vehicle_type": "bus",
                     "queue_count": max(0, round(q)), "wait_minutes": "", "oldest_wait_min": "",
                     "median_wait_min": "", "source": "belarusborder", "note": ""})
    pd.DataFrame(rows).to_csv(path, index=False)


def test_model():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "q.csv")
        synthetic_csv(p)
        s = model.series(model.load(p), "exit_by", "bus")
        fc = model.forecast(s, "bus", days=7)
        assert len(fc) == 168 and (fc.queue_pred >= 0).all()
        # Пятница вечер должна быть выше, чем будний день днём
        fri = fc[(fc.index.weekday == 4) & (fc.index.hour == 19)].queue_pred.mean()
        tue = fc[(fc.index.weekday == 1) & (fc.index.hour == 12)].queue_pred.mean()
        assert fri > tue, (fri, tue)
        bt = model.backtest(s, "bus")
        print(f"ok model | пт 19:00 ≈ {fri:.1f} авт., вт 12:00 ≈ {tue:.1f} авт. | "
              f"пропускная ≈ {fc.attrs['throughput_per_hour']:.1f} авт/ч | backtest {bt}")
        print(model.best_times(fc).round(1).to_string())
        # Холодный старт: всего 2 часа данных — не падаем
        s2 = s.iloc[:8]
        assert not model.forecast(s2, "bus").empty
        print("ok cold start")


def test_append():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x", "q.csv")
        ts = datetime.now(timezone.utc)
        collector.append_rows([{"direction": "exit_by", "vehicle_type": "bus", "queue_count": 3, "source": "t"}], ts, p)
        collector.append_rows([{"direction": "exit_by", "vehicle_type": "bus", "queue_count": 4, "source": "t"}], ts, p)
        assert len(pd.read_csv(p)) == 2
    print("ok csv append")


def test_long_horizon():
    """Прогноз на 28.12 из разных моментов: интервал должен сужаться."""
    import pandas as pd
    target = pd.Timestamp("2026-12-28 18:00", tz=model.TZ)
    with tempfile.TemporaryDirectory() as d:
        print("прогноз на 28.12 18:00 (въезд, автобусы), сделан:")
        widths = []
        for made, hist in [("2026-11-21", 5), ("2026-12-01", 15), ("2026-12-14", 28), ("2026-12-24", 38)]:
            p = os.path.join(d, f"q{hist}.csv")
            synthetic_csv(p, days=hist, direction="entry_by",
                          end=datetime.fromisoformat(made).replace(tzinfo=timezone.utc))
            s = model.series(model.load(p), "entry_by", "bus")
            r = model.forecast_point(s, "bus", target)
            assert r is not None and r["holiday"]
            widths.append(r["queue_hi"] - r["queue_lo"])
            print(f"  {made}: {r['queue_pred']:.1f} авт. ({r['queue_lo']:.1f}–{r['queue_hi']:.1f}), "
                  f"ожидание {r['wait_h_pred']:.1f} ч, уверенность {r['confidence']}")
        assert widths[-1] < widths[0], widths
    print("ok long horizon")


if __name__ == "__main__":
    test_belarusborder_parser()
    test_gpk_parser()
    test_nakordoni_parser()
    test_append()
    test_model()
    test_long_horizon()
    print("ВСЕ ТЕСТЫ ПРОШЛИ")
