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

from robokit_imu_patterns import (
    EVENT_LABELS,
    EVENT_PATTERNS,
    EVENT_PRINT_ORDER,
    ROTATED_LOG_NAME_RE,
    matching_event_names,
)

TS_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]")
IMU_RE = re.compile(r"\[DC\]\[d\] \[IMU\]\[")
ODOM_RE = re.compile(r"\[OC\]\[d\] \[Odometer\]\[")
FAIL_RE = re.compile(r"\[OC\]\[d\] \[odo_update_fail\]\[")
ALARM_RE = re.compile(
    r"\[Alarm\]\[(Warning|Error)\|([^|\]]+)\|([^|\]]+)(?:\|([^\]]+))?\]",
    re.IGNORECASE,
)


@dataclass
class LogStats:
    log_file: Path
    start_ts: Optional[dt.datetime]
    end_ts: Optional[dt.datetime]
    imu_total: int
    odometer_total: int
    odo_update_fail_total: int
    events: Dict[str, int]
    coincident_events: Dict[str, int]
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


@dataclass(frozen=True)
class EventOccurrence:
    ts: dt.datetime
    name: str
    instance_key: str


def parse_ts_from_line(line: str) -> Optional[dt.datetime]:
    """Parse the standard robokit timestamp prefix from one log line.

    根因脚本里所有时间窗、同窗和先后顺序判断都依赖这个时间戳。
    如果当前行没有标准前缀，就直接视为不能参与时序分析。
    """
    m = TS_RE.search(line)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%y%m%d %H%M%S.%f")


def event_instance_key(line: str) -> str:
    """Build a stable fingerprint for one event occurrence across mirrored log files.

    去重的目标是合并“同一运行时事件被多个日志源镜像记录”的情况，
    不是把同毫秒、同家族、但来自不同模块的两条事件压成一条。
    所以这里只去掉时间戳 / pid，保留模块、级别和正文。
    """
    return re.sub(r"^\[\d{6} \d{6}\.\d{3}\]\[\d+\]", "", line).strip()


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
    """Extract counters, fail times, alarms and local event times from one main log.

    这是整份根因分析的基础入口。它把一份主日志拆成四类信息：
    - 总量统计：IMU / Odometer / odo_update_fail
    - fail 时间点
    - Alarm 明细
    - 主日志内部命中的事件时间点
    """
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


def parse_event_logs(event_files: List[Path]) -> List[EventOccurrence]:
    """Parse all event sources into a single time-ordered event stream.

    这里会把 warning/error/main 里命中的指定事件都压平成事件实例流，
    供后续“主日志范围统计”和“与 fail 同窗统计”共用。
    """
    events: List[EventOccurrence] = []
    for file in event_files:
        with file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ts = parse_ts_from_line(line)
                if ts is None:
                    continue
                instance_key = event_instance_key(line)
                for event_name in matching_event_names(line):
                    events.append(EventOccurrence(ts=ts, name=event_name, instance_key=instance_key))
    events.sort(key=lambda x: x.ts)
    return events


def deduplicate_event_instances(events: List[EventOccurrence]) -> List[EventOccurrence]:
    """Collapse mirrored log hits into one event instance keyed by timestamp/name/body fingerprint.

    同一运行时事件可能同时落到 main + warning/error 多个文件里。
    根因分析层更关心“事件实例是否发生过”，不是它被多少个日志源重复记录。
    """
    return sorted(set(events), key=lambda x: x.ts)


def summarize_global_events(events: List[EventOccurrence]) -> Dict[str, int]:
    """Count each event type across the whole input without time filtering.

    这是“全局盘点口径”，用来回答整包里某类事件总共出现了多少次，
    不直接用于判断某一份主日志的局部根因。
    """
    c = Counter()
    for event in events:
        c[event.name] += 1
    return {k: c.get(k, 0) for k in EVENT_PATTERNS.keys()}


def nearest_fail_delta_ms(ts: dt.datetime, fail_times: List[dt.datetime]) -> Optional[float]:
    """Return the nearest absolute distance from one timestamp to any fail in ms.

    Alarm 同窗判断只需要知道“最近离 fail 多近”，不需要保留配对关系。
    用毫秒输出，是因为日志窗口常以 1s 内的细粒度差异为阈值。
    """
    if not fail_times:
        return None
    best = None
    for fts in fail_times:
        d = abs((ts - fts).total_seconds() * 1000.0)
        if best is None or d < best:
            best = d
    return best


def nearest_fail_delta_sec(ts: dt.datetime, fail_times_sec: List[float]) -> Optional[float]:
    """Return the nearest absolute distance from one timestamp to any fail in seconds.

    这个版本专门给已预排序的 fail 秒级数组用，避免在大样本下重复做 datetime
    到 timestamp 的转换，适合批量扫描事件流。
    """
    if not fail_times_sec:
        return None
    target = ts.timestamp()
    idx = bisect.bisect_left(fail_times_sec, target)
    best = None
    if idx < len(fail_times_sec):
        best = abs(fail_times_sec[idx] - target)
    if idx > 0:
        prev = abs(fail_times_sec[idx - 1] - target)
        if best is None or prev < best:
            best = prev
    return best


def match_event_name(text: str) -> Optional[str]:
    """Map one text message to the first known EVENT_PATTERNS key that matches.

    这里主要用在 Alarm 文本上，判断它是否已经落入当前已知事件家族，
    从而区分“已分类 Alarm”和“未分类 Alarm”。
    """
    event_names = matching_event_names(text)
    if event_names:
        return event_names[0]
    return None


def side_logs(log_dir: Path) -> List[Path]:
    """Return warning/error logs, including rotated `*.log.N` files.

    现场日志目录经常会发生轮转。如果这里只读 `*.log`，
    最近一轮之外的 warning/error 会被直接漏掉。
    """
    if not log_dir.exists():
        return []
    return sorted(p for p in log_dir.iterdir() if p.is_file() and ROTATED_LOG_NAME_RE.match(p.name))


def count_side_logs(log_dir: Path) -> int:
    """Count warning/error log files using the same rotated-log rule as parsing."""
    return len(side_logs(log_dir))


def count_coincident_alarms(
    fail_times: List[dt.datetime],
    alarms: List[Tuple[dt.datetime, str, str, str]],
    window_sec: float,
) -> Tuple[int, int, Dict[str, int], int]:
    """Count Alarm hits that fall near odo_update_fail.

    返回值拆成四块：
    - Warning 数
    - Error 数
    - 具体 Alarm 计数器
    - 未落入已知事件模式的 Alarm 数
    这样报告层才能把“同窗 Alarm”和“同窗指定事件”分开说清楚。
    """
    c = Counter()
    warn = 0
    err = 0
    uncategorized = 0
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
        if match_event_name(msg) is None:
            uncategorized += 1
    return warn, err, dict(c), uncategorized


def count_coincident_events(
    events: List[EventOccurrence],
    fail_times: List[dt.datetime],
    window_sec: float,
) -> Dict[str, int]:
    """Count known event-pattern hits that occur near odo_update_fail.

    这是本次修复后新增的关键函数。
    它解决的是：不能再把“整份主日志时间范围事件数”误当成“与 fail 同窗事件数”。
    """
    c = Counter()
    if not fail_times:
        return {k: 0 for k in EVENT_PATTERNS.keys()}

    fail_times_sec = sorted(ts.timestamp() for ts in fail_times)
    for event in events:
        d_sec = nearest_fail_delta_sec(event.ts, fail_times_sec)
        if d_sec is not None and d_sec <= window_sec:
            c[event.name] += 1
    return {k: c.get(k, 0) for k in EVENT_PATTERNS.keys()}


def calc_lead_ratio(event_times: List[dt.datetime], fail_times: List[dt.datetime], window_sec: float) -> float:
    """Measure how often one event appears shortly before fail.

    这个比值用于判断“某类事件是不是经常先于 fail 出现”，
    是 root_cause_priority 里做方向判断的重要参考。
    """
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


def judge_root_cause_priority(
    lead_encoder_ratio: float,
    lead_ethercat_ratio: float,
    fail_total: int,
    events: Dict[str, int],
) -> str:
    """Rank the most likely root-cause direction for one main-log window.

    这里偏向“优先级排序”而不是最终自然语言结论：
    先看更强的驱动/电机链路信号，再看 KINCO，再看 encoder/EtherCAT 的先后顺序。
    """
    if fail_total == 0:
        return "无odo_update_fail"
    if events["motor_timeout"] > 0 or events["odo_data_lost"] > 0 or events["motor_error"] > 0:
        return "底盘驱动/电机反馈链路优先"
    if (
        events["odo_failed_update"] > 0
        or events["odo_not_updated_500ms"] > 0
        or events["reset_prev_frame"] > 0
    ):
        return "Odometer上游更新异常优先"
    if events["kinco_can_err"] > 0:
        return "KINCO驱动链路优先"
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


def count_events_in_range(events: List[EventOccurrence], start: Optional[dt.datetime], end: Optional[dt.datetime]) -> Dict[str, int]:
    """Count known events inside the full time range of one main log.

    这是“主日志范围口径”，不是“与 fail 同窗口径”。
    它适合做窗口背景判断，但不能代替真正的 fail 邻域分析。
    """
    c = Counter()
    if start is None or end is None:
        return {k: 0 for k in EVENT_PATTERNS.keys()}
    for event in events:
        if start <= event.ts <= end:
            c[event.name] += 1
    return {k: c.get(k, 0) for k in EVENT_PATTERNS.keys()}


def merge_time_ranges(
    ranges: List[Tuple[Optional[dt.datetime], Optional[dt.datetime]]]
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Merge overlapping main-log time ranges into disjoint intervals.

    这一步专门解决“多份主日志窗口彼此重叠时，同一事件被按窗口重复累计”的问题。
    """
    normalized = sorted((s, e) for s, e in ranges if s is not None and e is not None)
    if not normalized:
        return []

    merged: List[Tuple[dt.datetime, dt.datetime]] = []
    cur_start, cur_end = normalized[0]
    for start, end in normalized[1:]:
        if start <= cur_end:
            if end > cur_end:
                cur_end = end
            continue
        merged.append((cur_start, cur_end))
        cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


def count_events_in_ranges_union(
    events: List[EventOccurrence],
    ranges: List[Tuple[Optional[dt.datetime], Optional[dt.datetime]]],
) -> Dict[str, int]:
    """Count event instances that fall inside the union of main-log ranges.

    这是“主日志范围并集口径”，用来避免两份主日志时间重叠时，
    同一条事件实例被重复加到汇总里。
    """
    merged = merge_time_ranges(ranges)
    if not merged:
        return {k: 0 for k in EVENT_PATTERNS.keys()}

    c = Counter()
    range_idx = 0
    for event in events:
        while range_idx < len(merged) and event.ts > merged[range_idx][1]:
            range_idx += 1
        if range_idx >= len(merged):
            break
        start, end = merged[range_idx]
        if start <= event.ts <= end:
            c[event.name] += 1
    return {k: c.get(k, 0) for k in EVENT_PATTERNS.keys()}


def classify_cause(imu_total: int, odometer_total: int, fail_total: int, events: Dict[str, int]) -> str:
    """Convert counters into a readable cause label for one main-log window.

    这个函数看的主要是“整份主日志窗口内部”的量级关系和事件分布，
    用途是给报告一个易读的原因标签，而不是做严格概率推断。
    """
    ether = events["ethercat_timeout"]
    enc = events["encoder_timeout"]
    dio = events["dio_disconnect"]
    no_odom = events["no_odom"]
    tf = events["transform_fail"]
    kinco = events["kinco_can_err"]
    odo_failed_update = events["odo_failed_update"]
    odo_not_updated_500ms = events["odo_not_updated_500ms"]
    reset_prev_frame = events["reset_prev_frame"]
    motor_timeout = events["motor_timeout"]
    odo_data_lost = events["odo_data_lost"]
    motor_error = events["motor_error"]
    robot_blocked = events["robot_blocked"]
    robot_slipping = events["robot_slipping"]

    if imu_total == 0 and odometer_total == 0 and fail_total == 0:
        return "非运动主日志/无数据"
    if fail_total == 0:
        return "未见 odo_update_fail"

    if motor_timeout > 0 or odo_data_lost > 0 or motor_error > 0:
        return "底盘驱动/电机反馈链路优先"
    if odo_failed_update > 0 or odo_not_updated_500ms > 0 or reset_prev_frame > 0:
        return "Odometer上游更新异常"
    if kinco > 0 and fail_total > 0:
        return "KINCO驱动链路优先"
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
    if robot_blocked > 0 and robot_slipping > 0 and fail_total > 0:
        return "运动受阻伴随里程计异常"
    if odometer_total == 0 and imu_total > 0:
        return "上游Odometer断流"
    if odometer_total < max(10, imu_total // 100):
        return "Odometer极稀疏(上游更新异常)"
    return "证据不足(需更多驱动侧日志)"


def write_tsv(out_file: Path, rows: List[LogStats]) -> None:
    """Write per-main-log statistics into a TSV table.

    TSV 是后续筛选、排序、二次分析的稳定中间格式。
    这里会同时落盘“主日志范围事件”和“与 fail 同窗事件”两套口径，避免后面再混淆。
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    event_keys = list(EVENT_PATTERNS.keys())
    with out_file.open("w", encoding="utf-8") as f:
        f.write(
            "log_file\tstart_ts\tend_ts\timu_total\todometer_total\todo_update_fail_total"
            + "".join(f"\t{key}" for key in event_keys)
            + "".join(f"\tcoincident_{key}" for key in event_keys)
            + "\tcoincident_warning_total\tcoincident_error_total\tcoincident_specified_total"
            + "\tcoincident_uncategorized_alarm_total\tcoincident_top1"
            + "\tlead_encoder_ratio\tlead_ethercat_ratio\troot_cause_priority"
            + "\tfail_per_imu\tcause\n"
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
                        *[str(r.events[key]) for key in event_keys],
                        *[str(r.coincident_events[key]) for key in event_keys],
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
    """Assign a coarse health label to one main log.

    它不是最终根因，只是帮助你快速从几十份日志里先挑出“严重/中等/轻微”窗口。
    规则故意保持简单，避免等级定义本身过度复杂。
    """
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
    global_coincident_events: Optional[Dict[str, int]] = None,
    global_overlap_events: Optional[Dict[str, int]] = None,
) -> None:
    """Print the main human-facing report for batch root-cause analysis.

    这份报告会把三种层次分开：
    - 主日志范围事件
    - 与 fail 同窗的指定事件
    - 与 fail 同窗的 Alarm
    这样读者能一眼看出哪些是背景噪声，哪些更接近因果证据。
    """
    print("=== 批量统计完成 ===")
    print(f"主日志数量: {len(rows)}")
    print(f"warning日志数量: {count_side_logs(Path(args.warning_dir))}")
    print(f"error日志数量: {count_side_logs(Path(args.error_dir))}")
    print(f"输出文件: {args.out}")

    print("\n=== 这份报告在看什么 ===")
    print("1) IMU 是否在持续上报")
    print("2) Odometer 是否能持续产出")
    print("3) odo_update_fail 是否高频")
    print("4) 同时间窗是否出现 EtherCAT/编码器/DIO/KINCO/Motor Timeout/odo data lost 等事件")

    cause_counter = Counter(r.cause for r in rows)
    print("\n=== 原因分布(按日志份数) ===")
    for cause, n in cause_counter.most_common():
        print(f"- {cause}: {n}")

    level_counter = Counter(health_level(r) for r in rows)
    print("\n=== 健康度分级 ===")
    for lv in ("严重", "中等", "轻微", "正常", "N/A(非运动日志)"):
        if lv in level_counter:
            print(f"- {lv}: {level_counter[lv]}")

    # 统计“主日志范围并集事件计数”和“全局事件计数”
    overlap_counter = Counter(global_overlap_events or {})

    coincident_event_counter = Counter(global_coincident_events or {})

    print("\n=== 主日志时间范围事件统计(按时间并集、按事件实例统计) ===")
    for key in EVENT_PRINT_ORDER:
        print(f"- {EVENT_LABELS[key]:18s}: {overlap_counter.get(key, 0)}")

    coincident_warning_sum = sum(r.coincident_warning_total for r in rows)
    coincident_error_sum = sum(r.coincident_error_total for r in rows)
    coincident_specified_sum = sum(r.coincident_specified_total for r in rows)
    coincident_other_sum = sum(r.coincident_other_total for r in rows)
    coincident_alarm_counter = Counter()
    for r in rows:
        for k, v in r.coincident_counter.items():
            coincident_alarm_counter[k] += v

    print("\n=== 与 odo_update_fail 同窗指定事件统计(跨日志源，按事件实例统计) ===")
    for key in EVENT_PRINT_ORDER:
        print(f"- {EVENT_LABELS[key]:18s}: {coincident_event_counter.get(key, 0)}")
    if global_coincident_events is not None:
        coincident_specified_sum = sum(global_coincident_events.values())
    print(f"- 同窗指定事件总数 : {coincident_specified_sum}")

    print("\n=== 与 odo_update_fail 同窗 Alarm 统计(仅主日志 Alarm) ===")
    print(f"- 同窗 Warning 总数: {coincident_warning_sum}")
    print(f"- 同窗 Error 总数  : {coincident_error_sum}")
    print(f"- 同窗 Alarm 总数  : {coincident_warning_sum + coincident_error_sum}")
    print(f"- 同窗未分类 Alarm : {coincident_other_sum}")
    top_alarm = coincident_alarm_counter.most_common(20)
    if top_alarm:
        print("- 同窗 Top20 Alarm:")
        for idx, (k, cnt) in enumerate(top_alarm, start=1):
            print(f"  {idx}. {k} -> {cnt}")
    else:
        print("- 同窗 Top20 Alarm: 无")

    print("\n=== 全局事件统计(不看时间重叠，按事件实例统计) ===")
    for key in EVENT_PRINT_ORDER:
        print(f"- {EVENT_LABELS[key]:18s}: {global_events.get(key, 0)}")

    print("\n=== Top10 (按 odo_update_fail_total) ===")
    print("字段说明: IMU帧数、odo_update_fail次数、Odometer帧数、主日志范围事件、同窗事件、同窗告警拆解")
    for idx, r in enumerate(sorted(rows, key=lambda x: x.odo_update_fail_total, reverse=True)[:10], start=1):
        fail_per_imu = (r.odo_update_fail_total / r.imu_total) if r.imu_total > 0 else 0.0
        odom_ratio = (r.odometer_total / r.imu_total * 100.0) if r.imu_total > 0 else 0.0
        window_event_summary = ", ".join(
            f"{EVENT_LABELS[key]}={r.events[key]}" for key in EVENT_PRINT_ORDER if r.events[key] > 0
        ) or "无"
        coincident_event_summary = ", ".join(
            f"{EVENT_LABELS[key]}={r.coincident_events[key]}"
            for key in EVENT_PRINT_ORDER
            if r.coincident_events[key] > 0
        ) or "无"
        print(
            f"{idx}. {r.log_file.name} | 等级={health_level(r)} | "
            f"IMU帧数={r.imu_total}, odo_update_fail次数={r.odo_update_fail_total}, "
            f"Odometer帧数={r.odometer_total}, "
            f"失败比(odo_update_fail/IMU)={fail_per_imu:.3f}, Odometer占比={odom_ratio:.2f}% | "
            f"主日志范围事件={window_event_summary} | "
            f"同窗指定事件={coincident_event_summary} | "
            f"同窗告警(Warning/Error)={r.coincident_warning_total}/{r.coincident_error_total}, "
            f"同窗Alarm总数={r.coincident_warning_total + r.coincident_error_total}, "
            f"同窗指定事件总数={r.coincident_specified_total}, 同窗未分类Alarm={r.coincident_other_total} | "
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
    coincident_event_sum = sum(coincident_event_counter.values())
    overlap_sum = sum(overlap_counter.values())
    if coincident_event_sum == 0 and overlap_sum > 0:
        print("- 主日志时间范围内有故障事件，但与 odo_update_fail 真正同窗的指定事件为 0。")
        print("- 这说明当前更像“同一段日志里有故障”，还不能直接判成“故障触发了 fail”。")
    elif coincident_event_sum == 0 and sum(global_events.values()) > 0:
        print("- 全局 warning/error 有故障事件，但本批主日志附近未捕捉到与 fail 同窗的指定事件。")
        print("- 这通常表示主日志与独立 warning/error 日志时间不重叠，当前无法做“同窗因果”推断。")
    elif (
        coincident_event_counter.get("motor_timeout", 0) > 0
        or coincident_event_counter.get("odo_data_lost", 0) > 0
        or coincident_event_counter.get("motor_error", 0) > 0
    ):
        print("- 同窗出现 Motor Timeout / odo data lost / Motor Error，优先检查底盘驱动与电机反馈链路。")
    elif (
        coincident_event_counter.get("odo_failed_update", 0) > 0
        or coincident_event_counter.get("odo_not_updated_500ms", 0) > 0
        or coincident_event_counter.get("reset_prev_frame", 0) > 0
    ):
        print("- 同窗出现 Odometer FAILED / 500ms stale / Reset previous frame，优先检查 Odometer 上游更新。")
    elif coincident_event_counter.get("kinco_can_err", 0) > 0:
        print("- 同窗 KINCO_CAN_ERR_CODE_DATA 高频，优先检查 KINCO 驱动与电机反馈。")
    elif (
        coincident_event_counter.get("ethercat_timeout", 0) > 0
        and coincident_event_counter.get("encoder_timeout", 0) > 0
    ):
        print("- 同窗出现 EtherCAT 与 编码器 timeout，优先判断底盘链路整体不稳。")
    elif coincident_event_counter.get("ethercat_timeout", 0) > 0:
        print("- 同窗 EtherCAT timeout 主导，优先检查 EtherCAT 主站周期/总线。")
    elif coincident_event_counter.get("encoder_timeout", 0) > 0:
        print("- 同窗编码器 timeout 主导，优先检查编码器反馈链路。")
    elif (
        coincident_event_counter.get("no_odom", 0) > 0
        or coincident_event_counter.get("transform_fail", 0) > 0
    ):
        print("- 同窗 no odom/Transform fail 主导，优先检查上层时序与队列匹配。")
    elif coincident_event_counter.get("robot_out_of_path", 0) > 0 or coincident_event_counter.get(
        "pgv_cannot_find_code", 0
    ) > 0:
        print("- 同窗出现路径偏离/PGV识别异常，提示导航感知链路有业务级异常。")
        print("- 这类事件会放大定位偏移，但不是 odo_update_fail 的直接底层根因。")
    else:
        print("- 未捕捉到可用于直接归因的同窗事件，建议补充驱动侧详细日志。")


def main() -> None:
    """Provide a standalone CLI for batch Odometer root-cause statistics.

    虽然统一入口脚本已经会调这个模块，但保留独立 CLI 仍然有价值：
    现场可以直接对解压后的 log/warning/error 目录单独跑。
    """
    parser = argparse.ArgumentParser(description="全量统计 Odometer 失败根因")
    parser.add_argument("--log-dir", required=True, help="主日志目录")
    parser.add_argument(
        "--log-glob",
        default="robokit_*.log*",
        help="主日志匹配模式，默认 robokit_*.log*；例如 robokit_2026-02-11_*.log*",
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
    warning_logs = side_logs(warning_dir)
    error_logs = side_logs(error_dir)

    # 事件来源包含:
    # 1) warning/*.log
    # 2) error/*.log
    # 3) robokit 主日志内部的 [R][w]/[R][e] 业务告警（如 robot out of path / PGV cannot find code）
    event_files = warning_logs + error_logs + main_logs
    events = deduplicate_event_instances(parse_event_logs(event_files))
    global_events = summarize_global_events(events)

    rows: List[LogStats] = []
    all_fail_times: List[dt.datetime] = []
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
        coincident_event_counts = count_coincident_events(events, fail_times, args.co_window_sec)
        all_fail_times.extend(fail_times)
        c_warn, c_err, c_counter, c_uncategorized = count_coincident_alarms(
            fail_times, alarms, args.co_window_sec
        )
        c_top1 = Counter(c_counter).most_common(1)[0][0] if c_counter else ""
        c_specified = sum(coincident_event_counts.values())
        lead_encoder_ratio = calc_lead_ratio(
            local_event_times.get("encoder_timeout", []), fail_times, args.lead_window_sec
        )
        lead_ethercat_ratio = calc_lead_ratio(
            local_event_times.get("ethercat_timeout", []), fail_times, args.lead_window_sec
        )
        root_cause_priority = judge_root_cause_priority(
            lead_encoder_ratio, lead_ethercat_ratio, fail_total, coincident_event_counts
        )
        cause = classify_cause(imu_total, odometer_total, fail_total, coincident_event_counts)
        rows.append(
            LogStats(
                log_file=log_file,
                start_ts=start_ts,
                end_ts=end_ts,
                imu_total=imu_total,
                odometer_total=odometer_total,
                odo_update_fail_total=fail_total,
                events=event_counts,
                coincident_events=coincident_event_counts,
                coincident_warning_total=c_warn,
                coincident_error_total=c_err,
                coincident_specified_total=c_specified,
                coincident_other_total=c_uncategorized,
                coincident_top1=c_top1,
                coincident_counter=c_counter,
                lead_encoder_ratio=lead_encoder_ratio,
                lead_ethercat_ratio=lead_ethercat_ratio,
                root_cause_priority=root_cause_priority,
                cause=cause,
            )
        )

    write_tsv(Path(args.out), rows)
    global_coincident_events = count_coincident_events(events, all_fail_times, args.co_window_sec)
    global_overlap_events = count_events_in_ranges_union(
        events, [(r.start_ts, r.end_ts) for r in rows]
    )
    print_human_report(rows, args, global_events, global_coincident_events, global_overlap_events)


if __name__ == "__main__":
    main()
