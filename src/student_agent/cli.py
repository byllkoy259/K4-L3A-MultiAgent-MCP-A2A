from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp import MCPError
from mcp_types import CONNECTION_CLOSED

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway, connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


MAX_RECONNECTS = 5
TRANSPORT_ERRORS = (httpx2.TransportError, anyio.ClosedResourceError, anyio.EndOfStream)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def _is_transport_error(exc: BaseException) -> bool:
    if isinstance(exc, MCPError):
        return exc.code == CONNECTION_CLOSED
    return isinstance(exc, TRANSPORT_ERRORS)


def _connection_lost(exc: BaseException) -> bool:
    """True when every underlying error is a transport failure, not a bug in our code."""
    leaves = _leaves(exc)
    return bool(leaves) and all(_is_transport_error(leaf) for leaf in leaves)


async def _solve_one(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    contracts: Contracts,
    output_root: Path,
) -> None:
    case_id = case["case_id"]
    trace.begin()
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    output = await solve_case(case, gateway, trace)
    contracts.validate_output(output, f"outputs/{case_id}.json")
    if output.get("case_id") != case_id:
        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
    target = output_root / f"{case_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
    trace.commit()


async def _run(root: Path, only: list[str] | None = None) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("*.json"):
        stale.unlink()
    trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    unknown = sorted(set(only or ()) - set(case_set.case_ids))
    if unknown:
        raise ValueError(f"unknown case ids: {unknown}")
    pending = [case_id for case_id in case_set.case_ids if not only or case_id in only]
    reconnects = 0
    while pending:
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
                while pending:
                    await _solve_one(
                        case_set.cases[pending[0]], gateway, trace, contracts, output_root
                    )
                    print(f"done {pending.pop(0)} ({len(pending)} left)", file=sys.stderr)
        except BaseException as exc:
            trace.rollback()
            if not _connection_lost(exc):
                # Surface a single real error (e.g. a contract violation) without the
                # task-group wrapping that anyio adds around it.
                leaves = _leaves(exc)
                if len(leaves) == 1 and leaves[0] is not exc:
                    raise leaves[0] from None
                raise
            reconnects += 1
            if reconnects > MAX_RECONNECTS:
                raise RuntimeError(f"MCP connection lost {reconnects} times; giving up") from exc
            print(f"WARN: MCP connection lost at {pending[0]}; reconnecting", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--case", action="append", dest="cases", metavar="CASE_ID",
        help="only run these cases (repeatable; for development, not for submission)",
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, args.cases))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
