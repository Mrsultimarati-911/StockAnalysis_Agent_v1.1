"""Configuration-driven entry points with a durable, credential-safe run log."""
from __future__ import annotations

import argparse
from dataclasses import replace
import importlib.metadata
import importlib.util
import math
from pathlib import Path
import re
import sys

from .config import (ConfigError, DEEPSEEK_MODEL, MAX_MODEL_CALLS, MAX_TOOL_CALLS,
                     ROOT, RunConfig, load_run_config)
from .models import normalize_symbol
from .runlog import RunLogger, runtime_environment


def _safe_output(value: object, api_key: str = "") -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, "[密钥已隐藏]")
    text = re.sub(r"(?i)\bsk-[a-z0-9_-]+", "[密钥已隐藏]", text)
    return re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", text)


def _doctor(config_path: str | Path | None = None) -> int:
    print("个股分析 Agent · 本地环境与配置检查（不联网、不请求模型）")
    healthy = sys.version_info >= (3, 10)
    print(f"Python：{sys.version.split()[0]}（{'符合要求' if healthy else '需要 3.10 或更高版本'}）")
    dependencies = {"akshare": "akshare", "pandas": "pandas", "numpy": "numpy", "openai": "openai",
                    "plotly": "plotly", "requests": "requests", "bs4": "beautifulsoup4", "pypdf": "pypdf"}
    for module, distribution in dependencies.items():
        exists = importlib.util.find_spec(module) is not None
        version = ""
        if exists:
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = "版本未知"
        print(f"{distribution}：{version if exists else '缺失'}")
        healthy = healthy and exists
    try:
        config = load_run_config(config_path)
    except ConfigError:
        return _configuration_failure()
    settings = config.settings
    print(f"固定模型：{DEEPSEEK_MODEL}；调用上限：模型 {MAX_MODEL_CALLS} 次，工具 {MAX_TOOL_CALLS} 次。")
    print("API 密钥：" + ("格式有效（未联网验证）" if settings.llm_api_key else "未配置；仅规则模式可运行"))
    print(f"目标股票：{config.stock_code}；研究模式：{'Agent' if config.use_llm else '规则'}")
    print("输出目录：" + _safe_output(settings.report_dir, settings.llm_api_key))
    print("运行日志目录：" + _safe_output(settings.log_dir, settings.llm_api_key))
    print("图表：独立离线 HTML；模型身份核验仅在运行 Agent 时联网执行。")
    if config.use_llm and not settings.llm_api_key:
        healthy = False
    return 0 if healthy else 1


def _configuration_failure() -> int:
    message = ("配置或参数校验失败；请检查 config.json、股票代码、持仓成本与路径。"
               "只允许 deepseek-flash、官方 DeepSeek URL 及模型 12 次 / 工具 36 次上限。")
    print(message, file=sys.stderr)
    # Invalid configuration is not trusted to select a directory or stock code.
    try:
        with RunLogger(ROOT / "logs", "unknown") as log:
            print(f"运行日志（TXT）：{log.path}", flush=True)
            log.write(message, level="ERROR", event="configuration_error", exit_code=1)
    except Exception:
        print("无法保存配置错误日志，请检查 agent_project/logs 的写入权限。", file=sys.stderr)
    return 1


def _exit_status(config: RunConfig, bundle: dict) -> int:
    if bundle.get("export_warnings"):
        return 2
    llm_status = (bundle.get("llm") or {}).get("status")
    agent_status = (bundle.get("agent") or {}).get("status")
    if config.use_llm and (llm_status not in {"ok", "success"} or agent_status in {"limited", "error"}):
        return 2
    return 0


def _execute_logged(config: RunConfig, log: RunLogger) -> int:
    settings = config.settings
    log.write("本次运行使用的解释器与绘图库版本。", event="runtime_environment", **runtime_environment())
    log.write("开始个股研究。", event="run_start", stock_code=config.stock_code,
              model=settings.llm_model, base_url=settings.llm_base_url,
              max_model_calls=MAX_MODEL_CALLS, max_tool_calls=MAX_TOOL_CALLS,
              use_llm=config.use_llm, refresh_data=config.refresh_data,
              holding_cost=config.holding_cost, output_directory=str(settings.report_dir),
              log_directory=str(settings.log_dir),
              note="记录模型公开说明的行动理由、证据缺口及工具活动；不记录模型内部隐藏推理。")
    if config.use_llm and not settings.llm_api_key:
        message = "启用 Agent 前请在 config.json 填写 llm.api_key；规则模式可设置 task.use_llm=false。"
        log.write(message, level="ERROR", event="missing_api_key", exit_code=1)
        print(message, file=sys.stderr)
        return 1

    def progress(message: str, done: int, total: int) -> None:
        safe_message = _safe_output(message, settings.llm_api_key)
        # Persist before displaying: a failed log sink must stop paid activity.
        log.write(safe_message, event="progress", done=done, total=total)
        print(f"[{done}/{total}] {safe_message}", flush=True)

    phase = "analysis"
    try:
        from .pipeline import run_analysis, save_report
        bundle = run_analysis(config.stock_code, cost=config.holding_cost, use_llm=config.use_llm,
                              refresh=config.refresh_data, settings=settings, progress=progress,
                              event_sink=log.event)
        log.record_bundle(bundle)
        phase = "export"
        result_path, log_path, chart_path, json_path = save_report(bundle, settings=settings)
        for diagnostic in bundle.get("export_diagnostics", []):
            log.write("离线图表导出失败，已有研究成果与证据保留。", level="ERROR",
                      event="chart_export_failed", **diagnostic)
        paths = {"result": str(result_path), "research_log": str(log_path),
                 "chart": str(chart_path), "evidence_json": str(json_path)}
        log.write("已保存研究成果。", event="outputs_saved", **paths)
        for label, path in (("分析报告（result）", result_path), ("研究日志（log.md）", log_path),
                            ("离线行情图表（HTML）", chart_path), ("内部 JSON 证据", json_path)):
            print(f"{label}：{_safe_output(path, settings.llm_api_key)}", flush=True)
        status = _exit_status(config, bundle)
        if status == 2:
            message = ("离线图表未完整生成；已有成果已保存，请查看日志。" if bundle.get("export_warnings")
                       else "Agent 有限完成或综合研判未完成；已有成果已保存，未决事项请查看日志。")
            log.write(message, level="WARNING", event="limited_completion")
            print(message, file=sys.stderr)
        agent = bundle.get("agent") or {}
        llm = bundle.get("llm") or {}
        log.write("本次运行结束。", event="run_finished", exit_code=status,
                  agent_status=agent.get("status"), llm_status=llm.get("status"),
                  model_calls=agent.get("model_calls", llm.get("model_calls", 0)),
                  tool_calls=agent.get("tool_calls", 0), stop_code=agent.get("stop_code", ""),
                  stop_reason=agent.get("stop_reason", ""), usage=llm.get("usage", {}),
                  export_warnings=bundle.get("export_warnings", []),
                  export_diagnostics=bundle.get("export_diagnostics", []))
        return status
    except KeyboardInterrupt:
        message = "分析已由用户中止，已有运行记录保留在 TXT 日志中。"
        log.write(message, level="WARNING", event="run_cancelled", phase=phase, exit_code=1)
        print(message, file=sys.stderr)
        return 1
    except Exception as exc:
        # Exception text may contain an SDK request or credentials; log type only.
        message = "分析或保存失败，请查看 TXT 日志中已记录的运行阶段和工具状态；原始异常未输出以保护密钥。"
        log.write(message, level="ERROR", event="run_failed", phase=phase,
                  error_class=type(exc).__name__, exit_code=1)
        print(message, file=sys.stderr)
        return 1


def _execute(config: RunConfig) -> int:
    settings = config.settings
    path = None
    try:
        with RunLogger(settings.log_dir, config.stock_code, api_key=settings.llm_api_key) as log:
            path = log.path
            print("运行日志（TXT）：" + _safe_output(path, settings.llm_api_key), flush=True)
            return _execute_logged(config, log)
    except KeyboardInterrupt:
        print("本次运行已中止。", file=sys.stderr)
        return 1
    except Exception:
        print("无法持续写入运行日志，已停止本次运行；请检查 logs 目录的权限与磁盘空间。", file=sys.stderr)
        return 1
    finally:
        if path is not None:
            print("运行日志（TXT）：" + _safe_output(path, settings.llm_api_key), flush=True)


def _run(args: argparse.Namespace) -> int:
    try:
        config = load_run_config(args.config)
        if args.command == "analyze":
            symbol = normalize_symbol(args.symbol)
            cost = config.holding_cost if args.cost is None else args.cost
            if cost is not None and (not math.isfinite(cost) or cost <= 0):
                raise ConfigError("持仓成本应为大于零的有限数值。")
            settings = config.settings
            if args.output_dir is not None:
                target = args.output_dir.expanduser()
                target = (target if target.is_absolute() else config.config_path.parent / target).resolve()
                settings = replace(settings, report_dir=target)
            config = replace(config, settings=settings, stock_code=symbol, holding_cost=cost,
                             use_llm=config.use_llm if args.use_llm is None else args.use_llm,
                             refresh_data=config.refresh_data if args.refresh is None else args.refresh)
    except (ConfigError, ValueError, OSError):
        return _configuration_failure()
    return _execute(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-agent", description="读取 config.json，自主研究 A 股，保存 TXT 日志、Markdown 与离线 HTML。")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="离线检查配置和依赖，不请求模型")
    doctor.add_argument("--config", type=Path, default=None, help="配置路径，默认 agent_project/config.json")
    run = commands.add_parser("run", help="按照 config.json 执行一次研究并退出")
    run.add_argument("--config", type=Path, default=None, help="配置路径，默认 agent_project/config.json")
    analyze = commands.add_parser("analyze", help="兼容旧入口；显式参数覆盖配置中的对应值")
    analyze.add_argument("symbol", help="六位 A 股代码，例如 600875")
    analyze.add_argument("--config", type=Path, default=None, help="配置路径，默认 agent_project/config.json")
    analyze.add_argument("--cost", type=float, default=None, help="持仓成本（元），未填写则沿用配置")
    refresh = analyze.add_mutually_exclusive_group()
    refresh.add_argument("--refresh", dest="refresh", action="store_true", help="跳过免费数据缓存")
    refresh.add_argument("--no-refresh", dest="refresh", action="store_false", help="允许使用缓存")
    analyze.set_defaults(refresh=None, use_llm=None)
    mode = analyze.add_mutually_exclusive_group()
    mode.add_argument("--no-llm", dest="use_llm", action="store_false", help="仅生成规则报告，不请求模型")
    mode.add_argument("--llm", dest="use_llm", action="store_true", help="启用 Agent 自主研究")
    analyze.add_argument("--output-dir", type=Path, help="覆盖输出目录，相对路径按配置文件目录解析")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor(args.config)
    if args.command in {"run", "analyze"}:
        return _run(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
