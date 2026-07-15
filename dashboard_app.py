from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from flask import Flask, jsonify, render_template, request

from config import DASHBOARD_HOST, DASHBOARD_LOG_PATH, DASHBOARD_PORT
from dashboard_queries import get_dashboard_status
from db import cleanup_old_data, clear_bad_samples, clear_monitoring_data, clear_poll_runs, get_connection

LOGGER = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Press 14–15 OPC UA Collector dashboard")
    parser.add_argument("--host", default=DASHBOARD_HOST, help="Host interface to bind")
    parser.add_argument("--port", default=DASHBOARD_PORT, type=int, help="Port to listen on")
    parser.add_argument("--waitress", action="store_true", help="Serve with Waitress instead of Flask dev server")
    return parser


def configure_logging() -> None:
    DASHBOARD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        DASHBOARD_LOG_PATH,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def dashboard() -> str:
        return render_template("dashboard.html")

    @app.get("/api/status")
    def api_status():
        status = get_dashboard_status()
        http_status = 200 if status["overall"]["status"] != "CRITICAL" or status["machines"] else 503
        return jsonify(status), http_status

    @app.post("/api/actions/cleanup-now")
    def cleanup_now():
        conn = None
        try:
            conn = get_connection()
            deleted = cleanup_old_data(conn=conn)
        except Exception:
            LOGGER.exception("Dashboard cleanup-now action failed")
            return jsonify({"ok": False, "error": "Cleanup failed."}), 500
        finally:
            if conn is not None:
                conn.close()
        LOGGER.warning("Dashboard action cleanup-now result=%s", deleted)
        return jsonify({"ok": True, "action": "cleanup-now", "deleted": deleted})

    @app.post("/api/actions/clear-poll-runs")
    def clear_poll_runs_action():
        return _confirmed_action("clear-poll-runs", clear_poll_runs)

    @app.post("/api/actions/clear-bad-samples")
    def clear_bad_samples_action():
        return _confirmed_action("clear-bad-samples", clear_bad_samples)

    @app.post("/api/actions/clear-monitoring-data")
    def clear_monitoring_data_action():
        return _confirmed_action("clear-monitoring-data", clear_monitoring_data)

    return app


def _confirmed_action(action_name: str, action_func):
    payload = request.get_json(silent=True) or {}
    if payload.get("confirm") is not True:
        return jsonify({"ok": False, "error": "Confirmation required: {\"confirm\": true}"}), 400
    conn = None
    try:
        conn = get_connection()
        result = action_func(conn=conn)
    except Exception:
        LOGGER.exception("Dashboard action %s failed", action_name)
        return jsonify({"ok": False, "error": "Requested action failed."}), 500
    finally:
        if conn is not None:
            conn.close()
    LOGGER.warning("Dashboard action %s result=%s", action_name, result)
    return jsonify({"ok": True, "action": action_name, "result": result})


def main() -> int:
    configure_logging()
    args = build_arg_parser().parse_args()
    app = create_app()
    if args.waitress:
        try:
            from waitress import serve
        except ImportError:
            print("Waitress is not installed. Run: pip install -r requirements.txt")
            return 2
        serve(app, host=args.host, port=args.port)
        return 0
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
