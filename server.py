import os
import logging
import string
import random
import datetime
import requests
from flask import Flask, request, jsonify, session, redirect
from flask_cors import CORS
from pymongo import MongoClient

# --- Setup ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "supersecretkey")
CORS(app)

logging.basicConfig(level=logging.DEBUG)

# --- Spotify API credentials ---
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

# --- Mongo setup ---
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["spotify_game"]
sessions_collection = db["sessions"]

# --- Helpers ---
def generate_random_string(length=16):
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

def get_stored_tokens(session_id):
    return sessions_collection.find_one({"session_id": session_id})

def update_tokens(session_id, access_token, refresh_token, expires_in):
    sessions_collection.update_one(
        {"session_id": session_id},
        {"$set": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
        }},
        upsert=True
    )

def refresh_access_token(refresh_token):
    logging.debug("Refreshing Spotify access token...")
    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        },
    )
    if response.status_code == 200:
        token_info = response.json()
        logging.debug(f"New token: {token_info}")
        return token_info
    else:
        logging.error(f"Failed to refresh token: {response.text}")
        return None

# --- Routes ---

@app.route("/login")
def login():
    state = generate_random_string(16)
    scope = (
        "user-read-private user-read-email "
        "playlist-read-private playlist-read-collaborative "
        "user-read-playback-state user-modify-playback-state "
        "streaming"
    )
    auth_url = (
        "https://accounts.spotify.com/authorize?"
        f"response_type=code&client_id={CLIENT_ID}"
        f"&scope={scope}&redirect_uri={REDIRECT_URI}&state={state}"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    response = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET
        },
    )
    token_info = response.json()
    access_token = token_info.get("access_token")
    refresh_token = token_info.get("refresh_token")
    expires_in = token_info.get("expires_in")

    session_id = generate_random_string(12)
    update_tokens(session_id, access_token, refresh_token, expires_in)

    return redirect(f"/?session_id={session_id}")

@app.route("/api/spotify/token", methods=["GET"])
def get_spotify_token():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    tokens = get_stored_tokens(session_id)
    if not tokens:
        return jsonify({"error": "Invalid session_id"}), 400

    expires_at = tokens.get("expires_at")
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    # If expired, refresh
    if expires_at < datetime.datetime.utcnow():
        refreshed = refresh_access_token(refresh_token)
        if refreshed and "access_token" in refreshed:
            access_token = refreshed["access_token"]
            expires_in = refreshed.get("expires_in", 3600)
            update_tokens(session_id, access_token, refresh_token, expires_in)
        else:
            return jsonify({"error": "Failed to refresh token"}), 400
    else:
        expires_in = int((expires_at - datetime.datetime.utcnow()).total_seconds())

    # Web Playback SDK expects these keys
    return jsonify({
        "access_token": access_token,
        "expires_in": expires_in
    })

@app.route("/api/spotify/me", methods=["GET"])
def get_me():
    session_id = request.args.get("session_id")
    tokens = get_stored_tokens(session_id)
    if not tokens:
        return jsonify({"error": "Invalid session_id"}), 400

    access_token = tokens.get("access_token")
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get("https://api.spotify.com/v1/me", headers=headers)

    return jsonify(response.json()), response.status_code

# --- Run ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
