"""同一接头 + 同一批次的观测对齐、证据分级与风险结论生成。

分级规则（可通过本模块常量调参）：
- 测温距离差值 > DISTANCE_TOL_M          → 观测条件不一致
- 校准版本不同                            → 观测条件不一致
- 负荷区间无交集                          → 观测条件不一致
- 其余 ≥2 条读数且温差极差 > CONTRADICTION_SPREAD_K → 相互矛盾
- 单条有效读数                            → 证据不足

风险：
- high:    相互矛盾，或最大温差 >= HIGH_TEMP_K（证据不足时降级为 medium）
- medium:  最大温差 >= MED_TEMP_K，或证据不足且偏高
- low:     其余

复测/复核意见的覆盖判定（封存门槛与处置清单共用）：
- “最新证据时间”取当前有效观测（superseded=0）中最大的 recorded_at；
- 复测只有在仍当前有效且其 recorded_at 不早于最新证据时间时，才算“覆盖最新证据”；
  被更正（superseded=1）的复测不算当前证据；
- 人工复核意见同理，其 recorded_at 不早于最新证据时间才算覆盖；
- 判定一律以记录上的观测/复核时间 recorded_at 为准，而非录入（提交）时间，
  因此较早时间的回填观测不会仅因提交较晚而使复测/意见过期。
"""
import json
from datetime import datetime, timezone

HIGH_TEMP_K = 8.0
MED_TEMP_K = 4.0
CONTRADICTION_SPREAD_K = 3.0
DISTANCE_TOL_M = 1.0

RETEST_OK = "current"          # 复测覆盖最新证据
RETEST_STALE = "stale"         # 复测仍有效，但已有更晚的有效观测
RETEST_SUPERSEDED = "superseded"  # 唯一复测已被更正，不再是当前证据
RETEST_NONE = "none"           # 从无复测

REVIEW_OK = "current"
REVIEW_STALE = "stale"
REVIEW_NONE = "none"


def parse_load(text):
    """'300-380A' / '300~380' / '300' -> (300.0, 380.0)；无法解析返回 None。"""
    s = (text or "").strip().lower().replace("a", "").replace("～", "~").replace("—", "-")
    for sep in ("~", "-", "至"):
        if sep in s:
            a, b = s.split(sep, 1)
            try:
                lo, hi = float(a.strip()), float(b.strip())
                return (lo, hi) if lo <= hi else (hi, lo)
            except ValueError:
                return None
    try:
        v = float(s)
        return (v, v)
    except ValueError:
        return None


def load_overlap(a, b):
    pa, pb = parse_load(a), parse_load(b)
    if pa is None or pb is None:
        return None  # 无法判定
    return max(pa[0], pb[0]) <= min(pa[1], pb[1])


def time_key(s):
    """recorded_at 排序键：可解析的 ISO 时间优先，无法解析时回退为字符串比较。"""
    s = (s or "").strip()
    try:
        v = s.replace("Z", "+00:00") if s.endswith("Z") else s
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (0, dt.astimezone(timezone.utc))
    except ValueError:
        return (1, s)


def coverage_info(active_obs, all_obs, reviews):
    """计算复测/复核意见对“最新有效观测”的覆盖状态。

    返回 dict：
      latest_obs_at / latest_obs_ids   最新证据时间及处于该时间的有效观测 id
      retest_status / latest_retest_at / stale_retest_obs / superseded_retest_obs
      review_status / latest_review_at
    """
    active_obs = list(active_obs)
    info = {
        "latest_obs_at": None,
        "latest_obs_ids": [],
        "retest_status": RETEST_NONE,
        "latest_retest_at": None,
        "stale_retest_obs": [],
        "superseded_retest_obs": [],
        "review_status": REVIEW_NONE,
        "latest_review_at": None,
    }
    if active_obs:
        latest = max(time_key(o["recorded_at"]) for o in active_obs)
        info["latest_obs_at"] = next(
            o["recorded_at"] for o in active_obs if time_key(o["recorded_at"]) == latest)
        info["latest_obs_ids"] = [o["id"] for o in active_obs
                                  if time_key(o["recorded_at"]) == latest]

    active_retests = [o for o in active_obs if o["is_retest"]]
    if active_retests:
        latest_retest = max(active_retests, key=lambda o: time_key(o["recorded_at"]))
        info["latest_retest_at"] = latest_retest["recorded_at"]
        if info["latest_obs_at"] is not None and \
                time_key(latest_retest["recorded_at"]) >= time_key(info["latest_obs_at"]):
            info["retest_status"] = RETEST_OK
        else:
            info["retest_status"] = RETEST_STALE
            info["stale_retest_obs"] = [
                o for o in active_retests
                if time_key(o["recorded_at"]) < time_key(info["latest_obs_at"])]
    # 被更正的复测不是当前证据：仅用于说明“为何复测不算数”
    superseded_retests = [o for o in (all_obs or [])
                          if o["is_retest"] and o["superseded"]]
    if superseded_retests:
        info["superseded_retest_obs"] = [dict(o) for o in superseded_retests]
        if info["retest_status"] == RETEST_NONE:
            info["retest_status"] = RETEST_SUPERSEDED

    if reviews:
        latest_review = max(reviews, key=lambda r: time_key(r["recorded_at"]))
        info["latest_review_at"] = latest_review["recorded_at"]
        if info["latest_obs_at"] is not None and \
                time_key(latest_review["recorded_at"]) >= time_key(info["latest_obs_at"]):
            info["review_status"] = REVIEW_OK
        else:
            info["review_status"] = REVIEW_STALE
    return info


def _pairwise_issues(obs):
    """返回不一致原因列表（逐条说明哪两条观测、因何不可比）。"""
    issues = []
    for i in range(len(obs)):
        for j in range(i + 1, len(obs)):
            a, b = obs[i], obs[j]
            if abs(a["measure_distance_m"] - b["measure_distance_m"]) > DISTANCE_TOL_M:
                issues.append(
                    f"观测#{a['id']}测温距离{a['measure_distance_m']}m 与 "
                    f"观测#{b['id']}{b['measure_distance_m']}m 相差超过{DISTANCE_TOL_M:g}m"
                )
            if a["calib_version"] != b["calib_version"]:
                issues.append(
                    f"观测#{a['id']}校准版本{a['calib_version']} 与 "
                    f"观测#{b['id']}{b['calib_version']} 不一致"
                )
            ov = load_overlap(a["load_range"], b["load_range"])
            if ov is False:
                issues.append(
                    f"观测#{a['id']}负荷区间{a['load_range']} 与 "
                    f"观测#{b['id']}{b['load_range']} 无交集"
                )
            elif ov is None and a["load_range"] != b["load_range"]:
                issues.append(
                    f"观测#{a['id']}负荷区间“{a['load_range']}” 与 "
                    f"观测#{b['id']}“{b['load_range']}” 无法对齐"
                )
    return issues


def _reading_desc(o):
    tag = "复测" if o["is_retest"] else "初测"
    return (
        f"观测#{o['id']}（{tag}）温差{o['temp_diff_k']}K，负荷{o['load_range']}，"
        f"测温距离{o['measure_distance_m']}m，校准版本{o['calib_version']}"
    )


def _retest_prefix(cov):
    if cov["retest_status"] == RETEST_STALE:
        return "补充覆盖最新证据（观测" + "、".join(
            f"#{i}" for i in cov["latest_obs_ids"]) + "）的复测："
    if cov["retest_status"] == RETEST_SUPERSEDED:
        return "原复测已被更正，重新安排覆盖最新证据的复测："
    return ""


def _review_prefix(cov):
    if cov["review_status"] == REVIEW_STALE:
        return "补充覆盖最新证据（观测" + "、".join(
            f"#{i}" for i in cov["latest_obs_ids"]) + "）的明确人工复核意见："
    return ""


def _build_dispositions(state, risk, cov):
    d = []
    if state == "contradictory":
        d += [
            {"code": "CHECK_EQUIP", "action": "核对测温设备校准状态，统一校准版本后在相同负荷区间、相同测温距离下重新复测"},
            {"code": "RETEST", "action": "安排第二次复测，至少取得两条同条件可比较读数"},
        ]
    if risk == "high":
        d.append({"code": "STANDBY", "action": "立即纳入重点盯守，缩短测温周期，必要时转移/限制负荷"})
        if cov["retest_status"] != RETEST_OK:
            d.append({"code": "RETEST",
                      "action": _retest_prefix(cov) +
                      "48小时内完成当前复测，复测须在同负荷区间与同测温距离下进行，且复测时间不得早于最新有效观测"})
        if cov["review_status"] != REVIEW_OK:
            d.append({"code": "MANUAL_REVIEW",
                      "action": _review_prefix(cov) +
                      "人工复核并签署明确意见（高风险接头封存前必须具备覆盖最新证据的当前复测或明确复核意见）"})
    elif risk == "medium":
        d.append({"code": "RETEST", "action": "一周内复测确认温升趋势"})
        if cov["review_status"] != REVIEW_OK:
            d.append({"code": "MANUAL_REVIEW", "action": "安排人工复核确认"})
    else:
        d.append({"code": "ROUTINE", "action": "纳入下一批次常规巡检"})

    if cov["retest_status"] == RETEST_OK:
        d.append({"code": "TREND", "action": "将复测读数与初测并列纳入温升趋势跟踪"})
    if cov["review_status"] == REVIEW_OK:
        d.append({"code": "CLOSEOUT", "action": "按人工复核意见执行并归档闭环"})

    # 去重（同 code 合并为一条，保留首次出现的表述）
    seen, out = set(), []
    for item in d:
        if item["code"] not in seen:
            seen.add(item["code"])
            out.append(item)
    return out


def _coverage_note(cov):
    """结论原因中关于复测/复核覆盖状态的人类可读说明。"""
    notes = []
    if cov["retest_status"] == RETEST_OK:
        notes.append(f"已有覆盖最新证据的当前复测读数（记录时间 {cov['latest_retest_at']}，不早于最新观测 "
                     f"{cov['latest_obs_at']}）")
    elif cov["retest_status"] == RETEST_STALE:
        ids = "、".join(f"#{o['id']}" for o in cov["stale_retest_obs"])
        notes.append(f"复测读数（观测{ids}，记录时间 {cov['latest_retest_at']}）早于最新有效观测"
                     f"（{'、'.join('#' + str(i) for i in cov['latest_obs_ids'])}，"
                     f"{cov['latest_obs_at']}），未覆盖最新证据，不能作为当前复测")
    elif cov["retest_status"] == RETEST_SUPERSEDED:
        ids = "、".join(f"#{o['id']}" for o in cov["superseded_retest_obs"])
        notes.append(f"原复测读数（观测{ids}）已被更正，不属于当前证据，须重新复测")
    if cov["review_status"] == REVIEW_OK:
        notes.append(f"已有人工复核意见覆盖最新证据（记录时间 {cov['latest_review_at']}）")
    elif cov["review_status"] == REVIEW_STALE:
        notes.append(f"最近人工复核意见（记录时间 {cov['latest_review_at']}）早于最新有效观测"
                     f"（{cov['latest_obs_at']}），未覆盖最新证据，须补充明确复核意见")
    return "；".join(notes)


def evaluate(active_obs, reviews, all_obs=None):
    """对同一接头同一批次的当前有效观测 + 复核记录生成结论 dict（不含 joint/batch id）。

    active_obs: 当前有效（未被更正）观测；reviews: 全部人工复核意见；
    all_obs: 含已更正记录的全部观测（用于识别“复测已被更正”）。
    """
    temps = [o["temp_diff_k"] for o in active_obs]
    max_diff = max(temps)
    spread = max(temps) - min(temps)
    cov = coverage_info(active_obs, all_obs if all_obs is not None else active_obs, reviews)

    if len(active_obs) <= 1:
        state = "insufficient"
        only_id = f"（{_reading_desc(active_obs[0])}）" if active_obs else "（当前无有效读数）"
        reason = "证据不足：同一接头、同一批次下仅有一条有效读数" + only_id + "，缺少可对齐的同条件观测。"
    else:
        issues = _pairwise_issues(active_obs)
        if issues:
            state = "insufficient"
            reason = "证据不足：读数无法对齐——" + "；".join(issues) + "。"
        elif spread > CONTRADICTION_SPREAD_K:
            state = "contradictory"
            ids = "、".join(f"#{o['id']}" for o in active_obs)
            reason = (
                f"相互矛盾：观测{ids}的测温距离、负荷区间与校准版本均可对齐，"
                f"但温差极差达{spread:.1f}K（>{CONTRADICTION_SPREAD_K:g}K），"
                f"读数{min(temps):g}K～{max(temps):g}K，不能相互印证。"
            )
        else:
            state = "comparable"
            ids = "、".join(f"#{o['id']}" for o in active_obs)
            reason = (
                f"可比较：观测{ids}测温距离、负荷区间、校准版本一致或相容，"
                f"温差极差{spread:.1f}K（≤{CONTRADICTION_SPREAD_K:g}K），读数相互印证，"
                f"最大温差{max_diff:g}K。"
            )

    if state == "contradictory":
        risk, confidence = "high", "high"
    elif state == "insufficient":
        risk = "high" if max_diff >= HIGH_TEMP_K and len(active_obs) > 0 else (
            "medium" if max_diff >= MED_TEMP_K and len(active_obs) > 0 else "low")
        confidence = "low"
    else:
        if max_diff >= HIGH_TEMP_K:
            risk, confidence = "high", "high"
        elif max_diff >= MED_TEMP_K:
            risk, confidence = "medium", "medium"
        else:
            risk, confidence = "low", "high"

    dispositions = _build_dispositions(state, risk, cov)
    reason += f" 综合判定风险等级：{ {'high':'高','medium':'中','low':'低'}[risk] }。"
    note = _coverage_note(cov)
    if note:
        reason += " " + note + "。"

    return {
        "state": state,
        "risk_level": risk,
        "confidence": confidence,
        "max_temp_diff_k": max_diff if active_obs else None,
        "spread_k": round(spread, 2),
        "reason": reason,
        "dispositions_json": json.dumps(dispositions, ensure_ascii=False),
        "coverage": cov,
    }
