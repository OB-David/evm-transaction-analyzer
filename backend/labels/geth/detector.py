"""Detect arbitrage labels from Geth call traces."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from .models import Arbitrage, Swap, Trace, Transfer


ETH_TOKEN_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
ZEROX_EXCHANGE_PROXY = "0xdef1c0ded9bec7f1a1670819833240f027b25eff"
BANCOR_NETWORK = "0x2f9ec37d6ccff1cab21733bdadedee11c823ccb0"
MAX_TOKEN_AMOUNT_PERCENT_DIFFERENCE = 0.01


def _selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


TRANSFER = _selector("transfer(address,uint256)")
TRANSFER_FROM = _selector("transferFrom(address,address,uint256)")
UNISWAP_V2_SWAP = _selector("swap(uint256,uint256,address,bytes)")
UNISWAP_V3_SWAP = _selector("swap(address,bool,int256,uint160,bytes)")
CURVE_EXCHANGE = _selector("exchange(int128,int128,uint256,uint256)")
CURVE_EXCHANGE_UNDERLYING = _selector(
    "exchange_underlying(int128,int128,uint256,uint256)"
)
BALANCER_SWAP_IN = _selector(
    "swapExactAmountIn(address,uint256,address,uint256,uint256)"
)
BALANCER_SWAP_OUT = _selector(
    "swapExactAmountOut(address,uint256,address,uint256,uint256)"
)
BANCOR_CONVERT = _selector(
    "convertByPath(address[],uint256,uint256,address,address,uint256)"
)


CURVE_POOLS = {
    "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7",
    "0xdebf20617708857ebe4f679508e7b7863a8a8eee",
    "0xa96a65c051bf88b4095ee1f2451c2a9d43f53ae2",
    "0x79a8c46dea5ada233abaffd40f3a0a2b1e5a4f27",
    "0xa2b47e3d5c44877cca798226b7b8118f9bfb7a56",
    "0x0ce6a5ff5217e38315f87032cf90686c96627caa",
    "0x4ca9b3063ec5866a4b82e437059d2c43d1be596f",
    "0x2dded6da1bf5dbdf597c45fcfaa3194e53ecfeaf",
    "0xf178c0b5bb7e7abf4e12a4838c7b7c5ba2c623c0",
    "0x06364f10b501e868329afbc005b3492902d6c763",
    "0x93054188d876f558f4a66b2ef1d97d16edf0895b",
    "0xf9440930043eb3997fc70e1339dbb11f341de7a8",
    "0xeb16ae0052ed37f479f7fe63849198df1765a733",
    "0x7fc77b5c7614e1533320ea6ddc2eb61fa00a9714",
    "0xc5424b857f758e906013f3555dad202e4bdb4567",
    "0xdc24316b9ae028f1497c275eb9192a3ea0f67022",
    "0xa5407eae9ba41422680e2e00537571bcc53efbfd",
    "0x52ea46506b9cc5ef470c5bf89f17dc28bb35d85c",
    "0x45f783cce6b7ff23b2ab2d70e416cdb7d6055f51",
    "0x8925d9d9b4569d737a48499def3f67baa5a144b9",
    "0x071c661b4deefb59e2a3ddb20db036821eee8f4b",
    "0x8038c01a0390a8c547446a0b2c18fc9aefecc10c",
    "0x4f062658eaaf2c1ccf8c8e36d6824cdf41167956",
    "0x3ef6a01a0f81d6046290f3e2a8c5b843e738e604",
    "0xe7a24ef0c5e95ffb0f6684b813a78f2a3ad7d171",
    "0x8474ddbe98f5aa3179b3b3f5942d724afcdec9f6",
    "0xd81da8d904b52208541bade1bd6595d8a251f8dd",
    "0x7f55dde206dbad629c080068923b36fe9d6bdbef",
    "0xc18cc39da8b11da8c3541c598ee022258f9744da",
    "0xc25099792e9349c7dd09759744ea681c7de2cb66",
    "0x3e01dd8a5e1fb3481f0f589056b428fc308af0fb",
    "0x0f9cb53ebe405d49a0bbdbd291a65ff571bc83e1",
    "0x42d7025938bec20b69cbae5a77421082407f053a",
    "0x890f4e345b1daed0367a877a1612f86a1f86985f",
}


ZEROX_SIGNATURES: dict[str, tuple[str, list[str]]] = {}
for _signature, _types in {
    "fillRfqOrder((address,address,uint128,uint128,address,address,address,bytes32,uint64,uint256),(uint8,uint8,bytes32,bytes32),uint128)": [
        "(address,address,uint128,uint128,address,address,address,bytes32,uint64,uint256)",
        "(uint8,uint8,bytes32,bytes32)",
        "uint128",
    ],
    "_fillRfqOrder((address,address,uint128,uint128,address,address,address,bytes32,uint64,uint256),(uint8,uint8,bytes32,bytes32),uint128,address,bool,address)": [
        "(address,address,uint128,uint128,address,address,address,bytes32,uint64,uint256)",
        "(uint8,uint8,bytes32,bytes32)",
        "uint128",
        "address",
        "bool",
        "address",
    ],
    "fillOrKillLimitOrder((address,address,uint128,uint128,uint128,address,address,address,address,bytes32,uint64,uint256),(uint8,uint8,bytes32,bytes32),uint128)": [
        "(address,address,uint128,uint128,uint128,address,address,address,address,bytes32,uint64,uint256)",
        "(uint8,uint8,bytes32,bytes32)",
        "uint128",
    ],
    "fillLimitOrder((address,address,uint128,uint128,uint128,address,address,address,address,bytes32,uint64,uint256),(uint8,uint8,bytes32,bytes32),uint128)": [
        "(address,address,uint128,uint128,uint128,address,address,address,address,bytes32,uint64,uint256)",
        "(uint8,uint8,bytes32,bytes32)",
        "uint128",
    ],
    "_fillLimitOrder((address,address,uint128,uint128,uint128,address,address,address,address,bytes32,uint64,uint256),(uint8,uint8,bytes32,bytes32),uint128,address,address)": [
        "(address,address,uint128,uint128,uint128,address,address,address,address,bytes32,uint64,uint256)",
        "(uint8,uint8,bytes32,bytes32)",
        "uint128",
        "address",
        "address",
    ],
}.items():
    ZEROX_SIGNATURES[_selector(_signature)] = (_signature, _types)


def detect_arbitrages(traces: list[Trace]) -> list[Arbitrage]:
    """Return the original mev-inspect closed-swap-cycle labels for one block."""

    classified = [_classify_trace(trace) for trace in traces]
    swaps = _get_swaps(classified)
    return _get_arbitrages(swaps)


def detect_profitable_transactions(traces: list[Trace]) -> list[dict[str, Any]]:
    """Return unique successful, positive-profit transaction markers."""

    by_hash: dict[str, int] = {}
    for arbitrage in detect_arbitrages(traces):
        if arbitrage.error is None and arbitrage.profit_amount > 0:
            by_hash[arbitrage.transaction_hash] = arbitrage.block_number
    return [
        {"tx_hash": tx_hash, "block_number": block_number}
        for tx_hash, block_number in sorted(by_hash.items())
    ]


def _classify_trace(trace: Trace) -> Trace:
    if trace.trace_type != "call" or trace.to_address is None:
        return trace
    selector = trace.input[:10]
    try:
        if selector == TRANSFER:
            recipient, amount = _decode_call(
                trace.input, ["address", "uint256"]
            )
            trace.classification = "transfer"
            trace.abi_name = "ERC20"
            trace.function_signature = "transfer(address,uint256)"
            trace.inputs = {
                "recipient": str(recipient).lower(),
                "amount": int(amount),
            }
        elif selector == TRANSFER_FROM:
            sender, recipient, amount = _decode_call(
                trace.input, ["address", "address", "uint256"]
            )
            trace.classification = "transfer"
            trace.abi_name = "ERC20"
            trace.function_signature = "transferFrom(address,address,uint256)"
            trace.inputs = {
                "sender": str(sender).lower(),
                "recipient": str(recipient).lower(),
                "amount": int(amount),
            }
        elif selector == UNISWAP_V2_SWAP:
            _, _, recipient, _ = _decode_call(
                trace.input, ["uint256", "uint256", "address", "bytes"]
            )
            _mark_swap(
                trace,
                protocol="uniswap_v2",
                abi_name="UniswapV2Pair",
                signature="swap(uint256,uint256,address,bytes)",
                recipient=str(recipient).lower(),
            )
        elif selector == UNISWAP_V3_SWAP:
            recipient, _, _, _, _ = _decode_call(
                trace.input,
                ["address", "bool", "int256", "uint160", "bytes"],
            )
            _mark_swap(
                trace,
                protocol="uniswap_v3",
                abi_name="UniswapV3Pool",
                signature="swap(address,bool,int256,uint160,bytes)",
                recipient=str(recipient).lower(),
            )
        elif selector in {CURVE_EXCHANGE, CURVE_EXCHANGE_UNDERLYING} and trace.to_address in CURVE_POOLS:
            _decode_call(trace.input, ["int128", "int128", "uint256", "uint256"])
            _mark_swap(
                trace,
                protocol="curve",
                abi_name="StableSwap",
                signature=(
                    "exchange(int128,int128,uint256,uint256)"
                    if selector == CURVE_EXCHANGE
                    else "exchange_underlying(int128,int128,uint256,uint256)"
                ),
                recipient=trace.from_address,
            )
        elif selector in {BALANCER_SWAP_IN, BALANCER_SWAP_OUT}:
            _decode_call(
                trace.input,
                ["address", "uint256", "address", "uint256", "uint256"],
            )
            _mark_swap(
                trace,
                protocol="balancer_v1",
                abi_name="BPool",
                signature=(
                    "swapExactAmountIn(address,uint256,address,uint256,uint256)"
                    if selector == BALANCER_SWAP_IN
                    else "swapExactAmountOut(address,uint256,address,uint256,uint256)"
                ),
                recipient=trace.from_address,
            )
        elif selector == BANCOR_CONVERT and trace.to_address == BANCOR_NETWORK:
            _decode_call(
                trace.input,
                ["address[]", "uint256", "uint256", "address", "address", "uint256"],
            )
            _mark_swap(
                trace,
                protocol="bancor",
                abi_name="BancorNetwork",
                signature="convertByPath(address[],uint256,uint256,address,address,uint256)",
                recipient=trace.from_address,
            )
        elif selector in ZEROX_SIGNATURES and trace.to_address == ZEROX_EXCHANGE_PROXY:
            signature, types = ZEROX_SIGNATURES[selector]
            decoded = _decode_call(trace.input, types)
            trace.classification = "swap"
            trace.protocol = "0x"
            trace.abi_name = "INativeOrdersFeature"
            trace.function_signature = signature
            trace.inputs = {
                "order": decoded[0],
                "takerTokenFillAmount": decoded[2],
            }
    except (DecodingError, ValueError, OverflowError):
        return trace
    return trace


def _mark_swap(
    trace: Trace,
    *,
    protocol: str,
    abi_name: str,
    signature: str,
    recipient: str | None,
) -> None:
    trace.classification = "swap"
    trace.protocol = protocol
    trace.abi_name = abi_name
    trace.function_signature = signature
    trace.inputs = {"recipient": recipient}


def _decode_call(data: str, types: list[str]) -> tuple[Any, ...]:
    return decode(types, bytes.fromhex(data[10:]), strict=True)


def _get_swaps(traces: list[Trace]) -> list[Swap]:
    by_transaction: dict[str, list[Trace]] = defaultdict(list)
    for trace in traces:
        by_transaction[trace.transaction_hash].append(trace)
    swaps: list[Swap] = []
    for transaction_traces in by_transaction.values():
        swaps.extend(_get_transaction_swaps(transaction_traces))
    return swaps


def _get_transaction_swaps(traces: list[Trace]) -> list[Swap]:
    ordered_traces = sorted(traces, key=lambda item: item.trace_address)
    swaps: list[Swap] = []
    prior_transfers: list[Transfer] = []
    for trace in ordered_traces:
        if trace.classification == "transfer":
            transfer = _get_transfer(trace)
            if transfer is not None:
                prior_transfers.append(transfer)
        elif trace.classification == "swap":
            child_transfers = [
                transfer
                for child in _child_traces(trace, traces)
                if (transfer := _get_transfer(child)) is not None
            ]
            swap = _parse_swap(
                trace,
                _remove_nested_transfers(prior_transfers),
                _remove_nested_transfers(child_transfers),
            )
            if swap is not None:
                swaps.append(swap)
    return swaps


def _get_transfer(trace: Trace) -> Transfer | None:
    if (
        trace.value is not None
        and trace.value > 0
        and trace.input == "0x"
        and trace.from_address is not None
        and trace.to_address is not None
    ):
        return Transfer(
            block_number=trace.block_number,
            transaction_hash=trace.transaction_hash,
            trace_address=trace.trace_address,
            amount=trace.value,
            to_address=trace.to_address,
            from_address=trace.from_address,
            token_address=ETH_TOKEN_ADDRESS,
        )
    if (
        trace.classification == "transfer"
        and trace.from_address is not None
        and trace.to_address is not None
    ):
        return Transfer(
            block_number=trace.block_number,
            transaction_hash=trace.transaction_hash,
            trace_address=trace.trace_address,
            amount=int(trace.inputs["amount"]),
            to_address=str(trace.inputs["recipient"]).lower(),
            from_address=str(trace.inputs.get("sender", trace.from_address)).lower(),
            token_address=trace.to_address,
        )
    return None


def _child_traces(parent: Trace, traces: Iterable[Trace]) -> list[Trace]:
    return [
        trace
        for trace in traces
        if trace.transaction_hash == parent.transaction_hash
        and _is_child_address(trace.trace_address, parent.trace_address)
    ]


def _is_child_address(child: list[int], parent: list[int]) -> bool:
    return len(child) > len(parent) and child[: len(parent)] == parent


def _remove_nested_transfers(transfers: list[Transfer]) -> list[Transfer]:
    result: list[Transfer] = []
    parents: dict[str, list[list[int]]] = defaultdict(list)
    for transfer in sorted(transfers, key=lambda item: item.trace_address):
        if not any(
            _is_child_address(transfer.trace_address, parent)
            for parent in parents[transfer.transaction_hash]
        ):
            result.append(transfer)
        parents[transfer.transaction_hash].append(transfer.trace_address)
    return result


def _parse_swap(
    trace: Trace,
    prior_transfers: list[Transfer],
    child_transfers: list[Transfer],
) -> Swap | None:
    if trace.from_address is None or trace.to_address is None or trace.protocol is None:
        return None
    if trace.protocol == "0x":
        return _parse_zero_x_swap(trace, child_transfers)
    if trace.protocol == "bancor":
        recipient = trace.from_address
        transfers_from = [
            transfer
            for transfer in [*prior_transfers, *child_transfers]
            if transfer.from_address == recipient
        ]
        transfers_to = [
            transfer for transfer in child_transfers if transfer.to_address == recipient
        ]
        if len(transfers_from) != 1 or len(transfers_to) != 1:
            return None
        return _build_swap(
            trace,
            BANCOR_NETWORK,
            transfers_from[0],
            transfers_to[0],
        )

    recipient = str(trace.inputs.get("recipient") or trace.from_address).lower()
    pool_address = trace.to_address
    transfers_to_pool: list[Transfer] = []
    if trace.value is not None and trace.value > 0:
        transfers_to_pool = [
            Transfer(
                block_number=trace.block_number,
                transaction_hash=trace.transaction_hash,
                trace_address=trace.trace_address,
                amount=trace.value,
                to_address=pool_address,
                from_address=trace.from_address,
                token_address=ETH_TOKEN_ADDRESS,
            )
        ]
    if not transfers_to_pool:
        transfers_to_pool = [
            transfer for transfer in prior_transfers if transfer.to_address == pool_address
        ]
    if not transfers_to_pool:
        transfers_to_pool = [
            transfer for transfer in child_transfers if transfer.to_address == pool_address
        ]
    if not transfers_to_pool:
        return None
    transfers_from_pool = [
        transfer
        for transfer in child_transfers
        if transfer.to_address == recipient and transfer.from_address == pool_address
    ]
    if len(transfers_from_pool) != 1:
        return None
    transfer_in = transfers_to_pool[-1]
    transfer_out = transfers_from_pool[0]
    if transfer_in.token_address == transfer_out.token_address:
        return None
    return _build_swap(trace, pool_address, transfer_in, transfer_out)


def _build_swap(
    trace: Trace,
    contract_address: str,
    transfer_in: Transfer,
    transfer_out: Transfer,
) -> Swap:
    return Swap(
        abi_name=trace.abi_name or "unknown",
        transaction_hash=trace.transaction_hash,
        transaction_position=trace.transaction_position,
        block_number=trace.block_number,
        trace_address=trace.trace_address,
        contract_address=contract_address,
        protocol=trace.protocol or "unknown",
        from_address=transfer_in.from_address,
        to_address=transfer_out.to_address,
        token_in_address=transfer_in.token_address,
        token_in_amount=transfer_in.amount,
        token_out_address=transfer_out.token_address,
        token_out_amount=transfer_out.amount,
        error=trace.error,
    )


def _parse_zero_x_swap(trace: Trace, child_transfers: list[Transfer]) -> Swap | None:
    if len(child_transfers) < 2 or trace.from_address is None or trace.to_address is None:
        return None
    order = trace.inputs["order"]
    token_out_address = str(order[0]).lower()
    token_in_address = str(order[1]).lower()
    is_rfq = "Rfq" in (trace.function_signature or "")
    taker_address = str(order[5] if is_rfq else order[6]).lower()
    token_out_amount = 0
    if trace.error is None:
        for transfer in child_transfers:
            if (
                taker_address == "0x0000000000000000000000000000000000000000"
                and transfer.token_address == token_out_address
            ) or transfer.to_address == taker_address:
                token_out_amount = transfer.amount
                break
        else:
            raise RuntimeError("unable to find transfers matching 0x order")
    return Swap(
        abi_name=trace.abi_name or "INativeOrdersFeature",
        transaction_hash=trace.transaction_hash,
        transaction_position=trace.transaction_position,
        block_number=trace.block_number,
        trace_address=trace.trace_address,
        contract_address=trace.to_address,
        protocol="0x",
        from_address=trace.from_address,
        to_address=trace.to_address,
        token_in_address=token_in_address,
        token_in_amount=int(trace.inputs["takerTokenFillAmount"]),
        token_out_address=token_out_address,
        token_out_amount=token_out_amount,
        error=trace.error,
    )


def _get_arbitrages(swaps: list[Swap]) -> list[Arbitrage]:
    by_transaction: dict[str, list[Swap]] = defaultdict(list)
    for swap in swaps:
        by_transaction[swap.transaction_hash].append(swap)
    result: list[Arbitrage] = []
    for transaction_swaps in by_transaction.values():
        result.extend(_transaction_arbitrages(transaction_swaps))
    return result


def _transaction_arbitrages(swaps: list[Swap]) -> list[Arbitrage]:
    all_arbitrages: list[Arbitrage] = []
    used_swaps: list[Swap] = []
    for start, ends in _all_start_ends(swaps):
        if start in used_swaps:
            continue
        route = _shortest_route(
            start,
            [end for end in ends if end not in used_swaps],
            swaps,
        )
        if route is None:
            continue
        error = next((swap.error for swap in route if swap.error is not None), None)
        arbitrage = Arbitrage(
            swaps=route,
            block_number=route[0].block_number,
            transaction_hash=route[0].transaction_hash,
            account_address=route[0].from_address,
            profit_token_address=route[0].token_in_address,
            start_amount=route[0].token_in_amount,
            end_amount=route[-1].token_out_amount,
            profit_amount=route[-1].token_out_amount - route[0].token_in_amount,
            error=error,
        )
        all_arbitrages.append(arbitrage)
        used_swaps.extend(route)
    if len(all_arbitrages) <= 1:
        return all_arbitrages
    return [
        arbitrage
        for arbitrage in all_arbitrages
        if arbitrage.swaps[0].trace_address < arbitrage.swaps[-1].trace_address
    ]


def _all_start_ends(swaps: list[Swap]) -> list[tuple[Swap, list[Swap]]]:
    pool_addresses = [swap.contract_address for swap in swaps]
    result: list[tuple[Swap, list[Swap]]] = []
    for index, start in enumerate(swaps):
        ends = [
            end
            for end in swaps[:index] + swaps[index + 1 :]
            if start.token_in_address == end.token_out_address
            and start.contract_address != end.contract_address
            and start.from_address == end.to_address
            and start.from_address not in pool_addresses
        ]
        if ends:
            result.append((start, ends))
    return result


def _shortest_route(
    start: Swap,
    ends: list[Swap],
    all_swaps: list[Swap],
    max_route_length: int | None = None,
) -> list[Swap] | None:
    if not ends or (max_route_length is not None and max_route_length < 2):
        return None
    for end in ends:
        if _swap_outputs_match_inputs(start, end):
            return [start, end]
    if max_route_length == 2:
        return None
    others = [swap for swap in all_swaps if swap is not start and swap not in ends]
    if not others:
        return None
    shortest: list[Swap] | None = None
    remaining_limit = None if max_route_length is None else max_route_length - 1
    for next_swap in others:
        if not _swap_outputs_match_inputs(start, next_swap):
            continue
        candidate = _shortest_route(next_swap, ends, others, remaining_limit)
        if candidate is not None and (shortest is None or len(candidate) < len(shortest)):
            shortest = candidate
            remaining_limit = len(candidate) - 1
    return None if shortest is None else [start, *shortest]


def _swap_outputs_match_inputs(first: Swap, second: Swap) -> bool:
    return (
        first.token_out_address == second.token_in_address
        and (
            first.contract_address == second.from_address
            or first.to_address == second.contract_address
            or first.to_address == second.from_address
        )
        and _equal_within_percent(
            first.token_out_amount,
            second.token_in_amount,
            MAX_TOKEN_AMOUNT_PERCENT_DIFFERENCE,
        )
    )


def _equal_within_percent(first: int, second: int, threshold: float) -> bool:
    denominator = 0.5 * (first + second)
    return denominator != 0 and abs((first - second) / denominator) < threshold
