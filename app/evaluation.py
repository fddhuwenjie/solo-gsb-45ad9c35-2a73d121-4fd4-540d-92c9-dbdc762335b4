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
"""
import json

HIGH_TEMP_K = 8.0
MED_TEMP_K = 4.0
CONTRADICTION_SPREAD_K = 3.0
DISTANCE_TOL_M = 1.0


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


def evidence_coverage(active_obs, reviews):
    """按 recorded_at 与更正链判断复测/人工复核是否覆盖“最新有效证据”。

    最新证据时间 = 当前有效（未被更正、superseded=0）观测 recorded_at 的最大值。
    - 复测覆盖：存在当前有效的复测读数，且其 recorded_at 不早于最新证据时间；
    - 意见覆盖：存在人工复核意见，且其 recorded_at 不早于最新证据时间。

    注意：判定只看观测自身的 recorded_at，与入库（提交）时刻无关——较早时间的
    回填记录即使提交更晚，也不会令复测过期；被更正的记录已被排除在 active_obs 外。
    """
    obs_times = [o["recorded_at"] for o in active_obs if o["recorded_at"]]
    latest_evidence_at = max(obs_times) if obs_times else None
    retest_times = [
        o["recorded_at"] for o in active_obs
        if o["is_retest"] and o["recorded_at"]
    ]
    review_times = [r["recorded_at"] for r in reviews if r["recorded_at"]]
    latest_retest_at = max(retest_times) if retest_times else None
    latest_review_at = max(review_times) if review_times else None
    return {
        "latest_evidence_at": latest_evidence_at,
        "latest_retest_at": latest_retest_at,
        "latest_review_at": latest_review_at,
        "retest_covers": (
            latest_retest_at is not None
            and (latest_evidence_at is None or latest_retest_at >= latest_evidence_at)
        ),
        "review_covers": (
            latest_review_at is not None
            and (latest_evidence_at is None or latest_review_at >= latest_evidence_at)
        ),
    }


def _build_dispositions(state, risk, max_diff, has_retest, has_manual, spread, coverage=None):
    cov = coverage or {}
    retest_covers = cov.get("retest_covers", False)
    review_covers = cov.get("review_covers", False)
    d = []
    if state == "contradictory":
        d += [
            {"code": "CHECK_EQUIP", "action": "核对测温设备校准状态，统一校准版本后在相同负荷区间、相同测温距离下重新复测"},
            {"code": "RETEST", "action": "安排第二次复测，至少取得两条同条件可比较读数，复测时间不得早于最新有效观测"},
            {"code": "MANUAL_REVIEW", "action": "人工现场复核接头并出具明确意见，意见时间不得早于最新有效观测"},
        ]
    if risk == "high":
        d.append({"code": "STANDBY", "action": "立即纳入重点盯守，缩短测温周期，必要时转移/限制负荷"})
        if not retest_covers and state != "contradictory":
            if cov.get("latest_retest_at"):
                d.append({"code": "RETEST", "action":
                    f"既有复测（{cov['latest_retest_at']}）早于最新有效观测"
                    f"（{cov['latest_evidence_at']}），未覆盖补录后的新证据：48小时内重新复测，"
                    "复测须在同负荷区间与同测温距离下进行，且复测时间不得早于最新有效观测"})
            else:
                d.append({"code": "RETEST", "action":
                    "48小时内完成当前复测，复测须在同负荷区间与同测温距离下进行，"
                    "且复测时间不得早于最新有效观测"})
        if not review_covers:
            if cov.get("latest_review_at"):
                d.append({"code": "MANUAL_REVIEW", "action":
                    f"既有人工复核意见（{cov['latest_review_at']}）早于最新有效观测"
                    f"（{cov['latest_evidence_at']}），未覆盖补录后的新证据：重新现场复核并签署"
                    "明确意见，意见时间不得早于最新有效观测（高风险接头封存前必须具备覆盖最新证据的复测或明确意见）"})
            else:
                d.append({"code": "MANUAL_REVIEW", "action":
                    "人工复核并签署明确意见，意见时间不得早于最新有效观测"
                    "（高风险接头封存前必须具备覆盖最新证据的当前复测或明确复核意见）"})
    elif risk == "medium":
        d.append({"code": "RETEST", "action": "一周内复测确认温升趋势"})
        if not has_manual:
            d.append({"code": "MANUAL_REVIEW", "action": "安排人工复核确认"})
    else:
        d.append({"code": "ROUTINE", "action": "纳入下一批次常规巡检"})

    if has_retest and state != "contradictory":
        d.append({"code": "TREND", "action": "将复测读数与初测并列纳入温升趋势跟踪"})
    if review_covers:
        d.append({"code": "CLOSEOUT", "action": "按人工复核意见执行并归档闭环"})

    # 去重（同 code 合并为一条）
    seen, out = set(), []
    for item in d:
        if item["code"] not in seen:
            seen.add(item["code"])
            out.append(item)
    return out


def evaluate(active_obs, reviews):
    """对同一接头同一批次的当前有效观测 + 复核记录生成结论 dict（不含 joint/batch id）。"""
    temps = [o["temp_diff_k"] for o in active_obs]
    max_diff = max(temps) if temps else 0.0
    spread = (max(temps) - min(temps)) if temps else 0.0
    has_retest = any(o["is_retest"] for o in active_obs)
    has_manual = len(reviews) > 0
    coverage = evidence_coverage(active_obs, reviews)

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

    dispositions = _build_dispositions(state, risk, max_diff, has_retest, has_manual, spread, coverage)
    reason += f" 综合判定风险等级：{ {'high':'高','medium':'中','low':'低'}[risk] }。"
    if has_retest:
        reason += f" 已有复测读数（{sum(o['is_retest'] for o in active_obs)}条）。"
        if not coverage["retest_covers"] and coverage["latest_evidence_at"]:
            reason += (
                f" 但最新复测（{coverage['latest_retest_at']}）早于最新有效观测"
                f"（{coverage['latest_evidence_at']}），该复测未覆盖补录后的新证据，"
                "不能再作为当前复测依据。"
            )
    if has_manual:
        reason += " 已有人工复核意见。"
        if not coverage["review_covers"] and coverage["latest_evidence_at"]:
            reason += (
                f" 但最新复核意见（{coverage['latest_review_at']}）早于最新有效观测"
                f"（{coverage['latest_evidence_at']}），该意见未覆盖补录后的新证据。"
            )

    return {
        "state": state,
        "risk_level": risk,
        "confidence": confidence,
        "max_temp_diff_k": max_diff if active_obs else None,
        "spread_k": round(spread, 2),
        "reason": reason,
        "dispositions_json": json.dumps(dispositions, ensure_ascii=False),
        "coverage": coverage,
    }
