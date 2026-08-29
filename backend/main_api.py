"""CLI wrapper that accepts tx_hash as argument and runs the analysis pipeline."""
import sys
import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict
from web3 import Web3

# 处理编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from dotenv import load_dotenv
from utils.evm_information import TraceFormatter
from utils.basic_block import BasicBlockProcessor
from utils.cfg_transaction import CFGConstructor
from utils.extract_token_changes import (
    pair_transactions,
    afg_to_fcfg,
    afg_to_pcfg,
    afg_to_call_tree,
    build_link_artifact,
    detect_arbitrage,
    compute_address_balances,
    build_balance_timeline,
)
from utils.swap_routes import build_arbitrage_artifact, build_swap_legs_artifact
from utils.indentify_swap import filter_to_file
from utils.plain_cfg_llm import PLAIN_SEMANTICS_FILENAME, write_plain_semantics_artifact
from utils.call_tree import build_refined_hierarchical_trace
from main import create_result_directory, save_graphs

load_dotenv()

ANALYSIS_TIMING_FILENAME = "analysis_timing.json"
ANALYSIS_STAGE_PREFIX = "ANALYSIS_STAGE="


def emit_analysis_stage(stage: str) -> None:
    """Publish ephemeral progress to the parent server through stdout."""
    print(f"{ANALYSIS_STAGE_PREFIX}{stage}", flush=True)


class AnalysisTimingRecorder:
    """Persist wall-clock timings without mixing profiling into result contracts."""

    def __init__(self, result_dir: str, tx_hash: str):
        self.result_dir = result_dir
        self.tx_hash = tx_hash
        self.started_at = datetime.now(timezone.utc)
        self.started_counter = perf_counter()
        self.phases: list[dict[str, Any]] = []

    @contextmanager
    def measure(self, name: str):
        started = perf_counter()
        try:
            yield
        finally:
            self.record(name, perf_counter() - started)

    def record(self, name: str, duration_seconds: float) -> None:
        self.phases.append({
            "name": name,
            "duration_ms": round(duration_seconds * 1000, 3),
        })

    def write(self, *, complete: bool, error: str | None = None) -> None:
        artifact_sizes = {}
        for name in os.listdir(self.result_dir):
            path = os.path.join(self.result_dir, name)
            if os.path.isfile(path) and name != ANALYSIS_TIMING_FILENAME:
                artifact_sizes[name] = os.path.getsize(path)
        payload = {
            "schema_version": 1,
            "tx_hash": self.tx_hash,
            "started_at": self.started_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "complete": complete,
            "total_ms": round((perf_counter() - self.started_counter) * 1000, 3),
            "phases": self.phases,
            "artifact_sizes_bytes": artifact_sizes,
            "error": error,
        }
        path = os.path.join(self.result_dir, ANALYSIS_TIMING_FILENAME)
        temp_path = f"{path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)


def _normalize_edge_step(value: Any) -> int:
    if isinstance(value, bool): return 0
    if isinstance(value, int): return value
    try:
        return int(str(value))
    except Exception:
        return 0

def _build_edge_step_information_compat(cfg: Any) -> Dict[str, Dict[str, Any]]:
    """为前端生成 edge_id 与 step 的映射关系"""
    edges = list(getattr(cfg, "edges", []))
    indexed_edges = list(enumerate(edges))
    sorted_edges = sorted(
        indexed_edges,
        key=lambda item: (_normalize_edge_step(getattr(item[1], "edge_step", 0)), item[0]),
    )

    edge_step_map: Dict[str, Dict[str, Any]] = {}
    for rank, (_original_index, edge) in enumerate(sorted_edges, start=1):
        edge_id = f"edge_{rank}"
        setattr(edge, "edge_id", edge_id)
        edge_step = _normalize_edge_step(getattr(edge, "edge_step", 0))

        source_id = getattr(getattr(edge, "source", None), "id", "unknown")
        target_id = getattr(getattr(edge, "target", None), "id", "unknown")

        edge_step_map[edge_id] = {
            "edge_id": edge_id,
            "edge_step": edge_step,
            "source_node": f"node_{source_id}",
            "target_node": f"node_{target_id}",
        }
    return edge_step_map

def run(tx_hash: str):
    PROVIDER_URL = os.environ.get("GETH_API")
    result_dir = create_result_directory(tx_hash)
    timings = AnalysisTimingRecorder(result_dir, tx_hash)
    emit_analysis_stage("analyzing")

    with timings.measure("rpc_validate_transaction"):
        web3 = Web3(Web3.HTTPProvider(PROVIDER_URL))
        tx = web3.eth.get_transaction(tx_hash)
        from_address = tx.get('from')
        to_address = tx.get('to')
        amount = tx.get('value', 0)

        # 基础检查
        if to_address is None or to_address == "":
            raise ValueError("This contract creation transaction is not supported.")

        contract_code = web3.eth.get_code(to_address)
        if len(contract_code) == 0:
            raise ValueError("This ETH transfer transaction is not supported.")

    formatter = TraceFormatter(PROVIDER_URL)
    processor = BasicBlockProcessor()

    # 1. 获取分析数据
    with timings.measure("trace_fetch_and_standardize"):
        standardized_trace = formatter.get_standardized_trace(tx_hash)
    contracts_addresses = standardized_trace.get("contracts_addresses", [])
    slot_map = standardized_trace.get("slot_map", {})
    users_addresses = standardized_trace.get("users_addresses", [])
    erc20_token_map = standardized_trace.get("erc20_token_map", {})
    full_address_name_map = standardized_trace.get("full_address_name_map", {})

    # 2. 生成 CFG
    with timings.measure("rpc_fetch_contract_bytecode"):
        contracts_bytecode = formatter.get_all_contracts_bytecode(all_contracts=contracts_addresses)
    with timings.measure("build_static_basic_blocks"):
        all_blocks = processor.process_multiple_contracts(contracts_bytecode)
    with timings.measure("rpc_fetch_token_decimals"):
        token_decimals_map = {addr: formatter.get_token_decimals(addr) for addr in erc20_token_map.keys()}
    cfg_constructor = CFGConstructor(all_blocks, token_decimals_map)

    with timings.measure("construct_cfg_and_extract_changes"):
        plain_cfg, folded_cfg, original_cfg, all_changes, folded_node_map, _ = cfg_constructor.construct_cfg(
            standardized_trace, slot_map, erc20_token_map
        )

    # 3. 资产流分析
    original_transfer = [from_address.lower(), to_address.lower(), int(amount)]

    with timings.measure("pair_asset_transfers"):
        pairs, annotations, pending_erc20 = pair_transactions(original_transfer, all_changes, token_decimals_map)
    with timings.measure("build_sequence_tree"):
        tree_data = build_refined_hierarchical_trace(
            standardized_trace["steps"],
            root_calldata=tx.get("input"),
        )
    with timings.measure("map_afg_to_folded_cfg"):
        edge_link_fcfg = afg_to_fcfg(pairs, pending_erc20, folded_cfg)
    with timings.measure("map_afg_to_plain_cfg"):
        edge_link_pcfg = afg_to_pcfg(pairs, pending_erc20, plain_cfg)
    with timings.measure("map_afg_to_call_tree"):
        edge_link_call_tree = afg_to_call_tree(pairs, pending_erc20, tree_data)
    with timings.measure("detect_arbitrage_candidates"):
        arb_result = detect_arbitrage(pairs, pending_erc20, tree_data)
    with timings.measure("compute_address_balances"):
        addr_balances = compute_address_balances(pairs, pending_erc20)
        balance_timeline = build_balance_timeline(pairs, pending_erc20)

    # 4. 保存文件
    # Persist compact evidence contracts. The full trace remains process-local.
    with timings.measure("write_core_analysis_artifacts"):
        with open(os.path.join(result_dir, "balance_and_eth_changes.json"), "w", encoding="utf-8") as f:
            json.dump(all_changes, f, indent=2, ensure_ascii=False)

        with open(os.path.join(result_dir, "link.json"), "w", encoding="utf-8") as f:
            json.dump(
                build_link_artifact(edge_link_fcfg, edge_link_pcfg, edge_link_call_tree),
                f,
                ensure_ascii=False,
            )
        for legacy_name in ("TFG_link_FCFG.json", "TFG_link_PCFG.json"):
            legacy_path = os.path.join(result_dir, legacy_name)
            if os.path.isfile(legacy_path):
                os.remove(legacy_path)

        # Arbitrage & Balances
        with open(os.path.join(result_dir, "arbitrage.json"), "w", encoding="utf-8") as f:
            json.dump(build_arbitrage_artifact(arb_result), f, indent=2, ensure_ascii=False)

        with open(os.path.join(result_dir, "swap_legs.json"), "w", encoding="utf-8") as f:
            json.dump(build_swap_legs_artifact(arb_result), f, indent=2, ensure_ascii=False)

        with open(os.path.join(result_dir, "address_balances.json"), "w", encoding="utf-8") as f:
            json.dump(addr_balances, f, indent=2, ensure_ascii=False)

        with open(os.path.join(result_dir, "balance_timeline.json"), "w", encoding="utf-8") as f:
            json.dump(balance_timeline, f, indent=2, ensure_ascii=False)

    def publish_graph_stage(stage: str) -> None:
        emit_analysis_stage(stage)

    # 5. 渲染图表
    save_graphs(
        result_dir=result_dir,
        plain_cfg=plain_cfg,
        folded_cfg=folded_cfg,
        full_address_name_map=full_address_name_map,
        erc20_token_map=erc20_token_map,
        users_addresses=users_addresses,
        pairs=pairs,
        annotations=annotations,
        pending_erc20=pending_erc20,
        tree_data=tree_data,
        arb_result=arb_result,
        progress_callback=publish_graph_stage,
        timing_callback=timings.record,
    )

    with timings.measure("build_and_write_folded_cfg_metadata"):
        folded_blocks_path = os.path.join(result_dir, "folded_blocks_information.json")
        folded_blocks_map = cfg_constructor.build_fcfg_blocks_information(folded_cfg)
        with open(folded_blocks_path, "w", encoding="utf-8") as f:
            json.dump(folded_blocks_map, f, indent=2, ensure_ascii=False)
        filter_to_file(folded_blocks_path, os.path.join(result_dir, "swap_in_fcfg.json"))

        edge_step_map = _build_edge_step_information_compat(folded_cfg)
        with open(os.path.join(result_dir, "edge_id-step.json"), "w", encoding="utf-8") as f:
            json.dump(edge_step_map, f, indent=2, ensure_ascii=False)
    emit_analysis_stage("folded_info")

    with timings.measure("build_and_write_plain_cfg_metadata"):
        plain_blocks_path = os.path.join(result_dir, "plain_blocks_information.json")
        plain_blocks_map = cfg_constructor.build_pcfg_blocks_information(plain_cfg, standardized_trace)
        with open(plain_blocks_path, "w", encoding="utf-8") as f:
            json.dump(plain_blocks_map, f, indent=2, ensure_ascii=False)
        filter_to_file(plain_blocks_path, os.path.join(result_dir, "swap_in_pcfg.json"))

    # The graph contract is complete at this point. Plain-block LLM semantics
    # are an optional warm cache and must never delay graph availability.
    timings.write(complete=True)
    emit_analysis_stage("complete")
    print(f"RESULT_DIR={os.path.abspath(result_dir)}")
    _warm_plain_semantics(result_dir, standardized_trace["steps"], plain_blocks_map)


def _warm_plain_semantics(result_dir: str, steps: list[Any], plain_blocks_map: Dict[str, Any]) -> None:
    """Build click-only LLM context after graph completion, at lower priority."""
    semantics_path = os.path.join(result_dir, PLAIN_SEMANTICS_FILENAME)
    temp_path = f"{semantics_path}.tmp"
    try:
        if os.name != "nt":
            try:
                os.nice(10)
            except OSError:
                pass
        started = perf_counter()
        write_plain_semantics_artifact(semantics_path, steps, plain_blocks_map)
        print(
            f"PLAIN_SEMANTICS_WARMUP_MS={round((perf_counter() - started) * 1000, 3)}",
            flush=True,
        )
    except Exception as exc:
        # Graph artifacts are already complete. A warm-cache failure must not
        # turn a successful visualization analysis into a failed analysis.
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        print(f"WARNING: plain semantics warmup failed: {exc}", flush=True)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python main_api.py <tx_hash>", file=sys.stderr)
        sys.exit(1)
    tx_hash_arg = sys.argv[1]
    try:
        run(tx_hash_arg)
    except Exception:
        raise
