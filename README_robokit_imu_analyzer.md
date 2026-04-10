# robokit_imu_analyzer 阅读说明

这份说明的目标不是重复源码，而是帮你先建立一个稳定的阅读骨架。

如果你想最快看懂，只看这 4 件事：

1. 入口在哪里
2. 输入先怎么被统一
3. `summary` / `root-cause` / `coincident` / `vel-rotate` 各自负责什么
4. 新增关键词以后应该改哪一处

---

## 1. 这个脚本到底解决什么问题

`robokit_imu_analyzer.py` 是一个统一入口。

它把原来 3 个离线分析脚本的能力收口到一个命令里：

- `summary`
- `root-cause`
- `coincident`
- `vel-rotate`

其中：

- `summary` 负责“先看全包里到底有哪些错误、多少条、热点在哪”
- `root-cause` 负责“按主日志窗口批量判断更像哪类根因”
- `coincident` 负责“提取和 odo_update_fail 同窗的告警”
- `vel-rotate` 负责“判断 vel_rotate 缺失是正常省略还是上游异常”

`imu_uart_tool.py` 没有并进来，是故意的。

原因很简单：

- `robokit_imu_analyzer.py` 处理的是**离线日志**
- `imu_uart_tool.py` 处理的是**在线串口抓包/CRC 校验**

这两个工具的“数据来源”和“使用场景”不一样，硬合并会让命令行变得很乱。

---

## 2. 阅读顺序建议

建议按这个顺序读：

1. [robokit_imu_patterns.py](/home/TOOLS/robokit_imu_patterns.py)
2. [robokit_imu_analyzer.py](/home/TOOLS/robokit_imu_analyzer.py)
3. [odometer_root_cause_stats.py](/home/TOOLS/odometer_root_cause_stats.py)
4. [odometer_coincident_events.py](/home/TOOLS/odometer_coincident_events.py)
5. [odometer_vel_rotate_check.py](/home/TOOLS/odometer_vel_rotate_check.py)

为什么这样读：

- `robokit_imu_patterns.py` 先告诉你“系统到底认识哪些错误类型”
- `robokit_imu_analyzer.py` 再告诉你“统一入口怎么调度这些能力”
- 后面 3 个脚本才是具体算法细节

---

## 3. 数据流怎么走

最重要的函数是：

- `build_context()`

它做的事可以一句话概括：

> 不管你给我的是 zip，还是目录，我都先把它整理成统一的 `log_dir / warning_dir / error_dir` 视图。

这一步做完后，后面的分析逻辑就不用再关心“输入最初是什么形态”。

也就是说，后面的代码都在吃同一种抽象：

```text
InputContext
├── log_dir
├── warning_dir
└── error_dir
```

这是整个脚本最重要的“降复杂度”点。

---

## 4. 四个子命令分别怎么看

### 4.1 `summary`

先看函数：

- `print_summary()`

它做了三层统计：

1. 统计业务日志里的 `error / warning / debug / info` 总量
2. 用 `SUMMARY_PATTERNS` 统计关键异常次数
3. 把 Alarm、普通 Error、普通 Warning 分开做 TopN

你可以把它理解成：

> “这个包先别急着下结论，先把里面到底发生过什么列出来。”

这个命令最适合回答你这类问题：

- 总共有多少报错？
- 哪类错误最多？
- 是不是有我之前漏掉的类型？

这里有 2 个口径要先记住：

1. `summary` 里的“关键异常计数”是**日志命中次数**，不是跨 `main/error/warning` 去重后的唯一事件数。
2. 系统日志现在会纳入：
   - `syslog`
   - `syslog.N`
   - `kern.log`
   - `kern.log.N`

另外，系统日志里的 `system_error_like` / `system_warning_like` 是启发式文本统计，不是 robokit 主日志那种严格的 `[e]/[w]` 级别字段。

---

### 4.2 `root-cause`

先看函数：

- `run_root_cause()`

它不是直接自己重写一套逻辑，而是复用：

- `odometer_root_cause_stats.py`

核心思路是：

1. 找到每份主日志
2. 为每份主日志算：
   - `IMU` 数量
   - `Odometer` 数量
   - `odo_update_fail` 数量
3. 再把同时间范围内命中的事件计数压到 `event_counts`
4. 最后用：
   - `judge_root_cause_priority()`
   - `classify_cause()`

给出优先级和原因

这部分不是“看整个 zip 的全局错误”，而是：

> “站在每个主日志窗口内部，判断这段时间更像什么问题。”

所以你会看到一个现象：

- `summary` 里能看到某些更强的 error
- 但 `root-cause` 不一定把它判成主因

这通常不是脚本坏了，而是：

> 那些更强的 error 发生在别的时间窗，不和当前主日志重叠

这里再补一个这版修过的关键点：

- `主日志范围事件`：指整份主日志时间范围内命中的指定事件
- `同窗指定事件`：指真正落在 `odo_update_fail` 附近窗口内的指定事件
- `同窗 Alarm`：只统计主日志里与 `odo_update_fail` 同窗的 Alarm

这三组数字现在是分开算的，不能再互相减来推导。

---

### 4.3 `coincident`

先看函数：

- `run_coincident()`

它本质上只是批量调用：

- `odometer_coincident_events.analyze_log()`

现在它分两层输出。

第一层是主日志里的原始同窗证据：

- Alarm
- 能命中 `EVENT_PATTERNS` 的高信号非 Alarm `e/w` 行

第二层是跨 `warning/error/main` 的指定事件汇总。

目的很直接：

> 把和 `odo_update_fail` 时间上挨得很近的高信号证据抓出来

这个命令适合做“证据对照”，不适合单独下最终结论。

这版有一个很重要的默认行为变化：

- 默认不会把所有普通 `e/w` 行都纳入
- 默认只保留高信号非 Alarm，避免 `manualcontrol / relocStarted` 这类噪声刷屏
- 如果你确实要看全量普通 `e/w`，再加：

```bash
python3 /home/TOOLS/robokit_imu_analyzer.py coincident --input <zip> --all-non-alarm
```

---

### 4.4 `vel-rotate`

先看函数：

- `run_vel_rotate()`

它复用的是：

- `odometer_vel_rotate_check.py`

这个命令回答的是一个更细的问题：

> `vel_rotate` 没了，到底是因为值为 0 被 proto3 省略，还是因为上游 Odometer 本来就异常？

所以它和 `summary` / `root-cause` 不是一个层级。

它更像“局部专项诊断”。

---

## 5. 关键词要去哪里加

如果以后现场又出现新报错，不要先到 3 个旧脚本里到处搜。

先改这里：

- [robokit_imu_patterns.py](/home/TOOLS/robokit_imu_patterns.py)

这里有两层模式：

### 第一层：`EVENT_PATTERNS`

这是“会参与根因判断”的事件。

比如：

- `kinco_can_err`
- `motor_timeout`
- `odo_data_lost`
- `odo_failed_update`

如果一个新错误会影响“根因分类”，就加到这里。

### 第二层：`SUMMARY_PATTERNS`

这是“只需要在 summary 里被统计出来”的模式。

比如：

- `imu_lines`
- `odometer_lines`
- `odo_update_fail`

如果一个新错误只是想先看到次数，不一定参与分类，也可以只放到这一层。

---

## 6. 你现在最值得看的源码位置

如果你时间只有 10 分钟，优先看这些函数：

- [robokit_imu_analyzer.py](/home/TOOLS/robokit_imu_analyzer.py)：`build_context()`
- [robokit_imu_analyzer.py](/home/TOOLS/robokit_imu_analyzer.py)：`print_summary()`
- [robokit_imu_analyzer.py](/home/TOOLS/robokit_imu_analyzer.py)：`run_root_cause()`
- [odometer_root_cause_stats.py](/home/TOOLS/odometer_root_cause_stats.py)：`classify_cause()`
- [odometer_root_cause_stats.py](/home/TOOLS/odometer_root_cause_stats.py)：`judge_root_cause_priority()`

看懂这 5 个点，整个工具的骨架就差不多了。

---

## 7. 常用命令

```bash
# 1. 先看整包到底有哪些错误
python3 /home/TOOLS/robokit_imu_analyzer.py summary --input /path/to/robokit-Debug.zip

# 2. 再看批量根因判断
python3 /home/TOOLS/robokit_imu_analyzer.py root-cause --input /path/to/robokit-Debug.zip

# 3. 专看和 odo_update_fail 同窗的告警
python3 /home/TOOLS/robokit_imu_analyzer.py coincident --input /path/to/robokit-Debug.zip
```

---

## 8. 一句话心智模型

你可以把这个工具组记成：

```text
patterns 定义“认识什么错误”
summary 负责“全包盘点”
root-cause 负责“窗口归因”
coincident 负责“同窗取证”
vel-rotate 负责“专项细查”
```

先记住这个，再回去读源码，会快很多。
