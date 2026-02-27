#!/usr/bin/env python3
"""
IMU UART frame verifier.

Protocol assumptions from firmware:
1) A frame is fixed length (default 68 bytes).
2) Header bytes are: A5 5A 01 3C
   - A5 5A: sync
   - 01: data type
   - 3C: payload length (60 bytes)
3) Last 4 bytes are CRC32 (little-endian uint32).
4) CRC algorithm matches firmware implementation:
   - polynomial reflected form 0xEDB88320
   - init value = 1
   - no final xor

中文说明（语法重点）：
1) `bytes` 支持下标访问：`data[i]` 返回 0~255 的整数。
2) 切片 `data[a:b]` 是左闭右开区间，包含 `a` 不包含 `b`。
3) `struct.unpack_from("<I", buf, off)`：
   - `<` 表示小端字节序（little-endian）
   - `I` 表示无符号 32 位整数（uint32）
   - `off` 表示从缓冲区第几个字节开始解包
4) Python 位运算：
   - `^` 按位异或
   - `>>` 右移
   - `& 0xFF` 取低 8 位（等价于对 256 取模）
"""

import argparse
import struct
from pathlib import Path


def build_crc32_table():
    """
    Build 256-entry CRC32 lookup table.

    Why 256:
    - Each input byte has 256 possible values (0..255).
    - Table lets us update CRC one byte at a time quickly.

    中文语法解释：
    - `for i in range(256)` 会产生 0..255。
    - `for _ in range(8)` 中 `_` 只是“占位变量”，表示循环变量不需要被使用。
    """
    table = []
    for i in range(256):
        # Start from one possible byte value.
        c = i
        for _ in range(8):
            # Shift one bit each round.
            # If LSB is 1, apply polynomial xor after shift.
            c = 0xEDB88320 ^ (c >> 1) if (c & 1) else (c >> 1)
        table.append(c)
    return table


# Build once at import time; reused by every crc32_fw() call.
CRC32_TAB = build_crc32_table()


def crc32_fw(buf, crc=1):
    """
    Firmware-compatible CRC32.

    Parameters:
    - buf: bytes-like object to checksum
    - crc: initial value (firmware uses 1)

    Expression detail:
    - (crc ^ b) & 0xFF: select lookup index using low 8 bits.
    - crc >> 8: consume one processed byte.
    - table[index] ^ (crc >> 8): standard reflected CRC update form.

    中文语法解释：
    - `for b in buf`：`buf` 是 bytes 时，每次循环得到 0~255 的 int。
    - `crc = ...`：Python int 无固定 32 位上限，因此最后用 `& 0xFFFFFFFF` 截断到 32 位。
    """
    for b in buf:
        crc = CRC32_TAB[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF


def analyze_file(path: Path, frame_size=68):
    """
    Scan full binary file, validate candidate frames, and extract metadata.

    Returns:
    - ok: number of valid frames (header + CRC pass)
    - bad: number of header hits whose CRC failed
    - ts: extracted trans_timestamp list from valid frames
    - offsets: file offsets of valid frames

    中文语法解释：
    - 类型标注 `path: Path` 只是“提示”，不会在运行时强制类型检查。
    - `while i + frame_size <= len(data)` 是边界保护，避免切片越界。

    接口调用说明：
    - path.read_bytes():
      - 入参: 无（path 已包含路径）
      - 返回: bytes
      - 失败: 文件不存在/权限不足会抛异常
    - struct.unpack_from("<I", buffer, offset):
      - 入参: 格式字符串、字节缓冲、偏移
      - 返回: tuple（本例取[0]）
      - 失败: 偏移越界会抛 struct.error
    """
    # Path.read_bytes() loads entire file as one bytes object.
    data = path.read_bytes()
    ok = 0
    bad = 0
    ts = []
    offsets = []

    # Sliding scan:
    # move byte-by-byte until we find a candidate header,
    # then validate full frame.
    i = 0
    while i + frame_size <= len(data):
        if data[i] == 0xA5 and data[i + 1] == 0x5A and data[i + 2] == 0x01 and data[i + 3] == 0x3C:
            # bytes slicing: [start:end], end excluded.
            # 中文：frm 长度是 frame_size；等价于“从 i 开始取 frame_size 个字节”。
            frm = data[i:i + frame_size]

            # CRC over frame except its final CRC field.
            # 中文：frm[:-4] 表示“从头到倒数第4个字节之前”，即去掉尾部 CRC4 字节。
            c_calc = crc32_fw(frm[:-4], 1)

            # struct.unpack_from(fmt, buffer, offset):
            # - "<I" means little-endian unsigned 32-bit int.
            # - offset = frame_size - 4 points to stored CRC field.
            # 中文：返回值是 tuple，所以要用 [0] 取第一个字段。
            c_recv = struct.unpack_from("<I", frm, frame_size - 4)[0]
            if c_calc == c_recv:
                ok += 1
                # trans_timestamp offset:
                # 2(head)+1(type)+1(len)+52(data before timestamp) = 56
                # 中文：仍然是 `<I`，因为 timestamp 在协议里也是 uint32 小端。
                ts.append(struct.unpack_from("<I", frm, 56)[0])
                offsets.append(i)
                # Fast-path step: valid fixed-length frame found.
                # 中文：既然这一帧合法，下一帧候选点直接跳到 i+frame_size。
                i += frame_size
                continue
            bad += 1
        # No valid frame here, shift by 1 byte and keep scanning.
        i += 1

    return ok, bad, ts, offsets


def main():
    # argparse creates a CLI parser and auto-generates --help text.
    parser = argparse.ArgumentParser(description="Check IMU UART frames by CRC32 and timestamp continuity.")
    parser.add_argument(
        "-i",
        "--input",
        default="/tmp/ttyS5.bin",
        help="Input binary capture file path (default: /tmp/ttyS5.bin)",
    )
    parser.add_argument(
        "--frame-size",
        type=int,
        default=68,
        help="Frame size in bytes (default: 68)",
    )

    # parse_args() converts command-line options to attributes:
    # --frame-size -> args.frame_size (dash becomes underscore).
    # 中文：argparse 会自动做类型转换（这里 --frame-size -> int）。
    args = parser.parse_args()
    # 接口说明（argparse）：
    # - parse_args() 从命令行读取参数
    # - 返回 Namespace，可通过属性访问如 args.input/args.frame_size

    path = Path(args.input)
    if not path.exists():
        # Raise readable CLI error and exit non-zero.
        raise SystemExit(f"input file not found: {path}")

    ok, bad, ts, offsets = analyze_file(path, args.frame_size)
    print(f"file={path}")
    print(f"valid_frames={ok}, crc_fail_hits={bad}")

    if ok > 1:
        # zip(ts, ts[1:]) pairs adjacent timestamps: (t0,t1), (t1,t2), ...
        # 中文：这是“相邻元素配对”常见写法，便于计算单调性/差分。
        mono = sum(1 for a, b in zip(ts, ts[1:]) if b >= a)
        print(f"timestamp_monotonic_ratio={mono}/{len(ts) - 1}")

        # List comprehension collects per-frame timestamp increments.
        # 中文：列表推导式语法 [expr for x in iter if cond]。
        deltas = [b - a for a, b in zip(ts, ts[1:])]
        if deltas:
            print(f"timestamp_delta_min={min(deltas)}, max={max(deltas)}")

        # Offsets of valid frames should often step by frame_size if stream is clean.
        frame_gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        if frame_gaps:
            print(f"offset_gap_min={min(frame_gaps)}, max={max(frame_gaps)}")


# This guard means:
# - run main() only when executed as a script
# - do not run main() when imported as a module
if __name__ == "__main__":
    main()
