import os
import logging
import datetime
import requests
from flask import Flask, request, jsonify, redirect
from pymongo import MongoClient
from bson import ObjectId

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Mongo setup
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client.hitster

# Spotify credentials
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

# --- Helpers ---
def refresh_access_token(refresh_token):
    url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(url, data=payload)
    if response.status_code != 200:
        logging.error(f"Failed to refresh token: {response.text}")
        return None
    return response.json()

# --- Routes ---
@app.route("/login")
def login():
    scope = "streaming user-read-email user-read-private user-modify-playback-state playlist-read-private"
    return redirect(
        f"https://accounts.spotify.com/authorize?response_type=code&client_id={SPOTIFY_CLIENT_ID}&scope={scope}&redirect_uri={SPOTIFY_REDIRECT_URI}"
    )

@app.route("/callback")
def callback():
    code = request.args.get("code")
    token_url = "https://accounts.spotify.com/api/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(token_url, data=payload)
    data = response.json()

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in")

    # Store session
    session = {
        "created_at": datetime.datetime.utcnow().isoformat(),
        "is_active": True,
        "spotify_access_token": access_token,
        "spotify_refresh_token": refresh_token,
        "token_expires_at": datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in),
    }
    result = db.sessions.insert_one(session)
    return redirect(f"/connected?session_id={result.inserted_id}")

@app.route("/api/spotify/token")
def get_token():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "No session_id provided"}), 400

    session = db.sessions.find_one({"_id": ObjectId(session_id)})
    if not session:
        return jsonify({"error": "Invalid session_id"}), 404

    access_token = session.get("spotify_access_token")
    refresh_token_value = session.get("spotify_refresh_token")
    expires_at = session.get("token_expires_at")

    # Check expiry
    if expires_at and datetime.datetime.utcnow() >= expires_at:
        logging.info("Access token expired, refreshing...")
        refreshed = refresh_access_token(refresh_token_value)
        if refreshed and "access_token" in refreshed:
            access_token = refreshed["access_token"]
            expires_in = refreshed.get("expires_in", 3600)
            new_expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)

            db.sessions.update_one(
                {"_id": ObjectId(session_id)},
                {"$set": {"spotify_access_token": access_token, "token_expires_at": new_expiry}},
            )
        else:
            return jsonify({"error": "Failed to refresh token"}), 401

    return jsonify({
        "access_token": access_token,
        "expires_in": int((expires_at - datetime.datetime.utcnow()).total_seconds()) if expires_at else 3600
    })

@app.route("/api/spotify/session")
def get_session():
    session_id = request.args.get("session_id")
    session = db.sessions.find_one({"_id": ObjectId(session_id)})
    if not session:
        return jsonify({"error": "Invalid session_id"}), 404

    return jsonify({
        "session_id": str(session["_id"]),
        "spotify_access_token": session.get("spotify_access_token"),
        "spotify_refresh_token": session.get("spotify_refresh_token"),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
