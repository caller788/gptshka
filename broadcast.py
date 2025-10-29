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

# ``python-dotenv`` is optional: if it isn't installed we skip auto-loading ``.env`` files
# and rely purely on the process environment variables. This keeps the broadcaster usable
# even in minimal deployments where only ``telethon`` is installed.
try:  # pragma: no cover - exercised indirectly via runtime behaviour
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    load_dotenv = None
from telethon import TelegramClient, functions, utils
from telethon.errors import RPCError
from telethon.tl import types

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


async def resolve_folder(client: TelegramClient, folder_title: str):
    response = await client(functions.messages.GetDialogFiltersRequest())
    filters: Sequence = getattr(response, "filters", ())
    for dialog_filter in filters:
        raw_title = getattr(dialog_filter, "title", "")
        if not isinstance(raw_title, str):
            raw_title = getattr(raw_title, "text", getattr(raw_title, "string", str(raw_title)))
        if raw_title.lower() == folder_title.lower():
            return dialog_filter
    raise ValueError(f"Folder '{folder_title}' was not found in your Telegram account.")


def _peer_ids(peers: Optional[Sequence]) -> set[int]:
    ids: set[int] = set()
    if not peers:
        return ids
    for peer in peers:
        try:
            ids.add(utils.get_peer_id(peer))
        except (TypeError, ValueError):
            continue
    return ids


def _is_muted(dialog) -> bool:
    settings = getattr(dialog.dialog, "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    if not mute_until:
        return False
    # Telegram uses large sentinel values for "mute forever"; any non-zero value
    # indicates that the dialog is muted for our purposes.
    return bool(mute_until)


def _matches_category(dialog, category_flags: dict[str, bool]) -> bool:
    entity = dialog.entity

    if category_flags.get("contacts") and isinstance(entity, types.User) and getattr(entity, "contact", False):
        return True
    if category_flags.get("non_contacts") and isinstance(entity, types.User) and not getattr(entity, "contact", False):
        return True
    if category_flags.get("bots") and isinstance(entity, types.User) and getattr(entity, "bot", False):
        return True
    if category_flags.get("groups") and dialog.is_group:
        return True
    if category_flags.get("broadcasts") and isinstance(entity, types.Channel) and getattr(entity, "broadcast", False):
        return True

    return False


async def collect_chats(client: TelegramClient, dialog_filter):
    """Return dialogs that belong to ``dialog_filter``.

    Telegram exposes folder membership via chat filters. To find chats we
    mirror the client-side filtering logic locally: explicitly included peers
    and chats matching the category toggles are accepted unless they are
    excluded or filtered out by the folder options.
    """

    include_ids = _peer_ids(getattr(dialog_filter, "include_peers", None))
    include_ids |= _peer_ids(getattr(dialog_filter, "pinned_peers", None))
    exclude_ids = _peer_ids(getattr(dialog_filter, "exclude_peers", None))

    category_flags: dict[str, bool] = {}
    if isinstance(dialog_filter, types.DialogFilter):
        category_flags = {
            "contacts": getattr(dialog_filter, "contacts", False),
            "non_contacts": getattr(dialog_filter, "non_contacts", False),
            "groups": getattr(dialog_filter, "groups", False),
            "broadcasts": getattr(dialog_filter, "broadcasts", False),
            "bots": getattr(dialog_filter, "bots", False),
        }

    include_all_by_default = not category_flags and not include_ids

    exclude_archived = getattr(dialog_filter, "exclude_archived", False)
    exclude_muted = getattr(dialog_filter, "exclude_muted", False)
    exclude_read = getattr(dialog_filter, "exclude_read", False)

    dialogs = await client.get_dialogs(limit=None)
    matched = []
    for dialog in dialogs:
        peer_id = getattr(dialog, "id", None)
        if peer_id is None:
            continue
        if peer_id in exclude_ids:
            continue
        if peer_id in include_ids:
            matched.append(dialog.entity)
            continue
        if exclude_archived and getattr(dialog, "archived", False):
            continue
        if exclude_muted and _is_muted(dialog):
            continue
        if exclude_read and getattr(dialog.dialog, "unread_count", 0) == 0:
            continue
        if include_all_by_default or _matches_category(dialog, category_flags):
            matched.append(dialog.entity)

    # Deduplicate while preserving order.
    seen = set()
    unique = []
    for chat in matched:
        peer_id = utils.get_peer_id(chat)
        if peer_id in seen:
            continue
        seen.add(peer_id)
        unique.append(chat)
    return unique


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
    if load_dotenv is not None:
        load_dotenv()
    else:
        _emit(logger, "python-dotenv не установлен — пропускаем загрузку .env")

    api_id_value = config.api_id if config.api_id is not None else os.getenv("TELEGRAM_API_ID")
    api_hash_value = config.api_hash or os.getenv("TELEGRAM_API_HASH")

    api_id = int(_resolve_credential(api_id_value, "TELEGRAM_API_ID"))
    api_hash = _resolve_credential(api_hash_value, "TELEGRAM_API_HASH")

    delays = _normalise_delays(config.delays)

    async with TelegramClient(config.session, api_id, api_hash) as client:
        dialog_filter = await resolve_folder(client, config.folder)
        chats = await collect_chats(client, dialog_filter)
        _emit(logger, f"Найдено {len(chats)} чатов в папке '{config.folder}'.")

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
