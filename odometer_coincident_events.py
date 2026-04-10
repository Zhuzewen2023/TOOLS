#!/usr/bin/env python3
"""提取与 odo_update_fail 同窗的 ERROR/WARNING 事件。

同窗定义：事件时间与任一 odo_update_fail 时间差 <= window_sec。
默认 window_sec=1.0 秒。
"""

import argparse
import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from robokit_imu_patterns import matching_event_names

TS_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]")
FAIL_RE = re.compile(r"\[OC\]\[d\] \[odo_update_fail\]\[")
LINE_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]\[\d+\]\[([^\]]+)\]\[([a-z])\]\s*(.*)$")
# 典型格式: [Alarm][Warning|54070|PGV cannot find code|1]
ALARM_RE = re.compile(
    r"\[Alarm\]\[(Warning|Error)\|([^|\]]+)\|([^|\]]+)(?:\|([^\]]+))?\]",
    re.IGNORECASE,
)


@dataclass
class EventHit:
    log_file: str
    level: str
    code: str
    message: str
    ts: dt.datetime
    nearest_fail_ms: float


def parse_ts(line: str) -> Optional[dt.datetime]:
    """Parse the robokit timestamp prefix from one log line.

    这个工具后面的所有“同窗”判断都建立在时间戳上。
    如果这一行没有标准时间戳，就不能参与时序分析。
    """
    m = TS_RE.search(line)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%y%m%d %H%M%S.%f")


def nearest_fail_delta_ms(ts: dt.datetime, fail_times: List[dt.datetime]) -> Optional[float]:
    """Return the nearest absolute delta from one event to any fail time.

    同窗判断的核心不是“和哪一次 fail 配对”，而是“离最近的 fail 有多近”。
    所以这里输出最小绝对时间差，供后面统一比较 window_sec。
    """
    if not fail_times:
        return None
    # 线性扫描足够用；日志规模一般可接受。
    min_abs = None
    for fts in fail_times:
        d = abs((ts - fts).total_seconds() * 1000.0)
        if min_abs is None or d < min_abs:
            min_abs = d
    return min_abs


def analyze_log(log_path: Path, window_sec: float, include_all_non_alarm: bool = False) -> List[EventHit]:
    """Extract coincident Alarm/high-signal events from one main log.

    默认行为：
    - 保留 Alarm
    - 保留能命中 EVENT_PATTERNS 的非 Alarm `e/w` 行

    可选行为：
    - `include_all_non_alarm=True` 时，把所有非 Alarm `e/w` 行都纳入

    这样既能保留高信号默认结果，又能在需要时切到“全量证据”模式。
    """
    fail_times: List[dt.datetime] = []
    alarm_lines: List[Tuple[dt.datetime, str, str, str]] = []

    with log_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            ts = parse_ts(line)
            if ts is None:
                continue

            if FAIL_RE.search(line):
                fail_times.append(ts)

            m_alarm = ALARM_RE.search(line)
            if m_alarm:
                level = m_alarm.group(1).capitalize()
                code = m_alarm.group(2).strip()
                msg = m_alarm.group(3).strip()
                alarm_lines.append((ts, level, code, msg))
                continue

            m_line = LINE_RE.match(line)
            if m_line:
                _ts_text, module, sev, msg = m_line.groups()
                event_names = matching_event_names(msg)
                if not event_names and not include_all_non_alarm:
                    continue
                if event_names:
                    for event_name in event_names:
                        if sev == "e":
                            alarm_lines.append((ts, "Error", f"EVENT@{module}:{event_name}", msg.strip()))
                        elif sev == "w":
                            alarm_lines.append((ts, "Warning", f"EVENT@{module}:{event_name}", msg.strip()))
                elif sev == "e":
                    alarm_lines.append((ts, "Error", f"NON_ALARM@{module}", msg.strip()))
                elif sev == "w":
                    alarm_lines.append((ts, "Warning", f"NON_ALARM@{module}", msg.strip()))

    hits: List[EventHit] = []
    max_ms = window_sec * 1000.0
    for ts, level, code, msg in alarm_lines:
        d_ms = nearest_fail_delta_ms(ts, fail_times)
        if d_ms is not None and d_ms <= max_ms:
            hits.append(
                EventHit(
                    log_file=str(log_path),
                    level=level,
                    code=code,
                    message=msg,
                    ts=ts,
                    nearest_fail_ms=d_ms,
                )
            )
    return hits


def write_tsv(out_path: Path, hits: List[EventHit]) -> None:
    """Write coincident Alarm hits to a TSV file for offline review.

    终端摘要适合快速看，TSV 适合后续筛选、排序、发给现场或继续加工。
    这里把输出格式固定下来，避免每次人工复制日志片段。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write("log_file\tts\tlevel\tcode\tmessage\tnearest_fail_ms\n")
        for h in hits:
            f.write(
                f"{h.log_file}\t{h.ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\t{h.level}\t"
                f"{h.code}\t{h.message}\t{h.nearest_fail_ms:.3f}\n"
            )


def print_summary(hits: List[EventHit]) -> None:
    """Print a human-readable summary for coincident Alarm hits.

    这个函数关注的是“同窗证据的分布”，所以会按级别、事件键、文件三个维度
    做一个快速盘点，帮助你先判断是否值得继续深挖。
    """
    print("=== 同窗 ERROR/WARNING 提取完成 ===")
    print(f"同窗事件总数: {len(hits)}")

    by_level = defaultdict(int)
    by_key = defaultdict(int)
    by_file = defaultdict(int)

    for h in hits:
        by_level[h.level] += 1
        by_key[(h.level, h.code, h.message)] += 1
        by_file[h.log_file] += 1

    print("\n=== 按级别统计 ===")
    for lv in sorted(by_level):
        print(f"- {lv}: {by_level[lv]}")

    print("\n=== Top20 事件(同窗次数) ===")
    top = sorted(by_key.items(), key=lambda x: x[1], reverse=True)[:20]
    for idx, ((lv, code, msg), cnt) in enumerate(top, start=1):
        print(f"{idx}. {lv}|{code}|{msg} -> {cnt}")

    print("\n=== Top20 日志文件(同窗事件数) ===")
    topf = sorted(by_file.items(), key=lambda x: x[1], reverse=True)[:20]
    for idx, (lf, cnt) in enumerate(topf, start=1):
        print(f"{idx}. {Path(lf).name}: {cnt}")


def main() -> None:
    """Provide a small CLI wrapper around coincident Alarm extraction.

    这个脚本本身很轻，main() 主要就是接参数、遍历日志、输出结果，
    保持和更大的统一入口脚本一样的使用习惯。
    """
    parser = argparse.ArgumentParser(description="提取与 odo_update_fail 同窗的 ERROR/WARNING")
    parser.add_argument("--log-dir", required=True, help="主日志目录")
    parser.add_argument("--glob", default="robokit_*.log*", help="日志文件匹配模式，默认robokit_*.log*")
    parser.add_argument("--window-sec", type=float, default=1.0, help="同窗阈值(秒)，默认1.0")
    parser.add_argument(
        "--all-non-alarm",
        action="store_true",
        help="把所有非 Alarm 的 e/w 行也纳入；默认仅保留高信号事件家族",
    )
    parser.add_argument("--out", required=True, help="输出TSV路径")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    logs = sorted(log_dir.glob(args.glob))

    all_hits: List[EventHit] = []
    for lf in logs:
        all_hits.extend(analyze_log(lf, args.window_sec, args.all_non_alarm))

    write_tsv(Path(args.out), all_hits)
    print_summary(all_hits)
    print(f"\n输出文件: {args.out}")


if __name__ == "__main__":
    main()
