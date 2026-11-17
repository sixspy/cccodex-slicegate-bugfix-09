
import threading

import pytest
from fastapi.testclient import TestClient
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c, username="admin", password="123456"):
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _add_slice(c, h, slice_no, row_count, expected_count, biz_date="2026-11-05", partition="mid"):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": slice_no, "biz_date": biz_date, "partition_key": partition,
        "row_count": row_count, "expected_count": expected_count})
    assert r.status_code == 200
    return r.json()["id"]


def _releases(c, h):
    return c.get("/api/releases", headers=h).json()["items"]


def _deviations(c, h):
    return c.get("/api/deviations", headers=h).json()["items"]


def _watermarks(c, h):
    return c.get("/api/watermarks", headers=h).json()["items"]


def test_incomplete_release_zero_writes_and_deviation():
    """未完整 → 409，零写入（无流水/无水位/状态不变），并追加筛选偏离流水。"""
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-ZW-1", 500, 1000)  # 部分完整
    r = c.post(f"/api/slices/{sid}/release", headers=h,
               json={"consumer": "ads_x", "filter": {"biz_date": "2026-11-05"}})
    assert r.status_code == 409
    # 零写入：无放行流水、无水位、放行态不变
    assert all(x["slice_id"] != sid for x in _releases(c, h))
    assert all(w["source_slice"] != "SL-ZW-1" for w in _watermarks(c, h))
    item = [s for s in c.get("/api/slices", headers=h).json()["items"] if s["id"] == sid][0]
    assert item["release_status"] == "未放行"
    # 筛选偏离流水留痕
    dev = [d for d in _deviations(c, h) if d["slice_id"] == sid]
    assert len(dev) == 1 and dev[0]["kind"] == "筛选偏离"
    assert "2026-11-05" in dev[0]["filter_key"] and dev[0]["actor"] == "admin"


def test_list_and_gate_same_source():
    """筛选列表的可放行集合与闸口裁决同源：列表说可放行的闸口必须放行，说不行的闸口必须拒。"""
    c = TestClient(app)
    h = _login(c)
    ok_id = _add_slice(c, h, "SL-SS-ok", 1000, 1000)
    bad_id = _add_slice(c, h, "SL-SS-bad", 999, 1000)
    items = {s["id"]: s for s in c.get("/api/slices", headers=h).json()["items"]}
    assert items[ok_id]["releasable"] is True and items[ok_id]["completeness"] == "完整"
    assert items[bad_id]["releasable"] is False and items[bad_id]["completeness"] == "部分完整"
    # 筛选参数生效且口径一致
    only = c.get("/api/slices?releasable=1", headers=h).json()
    assert all(s["releasable"] for s in only["items"])
    assert any(s["id"] == ok_id for s in only["items"]) and all(s["id"] != bad_id for s in only["items"])
    by_date = c.get("/api/slices?biz_date=2026-11-05&completeness=完整", headers=h).json()
    assert {s["id"] for s in by_date["items"]} == {ok_id}
    # 列表判定与闸口结果一致
    assert c.post(f"/api/slices/{ok_id}/release", headers=h, json={"consumer": "ads_x"}).status_code == 200
    assert c.post(f"/api/slices/{bad_id}/release", headers=h, json={"consumer": "ads_x"}).status_code == 409
    # 放行后列表同源刷新：不再可放行
    items = {s["id"]: s for s in c.get("/api/slices", headers=h).json()["items"]}
    assert items[ok_id]["releasable"] is False and "已放行" in items[ok_id]["releasable_reason"]


def test_relaxed_frontend_filter_cannot_bypass():
    """禁止靠前端放宽/伪造筛选绕过完整度：谎称完整照样拒，且零写入。"""
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-LIE-1", 10, 1000)
    r = c.post(f"/api/slices/{sid}/release", headers=h,
               json={"consumer": "ads_x", "filter": {"completeness": "完整"}})
    assert r.status_code == 409
    assert "筛选口径偏了" in r.json()["detail"]
    assert all(x["slice_id"] != sid for x in _releases(c, h))
    assert all(w["source_slice"] != "SL-LIE-1" for w in _watermarks(c, h))
    dev = [d for d in _deviations(c, h) if d["slice_id"] == sid]
    assert len(dev) == 1 and dev[0]["kind"] == "筛选口径偏了"


def test_filter_mismatch_recorded_and_rejected():
    """筛选条件与切片不符 → 记筛选口径偏了并拒绝。"""
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-MM-1", 1000, 1000, biz_date="2026-11-06")
    r = c.post(f"/api/slices/{sid}/release", headers=h,
               json={"consumer": "ads_x", "filter": {"biz_date": "1999-01-01"}})
    assert r.status_code == 409 and "筛选口径偏了" in r.json()["detail"]
    assert all(x["slice_id"] != sid for x in _releases(c, h))
    dev = [d for d in _deviations(c, h) if d["slice_id"] == sid]
    assert len(dev) == 1 and dev[0]["kind"] == "筛选口径偏了"


def test_contention_second_marked():
    """同一筛选条件下抢点：先手成功，后手标注筛选争用，水位只写一次。"""
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-CT-1", 1000, 1000)
    f = {"biz_date": "2026-11-05", "partition_key": "mid"}
    first = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_a", "filter": f})
    assert first.status_code == 200
    second = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_b", "filter": f})
    assert second.status_code == 409 and "筛选争用" in second.json()["detail"]
    rows = [x for x in _releases(c, h) if x["slice_id"] == sid]
    assert [x["status"] for x in rows] == ["争用", "成功"]  # 倒序返回
    assert rows[0]["filter_key"] == "biz_date=2026-11-05|partition_key=mid"
    assert len([w for w in _watermarks(c, h) if w["source_slice"] == "SL-CT-1"]) == 1


def test_concurrent_race_queued_single_winner():
    """两名值班同时抢点：写事务排队，恰好一成功一争用，水位恰一条。"""
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-RC-1", 1000, 1000)
    f = {"biz_date": "2026-11-05"}
    barrier = threading.Barrier(2)
    results = []

    def hit(consumer):
        client = TestClient(app)
        barrier.wait()
        r = client.post(f"/api/slices/{sid}/release", headers=h,
                        json={"consumer": consumer, "filter": f})
        results.append((r.status_code, r.json()))

    t1, t2 = threading.Thread(target=hit, args=("ads_a",)), threading.Thread(target=hit, args=("ads_b",))
    t1.start(); t2.start(); t1.join(); t2.join()
    codes = sorted(code for code, _ in results)
    assert codes == [200, 409]
    loser = [j for code, j in results if code == 409][0]
    assert "筛选争用" in loser["detail"]
    rows = [x for x in _releases(c, h) if x["slice_id"] == sid]
    assert sorted(x["status"] for x in rows) == ["争用", "成功"]
    assert len([w for w in _watermarks(c, h) if w["source_slice"] == "SL-RC-1"]) == 1


def test_detail_and_policy_aligned():
    """切片详情与列表同口径；策略条钉死乙处置且当日禁止改口。"""
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-DT-1", 0, 1000)  # 待检
    c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    d = c.get(f"/api/slices/{sid}", headers=h).json()
    assert d["slice"]["completeness"] == "待检" and d["slice"]["releasable"] is False
    assert len(d["deviations"]) == 1 and d["deviations"][0]["kind"] == "筛选偏离"
    p = c.get("/api/policy", headers=h).json()
    assert p["approach"] == "乙" and p["effective_date"] == "2026-11-17"
    assert "现场重算" in p["statement"] and "当日禁止改口" in p["no_same_day_change"]
    # 未登录不可查
    assert c.get("/api/policy").status_code == 401
    assert c.get("/api/deviations").status_code == 401
