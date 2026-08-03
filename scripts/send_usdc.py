#!/usr/bin/env python3
"""
Transfer USDC on Solana from a source keypair to a target wallet.

Before sending, shows a confirmation prompt with:
  - transfer details (from, to, amount, network)
  - recent incoming USDC transfers to the target wallet (date, amount, total)

Usage:
  pip install -r scripts/requirements-usdc.txt
  python3 scripts/send_usdc.py --keypair ~/.config/solana/id.json --to <TARGET> --amount 10
  python3 scripts/send_usdc.py --keypair ./source.json --to <TARGET> --all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

USDC_MINTS = {
    "mainnet": Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    "devnet": Pubkey.from_string("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"),
}
USDC_DECIMALS = 6
DEFAULT_RPC = {
    "mainnet": "https://api.mainnet-beta.solana.com",
    "devnet": "https://api.devnet.solana.com",
}


@dataclass(frozen=True)
class IncomingTransfer:
    signature: str
    block_time: datetime | None
    amount: Decimal
    source: str | None


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def load_keypair(path: Path) -> Keypair:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read keypair {path}: {exc}")
    if not isinstance(raw, list) or not raw:
        die(f"keypair {path} must be a JSON array of bytes")
    try:
        return Keypair.from_bytes(bytes(raw))
    except Exception as exc:  # noqa: BLE001
        die(f"invalid keypair bytes in {path}: {exc}")


def parse_pubkey(value: str, label: str) -> Pubkey:
    try:
        return Pubkey.from_string(value)
    except Exception as exc:  # noqa: BLE001
        die(f"invalid {label} address {value!r}: {exc}")


def usdc_amount_to_raw(amount: Decimal) -> int:
    scaled = (amount * Decimal(10**USDC_DECIMALS)).quantize(Decimal("1"))
    if scaled <= 0:
        die("amount must be greater than zero")
    if scaled != int(scaled):
        die(f"amount {amount} has more than {USDC_DECIMALS} decimal places")
    return int(scaled)


def raw_to_usdc(raw: int) -> Decimal:
    return Decimal(raw) / Decimal(10**USDC_DECIMALS)


def format_usdc(amount: Decimal) -> str:
    normalized = amount.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} USDC"


def format_datetime(dt: datetime | None) -> str:
    if dt is None:
        return "(unknown time)"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def get_client(network: str, rpc_url: str | None) -> Client:
    url = rpc_url or os.environ.get("SOLANA_RPC_URL") or DEFAULT_RPC[network]
    return Client(url, commitment=Confirmed)


def token_account_exists(client: Client, ata: Pubkey) -> bool:
    response = client.get_account_info(ata)
    return response.value is not None


def get_token_balance_raw(client: Client, ata: Pubkey) -> int:
    response = client.get_token_account_balance(ata)
    value = getattr(response, "value", None)
    if value is None:
        return 0
    return int(value.amount)


def iter_parsed_instructions(tx: dict[str, Any]):
    message = tx.get("transaction", {}).get("message", {})
    for instruction in message.get("instructions", []):
        yield instruction
    meta = tx.get("meta") or {}
    for group in meta.get("innerInstructions") or []:
        for instruction in group.get("instructions", []):
            yield instruction


def extract_incoming_usdc(
  tx: dict[str, Any],
  *,
  target_wallet: Pubkey,
  target_ata: Pubkey,
  usdc_mint: Pubkey,
) -> IncomingTransfer | None:
    signature = tx.get("transaction", {}).get("signatures", [None])[0]
    block_time = tx.get("blockTime")
    dt = datetime.fromtimestamp(block_time, tz=timezone.utc) if block_time else None
    target_wallet_s = str(target_wallet)
    target_ata_s = str(target_ata)
    mint_s = str(usdc_mint)

    for instruction in iter_parsed_instructions(tx):
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") not in {"transfer", "transferChecked"}:
            continue
        info = parsed.get("info") or {}
        if info.get("mint") and info.get("mint") != mint_s:
            continue
        destination = info.get("destination") or info.get("account")
        if destination not in {target_wallet_s, target_ata_s}:
            continue
        amount_raw = info.get("amount")
        if amount_raw is None and "tokenAmount" in info:
            amount_raw = info["tokenAmount"].get("amount")
        if amount_raw is None:
            continue
        try:
            amount = raw_to_usdc(int(amount_raw))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        return IncomingTransfer(
            signature=signature or "",
            block_time=dt,
            amount=amount,
            source=info.get("authority") or info.get("owner") or info.get("source"),
        )
    return None


def fetch_incoming_transfers(
    client: Client,
    *,
    target_wallet: Pubkey,
    target_ata: Pubkey,
    usdc_mint: Pubkey,
    limit: int,
) -> list[IncomingTransfer]:
    addresses = [target_ata, target_wallet]
    seen: set[str] = set()
    transfers: list[IncomingTransfer] = []

    for address in addresses:
        before: Signature | None = None
        while len(transfers) < limit:
            batch_limit = min(25, limit - len(transfers))
            kwargs: dict[str, Any] = {"limit": batch_limit}
            if before is not None:
                kwargs["before"] = before
            response = client.get_signatures_for_address(address, **kwargs)
            entries = response.value or []
            if not entries:
                break
            for entry in entries:
                sig = str(entry.signature)
                if sig in seen:
                    continue
                seen.add(sig)
                tx_response = client.get_transaction(
                    entry.signature,
                    encoding="jsonParsed",
                    max_supported_transaction_version=0,
                )
                tx = tx_response.value
                if tx is None:
                    continue
                if hasattr(tx, "to_json"):
                    tx = json.loads(tx.to_json())
                incoming = extract_incoming_usdc(
                    tx,
                    target_wallet=target_wallet,
                    target_ata=target_ata,
                    usdc_mint=usdc_mint,
                )
                if incoming is not None:
                    transfers.append(incoming)
                    if len(transfers) >= limit:
                        break
            before = entries[-1].signature
            if len(entries) < batch_limit:
                break
    transfers.sort(key=lambda item: item.block_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return transfers


def print_history(transfers: list[IncomingTransfer]) -> Decimal:
    print("\nRecent incoming USDC to target:")
    if not transfers:
        print("  (no recent incoming USDC transfers found)")
        return Decimal(0)

    total = sum((item.amount for item in transfers), start=Decimal(0))
    print(f"  {'Date':<22} {'Amount':>14}  From")
    print(f"  {'-' * 22} {'-' * 14}  {'-' * 44}")
    for item in transfers:
        source = item.source or "(unknown)"
        if len(source) > 44:
            source = source[:20] + "..." + source[-18:]
        print(f"  {format_datetime(item.block_time):<22} {format_usdc(item.amount):>14}  {source}")
    print(f"\n  Total across listed transfers: {format_usdc(total)}")
    return total


def build_transfer_transaction(
    client: Client,
    *,
    payer: Keypair,
    source_owner: Pubkey,
    destination_wallet: Pubkey,
    usdc_mint: Pubkey,
    amount_raw: int,
) -> VersionedTransaction:
    source_ata = get_associated_token_address(source_owner, usdc_mint)
    destination_ata = get_associated_token_address(destination_wallet, usdc_mint)

    instructions = []
    if not token_account_exists(client, destination_ata):
        instructions.append(
            create_associated_token_account(
                payer=source_owner,
                owner=destination_wallet,
                mint=usdc_mint,
            )
        )

    instructions.append(
        transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata,
                mint=usdc_mint,
                dest=destination_ata,
                owner=source_owner,
                amount=amount_raw,
                decimals=USDC_DECIMALS,
                signers=[],
            )
        )
    )

    blockhash = client.get_latest_blockhash().value.blockhash
    message = MessageV0.try_compile(
        payer=source_owner,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    return VersionedTransaction(message, [payer])


def parse_amount_arg(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        die(f"invalid amount {value!r}: {exc}")
    if amount <= 0:
        die("amount must be greater than zero")
    return amount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send USDC on Solana with confirmation and target history")
    parser.add_argument("--keypair", type=Path, required=True, help="path to source wallet keypair JSON")
    parser.add_argument("--to", required=True, help="target wallet public key")
    amount = parser.add_mutually_exclusive_group(required=True)
    amount.add_argument("--amount", help="USDC amount to send (human-readable, e.g. 12.5)")
    amount.add_argument("--all", action="store_true", help="send entire USDC balance")
    parser.add_argument("--network", choices=("mainnet", "devnet"), default="mainnet")
    parser.add_argument("--rpc", help="RPC URL (default: SOLANA_RPC_URL or public endpoint)")
    parser.add_argument("--history-limit", type=int, default=10, help="incoming transfers to show (default: 10)")
    parser.add_argument("--yes", "-y", action="store_true", help="skip interactive confirmation")
    parser.add_argument("--dry-run", action="store_true", help="show confirmation details without sending")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.history_limit < 1:
        die("--history-limit must be at least 1")

    payer = load_keypair(args.keypair.expanduser())
    source_owner = payer.pubkey()
    destination_wallet = parse_pubkey(args.to, "target wallet")
    usdc_mint = USDC_MINTS[args.network]
    client = get_client(args.network, args.rpc)

    source_ata = get_associated_token_address(source_owner, usdc_mint)
    destination_ata = get_associated_token_address(destination_wallet, usdc_mint)
    source_balance_raw = get_token_balance_raw(client, source_ata)
    if source_balance_raw <= 0:
        die(f"source wallet {source_owner} has no USDC balance on {args.network}")

    if args.all:
        amount_raw = source_balance_raw
    else:
        amount_raw = usdc_amount_to_raw(parse_amount_arg(args.amount))
        if amount_raw > source_balance_raw:
            die(
                f"insufficient balance: requested {format_usdc(raw_to_usdc(amount_raw))}, "
                f"available {format_usdc(raw_to_usdc(source_balance_raw))}"
            )

    amount = raw_to_usdc(amount_raw)
    destination_exists = token_account_exists(client, destination_ata)

    print("USDC transfer preview")
    print(f"  Network:      {args.network}")
    print(f"  From:         {source_owner}")
    print(f"  To:           {destination_wallet}")
    print(f"  Amount:       {format_usdc(amount)}")
    print(f"  Source bal.:  {format_usdc(raw_to_usdc(source_balance_raw))}")
    if not destination_exists:
        print("  Note:         destination USDC account will be created (rent paid by source wallet)")

    history_total = print_history(
        fetch_incoming_transfers(
            client,
            target_wallet=destination_wallet,
            target_ata=destination_ata,
            usdc_mint=usdc_mint,
            limit=args.history_limit,
        )
    )
    print(f"\n  Listed incoming total: {format_usdc(history_total)}")

    if args.dry_run:
        print("\nDry run — transaction not sent.")
        return

    if not args.yes:
        print("\nProceed with this transfer? [y/N]: ", end="", flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return

    transaction = build_transfer_transaction(
        client,
        payer=payer,
        source_owner=source_owner,
        destination_wallet=destination_wallet,
        usdc_mint=usdc_mint,
        amount_raw=amount_raw,
    )
    response = client.send_transaction(
        transaction,
        opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
    )
    signature = str(response.value)
    print(f"\nSent {format_usdc(amount)} to {destination_wallet}")
    print(f"Signature: {signature}")
    if args.network == "mainnet":
        print(f"Explorer:  https://solscan.io/tx/{signature}")
    else:
        print(f"Explorer:  https://solscan.io/tx/{signature}?cluster=devnet")


if __name__ == "__main__":
    main()
