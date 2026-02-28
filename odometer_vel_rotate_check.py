#!/usr/bin/env python3
import argparse
import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List

TS_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]")
ODOM_RE = re.compile(r"\[OC\]\[d\] \[Odometer\]\[([^\]]+)\]")
IMU_RE = re.compile(r"\[DC\]\[d\] \[IMU\]\[")
FAIL_RE = re.compile(r"\[OC\]\[d\] \[odo_update_fail\]\[")


@dataclass
class Stats:
    # 时间窗内命中的 Odometer 包总数。为 0 或明显偏低时，通常意味着上游断流或稀疏。
    odometer_total: int = 0
    # 时间窗内命中的 IMU 包总数，用来判断 IMU 是否持续在上报。
    imu_total: int = 0
    # 时间窗内 odo_update_fail 日志计数，反映 Odometer 更新失败频度。
    fail_total: int = 0
    # Odometer 中 vel_rotate == 0 的计数。可用于支持“proto3 默认值省略”假设。
    vel_rotate_zero: int = 0
    # Odometer 中 vel_rotate != 0 的计数。越高说明角速度有效非零样本越多。
    vel_rotate_nonzero: int = 0
    # Odometer 末列无法解析为数值的计数（日志格式异常或解析假设不成立）。
    vel_rotate_parse_fail: int = 0
    # 相邻 Odometer cycle 不是 +1 的次数，反映丢帧/跳帧频率。
    cycle_jump_count: int = 0
    # cycle 最大跳变步长。例如 100->150，步长=50，表示中间缺失 49 个 cycle。
    cycle_max_step: int = 1


def parse_ts(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%y%m%d %H%M%S.%f")


def in_range(ts: dt.datetime, start: Optional[dt.datetime], end: Optional[dt.datetime]) -> bool:
    if start and ts < start:
        return False
    if end and ts > end:
        return False
    return True


def parse_odometer_payload(payload: str):
    # Odometer 行格式示例:
    # [Odometer][cycle|timestamp|x|y|angle|...|vel_x|vel_y|vel_rotate]
    # 这里取首列 cycle，和末列 vel_rotate。
    parts = payload.split("|")
    if not parts:
        return None, None
    try:
        cycle = int(parts[0])
    except ValueError:
        cycle = None

    vel_rotate = None
    if len(parts) >= 2:
        try:
            vel_rotate = float(parts[-1])
        except ValueError:
            vel_rotate = None
    return cycle, vel_rotate


def analyze_log(log_path: Path, start: Optional[dt.datetime], end: Optional[dt.datetime]):
    stats = Stats()
    sec = defaultdict(lambda: {"odom": 0, "imu": 0, "fail": 0, "zero": 0, "nonzero": 0})

    prev_cycle = None
    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m_ts = TS_RE.search(line)
            if not m_ts:
                continue
            ts = parse_ts(m_ts.group(1))
            if not in_range(ts, start, end):
                continue

            sec_key = ts.strftime("%y%m%d %H:%M:%S")

            if IMU_RE.search(line):
                stats.imu_total += 1
                sec[sec_key]["imu"] += 1

            if FAIL_RE.search(line):
                stats.fail_total += 1
                sec[sec_key]["fail"] += 1

            m_odom = ODOM_RE.search(line)
            if m_odom:
                stats.odometer_total += 1
                sec[sec_key]["odom"] += 1

                cycle, vel_rotate = parse_odometer_payload(m_odom.group(1))
                if cycle is not None and prev_cycle is not None:
                    step = cycle - prev_cycle
                    if step != 1:
                        stats.cycle_jump_count += 1
                        if step > stats.cycle_max_step:
                            stats.cycle_max_step = step
                if cycle is not None:
                    prev_cycle = cycle

                if vel_rotate is None:
                    stats.vel_rotate_parse_fail += 1
                elif abs(vel_rotate) < 1e-9:
                    stats.vel_rotate_zero += 1
                    sec[sec_key]["zero"] += 1
                else:
                    stats.vel_rotate_nonzero += 1
                    sec[sec_key]["nonzero"] += 1

    return stats, sec


def count_keyword_logs(paths, start: Optional[dt.datetime], end: Optional[dt.datetime]):
    keywords = ("no odom", "Transform fail", "encoder timeout", "no imu")
    hit_count = defaultdict(int)
    hit_lines = []

    for path in paths:
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ts_match = TS_RE.search(line)
                if ts_match:
                    ts = parse_ts(ts_match.group(1))
                    if not in_range(ts, start, end):
                        continue
                lowered = line.lower()
                for k in keywords:
                    if k in lowered:
                        hit_count[k] += 1
                        if len(hit_lines) < 20:
                            hit_lines.append(line.rstrip())
                        break
    return hit_count, hit_lines


def classify(stats: Stats, keyword_hits) -> Tuple[str, List[str]]:
    reasons = []
    if stats.odometer_total == 0:
        reasons.append("时间窗内 Odometer 包数为 0。")
        return "异常：时间窗内无 Odometer 包，属于上游断流/不可用。", reasons

    fail_high = stats.fail_total > 0
    jump_high = stats.cycle_jump_count > max(2, int(stats.odometer_total * 0.01))
    no_odom_hit = keyword_hits.get("no odom", 0) > 0 or keyword_hits.get("transform fail", 0) > 0

    if stats.fail_total > 0:
        reasons.append(f"存在 odo_update_fail={stats.fail_total} 次。")
    if jump_high:
        reasons.append(
            f"Odometer cycle 跳变次数={stats.cycle_jump_count}，最大步长={stats.cycle_max_step}。"
        )
    if no_odom_hit:
        reasons.append("warning/error 出现 no odom 或 Transform fail。")

    if (fail_high and jump_high) or no_odom_hit:
        return "异常倾向：上游 Odometer 更新异常（非单纯 vel_rotate=0 省略）。", reasons

    if stats.vel_rotate_zero > 0 and stats.fail_total == 0 and stats.cycle_jump_count <= 1:
        reasons.append(
            f"vel_rotate 为 0 的样本数={stats.vel_rotate_zero}，且 fail/jump 基本无异常。"
        )
        return "正常倾向：vel_rotate 为 0 导致 proto3 省略字段。", reasons

    reasons.append("当前证据不够单向收敛，建议缩小时间窗复核。")
    return "灰区：请结合运动工况和更小时间窗复核。", reasons


def print_report(stats: Stats, sec, keyword_hits, keyword_lines, show_seconds: bool):
    # 告诉使用者脚本正在验证什么。
    print("=== vel_rotate 缺失原因验证报告 ===")
    print("【正在做什么】")
    print("1) 统计 Odometer/IMU/odo_update_fail 基础计数。")
    print("2) 验证假设A: vel_rotate=0 导致字段省略（正常）。")
    print("3) 验证假设B: Odometer 上游更新异常导致缺失（异常）。")

    print("\n【关键统计】")
    print(f"- Odometer 包数           : {stats.odometer_total}")
    print(f"- IMU 包数                : {stats.imu_total}")
    print(f"- odo_update_fail 次数    : {stats.fail_total}")
    print(f"- vel_rotate=0 次数       : {stats.vel_rotate_zero}")
    print(f"- vel_rotate!=0 次数      : {stats.vel_rotate_nonzero}")
    print(f"- vel_rotate 解析失败次数 : {stats.vel_rotate_parse_fail}")
    print(f"- cycle 跳变次数          : {stats.cycle_jump_count}")
    print(f"- cycle 最大跳变步长      : {stats.cycle_max_step}")

    print("\n【指标释义】")
    print("- odometer_total: 时间窗内 Odometer 包总数。")
    print("- odo_update_fail_total: 里程计更新失败日志总次数。")
    print("- vel_rotate_zero: Odometer 中 vel_rotate=0 的次数。")
    print("- vel_rotate_nonzero: Odometer 中 vel_rotate!=0 的次数。")
    print("- cycle_jump_count: Odometer cycle 非连续(+1)的次数。")
    print("- cycle_max_step: cycle 最大跳变步长，越大表示缺包越多。")

    print("\n【关联故障日志命中】")
    if keyword_hits:
        for k in sorted(keyword_hits):
            print(f"- {k:16s}: {keyword_hits[k]}")
    else:
        print("- 无")

    decision, reasons = classify(stats, keyword_hits)
    print("\n【在判断什么】")
    print("- 如果 Odometer 连续、无 fail/jump，且 vel_rotate=0 比例高 => 正常省略。")
    print("- 如果 fail 高频或 cycle 常跳变，或出现 no odom/Transform fail => 上游异常。")

    print("\n【结论】")
    print(f"- {decision}")

    print("\n【结论依据】")
    for reason in reasons:
        print(f"- {reason}")

    print("\n【可能问题在哪里】")
    if "异常倾向" in decision or "异常：" in decision:
        print("- 里程计上游输入不稳定（编码器/电机反馈超时、驱动链路抖动）。")
        print("- Odometer 计算周期丢帧或更新失败（由 odo_update_fail 与 cycle 跳变体现）。")
        print("- 时间同步/数据时序异常（常伴随 Transform fail、no odom）。")
    elif "正常倾向" in decision:
        print("- 主要是 vel_rotate 数值为 0，被 proto3 默认值规则省略。")
    else:
        print("- 证据不充分，建议缩小时间窗并对照运动工况再次验证。")

    if keyword_lines:
        print("\n【关键日志样例】(最多20条)")
        for line in keyword_lines:
            print(f"- {line}")

    if show_seconds:
        print("\n【每秒统计】")
        print("sec,odom,imu,fail,vr_zero,vr_nonzero")
        for sec_key in sorted(sec):
            d = sec[sec_key]
            print(f"{sec_key},{d['odom']},{d['imu']},{d['fail']},{d['zero']},{d['nonzero']}")


def main():
    parser = argparse.ArgumentParser(
        description="验证 vel_rotate 缺失是 0 省略还是上游 Odometer 异常"
    )
    parser.add_argument("--log", required=True, help="主日志路径（含 Odometer/IMU/odo_update_fail）")
    parser.add_argument("--warning", help="warning 日志路径")
    parser.add_argument("--error", dest="error_log", help="error 日志路径")
    parser.add_argument("--start", help="开始时间，格式: YYMMDD HHMMSS.mmm")
    parser.add_argument("--end", help="结束时间，格式: YYMMDD HHMMSS.mmm")
    parser.add_argument("--show-seconds", action="store_true", help="输出每秒统计")
    args = parser.parse_args()

    start = parse_ts(args.start) if args.start else None
    end = parse_ts(args.end) if args.end else None

    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"log not found: {log_path}")

    print("== 开始分析 ==")
    print(f"主日志: {log_path}")
    if args.warning:
        print(f"warning日志: {args.warning}")
    if args.error_log:
        print(f"error日志: {args.error_log}")
    print(f"时间窗: {args.start or '起点'} ~ {args.end or '终点'}")

    stats, sec = analyze_log(log_path, start, end)
    keyword_hits, keyword_lines = count_keyword_logs([args.warning, args.error_log], start, end)
    print_report(stats, sec, keyword_hits, keyword_lines, args.show_seconds)


if __name__ == "__main__":
    main()
