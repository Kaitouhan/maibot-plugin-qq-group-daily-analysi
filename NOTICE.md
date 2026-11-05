# 来源与许可

本项目移植自 [SXP-Simon/astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)。
保留原 MIT 许可及 Copyright (c) 2025 Helian Nuits。

`vendor/unified_message.py` 原样来自上游的统一消息值对象；`vendor/prompts.json` 提取自上游配置的默认提示词。
`vendor/upstream/` 保留完整上游领域、应用、分析、渲染、绘图、追踪、配置和模板源码及各模板的来源声明。
来源提交和压缩包摘要固定在 `upstream.lock.json`。`scripts/vendor_engine.py` 可重复转换宿主路径、日志、人格、时间和外部插件互调边界；麦麦 SDK、原生后台和 SnowLuma 连接由本移植实现。
