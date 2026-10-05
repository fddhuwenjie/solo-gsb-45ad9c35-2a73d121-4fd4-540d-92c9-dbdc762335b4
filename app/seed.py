"""演示数据：线路/接头/批次/观测/复核，封存报告由用户在界面触发（满足门槛即封）。"""
from .db import now_iso, audit
from . import services


def seed(conn):
    t = now_iso()
    conn.execute("INSERT INTO lines(name, created_at) VALUES (?,?)", ("220kV 东隧道线", t))
    conn.execute("INSERT INTO lines(name, created_at) VALUES (?,?)", ("110kV 西联络线", t))

    joints = [
        (1, "K12+340", "J-07"),
        (1, "K12+905", "J-08"),
        (1, "K13+480", "J-09"),
        (2, "K03+120", "J-02"),
        (2, "K03+760", "J-03"),
        (2, "K04+210", "J-04"),
    ]
    for line_id, mileage, no in joints:
        conn.execute(
            "INSERT INTO joints(line_id, mileage, joint_no, created_at) VALUES (?,?,?,?)",
            (line_id, mileage, no, t),
        )

    conn.execute(
        "INSERT INTO batches(name, inspected_at, created_at) VALUES (?,?,?)",
        ("2026-09 一轮", "2026-09-18", t),
    )
    conn.execute(
        "INSERT INTO batches(name, inspected_at, created_at) VALUES (?,?,?)",
        ("2026-10 二轮", "2026-10-02", t),
    )

    def obs(joint_id, batch_id, load, dist, diff, calib, inspector, note="",
            retest=0, corrects=None, recorded=None):
        cur = conn.execute(
            "INSERT INTO observations(joint_id, batch_id, load_range, measure_distance_m, "
            "temp_diff_k, calib_version, inspector, note, is_retest, corrects_id, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (joint_id, batch_id, load, dist, diff, calib, inspector, note, retest,
             corrects, recorded or t),
        )
        return cur.lastrowid

    # 一轮（批次1，巡检日 2026-09-18）
    obs(1, 1, "300-380A", 5.0, 2.5, "CAL-2026.07", "张工", "初测稳定",
        recorded="2026-09-18T09:00:00Z")
    obs(1, 1, "300-380A", 5.0, 2.8, "CAL-2026.07", "张工",
        recorded="2026-09-18T09:30:00Z")
    # 高风险但已复测+人工复核（复测/意见时间均不早于最新观测）→ 可封存
    obs(2, 1, "310-390A", 5.0, 9.6, "CAL-2026.07", "张工", "初测偏高",
        recorded="2026-09-18T10:00:00Z")
    obs(2, 1, "310-390A", 5.0, 9.2, "CAL-2026.07", "李工", "复测确认", retest=1,
        recorded="2026-09-18T15:00:00Z")
    conn.execute(
        "INSERT INTO reviews(joint_id, batch_id, reviewer, opinion, review_type, recorded_at) "
        "VALUES (?,?,?,?,?,?)",
        (2, 1, "王班长", "接头外观无异常，已安排限负荷运行并加密测温，判定为设备发热重点接头，继续跟踪两周。", "人工复核",
         "2026-09-18T16:30:00Z"),
    )
    # 证据不足：单条读数
    obs(3, 1, "320-400A", 5.0, 6.4, "CAL-2026.07", "张工", "仅一人读数",
        recorded="2026-09-18T11:00:00Z")
    obs(4, 1, "260-320A", 4.5, 3.1, "CAL-2026.07", "赵工",
        recorded="2026-09-18T09:00:00Z")
    obs(4, 1, "260-320A", 4.5, 3.4, "CAL-2026.07", "赵工",
        recorded="2026-09-18T09:30:00Z")

    # 二轮（批次2，巡检日 2026-10-02）：相互矛盾 / 条件不一致 / 更正示例
    obs(1, 2, "300-380A", 5.0, 3.0, "CAL-2026.09", "张工",
        recorded="2026-10-02T09:00:00Z")
    obs(1, 2, "300-380A", 5.0, 3.3, "CAL-2026.09", "张工",
        recorded="2026-10-02T09:30:00Z")
    obs(5, 2, "270-340A", 4.5, 3.2, "CAL-2026.09", "赵工",
        recorded="2026-10-02T09:00:00Z")
    obs(5, 2, "270-340A", 4.5, 8.8, "CAL-2026.09", "赵工",
        "补测读数，差异大待查", recorded="2026-10-02T09:30:00Z")
    # 条件不一致：距离与校准版本不同 → 证据不足
    obs(2, 2, "310-390A", 5.0, 7.0, "CAL-2026.09", "李工",
        recorded="2026-10-02T10:00:00Z")
    obs(2, 2, "310-390A", 8.0, 4.2, "CAL-2026.08", "陈工", "距离偏差，旧校准版本",
        recorded="2026-10-02T10:30:00Z")

    # 更正示例：录入错误 -> 更正，原记录保留并置 superseded
    bad = obs(3, 2, "320-400A", 5.0, 12.9, "CAL-2026.09", "张工", "录入有误",
              recorded="2026-10-02T11:00:00Z")
    conn.execute("UPDATE observations SET superseded=1 WHERE id=?", (bad,))
    good = obs(3, 2, "320-400A", 5.0, 3.9, "CAL-2026.09", "张工",
               "更正：上一条误录为12.9K，实际3.9K", corrects=bad,
               recorded="2026-10-02T11:20:00Z")

    # 复测被后续观测过期示例：先初测高风险并完成复测，随后补录一条 recorded_at 更晚的
    # 普通观测（即使提交更晚也按观测时间判定），原复测不再覆盖最新证据 → 阻断封存
    obs(6, 2, "290-360A", 5.0, 9.1, "CAL-2026.09", "赵工", "初测偏高",
        recorded="2026-10-02T09:10:00Z")
    obs(6, 2, "290-360A", 5.0, 8.9, "CAL-2026.09", "赵工", "当日复测确认",
        retest=1, recorded="2026-10-02T15:00:00Z")
    obs(6, 2, "290-360A", 5.0, 9.4, "CAL-2026.09", "赵工",
        "隔日补录普通观测，时间晚于既有复测", recorded="2026-10-03T08:30:00Z")

    services.recompute_batch(conn, 1)
    services.recompute_batch(conn, 2)
    audit(conn, "SEED", "system", None, "演示数据播种完成")
