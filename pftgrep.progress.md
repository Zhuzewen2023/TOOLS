# pftgrep - 开发进度

**创建日期：** 2026-03-25  
**最后更新：** 2026-03-25 14:06  
**目标：** 把 `.pft.zst` 二进制 trace 包装成接近 `grep` 手感的现场工具，优先解决 IMU 筛选不顺手的问题。  
**类型：** 单文件脚本

---

## 当前进度
✅ 步骤 1：读取软件入口、调试经验索引和 DSPChassis/IMU 链路文档
✅ 步骤 2：确认 `.pft.zst` 实际是 `zstd + Perfetto protobuf`，必须走 `zstdcat + strings + grep`
✅ 步骤 3：确定脚本放在 `/home/TOOLS`，采用单文件命令行工具方案
✅ 步骤 4：创建 `pftgrep` 并补齐 `grep -ar` 兼容接口与 IMU 预设
✅ 步骤 5：完成本地语法检查、帮助输出验证和远端安装
✅ 步骤 6：按现场反馈补上 `.pft.zst` 默认降噪，过滤 `msgImuInstallInfo` 这类 schema 垃圾

---

## 下一步
1. 如果现场后面更常搜“指标名”而不是泛词，可再补 `--name-only` 或更细的噪声白名单。
2. 如果后面需要把命中的 trace 片段导成文本，可再补 `--dump` 输出文件。
3. 如果确认会长期用，后续再决定是否给 `/usr/local/bin/pftgrep` 增加 man/help 示例。

---

## 遇到的问题
- `.pft.zst` 不是文本文件，不能直接 `grep`。
- 直接 `zstdcat | grep` 容易得到乱码或空结果。
- 现场真正需要的是“顺手命令”，不是再套一个重分析工具。
- `grep -ar "IMU"` 这类泛词可以工作，但会把 schema/配置字符串也带出来，噪声明显高于 `ImuTimingMetrics` 这类精确关键词。
- `grep -ar "Imu"` 在 `.pft.zst` 里容易只命中 `msgImuInstallInfo` 这类 protobuf 类型名，因此已默认过滤 schema-only 字符串，并保留 `--no-filter` 供排查原始内容。

---

## 已完成的代码

### pftgrep（/home/TOOLS/pftgrep）
```bash
#!/usr/bin/env bash
set -euo pipefail
```

### 验证
- `bash -n /home/TOOLS/pftgrep`
- `bash /home/TOOLS/pftgrep --help`
- `bash /home/TOOLS/pftgrep -ar 'IMU|ImuTimingMetrics' <临时目录>`
- `ssh root@192.168.192.5 'bash -n /usr/local/bin/pftgrep'`
- `ssh root@192.168.192.5 'cd /opt/.data/diagnosis/rbk/trace/event && /usr/local/bin/pftgrep --summary rbk+common_2026-03-25_*.pft.zst'`
- `ssh root@192.168.192.5 'cd /opt/.data/diagnosis/rbk/trace/event && /usr/local/bin/pftgrep -ar "IMU" .'`
- `ssh root@192.168.192.5 'cd /opt/.data/diagnosis/rbk/trace/event && /usr/local/bin/pftgrep -air "imu" rbk+common_2026-03-25_*.pft.zst'`
