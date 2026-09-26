"""设备时钟校准试算接口测试。共享夹具 client 见 conftest.py。"""
from __future__ import annotations

from datetime import datetime, timezone


def calibrated(client, runner, windows):
    return client.post(
        f"/api/runners/{runner}/splits/calibrated", json={"windows": windows}
    )


def window(station, start, end, offset):
    return {
        "station_seq": station,
        "start": start,
        "end": end,
        "offset_seconds": offset,
    }


def segment(data, frm, to):
    return next(
        s for s in data["segments"]
        if s["from_station"] == frm and s["to_station"] == to
    )


def station(data, seq):
    return next(s for s in data["stations"] if s["station_seq"] == seq)


def post(client, event_no, runner, station, occurred_at, corrects=None):
    body = {
        "event_no": event_no,
        "runner_id": runner,
        "station_seq": station,
        "occurred_at": occurred_at,
    }
    if corrects is not None:
        body["corrects"] = corrects
    return client.post("/api/checkins", json=body)


# 种子选手 R001：08:00 / 08:32 / 09:12 / 09:55 / 10:45 / 11:30，
# 分段 1920 / 2400 / 2580 / 3000 / 2700 秒，总计 12600 秒

def test_calibrated_basic_offset(client):
    # 第 2 站 08:32 命中窗口，+600 秒校准为 08:42
    r = calibrated(client, "R001", [window(2, "2026-09-25T08:30:00", "2026-09-25T08:40:00", 600)])
    assert r.status_code == 200
    data = r.json()
    assert data["runner_id"] == "R001"
    assert data["complete"] is True

    st2 = station(data, 2)
    assert st2["event_no"] == "S-R001-2"
    assert datetime.fromisoformat(st2["occurred_at"]) == datetime(2026, 9, 25, 8, 32, tzinfo=timezone.utc)
    assert datetime.fromisoformat(st2["calibrated_at"]) == datetime(2026, 9, 25, 8, 42, tzinfo=timezone.utc)
    # 未命中窗口的站点校准时间等于原始时间
    st1 = station(data, 1)
    assert st1["occurred_at"] == st1["calibrated_at"]

    assert segment(data, 1, 2)["duration_seconds"] == 42 * 60
    assert segment(data, 2, 3)["duration_seconds"] == 30 * 60
    # 中间站平移不改变总用时
    assert data["total_seconds"] == 12600
    assert data["total"] == "3:30:00"


def test_calibrated_negative_offset(client):
    # 第 3 站 09:12 命中窗口，-300 秒校准为 09:07
    r = calibrated(client, "R001", [window(3, "2026-09-25T09:10:00", "2026-09-25T09:15:00", -300)])
    data = r.json()
    assert datetime.fromisoformat(station(data, 3)["calibrated_at"]) == datetime(2026, 9, 25, 9, 7, tzinfo=timezone.utc)
    assert segment(data, 2, 3)["duration_seconds"] == 35 * 60
    assert segment(data, 3, 4)["duration_seconds"] == 48 * 60
    assert data["total_seconds"] == 12600


def test_calibrated_offset_can_change_total(client):
    # 末站 +600：仅末段变长，总用时增加
    r = calibrated(client, "R001", [window(6, "2026-09-25T11:00:00", "2026-09-25T12:00:00", 600)])
    data = r.json()
    assert segment(data, 5, 6)["duration_seconds"] == 2700 + 600
    assert data["total_seconds"] == 12600 + 600
    assert data["total"] == "3:40:00"


def test_calibrated_regression_becomes_pending(client):
    # 第 2 站 +3600 校准为 09:32，晚于第 3 站 09:12：2→3 时间倒退待定
    r = calibrated(client, "R001", [window(2, "2026-09-25T08:30:00", "2026-09-25T08:40:00", 3600)])
    data = r.json()
    assert data["complete"] is False
    seg = segment(data, 2, 3)
    assert seg["status"] == "pending"
    assert "时间倒退" in seg["reason"]
    assert data["total_seconds"] is None
    assert data["total"] is None


def test_calibrated_missing_station_stays_pending(client):
    # R003 缺第 4 站：校准其他站不影响缺站待定
    r = calibrated(client, "R003", [window(1, "2026-09-25T08:00:00", "2026-09-25T08:30:00", 300)])
    data = r.json()
    assert data["complete"] is False
    assert segment(data, 1, 2)["duration_seconds"] == 40 * 60 - 300
    for frm, to in [(3, 4), (4, 5)]:
        seg = segment(data, frm, to)
        assert seg["status"] == "pending"
        assert "站点 4" in seg["reason"]
    assert data["total_seconds"] is None


def test_calibrated_reselects_earliest_by_calibrated_time(client):
    # 同站两条有效打卡：原始时间 C-1A 08:00 早于 C-1B 08:10；
    # C-1A 命中窗口 +900 校准为 08:15 后，C-1B 成为该站最早
    post(client, "C-1A", "TC1", 1, "2026-09-25T08:00:00")
    post(client, "C-1B", "TC1", 1, "2026-09-25T08:10:00")
    post(client, "C-2", "TC1", 2, "2026-09-25T09:00:00")

    data = calibrated(client, "TC1", [window(1, "2026-09-25T08:00:00", "2026-09-25T08:05:00", 900)]).json()
    st1 = station(data, 1)
    assert st1["event_no"] == "C-1B"
    assert datetime.fromisoformat(st1["occurred_at"]) == datetime(2026, 9, 25, 8, 10, tzinfo=timezone.utc)
    assert datetime.fromisoformat(st1["calibrated_at"]) == datetime(2026, 9, 25, 8, 10, tzinfo=timezone.utc)
    assert segment(data, 1, 2)["duration_seconds"] == 50 * 60


def test_calibrated_tie_broken_by_event_no(client):
    # 校准后时间相同（平局）时取事件号较小者
    post(client, "C-2A", "TC2", 1, "2026-09-25T08:05:00")
    post(client, "C-2B", "TC2", 1, "2026-09-25T08:00:00")
    post(client, "C-2C", "TC2", 2, "2026-09-25T09:00:00")

    # C-2B 08:00 +300 = 08:05，与 C-2A 08:05 平局 → 事件号 C-2A 较小入选
    data = calibrated(client, "TC2", [window(1, "2026-09-25T08:00:00", "2026-09-25T08:05:00", 300)]).json()
    st1 = station(data, 1)
    assert st1["event_no"] == "C-2A"
    assert datetime.fromisoformat(st1["calibrated_at"]) == datetime(2026, 9, 25, 8, 5, tzinfo=timezone.utc)


def test_calibrated_window_left_closed_right_open(client):
    # 起点命中（左闭）
    post(client, "B-1", "TCL", 1, "2026-09-25T08:00:00")
    post(client, "B-2", "TCL", 2, "2026-09-25T09:00:00")
    data = calibrated(client, "TCL", [window(1, "2026-09-25T08:00:00", "2026-09-25T08:10:00", 600)]).json()
    assert datetime.fromisoformat(station(data, 1)["calibrated_at"]) == datetime(2026, 9, 25, 8, 10, tzinfo=timezone.utc)
    assert segment(data, 1, 2)["duration_seconds"] == 50 * 60

    # 终点不命中（右开）
    post(client, "B-3", "TCR", 1, "2026-09-25T08:10:00")
    post(client, "B-4", "TCR", 2, "2026-09-25T09:00:00")
    data = calibrated(client, "TCR", [window(1, "2026-09-25T08:00:00", "2026-09-25T08:10:00", 600)]).json()
    st1 = station(data, 1)
    assert st1["calibrated_at"] == st1["occurred_at"]
    assert segment(data, 1, 2)["duration_seconds"] == 50 * 60


def test_calibrated_matches_window_by_original_time(client):
    # 按原始打卡时间匹配窗口：原始 08:00 不在 [08:05, 08:15) 内，
    # 即使 +600 后的 08:10 落在窗内也不偏移
    post(client, "E-1", "TE1", 1, "2026-09-25T08:00:00")
    post(client, "E-2", "TE1", 2, "2026-09-25T09:00:00")
    data = calibrated(client, "TE1", [window(1, "2026-09-25T08:05:00", "2026-09-25T08:15:00", 600)]).json()
    st1 = station(data, 1)
    assert st1["calibrated_at"] == st1["occurred_at"]
    assert segment(data, 1, 2)["duration_seconds"] == 60 * 60


def test_calibrated_windows_normalized_to_utc(client):
    # 带时区偏移的校准窗统一转 UTC 后匹配
    r = calibrated(client, "R001", [window(2, "2026-09-25T16:30:00+08:00", "2026-09-25T16:40:00+08:00", 600)])
    data = r.json()
    assert datetime.fromisoformat(station(data, 2)["calibrated_at"]) == datetime(2026, 9, 25, 8, 42, tzinfo=timezone.utc)
    assert segment(data, 1, 2)["duration_seconds"] == 42 * 60


def test_calibrated_multiple_windows_and_stations(client):
    # 不同站的窗口时间范围可相同；各自独立匹配本站的打卡
    r = calibrated(client, "R001", [
        window(1, "2026-09-25T07:55:00", "2026-09-25T08:05:00", 300),
        window(2, "2026-09-25T07:55:00", "2026-09-25T08:35:00", -120),
    ])
    assert r.status_code == 200
    data = r.json()
    assert datetime.fromisoformat(station(data, 1)["calibrated_at"]) == datetime(2026, 9, 25, 8, 5, tzinfo=timezone.utc)
    assert datetime.fromisoformat(station(data, 2)["calibrated_at"]) == datetime(2026, 9, 25, 8, 30, tzinfo=timezone.utc)
    assert segment(data, 1, 2)["duration_seconds"] == 25 * 60


def test_calibrated_uses_only_active_checkins(client):
    # 被更正 / 已撤销的打卡不参与校准试算
    post(client, "D-1", "TD1", 1, "2026-09-25T08:00:00")
    post(client, "D-2", "TD1", 2, "2026-09-25T09:00:00")
    post(client, "D-2X", "TD1", 2, "2026-09-25T12:00:00")
    post(client, "D-2C", "TD1", 2, "2026-09-25T08:50:00", corrects="D-2X")
    client.post("/api/checkins/D-2/revoke")
    # 第 2 站仅 D-2C（08:50）有效：08:50 +600 = 09:00
    data = calibrated(client, "TD1", [window(2, "2026-09-25T08:00:00", "2026-09-25T09:30:00", 600)]).json()
    st2 = station(data, 2)
    assert st2["event_no"] == "D-2C"
    assert datetime.fromisoformat(st2["calibrated_at"]) == datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
    assert segment(data, 1, 2)["duration_seconds"] == 60 * 60


def test_calibrated_rejects_end_not_after_start(client):
    # 终点早于起点
    r = calibrated(client, "R001", [window(1, "2026-09-25T08:20:00", "2026-09-25T08:10:00", 60)])
    assert r.status_code == 422
    assert "校准窗" in r.json()["detail"]
    # 终点等于起点同样拒绝
    r = calibrated(client, "R001", [window(1, "2026-09-25T08:10:00", "2026-09-25T08:10:00", 60)])
    assert r.status_code == 422


def test_calibrated_rejects_station_out_of_range(client):
    r = calibrated(client, "R001", [window(7, "2026-09-25T08:00:00", "2026-09-25T08:10:00", 60)])
    assert r.status_code == 422
    assert "超出范围" in r.json()["detail"]
    # 站点序号小于 1 由模型校验拒绝
    r = calibrated(client, "R001", [window(0, "2026-09-25T08:00:00", "2026-09-25T08:10:00", 60)])
    assert r.status_code == 422


def test_calibrated_rejects_same_station_overlap(client):
    # 同站窗口重叠 → 422
    r = calibrated(client, "R001", [
        window(2, "2026-09-25T08:30:00", "2026-09-25T08:40:00", 60),
        window(2, "2026-09-25T08:35:00", "2026-09-25T08:45:00", -60),
    ])
    assert r.status_code == 422
    assert "重叠" in r.json()["detail"]


def test_calibrated_allows_adjacent_windows_same_station(client):
    # 同站窗口相接（前窗终点 == 后窗起点）允许
    r = calibrated(client, "R001", [
        window(2, "2026-09-25T08:30:00", "2026-09-25T08:40:00", 60),
        window(2, "2026-09-25T08:40:00", "2026-09-25T08:50:00", -60),
    ])
    assert r.status_code == 200
    data = r.json()
    # 08:32 命中第一窗 +60 → 08:33
    assert datetime.fromisoformat(station(data, 2)["calibrated_at"]) == datetime(2026, 9, 25, 8, 33, tzinfo=timezone.utc)


def test_calibrated_allows_overlapping_windows_across_stations(client):
    # 不同站的窗口时间范围重叠不受限
    r = calibrated(client, "R001", [
        window(1, "2026-09-25T07:55:00", "2026-09-25T08:35:00", 0),
        window(2, "2026-09-25T07:55:00", "2026-09-25T08:35:00", 0),
    ])
    assert r.status_code == 200


def test_calibrated_too_many_windows_422(client):
    windows = [window(1, "2026-09-25T08:00:00", "2026-09-25T08:10:00", 0)] * 101
    r = calibrated(client, "R001", windows)
    assert r.status_code == 422


def test_calibrated_unknown_runner_404(client):
    r = calibrated(client, "NOBODY", [window(1, "2026-09-25T08:00:00", "2026-09-25T09:00:00", 60)])
    assert r.status_code == 404
    # 空窗口同样 404
    assert calibrated(client, "NOBODY", []).status_code == 404


def test_calibrated_empty_windows_matches_realtime(client):
    realtime = client.get("/api/runners/R001/splits").json()
    r = calibrated(client, "R001", [])
    assert r.status_code == 200
    data = r.json()
    assert data["complete"] == realtime["complete"]
    assert data["total_seconds"] == realtime["total_seconds"]
    assert data["total"] == realtime["total"]
    assert data["segments"] == realtime["segments"]
    assert len(data["stations"]) == len(realtime["stations"])
    for st, rt in zip(data["stations"], realtime["stations"]):
        assert st["station_seq"] == rt["station_seq"]
        assert st["event_no"] == rt["event_no"]
        assert st["occurred_at"] == rt["occurred_at"]
        assert st["calibrated_at"] == rt["occurred_at"]


def test_calibrated_has_no_side_effects(client):
    # 发布 v1 后试算：实时成绩、打卡记录、已发布版本均不受影响
    pub = client.post("/api/publications")
    assert pub.status_code == 201 and pub.json()["version"] == 1
    splits_before = client.get("/api/runners/R001/splits").json()
    checkins_before = client.get("/api/checkins", params={"runner_id": "R001"}).json()

    r = calibrated(client, "R001", [window(6, "2026-09-25T11:00:00", "2026-09-25T12:00:00", 3600)])
    assert r.status_code == 200
    assert r.json()["total_seconds"] == 12600 + 3600

    assert client.get("/api/runners/R001/splits").json() == splits_before
    assert client.get("/api/checkins", params={"runner_id": "R001"}).json() == checkins_before
    latest = client.get("/api/publications/latest").json()
    assert latest["version"] == 1
    assert latest == pub.json()
