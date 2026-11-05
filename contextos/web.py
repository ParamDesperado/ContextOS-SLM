"""Local dashboard. Binds to 127.0.0.1 and rejects cross-site and rebinding requests."""
from __future__ import annotations

from dataclasses import asdict

from flask import Flask, abort, jsonify, render_template, request

from . import config
from .core import ContextOS


def create_app(system: ContextOS) -> Flask:
    app = Flask(__name__)
    allowed_hosts = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}

    @app.before_request
    def guard():
        # Any web page could otherwise POST to localhost and trigger actions.
        if request.host not in allowed_hosts and not app.testing:
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] not in allowed_hosts and not app.testing:
                abort(403)
            if not request.is_json:
                abort(415)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        service = system.service
        return jsonify({
            "snapshot": system.snapshot(),
            "history": system.store.history(300),
            "governor": {"enabled": system.governor.enabled, "engaged": system.governor.engaged,
                         "demoted": system.ledger.names(), "events": system.events[-8:]},
            "slm": {"backend": service.backend.name if service else "none (rule planner)",
                    "loaded": service.backend.loaded() if service else False,
                    **(asdict(service.metrics) if service else {})},
            "audit": system.audit.read(last=8),
            "turns": system.turns,
        })

    @app.post("/api/ask")
    def ask():
        text = str((request.get_json() or {}).get("text", "")).strip()[:500]
        if not text:
            abort(400)
        return jsonify(system.ask(text))

    @app.post("/api/confirm")
    def confirm():
        body = request.get_json() or {}
        return jsonify(system.confirm(str(body.get("token", "")), bool(body.get("approve"))))

    @app.post("/api/governor")
    def governor():
        system.governor.enabled = bool((request.get_json() or {}).get("enabled"))
        if not system.governor.enabled:
            system.governor.shutdown()
        return jsonify({"enabled": system.governor.enabled})

    @app.get("/api/audit/verify")
    def verify():
        intact, count = system.audit.verify()
        return jsonify({"intact": intact, "records": count})

    return app
