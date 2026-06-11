from __future__ import annotations

import argparse
from pathlib import Path

from flask import Flask, jsonify, render_template

from config import SQLITE_DB_PATH
from dashboard_queries import get_dashboard_status


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OPC UA collector dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", default=5050, type=int, help="Port to listen on")
    parser.add_argument("--db", default=str(SQLITE_DB_PATH), help="Path to SQLite database")
    return parser


def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app.config["DASHBOARD_DB_PATH"] = str(Path(db_path))

    @app.get("/")
    def dashboard() -> str:
        return render_template("dashboard.html")

    @app.get("/api/status")
    def api_status():
        status = get_dashboard_status(app.config["DASHBOARD_DB_PATH"])
        http_status = 200 if status["overall"]["status"] != "CRITICAL" or status["machines"] else 503
        return jsonify(status), http_status

    return app


def main() -> int:
    args = build_arg_parser().parse_args()
    app = create_app(args.db)
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
