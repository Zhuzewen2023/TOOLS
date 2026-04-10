#!/usr/bin/env python3
"""Shared patterns for Robokit IMU/Odometer log analysis."""

import re
from collections import OrderedDict
from typing import List


# These patterns participate in root-cause classification.
# If a new field issue should影响“结论怎么判”，优先加到这里。
EVENT_PATTERNS = OrderedDict(
    [
        ("ethercat_timeout", re.compile(r"EtherCAT Motor timeout", re.IGNORECASE)),
        ("encoder_timeout", re.compile(r"out encoder timeout", re.IGNORECASE)),
        ("dio_disconnect", re.compile(r"can not connect to DIO board", re.IGNORECASE)),
        ("no_odom", re.compile(r"no odom", re.IGNORECASE)),
        ("transform_fail", re.compile(r"Transform fail", re.IGNORECASE)),
        ("robot_out_of_path", re.compile(r"robot out of path", re.IGNORECASE)),
        ("pgv_cannot_find_code", re.compile(r"PGV cannot find code", re.IGNORECASE)),
        ("kinco_can_err", re.compile(r"KINCO_CAN_ERR_CODE_DATA", re.IGNORECASE)),
        (
            "odo_failed_update",
            re.compile(r"Odometer FAILED to update using the Message_MotorInfos", re.IGNORECASE),
        ),
        (
            "odo_not_updated_500ms",
            re.compile(r"odo has not been updated for 500 ms", re.IGNORECASE),
        ),
        (
            "reset_prev_frame",
            re.compile(r"Reset previous frame because dt and encoder dt", re.IGNORECASE),
        ),
        ("motor_timeout", re.compile(r"Motor Timeout", re.IGNORECASE)),
        ("odo_data_lost", re.compile(r"odo data lost", re.IGNORECASE)),
        ("motor_error", re.compile(r"Motor Error:", re.IGNORECASE)),
        ("robot_blocked", re.compile(r"robot is blocked", re.IGNORECASE)),
        ("robot_slipping", re.compile(r"robot is slip(?:ping|pling)", re.IGNORECASE)),
    ]
)

EVENT_LABELS = {
    "ethercat_timeout": "EtherCAT timeout",
    "encoder_timeout": "编码器 timeout",
    "dio_disconnect": "DIO 断连",
    "no_odom": "no odom",
    "transform_fail": "Transform fail",
    "robot_out_of_path": "robot out of path",
    "pgv_cannot_find_code": "PGV cannot find",
    "kinco_can_err": "KINCO CAN ERR",
    "odo_failed_update": "Odometer FAILED",
    "odo_not_updated_500ms": "odo 500ms stale",
    "reset_prev_frame": "Reset previous frame",
    "motor_timeout": "Motor Timeout",
    "odo_data_lost": "odo data lost",
    "motor_error": "Motor Error",
    "robot_blocked": "robot blocked",
    "robot_slipping": "robot slipping",
}

EVENT_PRINT_ORDER = list(EVENT_PATTERNS.keys())

# Summary also counts a few baseline health signals that are not "error types".
# 例如 IMU/Odometer/odo_update_fail 的总量，它们更像体征，不是具体告警。
SUMMARY_PATTERNS = OrderedDict(
    [
        ("imu_lines", re.compile(r"\[IMU\]\[")),
        ("odometer_lines", re.compile(r"\[Odometer\]\[")),
        ("odo_update_fail", re.compile(r"\[odo_update_fail\]\[")),
    ]
)
SUMMARY_PATTERNS.update(EVENT_PATTERNS)

ROTATED_LOG_NAME_RE = re.compile(r".*\.log(?:\.\d+)?$")
BUSINESS_LOG_NAME_RE = re.compile(r"^(?:robokit|RobodPro|Roboshop|crash).*\.log(?:\.\d+)?$")
SYSTEM_LOG_NAME_RE = re.compile(r"^(?:syslog(?:\.\d+)?|kern\.log(?:\.\d+)?)$")


def matching_event_names(text: str) -> List[str]:
    """Return all known event families that match one log line or message text.

    有些现场日志会在同一行同时带上两个高信号关键词，比如
    `KINCO_CAN_ERR_CODE_DATA` 和 `Motor Timeout`。
    旧实现只记录第一个命中项，会把后面的事件家族直接吃掉。
    """
    matches: List[str] = []
    for event_name, pattern in EVENT_PATTERNS.items():
        if pattern.search(text):
            matches.append(event_name)
    return matches
