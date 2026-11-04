"""生成安全的文本和图片报告 HTML。"""

from __future__ import annotations

from html import escape

from .domain import Report


def text_report(report: Report) -> str:
    """将经过验证的结果生成可降级发送的报告。"""
    stats = report.statistics
    lines = [
        f"群聊日报 · {report.day}",
        f"消息 {stats.message_count} · 参与 {stats.participant_count} 人 · 字数 {stats.total_characters}",
        f"表情 {stats.emoji_count} · 活跃时段 {stats.most_active_period}",
    ]
    lines.append("\n发言排行")
    for uid, count in list(stats.users.items())[:10]:
        lines.append(f"{stats.names.get(uid, uid)}：{count} 条")
    if report.topics:
        lines.append("\n热门话题")
        for topic in report.topics:
            lines.append(f"• {topic.topic}\n{topic.detail}")
    if report.titles:
        lines.append("\n群友称号（娱乐推测）")
        for title in report.titles:
            lines.append(
                f"{stats.names.get(title.user_id, title.user_id)} · {title.title} {title.mbti}\n{title.reason}"
            )
    if report.quotes:
        lines.append("\n群聊金句")
        for quote in report.quotes:
            uid = quote.sender.strip("[]")
            lines.append(f"“{quote.content}” — {stats.names.get(uid, uid)}\n{quote.reason}")
    if report.quality:
        lines.extend(["\n群聊质量", report.quality.title, report.quality.subtitle])
        lines.extend(f"{d.name} {d.percentage:g}%：{d.comment}" for d in report.quality.dimensions)
        lines.append(report.quality.summary)
    if report.warnings:
        lines.extend(["\n分析说明", *report.warnings])
    lines.append("\nMaiBot 群日常分析 · 源自 SXP-Simon/AstrBot 群分析插件")
    return "\n".join(lines)


def html_report(report: Report) -> str:
    """所有用户和模型内容先转义，禁用报告外部资源。"""
    text = escape(text_report(report))
    bars = "".join(
        f'<div class="hour"><div style="height:{max(2, n * 90 / max(1, max(report.statistics.hours))):.1f}px"></div><small>{h:02d}</small></div>'
        for h, n in enumerate(report.statistics.hours)
    )
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>body{{margin:0;padding:36px;width:828px;background:#edf3fa;color:#243247;font-family:sans-serif}}
article{{background:white;border-radius:20px;padding:30px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:18px/1.75 sans-serif}}
.chart{{display:flex;align-items:end;gap:7px;height:130px}}.hour{{width:24px;text-align:center}}.hour div{{background:#678bea;border-radius:4px}}small{{font-size:11px}}</style>
<article><h1>群聊日常分析</h1><div class="chart">{bars}</div><pre>{text}</pre></article></html>"""
