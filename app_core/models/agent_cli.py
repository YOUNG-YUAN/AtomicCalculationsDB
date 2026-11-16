"""Headless local Agent CLI adapter (Codex CLI / custom CLI agents)."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

from models.endpoint_pool import EndpointNode

log = logging.getLogger(__name__)

AGENT_CLI_PRESETS: dict[str, dict[str, str]] = {
    "codex": {
        "label": "OpenAI Codex CLI (codex exec)",
        "default_command": "codex",
        "default_sandbox": "read-only",
        "hint": "调用 codex exec --skip-git-repo-check --sandbox read-only --output-last-message ...",
    },
    "custom": {
        "label": "自定义命令行 Agent",
        "default_command": "",
        "default_sandbox": "read-only",
        "hint": "支持参数占位符 {prompt_file}、{output_file}、{model}、{workdir}；无占位符时通过 stdin 传入提示词并读取 stdout。",
    },
}


class AgentCLIError(RuntimeError):
    pass


def cli_preset_choices() -> list[tuple[str, str]]:
    return [(key, spec["label"]) for key, spec in AGENT_CLI_PRESETS.items()]


def _split_args(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    tokens = shlex.split(text, posix=(os.name != "nt"))
    cleaned: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
            cleaned.append(token[1:-1])
        else:
            cleaned.append(token)
    return cleaned


def resolve_cli_executable(node: EndpointNode) -> str:
    raw = str(node.cli_command or "").strip()
    if not raw:
        preset = AGENT_CLI_PRESETS.get(node.cli_preset or "codex") or {}
        raw = str(preset.get("default_command") or "").strip()
    if not raw:
        raise AgentCLIError(f"Agent CLI 节点 {node.name} 未配置命令路径")
    candidate = Path(raw)
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(raw)
    if found:
        return found
    raise AgentCLIError(f"找不到 Agent CLI 可执行文件：{raw}")


def build_cli_command(
    node: EndpointNode,
    *,
    executable: str,
    prompt_file: Path,
    output_file: Path,
    workdir: Path,
) -> tuple[list[str], bool]:
    """Return ``(argv, expects_output_file)`` for ``node``."""
    preset = (node.cli_preset or ("codex" if "codex" in Path(executable).stem.lower() else "custom")).lower()
    extra_raw = str(node.cli_args or "").strip()
    model = str(node.model or "").strip()

    if preset == "codex":
        cmd = [executable, "exec", "--skip-git-repo-check"]
        sandbox = str(node.cli_sandbox or "read-only").strip().lower()
        if sandbox in ("read-only", "workspace-write", "danger-full-access"):
            cmd.extend(["--sandbox", sandbox])
        cmd.extend(["--output-last-message", str(output_file)])
        if model:
            cmd.extend(["-m", model])
        if extra_raw:
            cmd.extend(_split_args(extra_raw))
        cmd.append("-")
        return cmd, True

    tokens = _split_args(extra_raw)
    mapping = {
        "prompt_file": str(prompt_file),
        "output_file": str(output_file),
        "model": model,
        "workdir": str(workdir),
    }
    expects_output_file = "{output_file}" in extra_raw
    formatted: list[str] = []
    for token in tokens:
        value = token
        for key, replacement in mapping.items():
            value = value.replace("{" + key + "}", replacement)
        if value:
            formatted.append(value)
    return [executable, *formatted], expects_output_file


def _cli_env(node: EndpointNode) -> dict[str, str]:
    env = dict(os.environ)
    if node.proxy_url:
        proxy = str(node.proxy_url).strip()
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
    return env


def run_agent_cli(
    node: EndpointNode,
    prompt: str,
    *,
    workspace: str | Path,
    timeout_sec: int | None = None,
    runner: Any = None,
) -> str:
    """Invoke a configured local Agent CLI in an isolated directory and return text."""
    executable = resolve_cli_executable(node)
    timeout = max(10, int(timeout_sec or node.cli_timeout_sec or 300))
    tmp_root = Path(workspace) / "agent_cli_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    explicit_workdir = Path(node.cli_workdir) if str(node.cli_workdir or "").strip() else None
    run = runner or subprocess.run

    with tempfile.TemporaryDirectory(dir=str(tmp_root), prefix="cli_") as temp_dir:
        temp_path = Path(temp_dir)
        workdir = explicit_workdir or temp_path
        workdir.mkdir(parents=True, exist_ok=True)
        prompt_file = temp_path / "prompt.txt"
        output_file = temp_path / "reply.txt"
        prompt_file.write_text(str(prompt or ""), encoding="utf-8")

        cmd, expects_output_file = build_cli_command(
            node,
            executable=executable,
            prompt_file=prompt_file,
            output_file=output_file,
            workdir=workdir,
        )
        uses_prompt_file = "{prompt_file}" in str(node.cli_args or "") and (
            (node.cli_preset or "").lower() == "custom"
        )
        stdin_text = None if uses_prompt_file else str(prompt or "")
        log.info("Invoking Agent CLI node %s: %s", node.node_id, cmd)
        try:
            completed = run(
                cmd,
                input=stdin_text,
                cwd=str(workdir),
                env=_cli_env(node),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentCLIError(
                f"Agent CLI 节点 {node.name} 执行超时（>{timeout}s）"
            ) from exc
        except OSError as exc:
            raise AgentCLIError(f"无法启动 Agent CLI（{executable}）：{exc}") from exc

        if int(getattr(completed, "returncode", 1)) != 0:
            stderr = str(getattr(completed, "stderr", "") or "").strip()
            stdout = str(getattr(completed, "stdout", "") or "").strip()
            detail = stderr or stdout or f"exit={completed.returncode}"
            raise AgentCLIError(
                f"Agent CLI 节点 {node.name} 返回错误：{detail[:400]}"
            )

        output_text = ""
        if expects_output_file and output_file.is_file():
            output_text = output_file.read_text(encoding="utf-8", errors="replace").strip()
        if not output_text:
            output_text = str(getattr(completed, "stdout", "") or "").strip()
        if not output_text:
            raise AgentCLIError(f"Agent CLI 节点 {node.name} 未返回有效输出")
        return output_text


def test_agent_cli(
    node: EndpointNode,
    *,
    workspace: str | Path,
    runner: Any = None,
) -> str:
    """Verify the configured Agent CLI executable is reachable and responds."""
    executable = resolve_cli_executable(node)
    run = runner or subprocess.run
    try:
        completed = run(
            [executable, "--version"],
            cwd=str(Path(workspace)),
            env=_cli_env(node),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception as exc:
        raise AgentCLIError(f"检测命令失败（{executable}）：{exc}") from exc
    if int(getattr(completed, "returncode", 1)) == 0:
        version = (
            str(getattr(completed, "stdout", "") or "").strip()
            or str(getattr(completed, "stderr", "") or "").strip()
            or "ok"
        )
        return f"可执行文件就绪：{executable}（{version.splitlines()[0][:120]}）"
    return f"已定位可执行文件：{executable}（--version 返回 {completed.returncode}）"


__all__ = [
    "AGENT_CLI_PRESETS",
    "AgentCLIError",
    "build_cli_command",
    "cli_preset_choices",
    "resolve_cli_executable",
    "run_agent_cli",
    "test_agent_cli",
]
