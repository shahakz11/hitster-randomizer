import os
import re
from flask import Flask, request, jsonify, redirect
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
logger.info(f"Environment: REDIRECT_URI={REDIRECT_URI}, FRONTEND_URL={FRONTEND_URL}")

# Valid icon names for playlists
VALID_ICONS = [
    'jukebox', 'boombox', 'microphone', 'bells',
    'music-note', 'record-player', 'guitar', 'headphones'
]
DEFAULT_ICON = 'music-note'

# MongoDB setup
try:
    mongodb = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000,
        retryWrites=True,
        w='majority'
    )
    db = mongodb['hitster']
    sessions = db['sessions']
    tracks = db['tracks']
    playlists = db['playlists']
    playlist_tracks = db['playlist_tracks']
    track_metadata = db['track_metadata']
    mongodb.admin.command('ping')  # Test connection
    # Create TTL index on tracks.expires_at for 2-hour expiry
    tracks.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=7200,
        partialFilterExpression={"expires_at": {"$exists": True}}
    )
    playlist_tracks.create_index([("playlist_id", 1)], unique=True)
    track_metadata.create_index([("track_name", 1), ("artist_name", 1)], unique=True)
    logger.info("MongoDB connected successfully and indexes ensured")
except Exception as e:
    logger.error(f"MongoDB connection or index creation failed: {e}")
    raise

# Get Client Credentials access token
def get_client_credentials_token():
    try:
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={'grant_type': 'client_credentials', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
        )
        response.raise_for_status()
        return response.json().get('access_token')
    except requests.RequestException as e:
        logger.error(f"Error getting client credentials token: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None

# Refresh Authorization Code access token
def refresh_access_token(session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session or not session.get('spotify_refresh_token'):
            logger.error(f"No refresh token for session {session_id}")
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
        expires_in = data.get('expires_in', 3600)
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data.get('access_token'),
                'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in)
            }}
        )
        logger.info(f"Refreshed access token for session {session_id}, modified: {result.modified_count}")
        return True
    except requests.RequestException as e:
        logger.error(f"Error refreshing access token for {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error in refresh_access_token for {session_id}: {e}")
        return False

# Fetch original release year from MusicBrainz
def get_original_release_year(track_name, artist_name, album_name, fallback_year):
    time.sleep(1)  # Respect MusicBrainz rate limit
    if fallback_year and (1900 <= fallback_year <= datetime.utcnow().year):
        logger.info(f"Using valid fallback year {fallback_year} for {track_name} by {artist_name}")
        return fallback_year
    cached = track_metadata.find_one({'track_name': track_name, 'artist_name': artist_name})
    if cached and cached.get('expires_at') > datetime.utcnow():
        logger.info(f"Using cached original year for {track_name} by {artist_name}: {cached['original_year']}")
        return cached['original_year']
    try:
        query = f'release:"{album_name}" AND artist:"{artist_name}"'
        response = requests.get(
            f'https://musicbrainz.org/ws/2/release?query={urlencode({"query": query})}&fmt=json',
            headers={'User-Agent': 'Songamizer/1.0 ( https://hitster-randomizer.onrender.com )'}
        )
        response.raise_for_status()
        data = response.json()
        logger.debug(f"MusicBrainz release response for {track_name} by {artist_name} (album: {album_name}): {data}")
        earliest_year = datetime.utcnow().year
        found_valid_year = False
        for release in data.get('releases', []):
            if 'date' in release and release['date']:
                try:
                    year = int(release['date'].split('-')[0])
                    if 1900 <= year <= datetime.utcnow().year:
                        if year < earliest_year or not found_valid_year:
                            earliest_year = year
                            found_valid_year = True
                except ValueError:
                    logger.warning(f"Invalid date format in release for {track_name} by {artist_name}: {release['date']}")
                    continue
        if found_valid_year:
            track_metadata.update_one(
                {'track_name': track_name, 'artist_name': artist_name},
                {'$set': {
                    'original_year': earliest_year,
                    'expires_at': datetime.utcnow() + timedelta(days=30)
                }},
                upsert=True
            )
            return earliest_year
        logger.warning(f"No valid year found for {track_name} by {artist_name}, using fallback: {fallback_year}")
        return fallback_year
    except Exception as e:
        logger.error(f"Error fetching original year for {track_name} by {artist_name}: {e}")
        return fallback_year  # Fallback to Spotify's year

# Get playlist tracks
def get_playlist_tracks(playlist_id, session_id):
    cache = playlist_tracks.find_one({'playlist_id': playlist_id})
    if cache and cache.get('cached_at') and (datetime.utcnow() - cache['cached_at']).total_seconds() < 300:  # 5 minutes
        return cache['tracks']
    token = get_client_credentials_token()
    if not token:
        logger.error(f"No client credentials token for playlist {playlist_id}")
        return []
    tracks = []
    offset = 0
    limit = 50
    while True:
        response = requests.get(
            f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit={limit}&offset={offset}',
            headers={'Authorization': f'Bearer {token}'}
        )
        if response.status_code == 401:
            token = get_client_credentials_token()
            if not token:
                logger.error(f"Failed to refresh client credentials token for playlist {playlist_id}")
                return []
            response = requests.get(
                f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit={limit}&offset={offset}',
                headers={'Authorization': f'Bearer {token}'}
            )
        response.raise_for_status()
        data = response.json()
        if 'items' not in data:
            logger.error(f"No 'items' in response for playlist {playlist_id}, Response: {data}")
            return []
        new_tracks = [item['track'] for item in data['items'] if item['track'] and item['track']['id']]
        tracks.extend(new_tracks)
        if len(data['items']) < limit:
            break
        offset += limit
    playlist_tracks.update_one(
        {'playlist_id': playlist_id},
        {'$set': {'tracks': tracks, 'cached_at': datetime.utcnow()}},
        upsert=True
    )
    return tracks

# Play track
def play_track(track_id, session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Invalid session_id {session_id}")
            return False, "No session found"
        token = session.get('spotify_access_token')
        if not token:
            logger.error(f"No access token for session {session_id}")
            return False, "No access token available"
        response = requests.get(
            'https://api.spotify.com/v1/me/player/devices',
            headers={'Authorization': f'Bearer {token}'}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                token = session.get('spotify_access_token')
                response = requests.get(
                    'https://api.spotify.com/v1/me/player/devices',
                    headers={'Authorization': f'Bearer {token}'}
                )
            else:
                return False, "Failed to refresh access token"
        response.raise_for_status()
        devices = response.json().get('devices', [])
        active_device = next((d for d in devices if d['is_active']), None)
        device_id = active_device['id'] if active_device else devices[0]['id'] if devices else None
        if not device_id:
            return False, "No active devices found. Open Spotify and play/pause a track."
        response = requests.put(
            'https://api.spotify.com/v1/me/player/play',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                token = session.get('spotify_access_token')
                response = requests.put(
                    'https://api.spotify.com/v1/me/player/play',
                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                    json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
                )
            else:
                return False, "Failed to refresh access token"
        if response.status_code == 403:
            return False, "Premium account required to play tracks."
        logger.info(f"Play request status for session {session_id}: {response.status_code}, Response: {response.text}")
        return response.status_code == 204, None
    except requests.RequestException as e:
        logger.error(f"Error checking devices or playing track for session {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False, f"Error checking devices: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in play_track for {session_id}: {e}")
        return False, f"Error playing track: {str(e)}"

# Authentication Endpoints
@app.route('/api/spotify/authorize')
def spotify_authorize():
    try:
        session_id = str(ObjectId())
        scope = 'user-read-private playlist-read-private user-read-email user-modify-playback-state'
        params = {
            'client_id': CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': REDIRECT_URI,
            'scope': scope,
            'state': session_id
        }
        auth_url = f'https://accounts.spotify.com/authorize?{urlencode(params)}'
        sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'created_at': datetime.utcnow(),
                'is_active': True
            }},
            upsert=True
        )
        logger.info(f"Initiated Spotify auth for session {session_id}")
        return redirect(auth_url)
    except Exception as e:
        logger.error(f"Error in spotify_authorize: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/spotify/callback')
def spotify_callback():
    code = request.args.get('code')
    session_id = request.args.get('state')
    if not code or not session_id:
        logger.error(f"Missing code or session_id: code={code}, session_id={session_id}")
        return jsonify({'error': 'Invalid callback parameters'}), 400
    try:
        response = requests.post(
            'https://accounts.spotify.com/api/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URI,
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET
            }
        )
        response.raise_for_status()
        data = response.json()
        expires_in = data.get('expires_in', 3600)
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data.get('access_token'),
                'spotify_refresh_token': data.get('refresh_token'),
                'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in)
            }}
        )
        logger.info(f"Spotify auth completed for session {session_id}, modified: {result.modified_count}")
        return redirect(f'{FRONTEND_URL}/game?session_id={session_id}')
    except requests.RequestException as e:
        logger.error(f"Spotify callback error: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Unexpected error in Spotify callback: {e}")
        return jsonify({'error': str(e)}), 400

# Playlist and Track Endpoints
@app.route('/api/playlists')
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
        token = session.get('spotify_access_token')
        if not token or (session.get('token_expires_at') and session['token_expires_at'] < datetime.utcnow()):
            if not refresh_access_token(session_id):
                logger.error(f"Failed to refresh token for session {session_id}")
                return jsonify({'error': 'Authentication required'}), 401
            session = sessions.find_one({'_id': ObjectId(session_id)})
            token = session.get('spotify_access_token')
        headers = {'Authorization': f'Bearer {token}'}
        response = requests.get('https://api.spotify.com/v1/me/playlists', headers=headers)
        response.raise_for_status()
        user_playlists = response.json().get('items', [])
        curated_playlists = playlists.find()
        all_playlists = []
        for playlist in curated_playlists:
            all_playlists.append({
                'id': playlist['playlist_id'],
                'name': playlist['name'],
                'icon': playlist.get('icon', DEFAULT_ICON)
            })
        for playlist in user_playlists:
            all_playlists.append({
                'id': playlist['id'],
                'name': playlist['name'],
                'icon': DEFAULT_ICON
            })
        logger.info(f"Retrieved {len(all_playlists)} playlists for session {session_id}")
        return jsonify(all_playlists)
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
            'created_at': session.get('created_at')
        })
    except Exception as e:
        logger.error(f"Error in get_session for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/tracks')
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
            track = tracks.find_one({'track_id': track_id, 'session_id': str(session['_id'])})
            if track:
                track_data.append({
                    'track_id': track['track_id'],
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

@app.route('/api/play-track/<playlist_id>')
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
        track_id = random_track['id']
        track_name = random_track['name']
        artist_name = random_track['artists'][0]['name']
        album_name = random_track['album']['name']
        fallback_year = int(random_track['album']['release_date'].split('-')[0]) if random_track['album']['release_date'] else datetime.utcnow().year
        original_year = get_original_release_year(track_name, artist_name, album_name, fallback_year)
        stream_url = get_stream_url(track_name, artist_name)
        if not stream_url:
            logger.error(f"Failed to get stream URL for {track_name} by {artist_name}")
            return jsonify({'error': 'Failed to get stream URL'}), 400
        tracks.insert_one({
            'track_id': track_id,
            'title': track_name,
            'artist': artist_name,
            'album': album_name,
            'release_year': original_year,
            'playlist_theme': playlist_id,
            'played_at': datetime.utcnow().isoformat(),
            'session_id': str(session['_id']),
            'expires_at': datetime.utcnow() + timedelta(hours=2),
            'stream_url': stream_url
        })
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$push': {'tracks_played': track_id}}
        )
        logger.info(f"Prepared track {track_id} for session {session_id}, modified: {result.modified_count}")
        return jsonify({
            'spotify_id': track_id,
            'title': track_name,
            'artist': artist_name,
            'release_year': original_year,
            'album': album_name,
            'playlist_theme': playlist_id,
            'played_at': datetime.utcnow().isoformat(),
            'stream_url': stream_url
        })
    except Exception as e:
        logger.error(f"Error in play_next_song for {session_id}: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/reset', methods=['POST'])
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
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
