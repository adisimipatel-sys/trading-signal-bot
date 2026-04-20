#!/usr/bin/env python3
"""
Web Dashboard Server for Trading Signal Bot
Serves signals data as API + static dashboard
"""

import json
import os
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template_string, send_from_directory
from bot import run_scan, load_signals, get_win_rate, SCAN_INTERVAL_MIN

app = Flask(__name__)

# Background scanner thread
_scan_thread = None
_last_scan   = None
_is_scanning = False

def background_scanner():
    global _last_scan, _is_scanning
    while True:
        _is_scanning = True
        try:
            run_scan()
            _last_scan = datetime.now().isoformat()
        except Exception as e:
            print(f"Scanner error: {e}")
        _is_scanning = False
        time.sleep(SCAN_INTERVAL_MIN * 60)

def start_scanner():
    global _scan_thread
    if _scan_thread is None or not _scan_thread.is_alive():
        _scan_thread = threading.Thread(target=background_scanner, daemon=True)
        _scan_thread.start()

# ─── API ROUTES ───────────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    signals = load_signals()
    signals.reverse()  # newest first
    return jsonify(signals[:50])

@app.route("/api/stats")
def api_stats():
    stats = get_win_rate()
    return jsonify({
        **stats,
        "last_scan": _last_scan,
        "is_scanning": _is_scanning,
        "scan_interval_min": SCAN_INTERVAL_MIN
    })

@app.route("/api/scan", methods=["POST"])
def api_manual_scan():
    """Trigger manual scan"""
    thread = threading.Thread(target=run_scan, daemon=True)
    thread.start()
    return jsonify({"status": "scan started"})

@app.route("/api/signal/<signal_id>/result", methods=["POST"])
def api_update_result():
    """Mark signal WIN/LOSS for tracking"""
    from flask import request
    data = request.json
    result = data.get("result")
    if result not in ("WIN","LOSS","SKIP"):
        return jsonify({"error": "result must be WIN, LOSS, or SKIP"}), 400

    signals = load_signals()
    for s in signals:
        if s["id"] == signal_id:
            s["result"] = result
            s["status"] = "CLOSED"
            break

    from bot import save_signals
    save_signals(signals)
    return jsonify({"status": "updated"})

@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "dashboard.html")) as f:
        return f.read()

if __name__ == "__main__":
    start_scanner()
    app.run(host="0.0.0.0", port=5000, debug=False)
