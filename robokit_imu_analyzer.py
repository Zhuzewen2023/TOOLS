#!/usr/bin/env python3
"""Unified CLI for Robokit IMU/Odometer log analysis.

Reading guide:
1) build_context(): normalize zip/dir input into a single directory view
2) print_summary(): package-level inventory of errors and warnings
3) run_root_cause(): window-based cause classification on main logs
4) run_coincident()/run_vel_rotate(): focused evidence extraction
"""

import argparse
import re
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional

from odometer_coincident_events import (
    EventHit,
    analyze_log as analyze_coincident_log,
    print_summary as print_coincident_summary,
    write_tsv as write_coincident_tsv,
)
from odometer_root_cause_stats import (
    LogStats,
    calc_lead_ratio,
    classify_cause,
    count_coincident_alarms,
    count_coincident_events,
    count_events_in_range,
    count_events_in_ranges_union,
    deduplicate_event_instances,
    judge_root_cause_priority,
    parse_event_logs,
    parse_main_log,
    print_human_report,
    summarize_global_events,
    write_tsv as write_root_cause_tsv,
)
from odometer_vel_rotate_check import (
    analyze_log as analyze_vel_rotate_log,
    count_keyword_logs,
    parse_ts as parse_vel_rotate_ts,
    print_report as print_vel_rotate_report,
)
from robokit_imu_patterns import (
    BUSINESS_LOG_NAME_RE,
    EVENT_LABELS,
    EVENT_PATTERNS,
    EVENT_PRINT_ORDER,
    ROTATED_LOG_NAME_RE,
    SUMMARY_PATTERNS,
    SYSTEM_LOG_NAME_RE,
)


LINE_RE = re.compile(r"^\[(\d{6} \d{6}\.\d{3})\]\[\d+\]\[([^\]]+)\]\[([a-z])\]\s*(.*)$")
ALARM_RE = re.compile(
    r"\[Alarm\]\[(Warning|Error)\|([^|\]]+)\|([^|\]]+)(?:\|([^\]]+))?\]",
    re.IGNORECASE,
)


@dataclass
class InputContext:
    root: Path
    log_dir: Path
    warning_dir: Path
    error_dir: Path
    temp_dir: Optional[tempfile.TemporaryDirectory]

    def cleanup(self) -> None:
        """Release the temporary extraction directory when the input was a zip.

        统一入口经常需要把 zip 解到临时目录。
        这个方法保证命令结束后能把临时文件收掉，不把 `/tmp` 越堆越大。
        """
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def normalize_text(text: str) -> str:
    """Collapse numeric noise so similar alarm texts group into one bucket.

    summary 的 TopN 更关注“哪一类问题高发”，而不是具体数字细节。
    这里把十六进制、整数、浮点数统一替换成占位符，减少同类文本被拆散。
    """
    # Normalize numeric noise so the same message family can be grouped together.
    text = text.strip()
    text = re.sub(r"0x[0-9A-Fa-f]+", "<hex>", text)
    text = re.sub(r"[-+]?\d+\.\d+[eE][-+]?\d+", "<num>", text)
    text = re.sub(r"[-+]?\d+\.\d+", "<num>", text)
    text = re.sub(r"[-+]?\d+", "<int>", text)
    text = re.sub(r"\s+", " ", text)
    return text


def should_extract_member(name: str) -> bool:
    """Decide whether a zip member belongs to the analyzer's input surface.

    这个函数把“zip 里哪些文件值得解出来”集中到一处维护，避免 build_context()
    把系统日志、业务日志和无关文件的过滤逻辑写散。
    """
    member = PurePosixPath(name)
    if not member.name or len(member.parts) < 2:
        return False
    base = member.name
    for part in member.parts[:-1]:
        if part in ("warning", "error"):
            return bool(ROTATED_LOG_NAME_RE.match(base))
        if part == "log":
            return bool(BUSINESS_LOG_NAME_RE.match(base) or SYSTEM_LOG_NAME_RE.match(base))
    return False


def discover_input_root(extract_root: Path) -> Path:
    """Locate the real package root after zip extraction.

    有些现场 zip 会在最外层再包一层目录，例如 `pkg/log/...`。
    如果直接假设临时目录下就是 `log/`，整包会被误判成空包。
    """
    if (extract_root / "log").exists():
        return extract_root

    candidates = []
    for log_dir in extract_root.rglob("log"):
        if not log_dir.is_dir():
            continue
        parent = log_dir.parent
        score = sum(1 for name in ("log", "warning", "error") if (parent / name).exists())
        if score > 0:
            candidates.append((-score, len(parent.parts), str(parent), parent))

    if candidates:
        return sorted(candidates)[0][3]
    return extract_root


def build_context(input_path: str) -> InputContext:
    """Normalize zip/dir input into one InputContext view.

    后续所有分析逻辑都只关心 log_dir / warning_dir / error_dir。
    这个函数负责吞掉“用户传的是 zip 还是目录”这种输入差异。
    """
    # The rest of the tool only wants log_dir / warning_dir / error_dir.
    # This function hides whether the caller passed a zip or an extracted directory.
    path = Path(input_path)
    if path.is_file() and path.suffix.lower() == ".zip":
        temp_dir = tempfile.TemporaryDirectory(prefix="robokit_imu_analyzer_")
        extract_root = Path(temp_dir.name)
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not should_extract_member(name):
                    continue
                zf.extract(name, extract_root)
        root = discover_input_root(extract_root)
        return InputContext(root=root, log_dir=root / "log", warning_dir=root / "warning", error_dir=root / "error", temp_dir=temp_dir)

    if not path.exists():
        raise SystemExit(f"input not found: {path}")

    if path.is_dir() and (path / "log").exists():
        root = path
        return InputContext(root=root, log_dir=root / "log", warning_dir=root / "warning", error_dir=root / "error", temp_dir=None)

    if path.is_dir() and path.name == "log":
        root = path.parent
        return InputContext(root=root, log_dir=path, warning_dir=root / "warning", error_dir=root / "error", temp_dir=None)

    raise SystemExit("input must be a zip file or a directory containing log/")


def business_logs(ctx: InputContext) -> List[Path]:
    """Return logs that describe robot runtime behavior.

    这类日志参与 summary 和 root-cause 的主体分析。
    设计上故意把 syslog/kern.log 排除掉，避免系统噪声混进业务统计。
    """
    # "Business logs" are the logs that describe robot runtime behavior.
    # We intentionally keep syslog/kern.log out of this bucket.
    logs: List[Path] = []
    if ctx.error_dir.exists():
        logs.extend(side_logs(ctx.error_dir))
    if ctx.warning_dir.exists():
        logs.extend(side_logs(ctx.warning_dir))
    if ctx.log_dir.exists():
        logs.extend(sorted(p for p in ctx.log_dir.iterdir() if p.is_file() and BUSINESS_LOG_NAME_RE.match(p.name)))
    return logs


def side_logs(log_dir: Path) -> List[Path]:
    """Return warning/error logs, including rotated `*.log.N` files."""
    if not log_dir.exists():
        return []
    return sorted(p for p in log_dir.iterdir() if p.is_file() and ROTATED_LOG_NAME_RE.match(p.name))


def system_logs(ctx: InputContext) -> List[Path]:
    """Return extracted syslog/kern.log files for package-level inspection.

    系统日志只在 summary 里做补充侧证，不参与业务根因分类。
    单独拆这个函数，是为了让“系统日志口径”与“业务日志口径”明确分层。
    """
    if not ctx.log_dir.exists():
        return []
    return sorted(p for p in ctx.log_dir.iterdir() if p.is_file() and SYSTEM_LOG_NAME_RE.match(p.name))


def classify_system_line(line: str) -> Optional[str]:
    """Heuristically classify a syslog line as error-like or warning-like.

    syslog/kern.log 没有 robokit 主日志那种稳定的 `[e]/[w]` 级别字段，
    所以这里只能做文本启发式分类，输出也明确写成 `*_like`。
    """
    lowered = line.lower()
    if re.search(r"\berror\b|\bfailed\b|\bfail\b|\btimeout\b|segfault|i/o error", lowered):
        return "error_like"
    if re.search(r"\bwarn\b|\bwarning\b", lowered):
        return "warning_like"
    return None


def main_logs(ctx: InputContext, log_glob: str) -> List[Path]:
    """Return the main robokit logs used for per-window root-cause analysis.

    root-cause / coincident / vel-rotate 都是围绕“主日志窗口”来工作的。
    因此这里要把主日志选择逻辑与 summary 的全包盘点逻辑分开。
    """
    if not ctx.log_dir.exists():
        return []
    return sorted(p for p in ctx.log_dir.glob(log_glob) if p.is_file())


def print_summary(args: argparse.Namespace) -> None:
    """Print package-level inventory before any causal judgment.

    summary 的目标不是下结论，而是先回答：
    这包里到底有什么日志、多少关键异常、热点集中在哪些文本和文件上。
    """
    # summary answers "what is inside this package?" before we attempt causality.
    ctx = build_context(args.input)
    try:
        sev_counts = Counter()
        pattern_counts = Counter()
        alarm_counts = Counter()
        non_alarm_error_counts = Counter()
        non_alarm_warning_counts = Counter()
        per_file_error = Counter()
        per_file_warning = Counter()
        system_sev = Counter()
        system_hits = Counter()

        for path in business_logs(ctx):
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    for key, pattern in SUMMARY_PATTERNS.items():
                        if pattern.search(line):
                            pattern_counts[key] += 1

                    match = LINE_RE.match(line)
                    if not match:
                        continue
                    _ts, _module, sev, msg = match.groups()
                    sev_counts[sev] += 1
                    if sev == "e":
                        per_file_error[path.name] += 1
                    elif sev == "w":
                        per_file_warning[path.name] += 1

                    alarm_match = ALARM_RE.search(msg)
                    if alarm_match:
                        level, code, alarm_msg, _state = alarm_match.groups()
                        alarm_counts[f"{level}|{code}|{normalize_text(alarm_msg)}"] += 1
                    elif sev == "e":
                        non_alarm_error_counts[normalize_text(msg)] += 1
                    elif sev == "w":
                        non_alarm_warning_counts[normalize_text(msg)] += 1

        for path in system_logs(ctx):
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    system_level = classify_system_line(line)
                    if system_level == "error_like":
                        system_sev["error_like"] += 1
                    elif system_level == "warning_like":
                        system_sev["warning_like"] += 1
                    if re.search(r"imu|ttyS9|serial|lpms|seer_imu", line, re.IGNORECASE):
                        system_hits["imu_or_serial_related"] += 1
                    if re.search(r"fail|error|timeout", line, re.IGNORECASE):
                        system_hits["fail_or_error_or_timeout"] += 1

        print("=== Robokit IMU 包级汇总 ===")
        print(f"输入: {args.input}")
        print(f"业务日志数量: {len(business_logs(ctx))}")
        print(f"系统日志数量: {len(system_logs(ctx))}")
        print("统计口径: 关键异常计数按日志命中次数累计，不做跨文件去重。")

        print("\n=== 业务日志总览 ===")
        print(f"- error级行数  : {sev_counts.get('e', 0)}")
        print(f"- warning级行数: {sev_counts.get('w', 0)}")
        print(f"- debug级行数  : {sev_counts.get('d', 0)}")
        print(f"- info级行数   : {sev_counts.get('i', 0)}")

        print("\n=== 关键异常计数 ===")
        for key in SUMMARY_PATTERNS.keys():
            label = EVENT_LABELS.get(key, key)
            print(f"- {label:18s}: {pattern_counts.get(key, 0)}")

        print("\n=== Top Alarm 类型 ===")
        top_alarm = alarm_counts.most_common(args.top)
        if top_alarm:
            for idx, (key, count) in enumerate(top_alarm, start=1):
                print(f"{idx}. {key} -> {count}")
        else:
            print("- 无")

        print("\n=== Top 非 Alarm Error 文本 ===")
        top_error = non_alarm_error_counts.most_common(args.top)
        if top_error:
            for idx, (key, count) in enumerate(top_error, start=1):
                print(f"{idx}. {key} -> {count}")
        else:
            print("- 无")

        print("\n=== Top 非 Alarm Warning 文本 ===")
        top_warning = non_alarm_warning_counts.most_common(args.top)
        if top_warning:
            for idx, (key, count) in enumerate(top_warning, start=1):
                print(f"{idx}. {key} -> {count}")
        else:
            print("- 无")

        print("\n=== Error 最多的业务日志文件 ===")
        for idx, (name, count) in enumerate(per_file_error.most_common(args.top), start=1):
            print(f"{idx}. {name} -> {count}")

        print("\n=== Warning 最多的业务日志文件 ===")
        for idx, (name, count) in enumerate(per_file_warning.most_common(args.top), start=1):
            print(f"{idx}. {name} -> {count}")

        print("\n=== 系统日志概览 ===")
        print(f"- system_error_like行数    : {system_sev.get('error_like', 0)}")
        print(f"- system_warning_like行数  : {system_sev.get('warning_like', 0)}")
        print(f"- imu_or_serial_related    : {system_hits.get('imu_or_serial_related', 0)}")
        print(f"- fail_or_error_or_timeout : {system_hits.get('fail_or_error_or_timeout', 0)}")
    finally:
        ctx.cleanup()


def run_root_cause(args: argparse.Namespace) -> None:
    """Run per-main-log root-cause classification on the normalized input.

    这一层看的不是整个 zip 的全局错误，而是“每份主日志自己的时间范围”
    与“靠近 odo_update_fail 的同窗证据”。
    """
    # root-cause works per main-log window, not per whole zip.
    # That distinction matters when strong errors live in a different time range.
    ctx = build_context(args.input)
    try:
        logs = main_logs(ctx, args.log_glob)
        warning_logs = side_logs(ctx.warning_dir)
        error_logs = side_logs(ctx.error_dir)
        event_files = warning_logs + error_logs + logs
        events = deduplicate_event_instances(parse_event_logs(event_files))
        global_events = summarize_global_events(events)

        rows: List[LogStats] = []
        all_fail_times = []
        for log_file in logs:
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
                    root_cause_priority=judge_root_cause_priority(
                        lead_encoder_ratio, lead_ethercat_ratio, fail_total, coincident_event_counts
                    ),
                    cause=classify_cause(imu_total, odometer_total, fail_total, coincident_event_counts),
                )
            )

        out_path = Path(args.out) if args.out else None
        if out_path is not None:
            write_root_cause_tsv(out_path, rows)
        report_args = argparse.Namespace(
            warning_dir=str(ctx.warning_dir),
            error_dir=str(ctx.error_dir),
            out=str(out_path) if out_path is not None else "(未写文件)",
        )
        global_coincident_events = count_coincident_events(events, all_fail_times, args.co_window_sec)
        global_overlap_events = count_events_in_ranges_union(
            events, [(r.start_ts, r.end_ts) for r in rows]
        )
        print_human_report(
            rows, report_args, global_events, global_coincident_events, global_overlap_events
        )
    finally:
        ctx.cleanup()


def run_coincident(args: argparse.Namespace) -> None:
    """Extract alarms that occur near odo_update_fail for evidence review.

    这个子命令分两层输出：
    1) 主日志里的同窗原始证据（Alarm + 高信号非 Alarm）
    2) 跨 warning/error/main 的指定事件汇总

    这样既能保留可追溯的原始样例，又不会漏掉只出现在独立 warning/error 文件里的
    驱动侧关键事件。
    """
    # coincident is evidence extraction only: it shows raw nearby evidence,
    # then adds a compact cross-source summary for higher-signal event families.
    ctx = build_context(args.input)
    try:
        logs = main_logs(ctx, args.log_glob)
        hits: List[EventHit] = []
        for log_file in logs:
            hits.extend(analyze_coincident_log(log_file, args.window_sec, args.all_non_alarm))
        if args.out:
            write_coincident_tsv(Path(args.out), hits)
        print_coincident_summary(hits)

        warning_logs = side_logs(ctx.warning_dir)
        error_logs = side_logs(ctx.error_dir)
        events = deduplicate_event_instances(parse_event_logs(warning_logs + error_logs + logs))
        all_fail_times = []
        for log_file in logs:
            (
                _start_ts,
                _end_ts,
                _imu_total,
                _odometer_total,
                _fail_total,
                fail_times,
                _alarms,
                _local_event_times,
            ) = parse_main_log(log_file)
            all_fail_times.extend(fail_times)

        cross_source_counter = Counter(count_coincident_events(events, all_fail_times, args.window_sec))

        print("\n=== 跨日志源同窗指定事件汇总(按事件实例统计) ===")
        has_cross_source = False
        for key in EVENT_PRINT_ORDER:
            count = cross_source_counter.get(key, 0)
            if count <= 0:
                continue
            has_cross_source = True
            print(f"- {EVENT_LABELS[key]:18s}: {count}")
        if not has_cross_source:
            print("- 无")
        if args.out:
            print(f"\n输出文件: {args.out}")
    finally:
        ctx.cleanup()


def run_vel_rotate(args: argparse.Namespace) -> None:
    """Run the specialized vel_rotate diagnostic on one main log.

    这是一个局部专项分析路径，用来区分：
    `vel_rotate` 缺失到底是 0 值省略，还是上游 Odometer 本来就异常。
    """
    # vel-rotate is a narrow diagnostic path kept for field debugging.
    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"log not found: {log_path}")

    start = parse_vel_rotate_ts(args.start) if args.start else None
    end = parse_vel_rotate_ts(args.end) if args.end else None
    stats, sec = analyze_vel_rotate_log(log_path, start, end)
    keyword_hits, keyword_lines = count_keyword_logs([args.warning, args.error_log], start, end)
    print("== 开始分析 ==")
    print(f"主日志: {log_path}")
    if args.warning:
        print(f"warning日志: {args.warning}")
    if args.error_log:
        print(f"error日志: {args.error_log}")
    print(f"时间窗: {args.start or '起点'} ~ {args.end or '终点'}")
    print_vel_rotate_report(stats, sec, keyword_hits, keyword_lines, args.show_seconds)


def main() -> None:
    """Build the CLI and dispatch subcommands.

    main() 的职责很单一：定义参数、把命令路由到对应功能。
    真正的业务逻辑都放在独立函数里，便于阅读、复用和单独测试。
    """
    parser = argparse.ArgumentParser(description="统一 Robokit IMU/Odometer 离线日志分析入口")
    sub = parser.add_subparsers(dest="cmd", required=True)

    summary_p = sub.add_parser("summary", help="汇总 zip/目录内的错误总数、类型和热点文件")
    summary_p.add_argument("--input", required=True, help="zip 文件或包含 log/ 的目录")
    summary_p.add_argument("--top", type=int, default=10, help="Top N，默认10")

    root_p = sub.add_parser("root-cause", help="批量统计 Odometer 失败根因")
    root_p.add_argument("--input", required=True, help="zip 文件或包含 log/ 的目录")
    root_p.add_argument("--log-glob", default="robokit_*.log*", help="主日志匹配模式")
    root_p.add_argument("--co-window-sec", type=float, default=1.0, help="同窗阈值(秒)")
    root_p.add_argument("--lead-window-sec", type=float, default=1.0, help="先后顺序阈值(秒)")
    root_p.add_argument("--out", help="可选 TSV 输出路径")

    coincident_p = sub.add_parser("coincident", help="提取与 odo_update_fail 同窗的 ERROR/WARNING")
    coincident_p.add_argument("--input", required=True, help="zip 文件或包含 log/ 的目录")
    coincident_p.add_argument("--log-glob", default="robokit_*.log*", help="主日志匹配模式")
    coincident_p.add_argument("--window-sec", type=float, default=1.0, help="同窗阈值(秒)")
    coincident_p.add_argument(
        "--all-non-alarm",
        action="store_true",
        help="把所有普通 e/w 行也纳入；默认仅保留 Alarm 与高信号非 Alarm",
    )
    coincident_p.add_argument("--out", help="可选 TSV 输出路径")

    vel_p = sub.add_parser("vel-rotate", help="验证 vel_rotate 缺失是 0 省略还是上游异常")
    vel_p.add_argument("--log", required=True, help="主日志路径（含 Odometer/IMU/odo_update_fail）")
    vel_p.add_argument("--warning", help="warning 日志路径")
    vel_p.add_argument("--error", dest="error_log", help="error 日志路径")
    vel_p.add_argument("--start", help="开始时间，格式: YYMMDD HHMMSS.mmm")
    vel_p.add_argument("--end", help="结束时间，格式: YYMMDD HHMMSS.mmm")
    vel_p.add_argument("--show-seconds", action="store_true", help="输出每秒统计")

    args = parser.parse_args()
    if args.cmd == "summary":
        print_summary(args)
    elif args.cmd == "root-cause":
        run_root_cause(args)
    elif args.cmd == "coincident":
        run_coincident(args)
    elif args.cmd == "vel-rotate":
        run_vel_rotate(args)


if __name__ == "__main__":
    main()
