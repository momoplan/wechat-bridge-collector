from __future__ import annotations

import argparse
import json
import sys
import time

from .autostart import install_autostart, result_json, start_collector, status
from .bridge import BridgeClient
from .config import CollectorConfig
from .query_server import QueryMethodServer
from .setup_keys import setup_collector
from .state import CollectorState
from .wechat_source import WeChatSource


def _load_config(args: argparse.Namespace) -> CollectorConfig:
    cfg = CollectorConfig.load(args.config)
    if getattr(args, "bridge_url", None):
        cfg.bridge_base_url = args.bridge_url
    if getattr(args, "event_token", None):
        cfg.bridge_event_token = args.event_token
    if getattr(args, "service_registration_token", None):
        cfg.service_registration_token = args.service_registration_token
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


def cmd_register(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    response = BridgeClient(cfg).register_service()
    print(response.body)
    if not response.ok:
        print(f"register failed: HTTP {response.status}", file=sys.stderr)
        return 1
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

    source = WeChatSource(cfg)
    method_server = QueryMethodServer(cfg, source)
    method_server.start()
    bridge = BridgeClient(cfg)
    state = CollectorState.load(cfg.state_path)
    first_start = not cfg.state_path.exists()

    try:
        if args.register:
            response = bridge.register_service(method_server.base_url)
            if not response.ok:
                print(f"register failed: HTTP {response.status} {response.body}", file=sys.stderr)
                return 1
            print("registered bridge-agent service methods and events")

        if first_start or args.reset_state:
            state = CollectorState()
            source.bootstrap_state(state, backfill_seconds=args.backfill_seconds)
            state.save(cfg.state_path)
            if args.backfill_seconds <= 0:
                print(f"initialized state without historical broadcast: {cfg.state_path}")

        print(
            f"collector running service={cfg.service_name}.{cfg.event_name} "
            f"bridge={cfg.bridge_events_url} methods={method_server.base_url} state={cfg.state_path}"
        )

        while True:
            try:
                current_sessions, changed = source.changed_usernames(state)
                emitted = 0
                failed = False
                for candidate in source.iter_new_messages(state, changed, cfg.batch_size):
                    if args.dry_run:
                        print(json.dumps(candidate.payload, ensure_ascii=False))
                        ok = True
                        status = 202
                        body = ""
                    else:
                        response = bridge.emit_message(
                            candidate.payload,
                            candidate.event_id,
                            candidate.occurred_at,
                        )
                        ok = response.ok
                        status = response.status
                        body = response.body
                    if not ok:
                        print(
                            f"emit failed: HTTP {status} {body}; "
                            "state cursor was not advanced",
                            file=sys.stderr,
                        )
                        failed = True
                        break
                    state.set_cursor(
                        candidate.cursor_key,
                        candidate.cursor.create_time,
                        candidate.cursor.local_id,
                    )
                    emitted += 1

                if not failed:
                    state.sessions = current_sessions
                state.save(cfg.state_path)

                if args.once:
                    print(f"emitted={emitted} changed_sessions={len(changed)}")
                    return 0

                time.sleep(cfg.poll_interval_secs)
            except KeyboardInterrupt:
                print("collector stopped")
                return 0
    finally:
        method_server.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-bridge-collector",
        description="Collect local WeChat messages and emit bridge-agent events.",
    )
    parser.add_argument("--config", help="collector config path")
    parser.add_argument("--bridge-url", help="bridge-agent local base URL")
    parser.add_argument("--event-token", help="bridge-agent event token")
    parser.add_argument("--service-registration-token", help="bridge-agent service registration token")
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

    register = sub.add_parser("register", help="register event declaration in bridge-agent")
    register.set_defaults(func=cmd_register)

    install_autostart_parser = sub.add_parser(
        "install-autostart",
        help="install the platform-specific background launcher and login startup hook",
    )
    install_autostart_parser.set_defaults(func=cmd_install_autostart)

    start = sub.add_parser("start", help="start the collector in the background and return")
    start.set_defaults(func=cmd_start)

    status_parser = sub.add_parser("status", help="check whether the local method server is healthy")
    status_parser.set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="run the collector loop")
    run.add_argument("--register", action="store_true", help="register service before running")
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
