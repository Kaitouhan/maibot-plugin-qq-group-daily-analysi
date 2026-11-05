# 麦麦群聊日常分析

面向 **MaiBot 1.3.15 / maibot-plugin-sdk 2.16+**，移植自 [AstrBot 群分析插件 v5.9.4](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)。1.0.0 使用上游完整分析、渲染、绘图和增量引擎，以麦麦公开 SDK 替换 AstrBot 宿主接口；QQ 扩展按 **SnowLuma** 对接。

| 功能 | 实现 |
| --- | --- |
| 群统计、话题、称号/MBTI、金句、质量锐评 | 原引擎，支持独立模型、人格、重试与降级 |
| 12 套主题及自定义模板 | 原 HTML/Typst 模板；GitHub/ZIP 安装、卸载、字体与素材管理 |
| 图片、HTML、文本、Markdown、PDF | 图片支持 Host/Typst/自定义渲染服务；PDF 为报告图片封装 |
| 每日漫画 | 原分镜、角色、参考图、随机角色及多供应商绘图客户端 |
| 增量分析 | 批次、游标去重、最终合并、即时报告与失败回退 |
| 历史、重渲染、检查点恢复、开机恢复 | 持久化分析快照与任务链路；恢复优先复用模型产物 |
| 管理后台 | 麦麦原生页面：任务、历史、模板、完整配置、链路、日志、参考图 |
| QQ / Telegram / Discord 群 | 使用麦麦已保存的记录；QQ 可选 SnowLuma 历史补拉 |
| SnowLuma 扩展 | 驱动识别、分页、禁言检测、群文件、群相册、表情回应接口 |

AstrBot 专用命令事件和 Web 服务由麦麦命令及原生 WebUI 替代，不需要安装 AstrBot。原 `general_plugin`/`big_banana` 互调改为配置麦麦生图插件公开 API；内置 HTTP 绘图供应商可直接使用。麦麦模型接口只返回最终响应时，原流式消费者接收单个完整结果；用量和成本取决于宿主返回数据。

## 安装与启用

将 [Release](https://github.com/Kaitouhan/maibot-plugin-qq-group-daily-analysi/releases) 的 ZIP 上传到麦麦插件管理，或执行：

```bash
git clone https://github.com/Kaitouhan/maibot-plugin-qq-group-daily-analysi.git plugins/qq-group-daily-analysis
```

重载插件，允许 manifest 申请的能力。首次加载生成 `config.toml`；打开麦麦侧栏 **群分析工作台**。自定义页面须使用支持 WebUI schema v2 的配套主程序和 dashboard；页面声明已用 v1.3.15 源码校验器验证。旧 dashboard 可使用群命令和宿主插件配置页。

在 MaiBot 的命令权限中授予操作员权限，然后在群内执行 `/群分析状态` 获取真实聊天流 ID。`allowed_streams`、`scheduled_streams` 填聊天流 ID，不能用 QQ 群号代替。

```text
/群分析 [YYYY-MM-DD] [image|text|html|markdown|pdf]
/群分析历史 [YYYY-MM-DD]
/群分析状态
/群分析帮助
/群漫画 [YYYY-MM-DD]
/增量分析 [YYYY-MM-DD]
/增量汇总 [YYYY-MM-DD]
/增量状态
/查看模板
/设置模板 HatsuneMiku
/设置格式 image
/分析设置
```

英文日报别名：`/daily_analysis`、`/group_analysis`。历史日报重发和重渲染不重新调用模型；单独生成漫画可复用已有话题。称号和 MBTI 为娱乐性推测。

## SnowLuma 配置

在宿主插件配置页设置可从 **麦麦进程/容器内部访问** 的 SnowLuma HTTP 地址与 Access Token：

```toml
onebot_endpoint = "http://snowluma:3000"
onebot_token = "你的AccessToken"
```

端点留空时，仅查询麦麦本地历史并通过麦麦发送消息。填写后补拉 QQ 历史并开放群文件/相册扩展。遵循 [SnowLuma API](https://snowluma.github.io/zh/docs/api)：HTTP 200 仍须 `retcode=0`；历史分页传整数 `group_id`、`message_id`，不发送 `reverseOrder`。

HTML/PDF 群文件先调用 `upload_file_stream` 分块上传、核对 SHA256，再用返回的远端路径调用 `upload_group_file`，支持麦麦与 SnowLuma 分容器部署。群相册开关、名称和严格匹配在完整配置中设置；相册必须存在且接口权限可用。

## 配置、漫画与定时任务

宿主配置页设置平台、群授权、默认输出、模型、冷却时间及 SnowLuma 连接。工作台的 **完整配置** 按原 Schema 编辑全部上游字段，包含分析开关、模型回退、提示词、渲染策略、绘图供应商和漫画角色。模板供应商条目须保留原 `__template_key`；可从 Schema 的 templates 查看完整字段。

麦麦 `model` 及上游独立 provider ID 对应宿主可用的模型任务/模型标识。`persona_profiles_json` 将原人格 ID 映射为提示词；未指定时可继承麦麦全局人格。参考图在工作台上传后，将返回路径填入 `daily_comic.comic_characters` 对应角色的 `reference_images`。

漫画须开启 `daily_comic.enable_daily_comic`，配置至少一个绘图供应商（API Key、端点、模型、协议），或填写 `external_drawing_api` 使用麦麦生图插件。外部 API 接收 `prompt`、`reference_images`（base64 数组）、`backend`，返回 `image_base64`。

两套定时入口可按需选用，避免对同群配置重复任务：

- 宿主 `scheduled_streams` + `send_at`：每天上海时间指定时刻发送 **昨天** 的日报。
- 完整配置 `auto_analysis`：原名单、多时刻调度，生成当天截至当前的日报；`incremental` 名单控制增量与最终汇总。也可用 `incremental_streams` 指定群流，间隔由 `incremental_interval_minutes` 控制。

默认自动名单为空，不自动向所有群发报告。每个日期/时隙持久占位；投递未知时不盲目重发，可手动重发历史。时间窗口固定 Asia/Shanghai。

完整配置保存到 Host 分配数据目录的 `upstream_config.json`。普通重启保留后台改动；修改宿主配置中对应字段会同步到原引擎。`advanced_json` 用于额外覆盖，只有内容变化时重新应用。数据库、配置、上传和报告不进入 Git 或发布包。更新前请保留整个插件数据目录；0.1.0 的缓存可重发，高级重渲染需要重新生成完整引擎快照。

## Git、更新与验证

来源固定于 `upstream.lock.json`，完整引擎在 `vendor/upstream/`。`scripts/vendor_engine.py` 对宿主边界做可重复转换，保留原许可和模板来源说明。

GitHub Actions 每天约 **10:23（上海时间）**检查上游。同步完整引擎、配置 Schema、模板与提示词后，经过 Ruff、Pyright、SDK 和完整引擎回归测试才提交；结构变化或测试失败时停止推送。新增宿主接口仍需人工适配。自动同步更新 **GitHub 仓库**，运行中的麦麦实例须通过插件管理更新，或 `git pull --ff-only` 后重载。

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check --exclude vendor .
pyright --pythonpath python
pytest -q
python scripts/package.py
```

CI 检查 Python 3.12/3.13 × SDK 2.16/latest。发布 `v*` 标签生成 Release 安装包。保留 `v0.1.0` 回退点；使用 `git revert` 回退提交，不改写历史。自动同步须允许 Actions `contents: write`，默认分支禁止直推时需调整为 PR 流程。

已测试真实 SDK 上的模拟 RPC、全部主题 HTML、图片/PDF 输出、原分析器、漫画 HTTP 链路、增量去重/汇总、检查点复用、配置保留和 SnowLuma 协议合约。**尚未接入用户的实际 MaiBot、QQ 账号或付费模型服务做在线联调**；字体、CDN、群相册及特定绘图供应商须在部署环境配置后验证。Host 兼容范围固定为 1.3.15。

## 来源与许可

遵循上游 MIT 许可，保留 Copyright (c) 2025 Helian Nuits。详情见 [NOTICE.md](NOTICE.md) 和各模板来源说明。
