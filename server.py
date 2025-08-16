import os
import re
import time
from flask import Flask, request, jsonify, redirect, g
from flask_cors import CORS
import requests
import random
from urllib.parse import urlencode
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ["https://preview--tune-twist-7ca04c74.base44.app", "*"]}})

# Configuration
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'https://preview--tune-twist-7ca04c74.base44.app')
MONGO_URI = os.getenv('MONGO_URI')

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, MONGO_URI]):
    missing = [k for k, v in {
        'SPOTIFY_CLIENT_ID': CLIENT_ID,
        'SPOTIFY_CLIENT_SECRET': CLIENT_SECRET,
        'SPOTIFY_REDIRECT_URI': REDIRECT_URI,
        'MONGO_URI': MONGO_URI
    }.items() if not v]
    logger.error(f"Missing environment variables: {missing}")
    raise ValueError(f"Missing environment variables: {missing}")

# Valid icon names for playlists
VALID_ICONS = [
    'jukebox', 'boombox', 'microphone', 'bells',
    'music-note', 'record-player', 'guitar', 'headphones'
]
DEFAULT_ICON = 'music-note'

# MongoDB setup with fork-safe configuration
mongodb = None

def init_db():
    global mongodb
    try:
        mongodb = MongoClient(
            MONGO_URI,
            connectTimeoutMS=10000,
            socketTimeoutMS=30000,
            maxPoolSize=10,
            retryWrites=True,
            retryReads=True,
            connect=False  # Critical for fork safety
        )
        
        # Verify connection works
        mongodb.admin.command('ping')
        logger.info("MongoDB connected successfully")
        
        db = mongodb['hitster']
        
        # Create indexes with error handling
        try:
            db.sessions.create_index(
                [("state", 1)],
                unique=True,
                partialFilterExpression={"state": {"$type": "string"}}
            )
            db.sessions.create_index(
                [("token_expires_at", 1)],
                expireAfterSeconds=0
            )
            logger.info("MongoDB indexes ensured")
        except Exception as e:
            logger.warning(f"Index creation warning: {str(e)}")
            
        return db
        
    except Exception as e:
        logger.error(f"MongoDB connection failed: {str(e)}")
        raise

db = init_db()
sessions = db['sessions']
tracks = db['tracks']
playlists = db['playlists']
playlist_tracks = db['playlist_tracks']
track_metadata = db['track_metadata']

# Middleware
@app.before_request
def start_timer():
    g.start_time = time.time()

@app.before_request
def check_db_connection():
    try:
        mongodb.admin.command('ping')
    except Exception as e:
        logger.critical(f"Database connection lost: {str(e)}")
        return jsonify({"error": "Database unavailable"}), 503

@app.after_request
def log_request(response):
    if request.path.startswith('/api/'):
        duration = (time.time() - g.get('start_time', time.time())) * 1000
        logger.info(
            f"{request.method} {request.path} - {response.status_code} "
            f"- {duration:.1f}ms"
        )
    return response

# Spotify Authorization Flow
@app.route('/api/spotify/authorize')
def spotify_authorize():
    try:
        state = str(random.randint(100000, 999999))
        
        # Clean up any existing session with this state
        sessions.delete_one({'state': state})
        
        # Create new session
        session_id = sessions.insert_one({
            'state': state,
            'created_at': datetime.utcnow(),
            'is_active': False,
            'tracks_played': []
        }).inserted_id
        
        params = {
            'client_id': CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': REDIRECT_URI,
            'state': state,
            'scope': 'streaming user-read-playback-state user-modify-playback-state user-read-email',
            'show_dialog': 'true'
        }
        
        auth_url = f"https://accounts.spotify.com/authorize?{urlencode(params)}"
        return redirect(auth_url)
        
    except Exception as e:
        logger.error(f"Authorization failed: {str(e)}", exc_info=True)
        return jsonify({"error": "Authorization setup failed"}), 500

@app.route('/api/spotify/callback')
def spotify_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    
    if error:
        logger.error(f"Spotify callback error: {error}")
        return redirect(f"{FRONTEND_URL}?error={error}")
    
    try:
        session = sessions.find_one({'state': state, 'is_active': False})
        if not session:
            logger.error(f"Invalid state: {state}")
            return redirect(f"{FRONTEND_URL}?error=invalid_state")
        
        # Exchange code for token
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URI,
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET
            },
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        # Update session
        sessions.update_one(
            {'_id': session['_id']},
            {'$set': {
                'spotify_access_token': data['access_token'],
                'spotify_refresh_token': data['refresh_token'],
                'token_expires_at': datetime.utcnow() + timedelta(seconds=data.get('expires_in', 3600)),
                'is_active': True,
                'state': None  # Clear state after use
            }}
        )
        
        return redirect(f"{FRONTEND_URL}?session_id={str(session['_id'])}")
        
    except Exception as e:
        logger.error(f"Callback failed: {str(e)}", exc_info=True)
        return redirect(f"{FRONTEND_URL}?error=auth_failed")

@app.route('/api/spotify/add-playlist', methods=['POST'])
def add_playlist():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in add_playlist")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in add_playlist: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        data = request.get_json()
        if not data or 'url' not in data:
            logger.error("No playlist URL provided")
            return jsonify({'error': 'Playlist URL required'}), 400
        url = data['url']
        playlist_id = parse_playlist_url(url)
        if not playlist_id:
            logger.error(f"Invalid Spotify playlist URL: {url}")
            return jsonify({'error': 'Invalid Spotify playlist URL'}), 400
        name, custom_icon, error = get_playlist_metadata(playlist_id)
        if error:
            logger.error(f"Error fetching playlist metadata: {error}")
            return jsonify({'error': error}), 400
        logger.info(f"Added playlist {playlist_id} to playlists collection")
        return jsonify({'id': playlist_id, 'name': name, 'custom_icon': custom_icon})
    except Exception as e:
        logger.error(f"Error in add_playlist for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/spotify/remove-playlist', methods=['POST'])
def remove_playlist():
    session_id = request.args.get('session_id')
    playlist_id = request.args.get('playlist_id')
    if not session_id or not playlist_id:
        logger.error(f"Missing session_id or playlist_id in remove_playlist: session_id={session_id}, playlist_id={playlist_id}")
        return jsonify({'error': 'Session ID and playlist ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in remove_playlist: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        # Remove from playlists collection
        result = playlists.delete_one({'playlist_id': playlist_id})
        logger.info(f"Removed playlist {playlist_id} from playlists collection, deleted: {result.deleted_count}")
        if result.deleted_count == 0:
            logger.warning(f"Playlist {playlist_id} not found in playlists collection")
        return jsonify({'message': 'Playlist removed successfully'})
    except Exception as e:
        logger.error(f"Error in remove_playlist for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

def get_playlist_tracks(playlist_id, session_id):
    """Fetch tracks from a Spotify playlist"""
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session or not session.get('spotify_access_token'):
            logger.error(f"No active session or access token for session {session_id}")
            return None

        headers = {
            'Authorization': f"Bearer {session['spotify_access_token']}",
            'Content-Type': 'application/json'
        }

        # First get the playlist to check if it's a user's playlist
        playlist_url = f"https://api.spotify.com/v1/playlists/{playlist_id}"
        playlist_response = requests.get(playlist_url, headers=headers)
        playlist_response.raise_for_status()
        playlist_data = playlist_response.json()

        # Get all tracks (handle pagination if needed)
        tracks_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
        tracks_response = requests.get(tracks_url, headers=headers)
        tracks_response.raise_for_status()
        tracks_data = tracks_response.json()

        # Extract track items
        track_items = []
        if 'items' in tracks_data:
            track_items = [item['track'] for item in tracks_data['items'] if item['track'] is not None]

        logger.info(f"Fetched {len(track_items)} tracks from playlist {playlist_id}")
        return track_items

    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching playlist tracks: {str(e)}")
        if e.response.status_code == 401:
            # Token might be expired, try to refresh
            refresh_token(session_id)
            return get_playlist_tracks(playlist_id, session_id)
        return None
    except Exception as e:
        logger.error(f"Error fetching playlist tracks: {str(e)}")
        return None

def refresh_token(session_id):
    """Refresh Spotify access token"""
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session or not session.get('spotify_refresh_token'):
            logger.error(f"No refresh token available for session {session_id}")
            return False

        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': session['spotify_refresh_token'],
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET
            }
        )
        response.raise_for_status()
        data = response.json()

        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data['access_token'],
                'token_expires_at': datetime.utcnow() + timedelta(seconds=data.get('expires_in', 3600))
            }}
        )
        logger.info(f"Refreshed token for session {session_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to refresh token for session {session_id}: {str(e)}")
        return False

def get_original_release_year(track_name, artist_name, album_name, spotify_year):
    """Get original release year (placeholder implementation)"""
    # In a real implementation, you might query MusicBrainz here
    return spotify_year

def play_track(track_id, session_id):
    """Play track on user's active Spotify device"""
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session or not session.get('spotify_access_token'):
            return False, "No active session or access token"

        headers = {
            'Authorization': f"Bearer {session['spotify_access_token']}",
            'Content-Type': 'application/json'
        }

        # First check available devices
        devices_response = requests.get(
            'https://api.spotify.com/v1/me/player/devices',
            headers=headers
        )
        
        if devices_response.status_code == 204:
            return False, "No active devices found. Open Spotify on a device."

        devices = devices_response.json().get('devices', [])
        if not devices:
            return False, "No active devices found. Open Spotify on a device."

        # Play on first available device
        play_response = requests.put(
            f'https://api.spotify.com/v1/me/player/play?device_id={devices[0]["id"]}',
            headers=headers,
            json={'uris': [f'spotify:track:{track_id}']}
        )

        if play_response.status_code == 204:
            return True, None
        else:
            return False, play_response.text

    except Exception as e:
        logger.error(f"Error playing track: {str(e)}")
        return False, str(e)



# Add this new endpoint to your server.py, right after the play_next_song endpoint:
@app.route('/api/spotify/get-next-track/<playlist_id>')
def get_next_track(playlist_id):
    """
    Get next track data without playing it (for SDK usage)
    """
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in get_next_track")
        return jsonify({'error': 'Session ID required'}), 400
    
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_next_track: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        
        # Update playlist theme
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'playlist_theme': playlist_id}}
        )
        logger.info(f"Updated playlist_theme for session {session_id}, modified: {result.modified_count}")
        
        tracks_list = get_playlist_tracks(playlist_id, session_id)
        if not tracks_list:
            logger.error(f"No tracks available for playlist {playlist_id}")
            return jsonify({'error': f'No tracks available for playlist {playlist_id}. Playlist may be empty or inaccessible.'}), 400
        
        random_track = random.choice(tracks_list)
        
        # Get track metadata
        spotify_year = int(random_track['album']['release_date'].split('-')[0])
        track_name = random_track['name']
        artist_name = random_track['artists'][0]['name']
        album_name = random_track['album']['name']
        original_year = get_original_release_year(track_name, artist_name, album_name, spotify_year)
        
        # Store track data
        tracks.insert_one({
            'spotify_id': random_track['id'],
            'title': track_name,
            'artist': artist_name,
            'album': album_name,
            'release_year': original_year,
            'playlist_theme': playlist_id,
            'played_at': datetime.utcnow().isoformat(),
            'session_id': str(session['_id']),
            'expires_at': datetime.utcnow() + timedelta(hours=2)
        })
        
        # Update tracks played
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$push': {'tracks_played': random_track['id']}}
        )
        logger.info(f"Prepared track {random_track['id']} for session {session_id}, modified: {result.modified_count}")
        
        return jsonify({
            'spotify_id': random_track['id'],
            'title': track_name,
            'artist': artist_name,
            'release_year': original_year,
            'album': album_name,
            'playlist_theme': playlist_id,
            'played_at': datetime.utcnow().isoformat(),
            'track_uri': f"spotify:track:{random_track['id']}"  # This is what the SDK needs
        })
        
    except Exception as e:
        logger.error(f"Error in get_next_track for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400



@app.route('/api/spotify/update-playlist-icon', methods=['POST'])
def update_playlist_icon():
    session_id = request.args.get('session_id')
    playlist_id = request.args.get('playlist_id')
    if not session_id or not playlist_id:
        logger.error(f"Missing session_id or playlist_id in update_playlist_icon: session_id={session_id}, playlist_id={playlist_id}")
        return jsonify({'error': 'Session ID and playlist ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in update_playlist_icon: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        playlist = playlists.find_one({'playlist_id': playlist_id})
        if not playlist:
            logger.error(f"Playlist {playlist_id} not found in update_playlist_icon")
            return jsonify({'error': 'Playlist not found'}), 400
        data = request.get_json()
        if not data or 'custom_icon' not in data:
            logger.error("No custom_icon provided in update_playlist_icon")
            return jsonify({'error': 'Custom icon name required'}), 400
        custom_icon = data['custom_icon']
        if custom_icon not in VALID_ICONS:
            logger.error(f"Invalid custom_icon in update_playlist_icon: {custom_icon}")
            return jsonify({'error': f"Invalid icon name. Must be one of: {', '.join(VALID_ICONS)}"}), 400
        result = playlists.update_one(
            {'playlist_id': playlist_id},
            {'$set': {'custom_icon': custom_icon}}
        )
        logger.info(f"Updated custom_icon for playlist {playlist_id} to {custom_icon}, modified: {result.modified_count}")
        return jsonify({'message': 'Playlist icon updated successfully', 'custom_icon': custom_icon})
    except Exception as e:
        logger.error(f"Error in update_playlist_icon for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/spotify/playlists')
def get_playlists():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in get_playlists")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_playlists: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        # Fetch from playlists collection where expires_at > now
        playlist_cursor = playlists.find({'expires_at': {'$gt': datetime.utcnow()}})
        custom_playlists = [
            {
                'id': p['playlist_id'],
                'name': p['name'],
                'custom_icon': p.get('custom_icon', DEFAULT_ICON)
            }
            for p in playlist_cursor
        ]
        logger.info(f"Retrieved {len(custom_playlists)} playlists for session {session_id}")
        return jsonify(custom_playlists)
    except Exception as e:
        logger.error(f"Error in get_playlists for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/spotify/session')
def get_session():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in get_session")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_session: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        logger.info(f"Retrieved session {session_id}")
        return jsonify({
            'session_id': str(session['_id']),
            'playlist_theme': session.get('playlist_theme'),
            'tracks_played': session.get('tracks_played', []),
            'is_active': session.get('is_active', True),
            'created_at': session.get('created_at'),
            'user_playlists': []  # Empty, as playlists are fetched from hitster.playlists
        })
    except Exception as e:
        logger.error(f"Error in get_session for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/spotify/tracks')
def get_tracks():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in get_tracks")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in get_tracks: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        track_ids = session.get('tracks_played', [])
        track_data = []
        for track_id in track_ids:
            track = tracks.find_one({'spotify_id': track_id, 'session_id': str(session['_id'])})
            if track:
                track_data.append({
                    'spotify_id': track['spotify_id'],
                    'title': track['title'],
                    'artist': track['artist'],
                    'album': track['album'],
                    'release_year': track['release_year'],
                    'playlist_theme': track['playlist_theme'],
                    'played_at': track['played_at']
                })
        logger.info(f"Retrieved {len(track_data)} tracks for session {session_id}")
        return jsonify(track_data)
    except Exception as e:
        logger.error(f"Error in get_tracks for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/spotify/play-track/<playlist_id>')
def play_next_song(playlist_id):
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in play_next_song")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in play_next_song: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'playlist_theme': playlist_id}}
        )
        logger.info(f"Updated playlist_theme for session {session_id}, modified: {result.modified_count}")
        tracks_list = get_playlist_tracks(playlist_id, session_id)
        if not tracks_list:
            logger.error(f"No tracks available for playlist {playlist_id}")
            return jsonify({'error': f'No tracks available for playlist {playlist_id}. Playlist may be empty or inaccessible.'}), 400
        random_track = random.choice(tracks_list)
        success, error = play_track(random_track['id'], session_id)
        if success:
            spotify_year = int(random_track['album']['release_date'].split('-')[0])
            track_name = random_track['name']
            artist_name = random_track['artists'][0]['name']
            album_name = random_track['album']['name']
            original_year = get_original_release_year(track_name, artist_name, album_name, spotify_year)
            tracks.insert_one({
                'spotify_id': random_track['id'],
                'title': track_name,
                'artist': artist_name,
                'album': album_name,
                'release_year': original_year,  # Use original year from MusicBrainz
                'playlist_theme': playlist_id,
                'played_at': datetime.utcnow().isoformat(),
                'session_id': str(session['_id']),
                'expires_at': datetime.utcnow() + timedelta(hours=2)
            })
            result = sessions.update_one(
                {'_id': ObjectId(session_id)},
                {'$push': {'tracks_played': random_track['id']}}
            )
            logger.info(f"Played track {random_track['id']} for session {session_id}, modified: {result.modified_count}")
            return jsonify({
                'spotify_id': random_track['id'],
                'title': track_name,
                'artist': artist_name,
                'release_year': original_year,  # Return original year
                'album': album_name,
                'playlist_theme': playlist_id,
                'played_at': datetime.utcnow().isoformat()
            })
        logger.error(f"Failed to play track for session {session_id}: {error}")
        return jsonify({'error': error or 'Failed to play track. Ensure Spotify is open on a device and your account is Premium.'}), 400
    except Exception as e:
        logger.error(f"Error in play_next_song for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/spotify/reset', methods=['POST'])
def reset_game():
    session_id = request.args.get('session_id')
    if not session_id:
        logger.error("No session_id provided in reset_game")
        return jsonify({'error': 'Session ID required'}), 400
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id in reset_game: {session_id}")
            return jsonify({'error': 'Invalid session_id'}), 400
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'tracks_played': [], 'playlist_theme': None}}
        )
        tracks.delete_many({'session_id': session_id})
        logger.info(f"Reset game for session {session_id}, modified: {result.modified_count}")
        return jsonify({'message': 'Game session reset'}), 200
    except Exception as e:
        logger.error(f"Error in reset_game for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/')
def index():
    return jsonify({'message': 'Hitster Song Randomizer Backend. Use the frontend to interact.'})

if __name__ == '__main__':
    # Initialize DB connection when running directly
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
