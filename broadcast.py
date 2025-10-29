"""Telegram broadcast scheduler.

This module exposes both a command line interface and reusable helpers for
sending a message to every dialog in a Telegram folder at user-defined hourly
intervals. The helpers are shared with the desktop GUI so that both
experiences stay in sync.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Optional, Sequence

from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.errors import RPCError

LogCallback = Callable[[str], None]


@dataclass
class BroadcastConfig:
    """Input data required to execute a broadcast schedule."""

    message: str
    folder: str
    delays: Iterable[int]
    session: str = os.getenv("TELEGRAM_SESSION", "broadcast")
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    dry_run: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a message to all chats from a specified Telegram folder at scheduled hours.",
    )
    parser.add_argument(
        "message",
        help="Text of the message to broadcast.",
    )
    parser.add_argument(
        "folder",
        help="Title of the Telegram folder to target.",
    )
    parser.add_argument(
        "--delays",
        type=int,
        nargs="+",
        default=[0],
        metavar="HOUR",
        help=(
            "List of hour delays (0-24) after the script starts when the broadcast should run. "
            "Use 0 for an immediate send."
        ),
    )
    parser.add_argument(
        "--session",
        default=os.getenv("TELEGRAM_SESSION", "broadcast"),
        help="Telethon session name or file path. Defaults to TELEGRAM_SESSION env or 'broadcast'.",
    )
    parser.add_argument(
        "--api-id",
        type=int,
        help="Telegram API ID. Defaults to TELEGRAM_API_ID environment variable.",
    )
    parser.add_argument(
        "--api-hash",
        help="Telegram API hash. Defaults to TELEGRAM_API_HASH environment variable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List chats that would receive the message without sending anything.",
    )
    return parser.parse_args()


def _emit(logger: LogCallback | None, message: str) -> None:
    (logger or print)(message)


def _normalise_delays(delays: Iterable[int]) -> List[int]:
    unique_delays = sorted({int(delay) for delay in delays})
    for delay in unique_delays:
        if delay < 0 or delay > 24:
            raise ValueError("Delays must be between 0 and 24 hours inclusive.")
    return unique_delays


def _resolve_credential(value: str | int | None, env_name: str) -> str:
    if value is not None:
        return str(value)
    try:
        return os.environ[env_name]
    except KeyError as exc:  # pragma: no cover - simple error conversion
        raise RuntimeError(
            f"Missing required credential: provide --{env_name.lower()} or set {env_name}"
        ) from exc


async def resolve_folder_id(client: TelegramClient, folder_title: str) -> int:
    response: Sequence = await client(functions.messages.GetDialogFiltersRequest())
    for dialog_filter in response:
        if getattr(dialog_filter, "title", "").lower() == folder_title.lower():
            return dialog_filter.id
    raise ValueError(f"Folder '{folder_title}' was not found in your Telegram account.")


async def collect_chats(client: TelegramClient, folder_id: int):
    dialogs = await client.get_dialogs(folder=folder_id)
    return [dialog.entity for dialog in dialogs]


async def broadcast_once(
    client: TelegramClient,
    chats,
    message: str,
    dry_run: bool = False,
    logger: LogCallback | None = None,
) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if dry_run:
        _emit(logger, f"[DRY RUN {timestamp}] Would send to {len(chats)} chats:")
        for chat in chats:
            _emit(
                logger,
                f" - {getattr(chat, 'title', getattr(chat, 'username', 'Unknown chat'))}",
            )
        return

    _emit(logger, f"[{timestamp}] Sending message to {len(chats)} chats...")
    for chat in chats:
        try:
            await client.send_message(chat, message)
            _emit(
                logger,
                f" ✓ Sent to {getattr(chat, 'title', getattr(chat, 'username', 'chat'))}",
            )
        except RPCError as error:
            _emit(
                logger,
                f" ✗ Failed to send to {getattr(chat, 'title', getattr(chat, 'username', 'chat'))}: {error}",
            )


async def run_schedule(config: BroadcastConfig, logger: LogCallback | None = None) -> None:
    load_dotenv()

    api_id_value = config.api_id if config.api_id is not None else os.getenv("TELEGRAM_API_ID")
    api_hash_value = config.api_hash or os.getenv("TELEGRAM_API_HASH")

    api_id = int(_resolve_credential(api_id_value, "TELEGRAM_API_ID"))
    api_hash = _resolve_credential(api_hash_value, "TELEGRAM_API_HASH")

    delays = _normalise_delays(config.delays)

    async with TelegramClient(config.session, api_id, api_hash) as client:
        folder_id = await resolve_folder_id(client, config.folder)
        chats = await collect_chats(client, folder_id)

        previous = 0
        for delay in delays:
            wait_hours = delay - previous
            if wait_hours > 0:
                wait_seconds = wait_hours * 3600
                _emit(logger, f"Waiting {wait_hours} hour(s) before next broadcast...")
                await asyncio.sleep(wait_seconds)
            await broadcast_once(
                client,
                chats,
                config.message,
                dry_run=config.dry_run,
                logger=logger,
            )
            previous = delay


def config_from_args(args: argparse.Namespace) -> BroadcastConfig:
    return BroadcastConfig(
        message=args.message,
        folder=args.folder,
        delays=args.delays,
        session=args.session,
        api_id=args.api_id,
        api_hash=args.api_hash,
        dry_run=args.dry_run,
    )


def main() -> None:
    args = parse_args()
    config = config_from_args(args)
    asyncio.run(run_schedule(config, logger=print))


if __name__ == "__main__":
    main()
