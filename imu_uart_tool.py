#!/usr/bin/env python3
"""
Reusable UART workflow tool for IMU bring-up.

Subcommands:
- show   : read current tty settings
- setup  : configure tty parameters (baud, 8N1, raw, flow control)
- capture: capture UART bytes to a file (time-based or size-based)
- find   : locate frame headers in captured binary
- dump   : show hex around specific offsets
- check  : run protocol-level CRC/timestamp validation
- all    : one-shot setup + capture + find + check

中文说明（语法重点）：
1) 这是一个“子命令式”CLI：`python imu_uart_tool.py <cmd> [options]`
2) 通过 argparse 的 subparsers 实现类似 git 的命令风格。
3) 外部命令（stty/dd/timeout）由 subprocess 调用，不依赖 shell 拼接字符串。
"""

import argparse
import subprocess
from pathlib import Path

from imu_uart_crc_check import analyze_file
# 接口说明：
# analyze_file(path, frame_size) -> (ok, bad, ts, offsets)
# - ok: CRC通过的帧数
# - bad: 命中帧头但CRC失败次数
# - ts: 每个有效帧的时间戳列表
# - offsets: 每个有效帧在文件中的字节偏移


def run_cmd(cmd):
    """
    Run command and raise if exit code is non-zero.

    subprocess.run(..., check=True) will throw CalledProcessError on failure.
    capture_output=True keeps stdout/stderr for controlled printing.

    中文语法解释：
    - `cmd` 是 list（如 ["stty", "-F", "/dev/ttyS5", "-a"]），
      这样可以避免 shell 转义问题。
    - `text=True` 让 stdout/stderr 以字符串返回，而不是 bytes。

    接口调用约定（很关键）：
    - 入参 cmd: List[str]，例如 ["stty", "-F", "/dev/ttyS5", "-a"]。
    - 返回值: subprocess.CompletedProcess，常用字段：
      - returncode: 退出码
      - stdout: 标准输出
      - stderr: 标准错误
    - 失败行为: 因为 check=True，非0退出码会直接抛异常并中断流程。
    """
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def stty_show(device):
    # Equivalent to: stty -F /dev/ttyS5 -a
    # 中文：device 是字符串路径，例如 "/dev/ttyS5"。
    # 接口说明（stty）：
    # -F <dev> 指定操作的TTY设备文件
    # -a 输出当前设备全部串口参数
    res = run_cmd(["stty", "-F", device, "-a"])
    print(res.stdout.strip())


def stty_setup(device, baud, hw_flow):
    # Common IMU binary protocol baseline: 8N1 + raw + no software flow.
    cmd = [
        "stty",
        "-F",
        device,
        str(baud),
        "cs8",
        "-cstopb",
        "-parenb",
        "-ixon",
        "-ixoff",
        "raw",
        "-echo",
    ]
    # Switch hardware flow control by flag.
    # 中文语法：三元表达式 `A if cond else B`。
    cmd.append("crtscts" if hw_flow else "-crtscts")
    # 接口说明（stty 设置项）：
    # - cs8: 8 data bits
    # - -parenb: 无奇偶校验
    # - -cstopb: 1 stop bit
    # - raw: 原始模式（关闭行缓冲/行编辑）
    # - -ixon -ixoff: 关闭软件流控
    # - crtscts/-crtscts: 开启/关闭硬件流控
    run_cmd(cmd)
    print(f"configured {device}: {baud} 8N1 raw hw_flow={'on' if hw_flow else 'off'}")


def capture_seconds(device, output, seconds, bs):
    # timeout protects against endless read when UART keeps streaming.
    # 中文：f"{seconds}s" 是 f-string，运行时把变量 seconds 插入字符串。
    run_cmd(
        [
            "timeout",
            f"{seconds}s",
            "dd",
            f"if={device}",
            f"of={output}",
            f"bs={bs}",
            "status=none",
        ]
    )
    # 接口说明（timeout + dd）：
    # - timeout Ns <cmd>: 最多运行N秒，超时会终止子进程
    # - dd if=<in> of=<out> bs=<size>: 从输入设备拷贝原始字节到文件
    # - status=none: 不打印dd统计信息
    print(f"captured {seconds}s to {output}")


def capture_bytes(device, output, bs, count):
    # Size-based capture: total bytes = bs * count
    # 中文：这里不加 timeout，dd 在读满 count 个块后自然退出。
    run_cmd(
        [
            "dd",
            f"if={device}",
            f"of={output}",
            f"bs={bs}",
            f"count={count}",
            "status=none",
        ]
    )
    # 接口说明（dd count）：
    # 读取块数为 count，每块 bs 字节，总字节约等于 bs*count。
    print(f"captured {bs * count} bytes to {output}")


def find_offsets(data, pattern, limit):
    """
    Find all (or first N) occurrences of byte pattern in bytes object.

    data.find(pattern, start) returns first index >= start, or -1.

    中文语法解释：
    - `pattern` 必须是 bytes，比如 b"\xA5\x5A\x01"。
    - `i += 1` 表示允许重叠匹配；如果不需要重叠可改成 `i += len(pattern)`。

    接口说明：
    - data: bytes（完整抓包内容）
    - pattern: bytes（例如 b"\\xA5\\x5A\\x01"）
    - limit: 最多返回多少个偏移（<=0表示不限制）
    - 返回: List[int]，每个元素是匹配起始字节偏移
    """
    out = []
    i = 0
    while True:
        i = data.find(pattern, i)
        if i < 0:
            break
        out.append(i)
        i += 1
        if limit > 0 and len(out) >= limit:
            break
    return out


def hex_dump_slice(data, start, count):
    """
    Minimal xxd-like hexdump.

    Output columns:
    - left : row offset inside this slice
    - mid  : hex bytes
    - right: printable ASCII, others replaced by '.'

    中文语法解释：
    - `f"{b:02x}"`：按 2 位十六进制输出，不足补 0。
    - `32 <= b <= 126`：判断是否可打印 ASCII。

    接口说明：
    - 该函数只负责打印，不返回值（返回 None）。
    - start 是绝对起始偏移，count 是希望输出的字节数。
    """
    chunk = data[start : start + count]
    for row in range(0, len(chunk), 16):
        line = chunk[row : row + 16]
        hx = " ".join(f"{b:02x}" for b in line)
        ascii_s = "".join(chr(b) if 32 <= b <= 126 else "." for b in line)
        print(f"{row:08x}: {hx:<47}  {ascii_s}")


def check_file(path, frame_size):
    # Delegate protocol parsing/CRC checks to shared verifier module.
    # 中文：这里把“协议细节”集中在 imu_uart_crc_check.py，避免重复代码。
    # 接口说明：
    # - path: Path 对象，指向抓包bin文件
    # - frame_size: 协议帧总长度（默认68）
    ok, bad, ts, offsets = analyze_file(path, frame_size)
    print(f"file={path}")
    print(f"valid_frames={ok}, crc_fail_hits={bad}")
    if ok > 1:
        mono = sum(1 for a, b in zip(ts, ts[1:]) if b >= a)
        print(f"timestamp_monotonic_ratio={mono}/{len(ts)-1}")
        deltas = [b - a for a, b in zip(ts, ts[1:])]
        if deltas:
            print(f"timestamp_delta_min={min(deltas)}, max={max(deltas)}")
        frame_gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        if frame_gaps:
            print(f"offset_gap_min={min(frame_gaps)}, max={max(frame_gaps)}")


def main():
    # Top-level CLI parser.
    # 中文：description 会出现在 `-h` 的帮助标题里。
    parser = argparse.ArgumentParser(description="Reusable IMU UART capture/probe/check tool.")

    # Subparsers create git-style commands: tool.py <subcommand> [options]
    # 中文：dest="cmd" 表示解析后子命令名保存到 args.cmd。
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Shared options reused by multiple subcommands.
    # 中文：add_help=False 避免把 -h 重复注入到父解析器。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--device", default="/dev/ttyS5")
    common.add_argument("-o", "--output", default="/tmp/ttyS5.bin")

    # Keep parser objects (show_p etc.) even if not referenced later,
    # because add_argument side effects are needed to define CLI schema.
    # 中文：这里“变量未再使用”是正常的，目的是注册命令参数定义。
    show_p = sub.add_parser("show", parents=[common], help="Show current tty config")  # noqa: F841
    setup_p = sub.add_parser("setup", parents=[common], help="Set tty config")
    setup_p.add_argument("--baud", type=int, default=115200)
    setup_p.add_argument("--hw-flow", action="store_true", help="Enable RTS/CTS")

    capture_p = sub.add_parser("capture", parents=[common], help="Capture UART bytes")
    # 中文：互斥组要求 --seconds 和 --count 只能二选一，且必须给一个。
    capture_group = capture_p.add_mutually_exclusive_group(required=True)
    capture_group.add_argument("--seconds", type=int)
    capture_group.add_argument("--count", type=int, help="dd count blocks")
    capture_p.add_argument("--bs", type=int, default=4096)

    find_p = sub.add_parser("find", parents=[common], help="Find frame headers")
    find_p.add_argument("--pattern", default="a55a01", help="Hex bytes, no spaces")
    find_p.add_argument("--limit", type=int, default=10)

    dump_p = sub.add_parser("dump", parents=[common], help="Dump bytes around offsets")
    dump_p.add_argument("--offsets", required=True, help="Comma-separated offsets")
    dump_p.add_argument("--pre", type=int, default=8)
    dump_p.add_argument("--count", type=int, default=64)

    check_p = sub.add_parser("check", parents=[common], help="CRC/timestamp check")
    check_p.add_argument("--frame-size", type=int, default=68)

    all_p = sub.add_parser("all", parents=[common], help="Setup + capture + find + check")
    all_p.add_argument("--baud", type=int, default=115200)
    all_p.add_argument("--seconds", type=int, default=3)
    all_p.add_argument("--bs", type=int, default=4096)
    all_p.add_argument("--pattern", default="a55a01")
    all_p.add_argument("--limit", type=int, default=10)
    all_p.add_argument("--frame-size", type=int, default=68)
    all_p.add_argument("--hw-flow", action="store_true", help="Enable RTS/CTS")

    # argparse converts options to attributes:
    # --frame-size -> args.frame_size
    # --hw-flow -> args.hw_flow
    # 中文：带短参数的如 -d 也会落到同一个属性 args.device。
    args = parser.parse_args()

    if args.cmd == "show":
        stty_show(args.device)
        return

    if args.cmd == "setup":
        stty_setup(args.device, args.baud, args.hw_flow)
        return

    if args.cmd == "capture":
        if args.seconds is not None:
            capture_seconds(args.device, args.output, args.seconds, args.bs)
        else:
            capture_bytes(args.device, args.output, args.bs, args.count)
        return

    if args.cmd == "find":
        # Read captured binary and parse hex string pattern.
        # 接口说明（Path.read_bytes）：
        # - 一次性读取整个文件到 bytes
        # - 适合小文件，超大文件可改流式处理
        data = Path(args.output).read_bytes()
        # bytes.fromhex("a55a01") -> b"\xA5\x5A\x01"
        # 中文：输入不能带 0x 前缀；可以带空格，如 "a5 5a 01" 也可解析。
        # 接口说明（bytes.fromhex）：
        # - 入参是十六进制文本
        # - 返回 bytes；格式非法会抛 ValueError
        pattern = bytes.fromhex(args.pattern)
        offsets = find_offsets(data, pattern, args.limit)
        for off in offsets:
            print(off)
        return

    if args.cmd == "dump":
        data = Path(args.output).read_bytes()
        # "6564,6904" -> [6564, 6904]
        # 中文语法：split(',') 后再 strip() 去空格，最后 int() 转整数。
        offs = [int(x.strip()) for x in args.offsets.split(",") if x.strip()]
        for off in offs:
            print(f"==== offset={off} ====")
            # Avoid negative slice start when off < pre.
            # 中文：max(off-pre, 0) 是边界保护，避免出现负索引误解读。
            start = max(off - args.pre, 0)
            hex_dump_slice(data, start, args.count)
        return

    if args.cmd == "check":
        # 接口说明：复用统一校验入口。
        check_file(Path(args.output), args.frame_size)
        return

    if args.cmd == "all":
        # 中文：all 是工作流编排命令，把常用步骤串起来一次执行。
        stty_setup(args.device, args.baud, args.hw_flow)
        capture_seconds(args.device, args.output, args.seconds, args.bs)
        data = Path(args.output).read_bytes()
        pattern = bytes.fromhex(args.pattern)
        offsets = find_offsets(data, pattern, args.limit)
        print("header_offsets=", ",".join(str(x) for x in offsets))
        check_file(Path(args.output), args.frame_size)
        return


# Script entry point guard.
if __name__ == "__main__":
    main()
