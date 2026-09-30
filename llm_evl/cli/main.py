"""Minimal CLI: run / serve / list-targets.

Per Q15: thin shell over the same core library the API uses.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ..core.evaluator import Evaluator, filter_targets, load_targets, has_plaintext_keys
from ..core.models import RunConfig, RunStatus

console = Console()


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context):
    """llm-evl: LLM performance benchmark."""
    if ctx.invoked_subcommand is None:
        # Default to `serve` for convenience.
        ctx.invoke(serve)


@cli.command()
@click.option("--config", "config_path", default="targets.yaml",
              help="Targets YAML config path.")
@click.option("--relay-config", "relay_config_path", default="relay.yaml",
              help="Relay YAML config path (routing strategy, breaker, weights, groups).")
@click.option("--host", default="127.0.0.1",
              help="Management UI bind address. Keep it on loopback: /api/* "
                   "returns provider keys.")
@click.option("--port", default=7788, type=int)
@click.option("--relay-host", default="0.0.0.0",
              help="Relay bind address. This is the LAN-facing port.")
@click.option("--relay-port", default=7789, type=int)
@click.option("--no-browser", is_flag=True, help="Do not auto-open the browser.")
def serve(config_path: str, relay_config_path: str, host: str, port: int,
          relay_host: str, relay_port: int, no_browser: bool):
    """Launch the web UI (default) and the relay endpoint."""
    from ..api.server import run

    # Warn on plaintext keys before starting.
    try:
        names = has_plaintext_keys(load_targets(config_path))
        if names:
            console.print(f"[yellow]WARNING:[/yellow] plaintext API keys in config for: {', '.join(names)}")
    except FileNotFoundError:
        console.print(f"[yellow]Note:[/yellow] {config_path} not found. "
                      f"Copy targets.yaml.example to targets.yaml and edit it.")
    run(host=host, port=port, config_path=config_path, open_browser=not no_browser,
        relay_config_path=relay_config_path, relay_host=relay_host,
        relay_port=relay_port)


@cli.command("list-targets")
@click.option("--config", "config_path", default="targets.yaml")
def list_targets(config_path: str):
    """List configured targets."""
    try:
        targets = load_targets(config_path)
    except FileNotFoundError:
        console.print(f"[red]error:[/red] {config_path} not found")
        sys.exit(1)
    table = Table(title=f"Targets in {config_path}")
    table.add_column("name")
    table.add_column("base_url")
    table.add_column("model")
    table.add_column("api_key")
    for t in targets:
        key_src = "plaintext" if t.has_plaintext_key() else (f"env:{t.api_key_env}" if t.api_key_env else "(none)")
        table.add_row(t.name, t.base_url, t.model, key_src)
    console.print(table)


@cli.command()
@click.option("--config", "config_path", default="targets.yaml")
@click.option("-o", "--output", default="", help="Output JSON path (default: run_<id>.json).")
@click.option("--concurrency", default="1,2,4,8,16,32",
              help="Comma-separated concurrency levels.")
@click.option("-n", "--samples", default=20, type=int, help="Effective samples per cell.")
@click.option("--warmup", default=2, type=int, help="Warmup requests discarded per cell.")
@click.option("--timeout", default=120.0, type=float, help="Per-request timeout (s).")
@click.option("--temperature", default=0.0, type=float, help="Sampling temperature.")
@click.option("--targets", "target_names", default="", help="Comma-separated target subset.")
@click.option("--prompts", "prompt_ids", default="", help="Comma-separated prompt ids.")
@click.option("--prompts-file", default="", help="Override prompt set from file.")
@click.option("--no-usage", is_flag=True, help="Disable stream_options.include_usage.")
@click.option("--mix-prompts", is_flag=True, help="Mix prompts within each cell (more realistic).")
def run(config_path: str, output: str, concurrency: str, samples: int,
        warmup: int, timeout: float, temperature: float,
        target_names: str, prompt_ids: str, prompts_file: str, no_usage: bool,
        mix_prompts: bool):
    """Run the benchmark matrix and write a JSON result file."""
    try:
        targets = load_targets(config_path)
    except FileNotFoundError:
        console.print(f"[red]error:[/red] {config_path} not found")
        sys.exit(1)

    names = target_names.split(",") if target_names else []
    pids = prompt_ids.split(",") if prompt_ids else []
    clevels = [int(x) for x in concurrency.split(",") if x.strip()]
    targets = filter_targets(targets, names)

    config = RunConfig(
        concurrency_levels=clevels,
        samples=samples,
        warmup=warmup,
        timeout=timeout,
        temperature=temperature,
        target_names=names,
        prompt_ids=pids,
        prompts_file=prompts_file,
        include_usage=not no_usage,
        mix_prompts=mix_prompts,
    )

    evaluator = Evaluator(config, targets)
    run_result = evaluator.run_result

    async def drive_and_drain():
        async for ev in evaluator.iter_events():
            if ev.type == "cell_done":
                c = ev.cell
                agg = c["aggregates"]
                console.print(
                    f"[cyan]{c['target']}[/cyan] / {c['prompt_label']} / c={c['concurrency']} "
                    f"-> TTFT p50={_fmt(agg['ttft_p50'])}s "
                    f"tok/s={_fmt(agg['tokens_per_second_mean'])} "
                    f"err={agg['error_rate']:.0%} "
                    f"progress={ev.progress:.0%}"
                )

    asyncio.run(drive_and_drain())

    # Persist to runs/ so the run appears in the UI history (single source of
    # truth). If -o is given, also write a copy there for convenience.
    runs_dir = Path("runs")
    runs_dir.mkdir(exist_ok=True)
    payload = json.dumps(run_result.to_dict(), ensure_ascii=False, indent=2)
    runs_path = runs_dir / f"run_{run_result.run_id}.json"
    runs_path.write_text(payload, encoding="utf-8")
    console.print(f"\n[green]done[/green] status={run_result.status} -> {runs_path}")
    if output:
        out_path = Path(output)
        out_path.write_text(payload, encoding="utf-8")
        console.print(f"[green]copy[/green] -> {out_path}")


def _fmt(v) -> str:
    return f"{v:.3f}" if isinstance(v, (int, float)) else "-"


if __name__ == "__main__":
    cli()
