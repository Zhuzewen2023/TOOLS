#!/usr/bin/env python3
import argparse
import struct
from pathlib import Path


def build_crc32_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = 0xEDB88320 ^ (c >> 1) if (c & 1) else (c >> 1)
        table.append(c)
    return table


CRC32_TAB = build_crc32_table()


def crc32_fw(buf, crc=1):
    for b in buf:
        crc = CRC32_TAB[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF


def analyze_file(path: Path, frame_size=68):
    data = path.read_bytes()
    ok = 0
    bad = 0
    ts = []
    offsets = []

    i = 0
    while i + frame_size <= len(data):
        if data[i] == 0xA5 and data[i + 1] == 0x5A and data[i + 2] == 0x01 and data[i + 3] == 0x3C:
            frm = data[i:i + frame_size]
            c_calc = crc32_fw(frm[:-4], 1)
            c_recv = struct.unpack_from("<I", frm, frame_size - 4)[0]
            if c_calc == c_recv:
                ok += 1
                ts.append(struct.unpack_from("<I", frm, 56)[0])  # trans_timestamp
                offsets.append(i)
                i += frame_size
                continue
            bad += 1
        i += 1

    return ok, bad, ts, offsets


def main():
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
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        raise SystemExit(f"input file not found: {path}")

    ok, bad, ts, offsets = analyze_file(path, args.frame_size)
    print(f"file={path}")
    print(f"valid_frames={ok}, crc_fail_hits={bad}")

    if ok > 1:
        mono = sum(1 for a, b in zip(ts, ts[1:]) if b >= a)
        print(f"timestamp_monotonic_ratio={mono}/{len(ts) - 1}")

        deltas = [b - a for a, b in zip(ts, ts[1:])]
        if deltas:
            print(f"timestamp_delta_min={min(deltas)}, max={max(deltas)}")

        frame_gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        if frame_gaps:
            print(f"offset_gap_min={min(frame_gaps)}, max={max(frame_gaps)}")


if __name__ == "__main__":
    main()
