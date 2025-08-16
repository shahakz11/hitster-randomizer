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
    # Create index on playlist_id for playlist_tracks
    playlist_tracks.create_index([("playlist_id", 1)], unique=True)
    # Create unique index on track_name and artist_name for track_metadata
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
    # Validate fallback year
    if fallback_year < 1900 or fallback_year > datetime.utcnow().year:
        logger.warning(f"Invalid Spotify fallback year {fallback_year} for {track_name} by {artist_name}, using current year")
        fallback_year = datetime.utcnow().year
    
    # Check cache first
    cached = track_metadata.find_one({'track_name': track_name, 'artist_name': artist_name})
    if cached and cached.get('expires_at') > datetime.utcnow():
        logger.info(f"Using cached original year for {track_name} by {artist_name}: {cached['original_year']}")
        return cached['original_year']
    
    try:
        # Query MusicBrainz release endpoint
        query = f'release:"{album_name}" AND artist:"{artist_name}"'
        response = requests.get(
            f'https://musicbrainz.org/ws/2/release?query={urlencode({"query": query})}&fmt=json',
            headers={'User-Agent': 'HitsterRandomizer/1.0 ( https://hitster-randomizer.onrender.com )'}
        )
        response.raise_for_status()
        data = response.json()
        logger.debug(f"MusicBrainz release response for {track_name} by {artist_name} (album: {album_name}): {data}")
        
        earliest_year = fallback_year
        found_valid_year = False
        
        # Check releases for date field
        for release in data.get('releases', []):
            if 'date' in release and release['date']:
                try:
                    year = int(release['date'].split('-')[0])
                    # Only accept reasonable years (post-1900)
                    if 1900 <= year <= datetime.utcnow().year:
                        if year < earliest_year or not found_valid_year:
                            earliest_year = year
                            found_valid_year = True
                except ValueError:
                    logger.warning(f"Invalid date format in release for {track_name} by {artist_name}: {release['date']}")
                    continue
        
        # Fallback to track-based search if no valid year found
        if not found_valid_year:
            query = f'recording:"{track_name}" AND artist:"{artist_name}"'
            response = requests.get(
                f'https://musicbrainz.org/ws/2/recording?query={urlencode({"query": query})}&fmt=json',
                headers={'User-Agent': 'HitsterRandomizer/1.0 ( https://hitster-randomizer.onrender.com )'}
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"MusicBrainz recording fallback response for {track_name} by {artist_name}: {data}")
            
            for recording in data.get('recordings', []):
                if 'first-release-date' in recording and recording['first-release-date']:
                    try:
                        year = int(recording['first-release-date'].split('-')[0])
                        if 1900 <= year <= datetime.utcnow().year:
                            if year < earliest_year or not found_valid_year:
                                earliest_year = year
                                found_valid_year = True
                    except ValueError:
                        logger.warning(f"Invalid date format in recording for {track_name} by {artist_name}: {recording['first-release-date']}")
                        continue
        
        # Cache only if we found a valid year different from fallback
        if found_valid_year and earliest_year != fallback_year:
            track_metadata.update_one(
                {'track_name': track_name, 'artist_name': artist_name},
                {'$set': {
                    'track_name': track_name,
                    'artist_name': artist_name,
                    'album_name': album_name,
                    'original_year': earliest_year,
                    'expires_at': datetime.utcnow() + timedelta(days=30)
                }},
                upsert=True
            )
            logger.info(f"Cached original year {earliest_year} for {track_name} by {artist_name} (album: {album_name})")
        else:
            logger.warning(f"No valid year found for {track_name} by {artist_name} (album: {album_name}), using fallback: {fallback_year}")
        
        return earliest_year
    except Exception as e:
        logger.error(f"Error fetching original year for {track_name} by {artist_name} (album: {album_name}): {e}")
        return fallback_year  # Fallback to Spotify's year

# Fetch playlist metadata
def get_playlist_metadata(playlist_id):
    cached = playlists.find_one({'playlist_id': playlist_id})
    if cached and cached.get('expires_at') > datetime.utcnow():
        return cached['name'], cached.get('custom_icon', DEFAULT_ICON), None
    token = get_client_credentials_token()
    if not token:
        return None, None, "Failed to get client credentials token"
    try:
        response = requests.get(
            f'https://api.spotify.com/v1/playlists/{playlist_id}?fields=name',
            headers={'Authorization': f'Bearer {token}'}
        )
        if response.status_code == 401:
            token = get_client_credentials_token()
            if not token:
                return None, None, "Failed to refresh client credentials token"
            response = requests.get(
                f'https://api.spotify.com/v1/playlists/{playlist_id}?fields=name',
                headers={'Authorization': f'Bearer {token}'}
            )
        response.raise_for_status()
        data = response.json()
        name = data.get('name', 'Unknown Playlist')
        result = playlists.update_one(
            {'playlist_id': playlist_id},
            {'$set': {
                'playlist_id': playlist_id,
                'name': name,
                'custom_icon': DEFAULT_ICON,
                'expires_at': datetime.utcnow() + timedelta(days=30),
            }},
            upsert=True
        )
        logger.info(f"Updated playlist metadata for {playlist_id}, modified: {result.modified_count}")
        return name, DEFAULT_ICON, None
    except requests.RequestException as e:
        logger.error(f"Error fetching playlist {playlist_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None, None, str(e)

# Fetch tracks from a playlist with caching
def get_playlist_tracks(playlist_id, session_id):
    # Check cache
    cache = playlist_tracks.find_one({'playlist_id': playlist_id})
    if cache and (datetime.utcnow() - cache['cached_at']).total_seconds() < 300:  # 5 minutes
        tracks = cache['tracks']
    else:
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
        # Update cache
        playlist_tracks.update_one(
            {'playlist_id': playlist_id},
            {'$set': {'tracks': tracks, 'cached_at': datetime.utcnow()}},
            upsert=True
        )
    
    session = sessions.find_one({'_id': ObjectId(session_id)})
    if not session:
        logger.error(f"Session {session_id} not found in get_playlist_tracks")
        return []
    played_track_ids = session.get('tracks_played', [])
    unplayed_tracks = [track for track in tracks if track['id'] not in played_track_ids]
    if not unplayed_tracks and tracks:
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {'tracks_played': []}}
        )
        logger.info(f"Reset tracks_played for session {session_id} for playlist {playlist_id}, modified: {result.modified_count}")
        return tracks
    if not unplayed_tracks:
        logger.info(f"No unplayed tracks for playlist {playlist_id}, Total tracks: {len(tracks)}, Played tracks: {len(played_track_ids)}")
        return unplayed_tracks
    return unplayed_tracks

# Check for active Spotify devices
def get_active_device(session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Session {session_id} not found in get_active_device")
            return None, "No session found"
        if not session.get('spotify_access_token'):
            logger.error(f"No access token for session {session_id}")
            return None, "No access token available"
        response = requests.get(
            'https://api.spotify.com/v1/me/player/devices',
            headers={'Authorization': f'Bearer {session["spotify_access_token"]}'}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                response = requests.get(
                    'https://api.spotify.com/v1/me/player/devices',
                    headers={'Authorization': f'Bearer {session["spotify_access_token"]}'}
                )
            else:
                logger.error(f"Failed to refresh access token for session {session_id}")
                return None, "Failed to refresh access token"
        response.raise_for_status()
        devices = response.json().get('devices', [])
        for device in devices:
            if device['is_active']:
                return device['id'], None
        return devices[0]['id'] if devices else None, "No active devices found. Open Spotify and play/pause a track."
    except requests.RequestException as e:
        logger.error(f"Error checking devices for session {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return None, f"Error checking devices: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in get_active_device for {session_id}: {e}")
        return None, f"Error checking devices: {str(e)}"

# Play a track
def play_track(track_id, session_id):
    try:
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session:
            logger.error(f"Session {session_id} not found in play_track")
            return False, "No session found"
        if not session.get('spotify_access_token'):
            logger.error(f"No access token for session {session_id}")
            return False, "Error: No access token set"
        device_id, error = get_active_device(session_id)
        if not device_id:
            return False, error or "Error: No active Spotify device found. Open Spotify and play/pause a track."
        response = requests.put(
            'https://api.spotify.com/v1/me/player/play',
            headers={'Authorization': f'Bearer {session["spotify_access_token"]}', 'Content-Type': 'application/json'},
            json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
        )
        if response.status_code == 401:
            if refresh_access_token(session_id):
                session = sessions.find_one({'_id': ObjectId(session_id)})
                response = requests.put(
                    'https://api.spotify.com/v1/me/player/play',
                    headers={'Authorization': f'Bearer {session["spotify_access_token"]}', 'Content-Type': 'application/json'},
                    json={'uris': [f'spotify:track:{track_id}'], 'device_id': device_id}
                )
            else:
                return False, "Failed to refresh access token"
        if response.status_code == 403:
            return False, "Premium account required to play tracks."
        logger.info(f"Play request status for session {session_id}: {response.status_code}, Response: {response.text}")
        return response.status_code == 204, None
    except requests.RequestException as e:
        logger.error(f"Error playing track {track_id} for session {session_id}: {e}, Response: {response.text if 'response' in locals() else 'No response'}")
        return False, f"Error playing track: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in play_track for {session_id}: {e}")
        return False, f"Error playing track: {str(e)}"

# Parse Spotify playlist URL
def parse_playlist_url(url):
    pattern = r'https?://open\.spotify\.com/playlist/([0-9a-zA-Z]{22})'
    match = re.match(pattern, url)
    if match:
        return match.group(1)
    return None

@app.route('/api/spotify/authorize')
def spotify_authorize():
    try:
        state = str(random.randint(100000, 999999))
        params = {
            'client_id': CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': REDIRECT_URI,
            'state': state,
            'scope': 'streaming user-read-playback-state user-modify-playback-state'
        }
        session_id = str(sessions.insert_one({
            'state': state,
            'created_at': datetime.utcnow().isoformat(),
            'is_active': False,
            'user_playlists': []  # Initialize empty, as playlists are fetched from hitster.playlists
        }).inserted_id)
        logger.info(f"Authorizing with state: {state}, session_id: {session_id}")
        auth_url = f"https://accounts.spotify.com/authorize?{urlencode(params)}"
        return redirect(auth_url)
    except Exception as e:
        logger.error(f"Error in spotify_authorize: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/spotify/callback')
def spotify_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')
    if error:
        logger.error(f"Spotify authorization error: {error}, state: {state}")
        return redirect(f"{FRONTEND_URL}?error={urlencode({'error': error})}")
    if not code or not state:
        error_msg = f"Invalid code or state: code={code}, state={state}"
        logger.error(error_msg)
        return redirect(f"{FRONTEND_URL}?error={urlencode({'error': error_msg})}")
    try:
        session = sessions.find_one({'state': state, 'is_active': False})
        if not session:
            error_msg = f"Invalid or used state: {state}"
            logger.error(error_msg)
            return redirect(f"{FRONTEND_URL}?error={urlencode({'error': error_msg})}")
        session_id = str(session['_id'])
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
        if response.status_code != 200:
            error_msg = f"Token exchange failed: status={response.status_code}, response={response.text}"
            logger.error(error_msg)
            return redirect(f"{FRONTEND_URL}?error={urlencode({'error': error_msg})}")
        data = response.json()
        expires_in = data.get('expires_in', 3600)
        result = sessions.update_one(
            {'_id': ObjectId(session_id)},
            {'$set': {
                'spotify_access_token': data.get('access_token'),
                'spotify_refresh_token': data.get('refresh_token'),
                'token_expires_at': datetime.utcnow() + timedelta(seconds=expires_in),
                'tracks_played': [],
                'is_active': True,
                'playlist_theme': None,
                'user_playlists': [],  # Empty, as playlists are fetched from hitster.playlists
                'created_at': datetime.utcnow().isoformat(),
                'state': None
            }}
        )
        logger.info(f"Callback for session {session_id}, state: {state}, modified: {result.modified_count}")
        session = sessions.find_one({'_id': ObjectId(session_id)})
        if not session or not session.get('is_active'):
            logger.error(f"Session {session_id} not updated correctly, session: {session}")
            return redirect(f"{FRONTEND_URL}?error={urlencode({'error': 'Session update failed'})}")
        logger.info(f"Callback success: session_id={session_id}, state={state}")
        return redirect(f"{FRONTEND_URL}?session_id={session_id}")
    except requests.RequestException as e:
        error_msg = f"Error in spotify_callback: {str(e)}, Response: {response.text if 'response' in locals() else 'No response'}"
        logger.error(error_msg)
        return redirect(f"{FRONTEND_URL}?error={urlencode({'error': error_msg})}")
    except Exception as e:
        error_msg = f"Unexpected error in spotify_callback: {str(e)}"
        logger.error(error_msg)
        return redirect(f"{FRONTEND_URL}?error={urlencode({'error': error_msg})}")

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
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
