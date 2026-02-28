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

TS_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]")
FAIL_RE = re.compile(r"\[OC\]\[d\] \[odo_update_fail\]\[")
# 典型格式: [Alarm][Warning|54070|PGV cannot find code|1]
ALARM_RE = re.compile(r"\[Alarm\]\[(Warning|Error)\|([^|\]]+)\|([^\]]+)\]", re.IGNORECASE)


@dataclass
class EventHit:
    log_file: str
    level: str
    code: str
    message: str
    ts: dt.datetime
    nearest_fail_ms: float


def parse_ts(line: str) -> Optional[dt.datetime]:
    m = TS_RE.search(line)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%y%m%d %H%M%S.%f")


def nearest_fail_delta_ms(ts: dt.datetime, fail_times: List[dt.datetime]) -> Optional[float]:
    if not fail_times:
        return None
    # 线性扫描足够用；日志规模一般可接受。
    min_abs = None
    for fts in fail_times:
        d = abs((ts - fts).total_seconds() * 1000.0)
        if min_abs is None or d < min_abs:
            min_abs = d
    return min_abs


def analyze_log(log_path: Path, window_sec: float) -> List[EventHit]:
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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write("log_file\tts\tlevel\tcode\tmessage\tnearest_fail_ms\n")
        for h in hits:
            f.write(
                f"{h.log_file}\t{h.ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}\t{h.level}\t"
                f"{h.code}\t{h.message}\t{h.nearest_fail_ms:.3f}\n"
            )


def print_summary(hits: List[EventHit]) -> None:
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
    parser = argparse.ArgumentParser(description="提取与 odo_update_fail 同窗的 ERROR/WARNING")
    parser.add_argument("--log-dir", required=True, help="主日志目录")
    parser.add_argument("--glob", default="*.log", help="日志文件匹配模式，默认*.log")
    parser.add_argument("--window-sec", type=float, default=1.0, help="同窗阈值(秒)，默认1.0")
    parser.add_argument("--out", required=True, help="输出TSV路径")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    logs = sorted(log_dir.glob(args.glob))

    all_hits: List[EventHit] = []
    for lf in logs:
        all_hits.extend(analyze_log(lf, args.window_sec))

    write_tsv(Path(args.out), all_hits)
    print_summary(all_hits)
    print(f"\n输出文件: {args.out}")


if __name__ == "__main__":
    main()
