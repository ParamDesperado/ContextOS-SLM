"""Command line entry point: python -m contextos <command>"""
from __future__ import annotations

import argparse
import json

from . import config


def _system():
    from .core import ContextOS, default_backend
    backend, need = default_backend()
    if backend is None:
        print("No model installed, so the rule planner is in use. Run: python -m contextos setup")
    return ContextOS(backend, need or 1.0)


def _print_reply(system, reply: dict) -> None:
    print(f"\n{reply['text']}")
    print(f"[{reply['planner']} -> {reply['tool']} {json.dumps(reply['args'])} | "
          f"{reply['decision']} | {reply['total_seconds']} s]")
    if reply.get("token"):
        try:
            answer = input("Type yes to approve: ").strip().lower()
        except EOFError:
            answer = "no"
        print(system.confirm(reply["token"], answer == "yes")["text"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="contextos", description="Context-aware OS layer using a small language model")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="download a small language model")
    setup.add_argument("--model", default=config.DEFAULT_MODEL, choices=list(config.MODELS))
    sub.add_parser("status", help="print the current context")
    ask = sub.add_parser("ask", help="ask one question")
    ask.add_argument("text", nargs="+")
    sub.add_parser("shell", help="interactive natural-language shell")
    sub.add_parser("serve", help="run the governor and the web dashboard")
    sub.add_parser("verify-audit", help="check the audit log's hash chain")
    args = parser.parse_args()

    if args.command == "setup":
        from .setup_models import install
        install(args.model)
    elif args.command == "verify-audit":
        from .security.audit import AuditLog
        intact, count = AuditLog(config.data_dir() / "audit.jsonl").verify()
        print(f"{'Intact' if intact else 'BROKEN'}: {count} valid records")
    elif args.command == "status":
        from .context.sensors import Sampler
        from .context.summary import summarize
        import time
        sampler = Sampler()
        sampler.sample()
        time.sleep(1)
        print(summarize(sampler.sample().to_dict()))
    elif args.command == "ask":
        system = _system()
        try:
            _print_reply(system, system.ask(" ".join(args.text)))
        finally:
            system.shutdown()
    elif args.command == "shell":
        system = _system()
        print("ContextOS shell. Type exit to leave.")
        try:
            while True:
                text = input("\ncontextos> ").strip()
                if text in {"exit", "quit"}:
                    break
                if text:
                    _print_reply(system, system.ask(text))
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            system.shutdown()
    elif args.command == "serve":
        from .web import create_app
        system = _system()
        print(f"Dashboard: http://{config.HOST}:{config.PORT}")
        try:
            create_app(system).run(config.HOST, config.PORT, threaded=True, use_reloader=False)
        finally:
            system.shutdown()


if __name__ == "__main__":
    main()
