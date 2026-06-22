import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8000))


@app.route("/webhook", methods=["POST"])
def webhook():
    if not DISCORD_WEBHOOK_URL:
        return jsonify({"error": "DISCORD_WEBHOOK_URL environment variable is not set"}), 500

    data = request.get_json(silent=True) or {}

    title = data.get("title", "VATSIM APAC Alert")
    description = data.get("description") or data.get("message") or data.get("text", "")
    url = data.get("url", "")
    color = int(data.get("color", 0x1F8B4C))

    embed = {
        "title": title,
        "color": color,
    }

    if description:
        embed["description"] = description

    if url:
        embed["url"] = url

    payload = {"embeds": [embed]}

    response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

    if response.status_code in (200, 204):
        return jsonify({"status": "ok"}), 200

    return jsonify({
        "error": "Failed to forward alert to Discord",
        "discord_status": response.status_code,
    }), 502


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
