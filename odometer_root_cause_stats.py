#!/usr/bin/env python3
"""批量统计 Odometer 失败相关日志，并给出根因优先级判断。

输入：
- 主日志目录（通常是 log/*.log）
- warning 日志目录
- error 日志目录

输出：
- TSV 明细（每个主日志一行）
- 终端汇总（总量、Top fail 日志、原因分布）
"""

import argparse
import bisect
import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TS_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]")
IMU_RE = re.compile(r"\[DC\]\[d\] \[IMU\]\[")
ODOM_RE = re.compile(r"\[OC\]\[d\] \[Odometer\]\[")
FAIL_RE = re.compile(r"\[OC\]\[d\] \[odo_update_fail\]\[")
ALARM_RE = re.compile(r"\[Alarm\]\[(Warning|Error)\|([^|\]]+)\|([^\]]+)\]", re.IGNORECASE)

EVENT_PATTERNS = {
    "ethercat_timeout": re.compile(r"EtherCAT Motor timeout", re.IGNORECASE),
    "encoder_timeout": re.compile(r"out encoder timeout", re.IGNORECASE),
    "dio_disconnect": re.compile(r"can not connect to DIO board", re.IGNORECASE),
    "no_odom": re.compile(r"no odom", re.IGNORECASE),
    "transform_fail": re.compile(r"Transform fail", re.IGNORECASE),
    "robot_out_of_path": re.compile(r"robot out of path", re.IGNORECASE),
    "pgv_cannot_find_code": re.compile(r"PGV cannot find code", re.IGNORECASE),
}


@dataclass
class LogStats:
    log_file: Path
    start_ts: Optional[dt.datetime]
    end_ts: Optional[dt.datetime]
    imu_total: int
    odometer_total: int
    odo_update_fail_total: int
    events: Dict[str, int]
    coincident_warning_total: int
    coincident_error_total: int
    coincident_specified_total: int
    coincident_other_total: int
    coincident_top1: str
    coincident_counter: Dict[str, int]
    lead_encoder_ratio: float
    lead_ethercat_ratio: float
    root_cause_priority: str
    cause: str


def parse_ts_from_line(line: str) -> Optional[dt.datetime]:
    m = TS_RE.search(line)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%y%m%d %H%M%S.%f")


def parse_main_log(
    log_file: Path,
) -> Tuple[
    Optional[dt.datetime],
    Optional[dt.datetime],
    int,
    int,
    int,
    List[dt.datetime],
    List[Tuple[dt.datetime, str, str, str]],
    Dict[str, List[dt.datetime]],
]:
    start_ts: Optional[dt.datetime] = None
    end_ts: Optional[dt.datetime] = None
    imu_total = 0
    odometer_total = 0
    fail_total = 0
    fail_times: List[dt.datetime] = []
    alarms: List[Tuple[dt.datetime, str, str, str]] = []
    local_event_times: Dict[str, List[dt.datetime]] = {k: [] for k in EVENT_PATTERNS.keys()}

    with log_file.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            ts = parse_ts_from_line(line)
            if ts is not None:
                if start_ts is None:
                    start_ts = ts
                end_ts = ts
            if IMU_RE.search(line):
                imu_total += 1
            if ODOM_RE.search(line):
                odometer_total += 1
            if FAIL_RE.search(line):
                fail_total += 1
                if ts is not None:
                    fail_times.append(ts)

            m_alarm = ALARM_RE.search(line)
            if m_alarm and ts is not None:
                level = m_alarm.group(1).capitalize()
                code = m_alarm.group(2).strip()
                msg = m_alarm.group(3).strip()
                alarms.append((ts, level, code, msg))

            if ts is not None:
                for event_name, pattern in EVENT_PATTERNS.items():
                    if pattern.search(line):
                        local_event_times[event_name].append(ts)

    return (
        start_ts,
        end_ts,
        imu_total,
        odometer_total,
        fail_total,
        fail_times,
        alarms,
        local_event_times,
    )


def parse_event_logs(event_files: List[Path]) -> List[Tuple[dt.datetime, str]]:
    events: List[Tuple[dt.datetime, str]] = []
    for file in event_files:
        with file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ts = parse_ts_from_line(line)
                if ts is None:
                    continue
                for event_name, pattern in EVENT_PATTERNS.items():
                    if pattern.search(line):
                        events.append((ts, event_name))
                        break
    events.sort(key=lambda x: x[0])
    return events


def summarize_global_events(events: List[Tuple[dt.datetime, str]]) -> Dict[str, int]:
    c = Counter()
    for _, name in events:
        c[name] += 1
    return {k: c.get(k, 0) for k in EVENT_PATTERNS.keys()}


def nearest_fail_delta_ms(ts: dt.datetime, fail_times: List[dt.datetime]) -> Optional[float]:
    if not fail_times:
        return None
    best = None
    for fts in fail_times:
        d = abs((ts - fts).total_seconds() * 1000.0)
        if best is None or d < best:
            best = d
    return best


def count_coincident_alarms(
    fail_times: List[dt.datetime],
    alarms: List[Tuple[dt.datetime, str, str, str]],
    window_sec: float,
) -> Tuple[int, int, Dict[str, int]]:
    """统计与 odo_update_fail 同窗(<=window_sec)的 Alarm 事件。"""
    c = Counter()
    warn = 0
    err = 0
    max_ms = window_sec * 1000.0
    for ts, level, code, msg in alarms:
        d_ms = nearest_fail_delta_ms(ts, fail_times)
        if d_ms is None or d_ms > max_ms:
            continue
        key = f"{level}|{code}|{msg}"
        c[key] += 1
        if level == "Warning":
            warn += 1
        elif level == "Error":
            err += 1
    return warn, err, dict(c)


def calc_lead_ratio(event_times: List[dt.datetime], fail_times: List[dt.datetime], window_sec: float) -> float:
    """事件在 fail 前 window_sec 内出现的覆盖率。"""
    if not fail_times or not event_times:
        return 0.0
    ev = sorted(t.timestamp() for t in event_times)
    hit = 0
    for f in fail_times:
        ft = f.timestamp()
        idx = bisect.bisect_left(ev, ft)
        prev = ev[idx - 1] if idx > 0 else None
        if prev is not None and 0.0 <= ft - prev <= window_sec:
            hit += 1
    return hit / len(fail_times)


def judge_root_cause_priority(lead_encoder_ratio: float, lead_ethercat_ratio: float, fail_total: int) -> str:
    if fail_total == 0:
        return "无odo_update_fail"
    if lead_encoder_ratio < 0.05 and lead_ethercat_ratio < 0.05:
        return "证据弱(需补充驱动侧日志)"
    if lead_encoder_ratio >= 0.3 and lead_ethercat_ratio >= 0.3:
        return "底盘链路整体优先(EtherCAT+编码器)"
    if lead_encoder_ratio - lead_ethercat_ratio >= 0.15:
        return "编码器链路优先"
    if lead_ethercat_ratio - lead_encoder_ratio >= 0.15:
        return "EtherCAT链路优先"
    if max(lead_encoder_ratio, lead_ethercat_ratio) >= 0.2:
        return "链路问题(方向待补证)"
    return "证据弱(需补充驱动侧日志)"


def count_events_in_range(events: List[Tuple[dt.datetime, str]], start: Optional[dt.datetime], end: Optional[dt.datetime]) -> Dict[str, int]:
    c = Counter()
    if start is None or end is None:
        return {k: 0 for k in EVENT_PATTERNS.keys()}
    for ts, name in events:
        if start <= ts <= end:
            c[name] += 1
    return {k: c.get(k, 0) for k in EVENT_PATTERNS.keys()}


def classify_cause(imu_total: int, odometer_total: int, fail_total: int, events: Dict[str, int]) -> str:
    ether = events["ethercat_timeout"]
    enc = events["encoder_timeout"]
    dio = events["dio_disconnect"]
    no_odom = events["no_odom"]
    tf = events["transform_fail"]

    if imu_total == 0 and odometer_total == 0 and fail_total == 0:
        return "非运动主日志/无数据"
    if fail_total == 0:
        return "未见 odo_update_fail"

    if ether > 0 and enc > 0:
        return "底盘链路整体不稳( EtherCAT+编码器 )"
    if ether > 0:
        return "EtherCAT链路优先"
    if enc > 0:
        return "编码器链路优先"
    if dio > 0 and (no_odom > 0 or tf > 0):
        return "底盘链路/DIO优先"
    if no_odom > 0 or tf > 0:
        return "上层时序/队列匹配失败(伴随无odom)"
    if odometer_total == 0 and imu_total > 0:
        return "上游Odometer断流"
    if odometer_total < max(10, imu_total // 100):
        return "Odometer极稀疏(上游更新异常)"
    return "证据不足(需更多驱动侧日志)"


def write_tsv(out_file: Path, rows: List[LogStats]) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as f:
        f.write(
            "log_file\tstart_ts\tend_ts\timu_total\todometer_total\todo_update_fail_total"
            "\tethercat_timeout\tencoder_timeout\tdio_disconnect\tno_odom\ttransform_fail"
            "\tcoincident_warning_total\tcoincident_error_total\tcoincident_specified_total"
            "\tcoincident_other_total\tcoincident_top1"
            "\tlead_encoder_ratio\tlead_ethercat_ratio\troot_cause_priority"
            "\tfail_per_imu\tcause\n"
        )
        for r in rows:
            fail_per_imu = (r.odo_update_fail_total / r.imu_total) if r.imu_total > 0 else 0.0
            f.write(
                "\t".join(
                    [
                        str(r.log_file),
                        r.start_ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if r.start_ts else "",
                        r.end_ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if r.end_ts else "",
                        str(r.imu_total),
                        str(r.odometer_total),
                        str(r.odo_update_fail_total),
                        str(r.events["ethercat_timeout"]),
                        str(r.events["encoder_timeout"]),
                        str(r.events["dio_disconnect"]),
                        str(r.events["no_odom"]),
                        str(r.events["transform_fail"]),
                        str(r.coincident_warning_total),
                        str(r.coincident_error_total),
                        str(r.coincident_specified_total),
                        str(r.coincident_other_total),
                        r.coincident_top1.replace("\t", " ").replace("\n", " "),
                        f"{r.lead_encoder_ratio:.6f}",
                        f"{r.lead_ethercat_ratio:.6f}",
                        r.root_cause_priority,
                        f"{fail_per_imu:.6f}",
                        r.cause,
                    ]
                )
                + "\n"
            )


def health_level(r: LogStats) -> str:
    """给单个日志打一个可读的健康等级。"""
    if r.imu_total == 0 and r.odometer_total == 0 and r.odo_update_fail_total == 0:
        return "N/A(非运动日志)"
    if r.odo_update_fail_total == 0:
        return "正常"

    fail_per_imu = (r.odo_update_fail_total / r.imu_total) if r.imu_total > 0 else 999.0
    odom_ratio = (r.odometer_total / r.imu_total) if r.imu_total > 0 else 0.0

    if fail_per_imu > 2.0 and odom_ratio < 0.01:
        return "严重"
    if fail_per_imu > 1.0:
        return "中等"
    return "轻微"


def print_human_report(
    rows: List[LogStats],
    args: argparse.Namespace,
    global_events: Dict[str, int],
) -> None:
    print("=== 批量统计完成 ===")
    print(f"主日志数量: {len(rows)}")
    print(f"warning日志数量: {len(list(Path(args.warning_dir).glob('*.log')))}")
    print(f"error日志数量: {len(list(Path(args.error_dir).glob('*.log')))}")
    print(f"输出文件: {args.out}")

    print("\n=== 这份报告在看什么 ===")
    print("1) IMU 是否在持续上报")
    print("2) Odometer 是否能持续产出")
    print("3) odo_update_fail 是否高频")
    print("4) 同时间窗是否出现 EtherCAT/编码器/DIO/no odom/Transform fail")

    cause_counter = Counter(r.cause for r in rows)
    print("\n=== 原因分布(按日志份数) ===")
    for cause, n in cause_counter.most_common():
        print(f"- {cause}: {n}")

    level_counter = Counter(health_level(r) for r in rows)
    print("\n=== 健康度分级 ===")
    for lv in ("严重", "中等", "轻微", "正常", "N/A(非运动日志)"):
        if lv in level_counter:
            print(f"- {lv}: {level_counter[lv]}")

    # 统计“同时间窗事件计数”和“全局事件计数”
    overlap_counter = Counter()
    for r in rows:
        for k, v in r.events.items():
            overlap_counter[k] += v

    print("\n=== 同时间窗事件统计(与每个主日志时间范围重叠) ===")
    print(f"- EtherCAT timeout : {overlap_counter.get('ethercat_timeout', 0)}")
    print(f"- 编码器 timeout    : {overlap_counter.get('encoder_timeout', 0)}")
    print(f"- DIO 断连          : {overlap_counter.get('dio_disconnect', 0)}")
    print(f"- no odom          : {overlap_counter.get('no_odom', 0)}")
    print(f"- Transform fail   : {overlap_counter.get('transform_fail', 0)}")
    print(f"- robot out of path: {overlap_counter.get('robot_out_of_path', 0)}")
    print(f"- PGV cannot find  : {overlap_counter.get('pgv_cannot_find_code', 0)}")

    coincident_warning_sum = sum(r.coincident_warning_total for r in rows)
    coincident_error_sum = sum(r.coincident_error_total for r in rows)
    coincident_specified_sum = sum(r.coincident_specified_total for r in rows)
    coincident_other_sum = sum(r.coincident_other_total for r in rows)
    coincident_alarm_counter = Counter()
    for r in rows:
        for k, v in r.coincident_counter.items():
            coincident_alarm_counter[k] += v

    print("\n=== 与 odo_update_fail 同窗 Alarm 统计(全量提取) ===")
    print(f"- 同窗 Warning 总数: {coincident_warning_sum}")
    print(f"- 同窗 Error 总数  : {coincident_error_sum}")
    print(f"- 同窗总告警数     : {coincident_warning_sum + coincident_error_sum}")
    print(f"- 同窗指定7类事件数: {coincident_specified_sum}")
    print(f"- 同窗其他事件数   : {coincident_other_sum}")
    top_alarm = coincident_alarm_counter.most_common(20)
    if top_alarm:
        print("- 同窗 Top20 Alarm:")
        for idx, (k, cnt) in enumerate(top_alarm, start=1):
            print(f"  {idx}. {k} -> {cnt}")
    else:
        print("- 同窗 Top20 Alarm: 无")

    print("\n=== 全局事件统计(不看时间重叠) ===")
    print(f"- EtherCAT timeout : {global_events.get('ethercat_timeout', 0)}")
    print(f"- 编码器 timeout    : {global_events.get('encoder_timeout', 0)}")
    print(f"- DIO 断连          : {global_events.get('dio_disconnect', 0)}")
    print(f"- no odom          : {global_events.get('no_odom', 0)}")
    print(f"- Transform fail   : {global_events.get('transform_fail', 0)}")
    print(f"- robot out of path: {global_events.get('robot_out_of_path', 0)}")
    print(f"- PGV cannot find  : {global_events.get('pgv_cannot_find_code', 0)}")

    print("\n=== Top10 (按 odo_update_fail_total) ===")
    print("字段说明: IMU帧数、odo_update_fail次数、Odometer帧数、同窗告警拆解")
    for idx, r in enumerate(sorted(rows, key=lambda x: x.odo_update_fail_total, reverse=True)[:10], start=1):
        fail_per_imu = (r.odo_update_fail_total / r.imu_total) if r.imu_total > 0 else 0.0
        odom_ratio = (r.odometer_total / r.imu_total * 100.0) if r.imu_total > 0 else 0.0
        print(
            f"{idx}. {r.log_file.name} | 等级={health_level(r)} | "
            f"IMU帧数={r.imu_total}, odo_update_fail次数={r.odo_update_fail_total}, "
            f"Odometer帧数={r.odometer_total}, "
            f"失败比(odo_update_fail/IMU)={fail_per_imu:.3f}, Odometer占比={odom_ratio:.2f}% | "
            f"指定事件计数(以太网总线超时/编码器超时/DIO断连/no odom/Transform fail/"
            f"路径偏离/PGV找不到码)="
            f"{r.events['ethercat_timeout']}/{r.events['encoder_timeout']}/"
            f"{r.events['dio_disconnect']}/{r.events['no_odom']}/{r.events['transform_fail']}/"
            f"{r.events['robot_out_of_path']}/{r.events['pgv_cannot_find_code']} | "
            f"同窗告警(Warning/Error)={r.coincident_warning_total}/{r.coincident_error_total}, "
            f"同窗告警总数={r.coincident_warning_total + r.coincident_error_total}, "
            f"同窗指定7类事件数={r.coincident_specified_total}, 同窗其他事件数={r.coincident_other_total} | "
            f"先后顺序覆盖(编码器先于fail)={r.lead_encoder_ratio:.2%}, "
            f"先后顺序覆盖(EtherCAT先于fail)={r.lead_ethercat_ratio:.2%}, "
            f"主因优先级={r.root_cause_priority} | "
            f"原因={r.cause}"
        )

    print("\n=== 一句话结论 ===")
    severe = level_counter.get("严重", 0)
    medium = level_counter.get("中等", 0)
    if severe > 0:
        print(f"- 发现 {severe} 份严重异常日志，表现为 Odometer 极稀疏且 fail 高频。")
    elif medium > 0:
        print(f"- 发现 {medium} 份中等异常日志，里程计更新不稳定。")
    else:
        print("- 未见明显异常高发窗口。")

    print("\n=== 事件推断 ===")
    overlap_sum = sum(overlap_counter.values())
    if overlap_sum == 0 and sum(global_events.values()) > 0:
        print("- 同时间窗事件计数为0，但全局warning/error有故障事件。")
        print("- 这通常表示主日志与warning/error日志时间不重叠，当前无法做“同窗因果”推断。")
    elif overlap_counter.get("ethercat_timeout", 0) > 0 and overlap_counter.get("encoder_timeout", 0) > 0:
        print("- 同窗出现 EtherCAT 与 编码器 timeout，优先判断底盘链路整体不稳。")
    elif overlap_counter.get("ethercat_timeout", 0) > 0:
        print("- 同窗 EtherCAT timeout 主导，优先检查 EtherCAT 主站周期/总线。")
    elif overlap_counter.get("encoder_timeout", 0) > 0:
        print("- 同窗编码器 timeout 主导，优先检查编码器反馈链路。")
    elif overlap_counter.get("no_odom", 0) > 0 or overlap_counter.get("transform_fail", 0) > 0:
        print("- 同窗 no odom/Transform fail 主导，优先检查上层时序与队列匹配。")
    elif overlap_counter.get("robot_out_of_path", 0) > 0 or overlap_counter.get(
        "pgv_cannot_find_code", 0
    ) > 0:
        print("- 同窗出现路径偏离/PGV识别异常，提示导航感知链路有业务级异常。")
        print("- 这类事件会放大定位偏移，但不是 odo_update_fail 的直接底层根因。")
    else:
        print("- 未捕捉到可用于直接归因的同窗事件，建议补充驱动侧详细日志。")


def main() -> None:
    parser = argparse.ArgumentParser(description="全量统计 Odometer 失败根因")
    parser.add_argument("--log-dir", required=True, help="主日志目录")
    parser.add_argument(
        "--log-glob",
        default="*.log",
        help="主日志匹配模式，默认 *.log；例如 robokit_2026-02-11_*.log",
    )
    parser.add_argument("--warning-dir", required=True, help="warning 日志目录")
    parser.add_argument("--error-dir", required=True, help="error 日志目录")
    parser.add_argument(
        "--co-window-sec",
        type=float,
        default=1.0,
        help="与 odo_update_fail 的同窗阈值(秒)，默认1.0",
    )
    parser.add_argument(
        "--lead-window-sec",
        type=float,
        default=1.0,
        help="先后顺序窗口：事件需在 fail 前该秒数内，默认1.0",
    )
    parser.add_argument("--out", required=True, help="输出 TSV 路径")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    warning_dir = Path(args.warning_dir)
    error_dir = Path(args.error_dir)

    main_logs = sorted(log_dir.glob(args.log_glob))
    warning_logs = sorted(warning_dir.glob("*.log"))
    error_logs = sorted(error_dir.glob("*.log"))

    # 事件来源包含:
    # 1) warning/*.log
    # 2) error/*.log
    # 3) robokit 主日志内部的 [R][w]/[R][e] 业务告警（如 robot out of path / PGV cannot find code）
    event_files = warning_logs + error_logs + main_logs
    events = parse_event_logs(event_files)
    global_events = summarize_global_events(events)

    rows: List[LogStats] = []
    for log_file in main_logs:
        (
            start_ts,
            end_ts,
            imu_total,
            odometer_total,
            fail_total,
            fail_times,
            alarms,
            local_event_times,
        ) = parse_main_log(log_file)
        event_counts = count_events_in_range(events, start_ts, end_ts)
        c_warn, c_err, c_counter = count_coincident_alarms(fail_times, alarms, args.co_window_sec)
        c_top1 = Counter(c_counter).most_common(1)[0][0] if c_counter else ""
        c_specified = (
            event_counts["ethercat_timeout"]
            + event_counts["encoder_timeout"]
            + event_counts["dio_disconnect"]
            + event_counts["no_odom"]
            + event_counts["transform_fail"]
            + event_counts["robot_out_of_path"]
            + event_counts["pgv_cannot_find_code"]
        )
        c_total = c_warn + c_err
        c_other = c_total - c_specified if c_total >= c_specified else 0
        lead_encoder_ratio = calc_lead_ratio(
            local_event_times.get("encoder_timeout", []), fail_times, args.lead_window_sec
        )
        lead_ethercat_ratio = calc_lead_ratio(
            local_event_times.get("ethercat_timeout", []), fail_times, args.lead_window_sec
        )
        root_cause_priority = judge_root_cause_priority(
            lead_encoder_ratio, lead_ethercat_ratio, fail_total
        )
        cause = classify_cause(imu_total, odometer_total, fail_total, event_counts)
        rows.append(
            LogStats(
                log_file=log_file,
                start_ts=start_ts,
                end_ts=end_ts,
                imu_total=imu_total,
                odometer_total=odometer_total,
                odo_update_fail_total=fail_total,
                events=event_counts,
                coincident_warning_total=c_warn,
                coincident_error_total=c_err,
                coincident_specified_total=c_specified,
                coincident_other_total=c_other,
                coincident_top1=c_top1,
                coincident_counter=c_counter,
                lead_encoder_ratio=lead_encoder_ratio,
                lead_ethercat_ratio=lead_ethercat_ratio,
                root_cause_priority=root_cause_priority,
                cause=cause,
            )
        )

    write_tsv(Path(args.out), rows)
    print_human_report(rows, args, global_events)


if __name__ == "__main__":
    main()
