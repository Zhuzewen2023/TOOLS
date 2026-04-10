# robokit_imu_analyzer - 开发进度

**创建日期：** 2026-03-11  
**最后更新：** 2026-03-12 00:16  
**目标：** 统一 Robokit IMU/Odometer 离线日志分析入口，并补齐当前案例缺失的错误模式覆盖。  
**类型：** 多文件项目

---

## 当前进度
✅ 步骤 1：读取历史记录并确认现有脚本边界
✅ 步骤 2：全量统计两份 zip 的错误总数与类型
✅ 步骤 3：新增统一入口脚本并补强根因分类
✅ 步骤 4：回归验证两份 zip 并做 Python 语法检查
✅ 步骤 5：修复同窗事件口径、系统日志读取和关键词命中缺陷
✅ 步骤 6：为统一分析工具链的每个函数补齐 docstring 注释
✅ 步骤 7：为 IMU UART 抓包/CRC 工具的每个函数补齐 docstring 注释
✅ 步骤 8：继续第二轮审查，修复 `coincident` 默认噪声过大并补上跨日志源同窗汇总
✅ 步骤 9：修复轮转日志遗漏、多事件同行漏计、跨主日志重复累加和 UART timestamp 偏移写死问题
✅ 步骤 10：统一 `cause/priority` 判定顺序，并把主日志范围统计改成按时间并集计数
✅ 步骤 11：把 root-cause/coincident 的跨文件镜像事件切到“事件实例去重后再统计”
✅ 步骤 12：修复 zip 外层目录兼容、事件去重过粗、UART header 透传和 `capture_bytes` 统计口径

---

## 下一步
1. 如果现场继续出现“同一事件在 main/error/warning 重复落点”的样本，再决定是否做跨日志去重。
2. 如确认统一入口稳定，可逐步把旧脚本文档入口切换到 `robokit_imu_analyzer.py`。
3. 如需要，再补 `summary` 的 TSV/JSON 导出。
4. 如需要，可继续统一整理这两类工具的注释风格，比如把“接口说明”和 docstring 再收敛成同一模板。
5. 如要进一步压测，可固定一份解压样例目录，补 3 到 5 个最小回归样本。
6. 如要把 UART 校验器做成更通用工具，可继续把 header/type/length 也参数化，而不只开放 `frame_size/timestamp_offset`。
7. 如现场出现“同模块、同毫秒、同正文”的真实双事件，再决定是否把事件实例键进一步扩成正文+来源位置。

---

## 遇到的问题
- 当前 `odometer_root_cause_stats.py` 只覆盖 `EtherCAT/encoder/DIO/no odom/Transform fail`，对当前案例命中不足。
- `imu_uart_tool.py` 属于串口抓包工具，不适合与离线日志分析逻辑硬合并。
- `2026-03-10` 的更强告警主要出现在更早的 error 日志窗口，和 `11:09+` 主日志不完全重叠，所以 `root-cause` 仍会保守地优先判到 `KINCO驱动链路优先`。
- 旧版本里把“主日志时间范围事件”和“与 odo_update_fail 同窗事件”混在一起计算，导致 `同窗总数=0` 但 `同窗指定事件数很大` 的假象。
- 旧版本 zip 只抽取 `*.log`，会漏掉 `syslog`、`syslog.1`、`kern.log.1` 这类系统日志。
- `odometer_vel_rotate_check.py` 里 `Transform fail` 因大小写不一致而完全漏命中。
- `coincident` 一度把所有普通 `e/w` 行都纳入，虽然不再漏 KINCO，但会被 `manualcontrol/relocStarted` 等低信号文本淹没。
- 统一入口 `coincident` 之前只展示主日志原始同窗样例，不顺手暴露 warning/error 里的同窗指定事件，读者容易误以为“外部日志没证据”。
- `warning/error` 目录若出现 `*.log.1` 轮转文件，旧版本会直接漏读。
- 一行日志若同时命中多个事件家族，旧版本只保留第一个，后面的高信号事件会被吃掉。
- 跨日志源同窗指定事件旧版本按“每份主日志窗口”累加，多个主日志覆盖同一事件时会重复计数。
- `imu_uart_crc_check.py` 虽然暴露了 `--frame-size`，但 timestamp 偏移旧版本仍写死为 56。
- `classify_cause()` 与 `judge_root_cause_priority()` 旧版本在组合事件下优先级顺序不一致，会出现“原因”和“主因优先级”互相打架。
- “主日志时间范围事件统计”旧版本按每份主日志窗口累加，主日志时间重叠时会把同一事件实例重复统计。
- 同一运行时事件如果同时落到 `main + warning/error`，旧版本会在根因分析层被重复视为两条事件实例。
- 统一入口旧版本要求 zip 顶层直接是 `log/warning/error`，遇到外层包目录会把整包误判成空包。
- 事件实例键旧版本把模块维度也去掉了，会把“同毫秒、同文案、不同模块”的独立事件压成一条。
- UART `find/all` 与 `check` 旧版本使用不同 header 来源，自定义帧头时会出现“能找到但校验不到”。
- `capture_bytes()` 旧版本按 `bs*count` 回报抓包大小，遇到短输入会把字节数报大。

---

## 已完成的代码

### 新增文件
- `/home/TOOLS/robokit_imu_patterns.py`
- `/home/TOOLS/robokit_imu_analyzer.py`
- `/home/TOOLS/README_robokit_imu_analyzer.md`

### 修改文件
- `/home/TOOLS/imu_uart_tool.py`
- `/home/TOOLS/imu_uart_crc_check.py`
- `/home/TOOLS/odometer_root_cause_stats.py`
- `/home/TOOLS/odometer_coincident_events.py`
- `/home/TOOLS/odometer_vel_rotate_check.py`
- `/home/TOOLS/robokit_imu_patterns.py`
- `/home/TOOLS/robokit_imu_analyzer.py`
- `/home/TOOLS/README_robokit_imu_analyzer.md`

### 验证
- `python3 -m py_compile /home/TOOLS/robokit_imu_patterns.py /home/TOOLS/robokit_imu_analyzer.py /home/TOOLS/odometer_root_cause_stats.py /home/TOOLS/odometer_coincident_events.py /home/TOOLS/odometer_vel_rotate_check.py /home/TOOLS/imu_uart_tool.py /home/TOOLS/imu_uart_crc_check.py`
- `python3 /home/TOOLS/robokit_imu_analyzer.py summary --input <zip>`
- `python3 /home/TOOLS/robokit_imu_analyzer.py root-cause --input <zip>`
- `python3 /home/TOOLS/robokit_imu_analyzer.py --help`
- `python3 - <<'PY' ... odometer_vel_rotate_check.count_keyword_logs(...) ... PY`
- `python3 - <<'PY' ... robokit_imu_analyzer.should_extract_member(...) ... PY`
- `python3 - <<'PY' ... ast.get_docstring(...) 检查所有函数均有 docstring ... PY`
- `python3 -m py_compile /home/TOOLS/imu_uart_tool.py /home/TOOLS/imu_uart_crc_check.py`
- `python3 - <<'PY' ... odometer_coincident_events.analyze_log(..., include_all_non_alarm=False/True) ... PY`
- `python3 - <<'PY' ... build_context()/business_logs() 验证 *.log.1 被正确纳入 ... PY`
- `python3 - <<'PY' ... parse_event_logs()/analyze_log() 验证一行双事件可同时计数 ... PY`
- `python3 - <<'PY' ... count_coincident_events(all_fail_times) 验证跨主日志重复累计已可避免 ... PY`
- `python3 - <<'PY' ... analyze_file(frame_size, timestamp_offset) 验证 timestamp 偏移可配置 ... PY`
- `python3 - <<'PY' ... classify_cause()/judge_root_cause_priority() 验证组合事件优先级一致 ... PY`
- `python3 - <<'PY' ... count_events_in_ranges_union() 验证主日志重叠窗口去重 ... PY`
- `python3 - <<'PY' ... deduplicate_event_instances() 验证 main+warning 镜像事件不再双计 ... PY`
- `python3 - <<'PY' ... build_context() 验证 zip 外层包目录仍可正确发现 log/ ... PY`
- `python3 - <<'PY' ... deduplicate_event_instances() 验证同毫秒同文案但不同模块事件不再误合并 ... PY`
- `python3 - <<'PY' ... capture_bytes()/check_file() 验证实际抓包字节数与自定义 header 校验已修正 ... PY`
- `python3 /home/TOOLS/imu_uart_tool.py check -h`
- `python3 /home/TOOLS/imu_uart_tool.py all -h`
