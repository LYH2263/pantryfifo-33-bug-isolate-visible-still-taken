"""脏批隔离 / 正区对齐验收.

Seed 里有两行问题批（隔离区可见）：
- 冻饺 lot: dirty, qty 1, expiry 2025-01-01（日历已过期）
- 鸡蛋 lot: dirty, qty -3（余量非正）

铁律：
1. 隔离批只进隔离口 —— 不进正区、不进 FEFO 候选、紧急条不当普通到期。
2. 清洗预览不改状态。
3. 清洗失败：正区条数停在清洗前，隔离仍挂着。
4. 清洗确认与过期下架同时提交：同一行只留一种 status，正区无负余量，
   紧急条不报已收口批。
5. 清洗成功后该行要么只出现在正区一处，要么两处都不再报它 —— 两页同一收口。
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app

TODAY = date.today().isoformat()
FAR_FUTURE = "2099-01-01"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def quarantine_rows(c):
    return c.get("/api/quarantine").json()


def quarantine_ids(c):
    return {r["id"] for r in quarantine_rows(c)}


def fridge_rows(c):
    return c.get("/api/fridge").json()


def dirty_lot(c, name):
    """Find a quarantined lot by item name."""
    for r in quarantine_rows(c):
        if r["name"] == name:
            return r
    raise AssertionError(f"{name} not in quarantine")


# --- 1. 隔离口能见那两行问题批，正区/紧急条见不到 -------------------------

def test_quarantine_lists_exactly_the_two_problem_lots(client):
    rows = quarantine_rows(client)
    assert {r["name"] for r in rows} == {"冻饺", "鸡蛋"}
    by_name = {r["name"]: r for r in rows}
    assert by_name["冻饺"]["data_quality"] == "dirty"
    assert "dirty" in by_name["冻饺"]["reasons"]
    assert by_name["鸡蛋"]["qty_remain"] == -3
    assert "non_positive_qty" in by_name["鸡蛋"]["reasons"]


def test_fridge_positive_zone_excludes_quarantined(client):
    qids = quarantine_ids(client)
    rows = fridge_rows(client)
    assert qids, "seed must have quarantined lots"
    assert qids.isdisjoint(r["id"] for r in rows)
    for r in rows:
        assert r["qty_remain"] > 0
        assert r["data_quality"] != "dirty"


def test_alerts_never_list_quarantined_as_plain_expiry(client):
    qids = quarantine_ids(client)
    alerts = client.get("/api/alerts").json()
    assert qids.isdisjoint(r["id"] for r in alerts)
    for r in alerts:
        assert r["data_quality"] != "dirty"


# --- 2. 正区扣减打不到隔离批 ----------------------------------------------

def test_consume_response_never_carries_quarantined_identity(client):
    # 冻饺 only has the dirty lot: FEFO must report short, not deduct it.
    dumpling = dirty_lot(client, "冻饺")
    r = client.post("/api/consume", json={"item_id": dumpling["item_id"], "qty": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "short"
    # untouched in quarantine
    assert dirty_lot(client, "冻饺")["qty_remain"] == 1

    # 鸡蛋 has a clean lot (12) plus the dirty -3 one: deductions hit only the clean lot.
    egg = dirty_lot(client, "鸡蛋")
    before = sum(x["qty_remain"] for x in fridge_rows(client) if x["item_id"] == egg["item_id"])
    r = client.post("/api/consume", json={"item_id": egg["item_id"], "qty": 5})
    assert r.status_code == 200
    qids = quarantine_ids(client)
    assert qids.isdisjoint(d["lot_id"] for d in r.json()["deductions"])
    after = sum(x["qty_remain"] for x in fridge_rows(client) if x["item_id"] == egg["item_id"])
    assert before - after == 5
    assert dirty_lot(client, "鸡蛋")["qty_remain"] == -3  # 隔离批原样


# --- 3. 清洗预览不改状态 ----------------------------------------------------

def test_preview_changes_nothing(client):
    lot = dirty_lot(client, "冻饺")
    fridge_before = fridge_rows(client)
    quarantine_before = quarantine_ids(client)
    r = client.post("/api/quarantine/preview", json={"lot_id": lot["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["final_status"] == "expired"  # 日历已过期 -> 收口为过期下架
    assert quarantine_ids(client) == quarantine_before  # 隔离仍挂着
    assert [x["id"] for x in fridge_rows(client)] == [x["id"] for x in fridge_before]


# --- 4. 清洗失败：正区条数停在清洗前，隔离仍挂着 ----------------------------

@pytest.mark.parametrize("bad_qty", [-3, -0.5])
def test_failed_clean_keeps_quarantine_and_positive_zone(client, bad_qty):
    lot = dirty_lot(client, "鸡蛋")
    fridge_before = fridge_rows(client)
    r = client.post("/api/quarantine/clean", json={"lot_id": lot["id"], "qty_remain": bad_qty})
    assert r.status_code == 400
    # 隔离仍挂着
    assert lot["id"] in quarantine_ids(client)
    # 正区一条不多一条不少
    assert [x["id"] for x in fridge_rows(client)] == [x["id"] for x in fridge_before]


def test_clean_without_fix_of_negative_qty_fails(client):
    lot = dirty_lot(client, "鸡蛋")
    r = client.post("/api/quarantine/clean", json={"lot_id": lot["id"]})
    assert r.status_code == 400  # 余量非正未修正 -> 拒绝，保持隔离
    assert lot["id"] in quarantine_ids(client)


# --- 5. 清洗确认 × 过期下架同时提交：同一行只留一种 status ------------------

def assert_dumpling_finalized_everywhere(client, lot_id):
    """收口后：隔离空了、正区没有它（更无负余量）、紧急条不报。"""
    assert lot_id not in quarantine_ids(client)
    fridge = fridge_rows(client)
    assert lot_id not in {r["id"] for r in fridge}
    assert all(r["qty_remain"] > 0 for r in fridge)
    assert lot_id not in {r["id"] for r in client.get("/api/alerts").json()}


def test_clean_then_sweep_on_expired_dirty_lot(client):
    lot = dirty_lot(client, "冻饺")
    r = client.post("/api/quarantine/clean", json={"lot_id": lot["id"]})
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json()["lot"]["status"] == "expired"  # 日历已过期 -> 下架，不回正区
    sweep = client.post("/api/expire-sweep").json()
    assert lot["id"] not in sweep["expired_ids"]  # 已收口，收走不再碰
    assert_dumpling_finalized_everywhere(client, lot["id"])


def test_sweep_then_clean_on_expired_dirty_lot(client):
    lot = dirty_lot(client, "冻饺")
    sweep = client.post("/api/expire-sweep").json()
    assert lot["id"] in sweep["expired_ids"]  # 收走先收口
    r = client.post("/api/quarantine/clean", json={"lot_id": lot["id"]})
    assert r.status_code == 200 and not r.json()["ok"]  # 清洗不覆盖已收口状态
    assert r.json()["lot"]["status"] == "expired"
    assert_dumpling_finalized_everywhere(client, lot["id"])


# --- 6. 清洗成功：只出现在正区一处，或两处都不再报它 ------------------------

def test_clean_success_lands_in_positive_zone_only(client):
    lot = dirty_lot(client, "鸡蛋")
    r = client.post("/api/quarantine/clean",
                    json={"lot_id": lot["id"], "qty_remain": 3, "expiry": FAR_FUTURE})
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json()["lot"]["status"] == "on_shelf"
    # 正区恰好一处
    assert [x["id"] for x in fridge_rows(client)].count(lot["id"]) == 1
    # 隔离不再挂
    assert lot["id"] not in quarantine_ids(client)
    # 紧急条不报（远期到期）
    assert lot["id"] not in {x["id"] for x in client.get("/api/alerts").json()}


def test_clean_success_near_expiry_same_outcome_on_both_pages(client):
    """回正区且临近到期：正区与紧急条必须同一收口 —— 都把它当普通正区批。"""
    lot = dirty_lot(client, "鸡蛋")
    soon = (date.today() + timedelta(days=1)).isoformat()
    r = client.post("/api/quarantine/clean",
                    json={"lot_id": lot["id"], "qty_remain": 3, "expiry": soon})
    assert r.json()["lot"]["status"] == "on_shelf"
    assert lot["id"] in {x["id"] for x in fridge_rows(client)}
    alerts = {x["id"]: x for x in client.get("/api/alerts").json()}
    assert lot["id"] in alerts
    assert alerts[lot["id"]]["level"] == "soon"  # 普通临期，不是隔离外误报


def test_clean_success_expired_calendar_disappears_everywhere(client):
    """日历已过期且清洗确认：不进正区，紧急条也不报 —— 两处都不再报它。"""
    lot = dirty_lot(client, "冻饺")
    r = client.post("/api/quarantine/clean", json={"lot_id": lot["id"]})
    assert r.json()["lot"]["status"] == "expired"
    assert_dumpling_finalized_everywhere(client, lot["id"])
