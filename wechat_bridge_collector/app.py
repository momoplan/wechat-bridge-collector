from __future__ import annotations

import argparse
import json
import sys
import threading

from .autostart import install_autostart, result_json, start_collector, status, stop_collector
from .bridge import BridgeClient
from .collection import IndependentLoops
from .config import CollectorConfig
from .query_server import QueryMethodServer
from .setup_keys import setup_collector
from .source_runtime import SourceRuntime
from .state import CollectorState
from .wechat_source import DatabaseSnapshotError, WeChatSource


def _load_config(args: argparse.Namespace) -> CollectorConfig:
    cfg = CollectorConfig.load(args.config)
    if getattr(args, "bridge_url", None):
        cfg.bridge_base_url = args.bridge_url
    if getattr(args, "event_token", None):
        cfg.bridge_event_token = args.event_token
    if getattr(args, "wechat_decrypt_dir", None):
        cfg.wechat_decrypt_dir = args.wechat_decrypt_dir
    if getattr(args, "wechat_decrypt_config", None):
        cfg.wechat_decrypt_config = args.wechat_decrypt_config
    if getattr(args, "db_dir", None):
        cfg.db_dir = args.db_dir
    if getattr(args, "keys_file", None):
        cfg.keys_file = args.keys_file
    if getattr(args, "state_dir", None):
        cfg.state_dir = args.state_dir
    if getattr(args, "method_host", None):
        cfg.method_host = args.method_host
    if getattr(args, "method_port", None) is not None:
        cfg.method_port = args.method_port
    if getattr(args, "poll_interval", None) is not None:
        cfg.poll_interval_secs = args.poll_interval
    if getattr(args, "batch_size", None) is not None:
        cfg.batch_size = args.batch_size
    return cfg


def cmd_init_config(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    path = cfg.save(args.output)
    print(f"wrote config: {path}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    result = setup_collector(cfg, force=args.force, extract_keys=not args.no_extract_keys)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    source = WeChatSource(cfg)
    print(json.dumps(source.probe(), ensure_ascii=False, indent=2))
    return 0


def cmd_install_autostart(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    result = install_autostart(cfg)
    print(result_json(result))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    result = start_collector(cfg)
    print(result_json(result))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    result = stop_collector(cfg)
    print(result_json(result))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    result = status(cfg)
    print(result_json(result))
    return 0 if result.status == "running" else 1


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if args.no_text:
        cfg.include_text = False
    if args.incoming_only:
        cfg.include_outgoing = False

    source_runtime = SourceRuntime(cfg)
    method_server = QueryMethodServer(cfg, source_runtime=source_runtime)
    method_server.start()
    source_runtime.initialize_async()
    state = CollectorState.load(cfg.state_path)
    loops = IndependentLoops()
    state_lock = threading.RLock()
    initialized = threading.Event()
    initialize_state = not cfg.state_path.exists() or args.reset_state

    def save_state() -> None:
        with state_lock:
            state.save(cfg.state_path)

    def messages() -> int:
        nonlocal initialize_state
        bridge = BridgeClient(cfg)
        failure_count = 0
        while not loops.stop.is_set():
            try:
                source = source_runtime.source_or_none()
                if source is None:
                    if args.once:
                        print("source is not ready", file=sys.stderr)
                        initialized.set()
                        return 1
                    loops.stop.wait(max(1.0, cfg.poll_interval_secs))
                    continue
                source.assert_complete_coverage()
                if initialize_state:
                    # The contacts loop waits for bootstrap, so it cannot save
                    # a contact checkpoint over a fresh/reset message cursor.
                    fresh = CollectorState()
                    source.bootstrap_state(fresh, backfill_seconds=args.backfill_seconds)
                    source.assert_complete_coverage()
                    with state_lock:
                        state.sessions = fresh.sessions
                        state.cursors = fresh.cursors
                        state.contact_snapshot_token = ""
                        state.save(cfg.state_path)
                    initialize_state = False
                    if args.backfill_seconds <= 0:
                        print(f"initialized state without historical broadcast: {cfg.state_path}")
                initialized.set()
                current_sessions, changed = source.changed_usernames(state)
                emitted = 0
                failed = False
                for candidate in source.iter_new_messages(state, changed, cfg.batch_size):
                    if loops.stop.is_set():
                        failed = True
                        break
                    if args.dry_run:
                        print(json.dumps(candidate.payload, ensure_ascii=False))
                    else:
                        response = bridge.emit_message(
                            candidate.payload, candidate.event_id, candidate.occurred_at,
                        )
                        if not response.ok:
                            print(
                                f"emit failed: HTTP {response.status} {response.body}; "
                                "state cursor was not advanced",
                                file=sys.stderr,
                            )
                            failed = True
                            break
                    with state_lock:
                        state.set_cursor(
                            candidate.cursor_key, candidate.cursor.create_time,
                            candidate.cursor.local_id,
                        )
                    emitted += 1
                source.assert_complete_coverage()
                with state_lock:
                    if not failed:
                        state.sessions = current_sessions
                    state.save(cfg.state_path)
                failure_count = failure_count + 1 if failed else 0
                if args.once:
                    print(f"emitted={emitted} changed_sessions={len(changed)}")
                    return int(failed)
            except DatabaseSnapshotError as exc:
                failure_count += 1
                print(
                    f"message snapshot failed: {exc}; state session markers were not advanced; "
                    f"retrying in {retry_delay(cfg.poll_interval_secs, failure_count):.1f}s",
                    file=sys.stderr,
                )
                if not initialize_state:
                    save_state()
                if args.once:
                    initialized.set()
                    return 1
            delay = retry_delay(cfg.poll_interval_secs, failure_count) if failure_count else cfg.poll_interval_secs
            loops.stop.wait(delay)
        return 0

    def contacts() -> int:
        bridge = BridgeClient(cfg)
        failure_count = 0
        while not initialized.wait(timeout=0.1):
            if loops.stop.is_set():
                return 0
        while not loops.stop.is_set():
            try:
                source = source_runtime.source_or_none()
                if source is None or initialize_state:
                    if args.once:
                        return 1
                    loops.stop.wait(max(1.0, cfg.poll_interval_secs))
                    continue
                snapshot = load_complete_contact_snapshot(source)
                snapshot_token = snapshot["snapshotToken"]
                with state_lock:
                    previous_token = state.contact_snapshot_token
                if snapshot_token != previous_token:
                    if not emit_contact_snapshot(bridge, snapshot, args.dry_run, loops.stop):
                        failure_count += 1
                        if args.once:
                            return 1
                        loops.stop.wait(retry_delay(cfg.poll_interval_secs, failure_count))
                        continue
                    with state_lock:
                        state.contact_snapshot_token = snapshot_token
                        state.save(cfg.state_path)
                failure_count = 0
                if args.once:
                    return 0
            except DatabaseSnapshotError as exc:
                failure_count += 1
                print(
                    f"contact snapshot failed: {exc}; checkpoint was not advanced; "
                    f"retrying in {retry_delay(cfg.poll_interval_secs, failure_count):.1f}s",
                    file=sys.stderr,
                )
                if args.once:
                    return 1
            delay = retry_delay(cfg.poll_interval_secs, failure_count) if failure_count else cfg.poll_interval_secs
            loops.stop.wait(delay)
        return 0

    try:
        print(
            f"collector running localApp={cfg.app_id}.{cfg.event_name} "
            f"bridge={cfg.bridge_events_url} methods={method_server.base_url} state={cfg.state_path}"
        )
        return loops.run(messages, contacts)
    except KeyboardInterrupt:
        print("collector stopped")
        return 0
    finally:
        method_server.stop()


def retry_delay(poll_interval_secs: float, failure_count: int) -> float:
    return max(
        poll_interval_secs,
        min(60.0, max(2.0, poll_interval_secs) * (2 ** min(max(failure_count, 1) - 1, 5))),
    )


def load_complete_contact_snapshot(source: WeChatSource, page_size: int = 500) -> dict:
    offset = 0
    contacts: list[dict] = []
    first_page: dict | None = None
    while True:
        page = source.contact_snapshot(
            limit=page_size,
            offset=offset,
            include_groups=False,
        )
        if first_page is None:
            first_page = page
        elif page["snapshotToken"] != first_page["snapshotToken"]:
            raise DatabaseSnapshotError("contact database changed while reading snapshot pages")
        contacts.extend(page["contacts"])
        if not page["hasMore"]:
            break
        offset += len(page["contacts"])
        if offset <= 0:
            raise DatabaseSnapshotError("contact snapshot pagination did not advance")
    assert first_page is not None
    return {
        **first_page,
        "contacts": contacts,
        "offset": 0,
        "limit": len(contacts),
        "total": len(contacts),
        "hasMore": False,
    }


def emit_contact_snapshot(
    bridge: BridgeClient, snapshot: dict, dry_run: bool,
    stop: threading.Event | None = None,
) -> bool:
    account = snapshot["account"]
    account_id = account["accountId"]
    snapshot_token = snapshot["snapshotToken"]
    base = {
        "accountId": account_id,
        "snapshotToken": snapshot_token,
        "source": account["source"],
        "platform": account["platform"],
    }
    events = [("started", {**base, "phase": "started"})]
    for contact in snapshot["contacts"]:
        events.append((contact["username"], {
            **base,
            "phase": "contact",
            "contactId": contact["username"],
            "displayName": contact["displayName"],
            "nickName": contact["nickName"],
            "remark": contact["remark"],
        }))
    events.append(("completed", {**base, "phase": "completed", "contactCount": snapshot["total"]}))
    for suffix, payload in events:
        if stop is not None and stop.is_set():
            return False
        event_id = f"contact-snapshot:{account_id}:{snapshot_token}:{suffix}"
        if dry_run:
            print(json.dumps(payload, ensure_ascii=False))
            continue
        response = bridge.emit_event("contactSnapshotChanged", payload, event_id)
        if not response.ok:
            print(f"contact snapshot emit failed: HTTP {response.status} {response.body}", file=sys.stderr)
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-bridge-collector",
        description="Collect local WeChat messages and emit bridge-agent events.",
    )
    parser.add_argument("--config", help="collector config path")
    parser.add_argument("--bridge-url", help="bridge-agent local base URL")
    parser.add_argument("--event-token", help="bridge-agent event token")
    parser.add_argument("--wechat-decrypt-dir", help="wechat-decrypt source directory")
    parser.add_argument("--wechat-decrypt-config", help="wechat-decrypt config.json path")
    parser.add_argument("--db-dir", help="WeChat db_storage directory")
    parser.add_argument("--keys-file", help="wechat-decrypt all_keys.json path")
    parser.add_argument("--state-dir", help="collector state directory")
    parser.add_argument("--method-host", help="local method server host")
    parser.add_argument("--method-port", type=int, help="local method server port")

    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-config", help="write default collector config")
    init.add_argument("--output", help="output config path")
    init.set_defaults(func=cmd_init_config)

    setup = sub.add_parser("setup", help="initialize collector config and local key file")
    setup.add_argument("--force", action="store_true", help="overwrite existing all_keys.json")
    setup.add_argument("--no-extract-keys", action="store_true", help="write config only")
    setup.set_defaults(func=cmd_setup)

    probe = sub.add_parser("probe", help="verify local WeChat decrypt/read access")
    probe.set_defaults(func=cmd_probe)

    install_autostart_parser = sub.add_parser(
        "install-autostart",
        help="install the platform-specific background launcher and login startup hook",
    )
    install_autostart_parser.set_defaults(func=cmd_install_autostart)

    start = sub.add_parser("start", help="start the collector in the background and return")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop the background collector")
    stop.set_defaults(func=cmd_stop)

    status_parser = sub.add_parser("status", help="check whether the local method server is healthy")
    status_parser.set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="run the collector loop")
    run.add_argument("--reset-state", action="store_true", help="discard collector cursor state")
    run.add_argument("--backfill-seconds", type=int, default=0, help="broadcast recent history on fresh/reset state")
    run.add_argument("--poll-interval", type=float, default=None, help="poll interval in seconds")
    run.add_argument("--batch-size", type=int, default=None, help="max rows per table per poll")
    run.add_argument("--once", action="store_true", help="run one poll and exit")
    run.add_argument("--dry-run", action="store_true", help="print events instead of posting to bridge-agent")
    run.add_argument("--no-text", action="store_true", help="omit message text from payloads")
    run.add_argument("--incoming-only", action="store_true", help="skip outgoing one-to-one messages when detected")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
