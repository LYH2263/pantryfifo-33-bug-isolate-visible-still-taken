"""脏批隔离：隔离口可见 / 正区与 FEFO 与紧急条三不沾，清洗收口一致。"""
from datetime import date as real_date

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import connect

TODAY = "2026-10-06"


class _FixedDate(real_date):
    @classmethod
    def today(cls):
        return real_date.fromisoformat(TODAY)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "date", _FixedDate)
    with TestClient(main.app) as c:
        yield c


def _ids(rows, key="id"):
    return {r[key] for r in rows}


def _set_dirty(lot_id, **cols):
    c = connect()
    sets = ", ".join(f"{k}=?" for k in cols)
    args = list(cols.values()) + [lot_id]
    c.execute(f"UPDATE lots SET {sets} WHERE id=?", args)
    c.commit()
    c.close()


def _lot(lot_id):
    c = connect()
    row = dict(c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone())
    c.close()
    return row


# seed: 4=冻饺 dirty 余量1 已过期(2025-01-01)，5=鸡蛋 dirty 余量-3
DIRTY_EXPIRED = 4
DIRTY_NEG = 5


def test_quarantine_sees_problem_lots_positive_zone_does_not(ctx):
    q = ctx.get("/api/quarantine").json()
    assert _ids(q) == {DIRTY_EXPIRED, DIRTY_NEG}

    f = ctx.get("/api/fridge").json()
    assert _ids(f) == {1, 2, 3}
    assert all(r["data_quality"] != "dirty" and r["qty_remain"] > 0 for r in f)


def test_alert_bar_never_treats_isolated_as_plain_expiry(ctx):
    # 脏批 4 日历上早过期，但顶条只能报正区的 1/2
    alerts = ctx.get("/api/alerts").json()
    assert _ids(alerts) == {1, 2}
    assert {a["level"] for a in alerts} == {"expired"}


def test_consume_does_not_deduct_isolated_identity(ctx):
    # 冻饺只剩隔离里那一行 dirty：应 short，回包不带任何隔离身份扣减
    r = ctx.post("/api/consume", json={"item_id": 3, "qty": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["deductions"] == []
    assert r.json()["detail"]["reason"] == "short"
    lot = _lot(DIRTY_EXPIRED)
    assert lot["qty_remain"] == 1 and lot["status"] == "on_shelf"  # 没被打穿


def test_consume_fefo_uses_positive_lots_only(ctx):
    r = ctx.post("/api/consume", json={"item_id": 1, "qty": 2})
    assert r.status_code == 200
    # FEFO：lot2(09-28) 先、lot1(10-01) 后；两者都是正区干净批
    assert [d["lot_id"] for d in r.json()["deductions"]] == [2, 1]
    assert _lot(2)["status"] == "consumed"
    assert _lot(1)["qty_remain"] == 1 and _lot(1)["status"] == "on_shelf"
    # 隔离两行纹丝不动
    assert _lot(DIRTY_EXPIRED)["qty_remain"] == 1
    assert _lot(DIRTY_NEG)["qty_remain"] == -3


def test_preview_changes_nothing(ctx):
    r = ctx.post("/api/quarantine/preview",
                 json={"lot_id": DIRTY_EXPIRED, "qty_remain": 1, "expiry": "2026-12-01"})
    assert r.status_code == 200
    assert r.json()["final_status"] == "on_shelf"
    before = _lot(DIRTY_EXPIRED)
    assert before["status"] == "on_shelf" and before["data_quality"] == "dirty"
    assert _ids(ctx.get("/api/quarantine").json()) == {DIRTY_EXPIRED, DIRTY_NEG}


def test_clean_when_calendar_expired_finalizes_expired_everywhere(ctx):
    # 收走同时提交：清洗给正余量但日历已过期 -> expired
    r = ctx.post("/api/quarantine/clean",
                 json={"lot_id": DIRTY_EXPIRED, "qty_remain": 1, "expiry": "2026-10-01"})
    assert r.status_code == 200 and r.json()["lot"]["status"] == "expired"
    # 隔离已空该行；正区无；紧急条也不再报（两处都不再报它）
    assert DIRTY_EXPIRED not in _ids(ctx.get("/api/quarantine").json())
    assert DIRTY_EXPIRED not in _ids(ctx.get("/api/fridge").json())
    assert DIRTY_EXPIRED not in _ids(ctx.get("/api/alerts").json())


def test_clean_non_positive_qty_is_written_off(ctx):
    r = ctx.post("/api/quarantine/clean", json={"lot_id": DIRTY_NEG, "qty_remain": 0})
    assert r.status_code == 200
    lot = r.json()["lot"]
    assert lot["status"] == "consumed" and lot["qty_remain"] == 0
    assert DIRTY_NEG not in _ids(ctx.get("/api/quarantine").json())
    assert DIRTY_NEG not in _ids(ctx.get("/api/fridge").json())


def test_clean_failure_freezes_both_zones(ctx):
    positive_before = _ids(ctx.get("/api/fridge").json())
    r = ctx.post("/api/quarantine/clean", json={"lot_id": DIRTY_EXPIRED, "qty_remain": -1})
    assert r.status_code == 400
    # 正区条数停在清洗前；隔离仍挂着同一行
    assert _ids(ctx.get("/api/fridge").json()) == positive_before
    assert DIRTY_EXPIRED in _ids(ctx.get("/api/quarantine").json())
    db = _lot(DIRTY_EXPIRED)
    assert db["data_quality"] == "dirty" and db["qty_remain"] == 1


def test_clean_success_returns_to_positive_zone_single_closure(ctx):
    r = ctx.post("/api/quarantine/clean",
                 json={"lot_id": DIRTY_EXPIRED, "qty_remain": 1, "expiry": "2026-12-01"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 只出现在正区一处
    assert DIRTY_EXPIRED in _ids(ctx.get("/api/fridge").json())
    assert DIRTY_EXPIRED not in _ids(ctx.get("/api/quarantine").json())
    assert DIRTY_EXPIRED not in _ids(ctx.get("/api/alerts").json())  # 远期，非紧急


def test_clean_success_near_expiry_reported_consistently_by_both_pages(ctx):
    # 回正区且确实临期：正区与紧急条同源（POSITIVE_WHERE），两处口径一致
    r = ctx.post("/api/quarantine/clean",
                 json={"lot_id": DIRTY_EXPIRED, "qty_remain": 1, "expiry": "2026-10-08"})
    assert r.status_code == 200
    assert DIRTY_EXPIRED in _ids(ctx.get("/api/fridge").json())
    alerts = ctx.get("/api/alerts").json()
    near = [a for a in alerts if a["id"] == DIRTY_EXPIRED]
    assert len(near) == 1 and near[0]["level"] == "soon"


def test_sweep_then_clean_leaves_single_status(ctx):
    ctx.post("/api/expire-sweep")
    assert _lot(DIRTY_EXPIRED)["status"] == "expired"  # 清扫先收口
    assert DIRTY_EXPIRED not in _ids(ctx.get("/api/quarantine").json())
    r = ctx.post("/api/quarantine/clean",
                 json={"lot_id": DIRTY_EXPIRED, "qty_remain": 1, "expiry": "2026-12-01"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["reason"] == "not_in_quarantine"
    assert _lot(DIRTY_EXPIRED)["status"] == "expired"  # 不被清洗覆盖回架


def test_clean_then_sweep_does_not_resurrect(ctx):
    ctx.post("/api/quarantine/clean",
             json={"lot_id": DIRTY_EXPIRED, "qty_remain": 1, "expiry": "2025-01-01"})
    assert _lot(DIRTY_EXPIRED)["status"] == "expired"
    ctx.post("/api/expire-sweep")
    assert _lot(DIRTY_EXPIRED)["status"] == "expired"  # 不复活
