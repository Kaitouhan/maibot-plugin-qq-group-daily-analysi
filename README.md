# 麦麦群聊日常分析

适用于 **MaiBot 1.3.15 / maibot-plugin-sdk 2.16+**，移植自 [SXP-Simon 的 AstrBot 群分析插件](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)。当前 `0.1.0` 为核心功能移植版，**并非上游 v5.9.4 全部功能的等价版本**。

| 功能 | 状态 |
| --- | --- |
| 群消息、参与人数、字数、表情、小时活跃统计 | 支持 |
| 话题、娱乐性称号/MBTI、金句、聊天质量 | 支持，复用上游提示词，调用麦麦模型 |
| 长记录分批、跨批话题归并 | 支持，超出预算会明确标注 |
| 图片日报、降级文本、指定日期、历史报告 | 支持 |
| 定时日报、热更新、卸载回收、重启去重 | 支持 |
| 原管理后台、全部主题、Typst/PDF、漫画生图 | 未移植 |
| NapCat 历史补拉、Telegram/Discord | 未移植；仅分析麦麦已保存的 QQ 群记录 |

## 安装

上传 Release 的 ZIP 至麦麦插件管理页，或者在 MaiBot 根目录执行：

```bash
git clone https://github.com/Kaitouhan/maibot-plugin-qq-group-daily-analysi.git plugins/qq-group-daily-analysis
```

随后重启/重载插件。无需 AstrBot；图片依赖麦麦 Host 可用的浏览器渲染服务，失败时降级文本。首次加载自动生成 `config.toml`，通过 WebUI 修改。

命令遵循宿主操作员权限：先在 MaiBot `[plugin].permission` 设置 `qq:你的用户号`，或在 WebUI 对命令放行。

```text
/群分析                         分析今天（截至当前）
/群分析 2026-11-04 text         分析指定自然日并发送文本
/群分析历史                     列出本群最近14个报告日期
/群分析历史 2026-11-04          重发缓存日报，不调用模型
/群分析状态                     获取真实聊天流 ID 和任务状态
/群分析帮助                     显示帮助
```

英文对应命令：`/daily_analysis`、`/daily_analysis_history`、`/daily_analysis_status`、`/daily_analysis_help`。

## 配置与定时日报

先在目标群运行 `/群分析状态`，复制 **真实聊天流 ID（不是 QQ 群号）**，通过插件配置页面设置：

```toml
allowed_streams = ["真实聊天流ID"]
scheduled_streams = ["真实聊天流ID"]
send_at = "00:10"
output = "image"
model = ""
min_messages = 20
max_messages = 10000
chunk_chars = 18000
max_chunks = 20
max_topics = 8
max_quotes = 5
quality_titles_llm_timeout = 120
cooldown_seconds = 300

[plugin]
enabled = true
config_version = "0.1.0"
```

默认 `allowed_streams=[]` 允许所有真实 QQ 群手动命令，`scheduled_streams=[]` 不自动发送。`model=""` 使用宿主默认 utils 任务。配置时间固定为上海时间，每天指定时刻后生成**昨天**的日报，重启会补做昨天未占位的任务，不追补多天历史。

每批话题和金句各调用一次模型，启用质量时额外一次，多批还有归并调用。请按群活跃程度和模型成本设置上限。超出消息/批次上限会在报告里标明，截断数据不代表完整全天。金句必须逐字匹配发言及发送者。称号与 MBTI 仅作娱乐推测。

报告和投递状态保存在宿主分配的 `data/plugins/kaitouhan.qq-group-daily-analysis/daily_analysis.sqlite3`，更新源码不覆盖数据库。定时先写占位，崩溃或投递结果未知时不盲目重发，可通过历史命令重发。

## Git 与自动更新

保留初始提交；使用功能分支、语义版本与版本标签。上游材料放在 `vendor/`，适配代码独立维护，来源提交固定在 `upstream.lock.json`。

GitHub Actions 每天约 **10:23（上海时间）**检查上游，也可手动运行“每日同步上游”；GitHub 调度可能延迟。同步限定为统一消息对象、默认提示词、MIT 许可与来源锁文件。通过 lint、类型检查和 SDK 回归测试后才自动提交默认分支，失败不推送。**上游新增业务功能不能自动移植**。

这会更新 GitHub 仓库，**不会自动更新正在运行的麦麦实例**。实例需通过插件管理更新，或插件目录 `git pull --ff-only` 后重载。运行配置和数据库不纳入 Git。

Actions 需允许 `contents: write`；若默认分支禁止直接推送，需按分支规则改成 PR 流程。发布 `v*` 标签会生成安装 ZIP 和 GitHub Release，手动执行打包工作流也能下载产物。

本地验证：

```bash
python -m pip install -e '.[dev]'
python -m pip install maibot-plugin-sdk==2.16.0
ruff check .
ruff format --check --exclude vendor .
pyright --pythonpath python
pytest -q
python scripts/package.py
```

版本发布和回退：

```bash
git tag -a v0.1.0 -m '首个麦麦核心移植版本'
git push origin v0.1.0
# 使用 git revert <提交SHA> 回退，保留历史；不使用强制推送改写历史。
```

已使用真实 SDK 代理做契约测试；实际 QQ 群、模型服务及 NapCat 的端到端联调仍需在麦麦实例验证。Host 范围刻意固定为 `1.3.15`，升级后先验证再调整 manifest。

## 来源与许可

MIT，保留原版权，见 [NOTICE.md](NOTICE.md)。开发依据：[MaiBot 指南](https://docs.maibot.chat/plugin/) 与 [AstrBot 指南](https://docs.astrbot.app/dev/star/plugin-new.html)。
