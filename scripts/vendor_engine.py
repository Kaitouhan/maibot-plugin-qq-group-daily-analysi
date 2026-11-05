"""可重复的上游边界转换；不执行下载的代码，结构漂移时拒绝同步。"""

from __future__ import annotations

import ast
import re


def patch_source(files: dict[str, bytes]) -> dict[str, bytes]:
    required = {
        "src/application/services/comic_application_service.py",
        "src/application/services/analysis_application_service.py",
        "src/infrastructure/analysis/analyzers/base_analyzer.py",
        "src/infrastructure/reporting/render_data_preparer.py",
        "src/infrastructure/reporting/typst_data.py",
    }
    if not required.issubset(files):
        raise ValueError("上游完整引擎结构发生变化，停止同步")
    output = dict(files)
    for filename, data in files.items():
        if not filename.startswith("src/") or not filename.endswith(".py"):
            continue
        text = data.decode("utf-8")
        dots = "." * len(filename.removeprefix("src/").split("/"))
        text = text.replace(
            "from astrbot.api.star import StarTools", f"from {dots}host import RuntimePaths as StarTools"
        )
        text = text.replace("from astrbot.api.provider import LLMResponse", f"from {dots}host import LLMResponse")
        text = text.replace(
            "from astrbot.api import logger as astrbot_logger",
            'import logging\nastrbot_logger = logging.getLogger("plugin.kaitouhan.qq-group-daily-analysis")',
        )
        text = text.replace("from astrbot.api import sp", f"from {dots}host import preferences as sp")

        def local_datetime(match: re.Match) -> str:
            indent, imported = match.group(1), match.group(2).split(",")
            names = [name.strip() for name in imported]
            if "datetime" not in names:
                return match.group(0)
            rest = [name for name in names if name != "datetime"]
            return (
                (indent + "from datetime import " + ", ".join(rest) + "\n" if rest else "")
                + indent
                + f"from {dots}host import LocalDatetime as datetime"
            )

        text = re.sub(r"(?m)^(\s*)from datetime import ([^\n()]+)$", local_datetime, text)
        text = text.replace("import datetime as dt", f"from {dots}host import local_datetime_module as dt")
        text = re.sub(r"(?m)^import datetime$", f"from {dots}host import local_datetime_module as datetime", text)
        if filename.endswith("comic_application_service.py"):
            try:
                start = text.index("    async def _generate_via_general_plugin(")
                banana = text.index("    async def _generate_via_big_banana(", start)
                following = re.search(r"\n    (?:async )?def ", text[banana + 20 :])
                end = banana + 20 + following.start() if following else len(text)
            except ValueError as error:
                raise ValueError("上游生图互调边界发生变化") from error
            replacement = ""
            for name, backend in (
                ("_generate_via_general_plugin", "general_plugin"),
                ("_generate_via_big_banana", "big_banana"),
            ):
                replacement += f'    async def {name}(self, scene_prompt: str, images_data=None):\n        """通过麦麦公开 API 调用对应的外部生图能力。"""\n        if self.context is None:\n            return None\n        return await self.context.generate_external_image(scene_prompt, images_data, backend="{backend}")\n\n'
            text = text[:start] + replacement + text[end:]
        dates = {
            "src/infrastructure/reporting/render_data_preparer.py": (
                "format_report_date(report_language, datetime.now())",
                "format_report_date(report_language, report_datetime())",
            ),
            "src/infrastructure/reporting/typst_data.py": (
                "format_report_date(explicit_language, datetime.now())",
                "format_report_date(explicit_language, report_datetime())",
            ),
            "src/application/services/analysis_application_service.py": (
                'date_str = dt.datetime.now().strftime("%Y-%m-%d")',
                'date_str = report_datetime().strftime("%Y-%m-%d")',
            ),
        }
        if filename in dates:
            old, new = dates[filename]
            if old not in text:
                raise ValueError(f"上游报告日期边界发生变化：{filename}")
            text = text.replace(old, new).replace(
                "from __future__ import annotations",
                f"from __future__ import annotations\nfrom {dots}host import report_datetime",
            )
        if filename.endswith("incremental_analysis_service.py"):
            text = text.replace(
                "from __future__ import annotations",
                f"from __future__ import annotations\nfrom {dots}host import report_datetime, report_window_end",
            )
            text = text.replace("dt.datetime.now()", "report_datetime()")
            text = text.replace("window_end = time_mod.time()", "window_end = report_window_end()")
            text = text.replace(
                "timestamp=time_mod.time(),", "timestamp=min(time_mod.time(), report_window_end() - 0.001),"
            )
        if filename.endswith("persistence/history_manager.py"):
            text = text.replace(
                "from __future__ import annotations",
                f"from __future__ import annotations\nfrom {dots}host import report_datetime",
            )
            text = text.replace(
                'date_str = now.strftime("%Y-%m-%d")', 'date_str = report_datetime().strftime("%Y-%m-%d")'
            )
        ast.parse(text, filename)
        output[filename] = text.encode("utf-8")
    output["__init__.py"] = b""
    output["src/host.py"] = (
        b"from ....compatibility import LLMResponse, RuntimePaths, preferences, LocalDatetime, local_datetime_module, report_datetime, report_window_end\n"
    )
    return output
