#!/usr/bin/env python3
import os
import requests
import re
import time
import threading
import json
import socket
import Levenshtein
import socketio
from io import BytesIO
from dotenv import load_dotenv
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler

# Load environment variables
load_dotenv()

# Icecast2 server URL
ICECAST_URL = "https://c21.radioboss.fm:8239/autodj"

# Socket.IO server configuration
SOCKETIO_HOST = "0.0.0.0"
SOCKETIO_PORT = 5000

class BrainyFredServer:
    def __init__(self):
        # Variables
        self.current_song = "Not playing"
        self.artist = "Unknown Artist"
        self.title = "Unknown Title"
        self.album = ""
        self.artist_url = None
        self.artwork_url = None
        self.last_song_info = None
        self.artist_relations = []

        # Connection tracking
        self.connected_clients = set()
        self.stream_active = False
        self.stream_thread = None
        self.stream_lock = threading.Lock()

        # Try to initialize Socket.IO with gevent
        self.sio = socketio.Server(
            cors_allowed_origins='*',
            async_mode='gevent',
            json=json,
            logger=True,
            engineio_logger=False,
            max_http_buffer_size=1000000
        )
        self.socketio_mode = 'gevent'

        self.app = socketio.WSGIApp(self.sio)

        # Socket.IO event handlers
        self.sio.on('connect', self.socketio_connect)
        self.sio.on('disconnect', self.socketio_disconnect)

        # Log that server is ready
        self.log("Server ready - will start streaming when clients connect")

        # Start Socket.IO server
        self.start_socketio_server()

    def start_socketio_server(self):
        """Start the Socket.IO server with proper error handling"""
        try:
            self.log(f"Starting Socket.IO server on port {SOCKETIO_PORT}...")

            # Use gevent's WSGI server with WebSocket support
            server = pywsgi.WSGIServer(
                (SOCKETIO_HOST, SOCKETIO_PORT),
                self.app,
                handler_class=WebSocketHandler,
                log=None  # Disable access logging to console
            )

            self.log(f"Socket.IO server (gevent) running on port {SOCKETIO_PORT}")
            server.serve_forever()

        except Exception as e:
            self.log(f"Error starting Socket.IO server: {e}")

    def socketio_connect(self, sid, environ):
        """Handle Socket.IO client connection"""
        client_ip = environ.get('REMOTE_ADDR', 'unknown')
        self.log(f"Socket.IO client connected: {client_ip} (sid: {sid})")

        with self.stream_lock:
            self.connected_clients.add(sid)

            # Start streaming if this is the first client
            if len(self.connected_clients) == 1 and not self.stream_active:
                self.log("First client connected, starting stream...")
                self.start_streaming()

        # Send current song info immediately upon connection
        self.socketio_send_value_data(sid)

    def socketio_disconnect(self, sid):
        """Handle Socket.IO client disconnection"""
        self.log(f"Socket.IO client disconnected (sid: {sid})")

        with self.stream_lock:
            self.connected_clients.discard(sid)

            # Stop streaming if no clients are connected
            if len(self.connected_clients) == 0 and self.stream_active:
                self.log("Last client disconnected, stopping stream...")
                self.stop_streaming()

    def socketio_send_value_data(self, sid=None):
        """Send live value info to clients"""
        line = []
        if self.album or self.artist:
            line = [
                self.album or "",
                self.artist or ""
            ]

        link = {}
        if self.artist_url:
            link = {
                "text": "Artist website",
                "url": self.artist_url
            }

        extended_data = {
            "image": self.artwork_url or "https://assets.godcaster.fm/image_18_1733337133.jpg",
            "title": self.title or "Unknown Title",
            "line": line,
            "description": "Hello Fred! - 830-326-6365",
            "value": {},
            "type": "music",
            "link": link,
            "chaptersUrl": None,
            "enclosureUrl": "https://c21.radioboss.fm:8239/stream",
            "feedGuid": "35f494b2-cf5a-57cf-860e-198676e27d6f",
            "feedUrl": "https://feeds.godcaster.fm/player_18.xml",
            "medium": "music",
            "itemGuid": "35f494b2-cf5a-57cf-860e-198676e27d6f1740401883",
            "duration": 333.33,
            "startTime": 0
        }

        # If sid is provided, send to specific client, otherwise broadcast to all
        if sid:
            # Using the exact format requested
            self.sio.emit('remoteValue', extended_data, room=sid)
        else:
            # Using the exact format requested
            self.sio.emit('remoteValue', extended_data)

        print('remoteValue', extended_data)

    def start_streaming(self):
        """Start the streaming thread"""
        if not self.stream_active:
            self.stream_active = True
            self.stream_thread = threading.Thread(target=self.stream_background, daemon=True)
            self.stream_thread.start()
            self.log("Streaming started")

    def stop_streaming(self):
        """Stop the streaming thread"""
        self.stream_active = False
        self.log("Streaming stopped")

    def stream_background(self):
        """Background thread that continuously streams and processes metadata"""
        while self.stream_active:
            try:
                # Check if we still have connected clients
                with self.stream_lock:
                    if len(self.connected_clients) == 0:
                        self.log("No clients connected, stopping stream...")
                        self.stream_active = False
                        break

                self.log("Connecting to stream...")

                song_info_generator = self.stream_icecast()

                while self.stream_active:
                    try:
                        # Check if we still have connected clients
                        with self.stream_lock:
                            if len(self.connected_clients) == 0:
                                self.log("No clients connected, stopping stream...")
                                self.stream_active = False
                                break

                        song_info = next(song_info_generator)

                        if not song_info or song_info == self.last_song_info:
                            continue

                        # Process the new song
                        self.process_song_info(song_info)

                    except StopIteration:
                        break

                    except RecursionError:
                        self.log("Recursion error in stream processing, restarting...")
                        break

                    except Exception as e:
                        self.log(f"Error processing stream item: {e}")
                        break

                if self.stream_active:  # Only wait if we're still supposed to be active
                    time.sleep(5)  # Wait before reconnecting

            except Exception as e:
                self.log(f"Stream error: {e}")
                if self.stream_active:  # Only wait if we're still supposed to be active
                    time.sleep(10)  # Wait before trying again

        self.log("Stream background thread ended")

    def process_song_info(self, song_info):
        """Process a new song from the stream"""
        try:
            self.last_song_info = song_info
            self.current_song = song_info

            self.title = song_info
            self.artist = ""
            self.album = ""
            self.artist_url = None
            self.artwork_url = None
            self.artist_relations = []

            # Parse song info in "Artist - Title" format
            artist, title = self.parse_song_info(song_info)

            if artist and title:
                self.title = title
                self.artist = artist

            # Send updated info to all connected Socket.IO clients
            self.socketio_send_value_data()

            # Get additional info from MusicBrainz
            if artist and title:
                self.log(f"Looking up '{title}' by {artist} on MusicBrainz...")
                threading.Thread(target=lambda: self.fetch_track_info(artist, title), daemon=True).start()

        except Exception as e:
            self.log(f"Error processing song: {str(e)}")

    def fetch_track_info(self, artist, title):
        """Fetch track info from MusicBrainz in a separate thread"""
        track_info = self.search_musicbrainz(artist, title)

        if not track_info:
            self.log("Track not found on MusicBrainz")
            return

        # Update information
        self.album = track_info.get('album', '')

        # Send basic track/artist/album info
        self.socketio_send_value_data()

        # Get cover art if available
        if track_info["mbid"]:
            self.artwork_url = self.get_cover_art_url("release", track_info["mbid"])

        if not self.artwork_url and track_info["album_mbid"]:
            self.artwork_url = self.get_cover_art_url("release-group", track_info["album_mbid"])

        # Get artist website and relations
        if track_info["artist_mbid"]:
            self.artist_url = self.get_artist_website(track_info["artist_mbid"])

        self.log(f"Now playing: {title} by {artist}")

        # Send updated info with artwork to all connected Socket.IO clients
        self.socketio_send_value_data()

    def parse_song_info(self, song_info):
        """Parse artist and title from song info string"""
        if not song_info:
            return None, None

        parts = song_info.split(" - ", 1)

        if len(parts) != 2:
            return None, None

        return parts[0].strip(), parts[1].strip()

    def search_musicbrainz(self, artist, title):
        """Search MusicBrainz for track information and cover art"""

        # Try searching for the artist/title,
        # then try without "featuring" text, e.g.: featuring Some Guy,
        # then try without parenthesis text, e.g.: (Radio version)
        search_options = [
            (artist, title),
            (self._remove_featuring(artist), self._remove_featuring(title)),
            (self._remove_parenthetical_text(artist), self._remove_parenthetical_text(title))
        ]

        last_artist = None
        last_title = None

        # Check if this is a live track
        is_live_track = self._is_live_track(title)

        for artist_query, title_query in search_options:
            if (artist_query, title_query) == (last_artist, last_title):
                continue # skip same search

            print(f"Trying search with: {title_query} - {artist_query}")
            result = self._do_musicbrainz_search(artist_query, title_query, is_live_track)

            if result:
                return result

            time.sleep(1) # response api limit

            last_artist = artist_query
            last_title = title_query

        return None

    def _remove_parenthetical_text(self, text):
        """Remove text within parentheses () and brackets []"""
        clean = re.sub(r'\([^)]*\)|\[[^\]]*\]', '', text)
        return re.sub(r'\s+', ' ', clean).strip()

    def _remove_featuring(self, text):
        """Remove 'featuring', 'feat.', 'ft.' parts from the title"""
        patterns = [
            r'\(feat\.[^)]*\)', r'\(ft\.[^)]*\)', r'\(featuring[^)]*\)',
            r'\s+featuring\s+[^)]+', r'\s+feat\.\s+[^)]+', r'\s+ft\.\s+[^)]+',
            r'\s+with\s+[^)]+', r',\s+featuring\s+[^)]+', r',\s+feat\.\s+[^)]+',
            r',\s+ft\.\s+[^)]+',
        ]

        clean_text = text
        for pattern in patterns:
            clean_text = re.sub(pattern, '', clean_text, flags=re.IGNORECASE)

        return re.sub(r'\s+', ' ', clean_text).strip()

    def _do_musicbrainz_search(self, artist, title, is_live_track):
        """Perform the actual MusicBrainz search"""
        url = "https://musicbrainz.org/ws/2/recording"

        params = {
            "query": f"artist:\"{artist}\" AND recording:\"{title}\"",
            "fmt": "json",
            "inc": "releases artist-rels url-rels"
        }

        headers = {
            "User-Agent": "BrainyFred/1.0.0 (your@email.com)"
        }

        try:
            response = requests.get(url, params=params, headers=headers)

            if response.status_code != 200:
                return None

            # Gather all releases of this track
            releases = []

            data = response.json()

            if "recordings" not in data or not data["recordings"]:
                print(f"No recordings found for {artist} - {title}")
                return None

            # Find best recording match using fuzzy matching
            for recording in data["recordings"]:
                mb_artist = recording["artist-credit"][0]["artist"]["name"]
                mb_title = recording["title"]

                # Check if the returned artist and title match our search terms
                if not (self.fuzzy_match(artist, mb_artist) and self.fuzzy_match(title, mb_title)):
                    continue

                if "releases" not in recording:
                    continue # no album releases

                for release in recording["releases"]:
                    releases.append({
                        "artist": mb_artist,
                        "title": mb_title,
                        "artist_mbid": recording["artist-credit"][0]["artist"]["id"],
                        **release,
                    })

        except Exception as e:
            self.log(f"Error searching MusicBrainz: {e}")
            return None

        if not releases:
            return None

        # Find the best release to show (try to skip compilation/soundtrack releases)
        release = self._select_best_release(releases, is_live_track)

        if not release:
            return None

        track = {}
        if "media" in release:
            track = release["media"][0]["track"][0]

        return {
            "title": track.get("title", title),
            "artist": release.get("artist", artist),
            "album": release["title"],
            "mbid": release["id"],
            "artist_mbid": release.get("artist_mbid"),
            "album_mbid": release.get("release-group", {}).get("id"),
        }

    def _is_live_track(self, title):

        # Check for (city name) or [venue name] patterns
        if re.search(r'\([^)]*\d{4}\)', title) or re.search(r'\[[^]]*\d{4}\]', title):
            return True

        return False

    def _select_best_release(self, releases, is_live_track):
        """Select the best release from a list based on type and country"""

        # Try to figure out the best release to show for this track
        release_groups = {
            "live": [], "single": [], "studio": [], "other": [],
        }

        for release in releases:
            # Determine if it's a studio or live album and its country
            is_live = self._is_live_release(release)

            # Primary/Secondary types (e.g. Album / Compilation)
            primary_type = release["release-group"].get("primary-type")
            secondary_types = release["release-group"].get("secondary-types", [])

            is_single = "Single" == primary_type
            is_compilation = "Compilation" in secondary_types

            # Categorize
            if is_live: # live releases
                release_groups["live"].append(release)

            elif is_single and not is_compilation: # single
                release_groups["single"].append(release)

            elif not is_compilation: # album releases
                release_groups["studio"].append(release)

            else: # whatever
                release_groups["other"].append(release)

        # prioritize US and worldwide (XW) releases over all others
        if is_live_track: # prioritize live releases if live track
            priority_order = ["live", "single", "studio", "other"]

        else: # prioritize album release if not live track
            priority_order = ["single", "studio", "other"]

        # Return the first release from the first non-empty group
        for group_name in priority_order:
            if release_groups[group_name]:
                return release_groups[group_name][0]

        return None

    def _is_live_release(self, release):
        """Determine if a release is a live album"""

        # Check secondary types for "Live" marker
        if "release-group" in release and "secondary-types" in release["release-group"]:
            if "Live" in release["release-group"]["secondary-types"]:
                return True

        # Check title for live indicators
        if "title" in release:
            title_lower = release["title"].lower()
            live_indicators = ["live", "concert", "in concert"]

            if any(indicator in title_lower for indicator in live_indicators):
                return True

        return False

    def get_cover_art_url(self, type, mbid):
        """Add cover art URL to the result dict if available"""
        try:
            cover_art_url = f"https://coverartarchive.org/{type}/{mbid}/front-500"

            if requests.head(cover_art_url).status_code < 400:
                return cover_art_url

        except Exception:
            pass

        return None

    def get_artist_website(self, artist_mbid):
        """Get the artist's website and relations from MusicBrainz"""
        try:
            url = f"https://musicbrainz.org/ws/2/artist/{artist_mbid}"
            params = {"fmt": "json", "inc": "url-rels"}
            headers = {"User-Agent": "BrainyFred/1.0.0 (your@email.com)"}

            response = requests.get(url, params=params, headers=headers)

            if response.status_code != 200:
                self.log(f"Error getting artist website: HTTP {response.status_code}")
                return None

            data = response.json()

            if "relations" not in data:
                return None

            relations = {
                (rel["type"], rel["url"]["resource"]) for rel in data["relations"] if rel["url"]
            }

            # Return official homepage if available
            if "official homepage" in relations:
                return relations["official homepage"]

            # Try to get website from Wikidata if no official homepage
            if "wikidata" in relations:
                website_url = self.get_website_from_wikidata(relations["wikidata"])

                if website_url:
                    return website_url

            # Return whatever is available
            for type in ["wikipedia", "social network", "bandcamp", "youtube"]:
                if type in relations:
                    return relations[type]

        except Exception as e:
            self.log(f"Error getting artist website: {e}")

        return None

    def get_website_from_wikidata(self, wikidata_url):
        """Extract entity ID from Wikidata URL and get the official website"""
        try:
            # Extract the entity ID (Q followed by numbers)
            match = re.search(r'(Q\d+)', wikidata_url)
            if not match:
                return None

            entity_id = match.group(1)

            # Query Wikidata for the official website property
            url = "https://www.wikidata.org/w/api.php"

            params = {
                "action": "wbgetclaims",
                "format": "json",
                "entity": entity_id,
                "property": "P856"  # Property ID for "official website"
            }

            response = requests.get(url, params=params)

            if response.status_code == 200:
                data = response.json()

                if "claims" in data and "P856" in data["claims"]:
                    claims = data["claims"]["P856"]

                    if claims and "mainsnak" in claims[0] and "datavalue" in claims[0]["mainsnak"]:
                        return claims[0]["mainsnak"]["datavalue"]["value"]

            return None

        except Exception:
            return None

    def fuzzy_match(self, str1, str2, threshold=0.8):
        """Compare two strings using Levenshtein distance"""
        if not str1 or not str2:
            return False

        # Convert to lowercase
        s1 = str1.lower()
        s2 = str2.lower()

        # Quick exact match check
        if s1 == s2:
            return True

        # Check if one string contains the other
        if s1 in s2 or s2 in s1:
            return True

        # Calculate Levenshtein similarity
        distance = Levenshtein.distance(s1, s2)
        max_len = max(len(s1), len(s2))
        if max_len == 0:
            return False

        similarity = 1 - (distance / max_len)
        return similarity >= threshold

    def log(self, message):
        """Log a message to console"""
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}")

    def stream_icecast(self):
        """Stream from Icecast server and yield metadata as it changes"""
        headers = {
            "Icy-MetaData": "1",
            "User-Agent": "BrainyFred/1.0",
            "Accept": "audio/mpeg, audio/aac, audio/ogg, */*"
        }

        try:
            with requests.get(ICECAST_URL, headers=headers, stream=True, timeout=60) as response:
                # Check headers for initial song info
                for header in ['icy-title', 'icy-streamtitle']:
                    if header in response.headers:
                        song_info = response.headers.get(header)
                        if song_info and song_info != "Unknown":
                            yield song_info
                            break

                # If metaint is provided, read metadata from the stream
                if 'icy-metaint' in response.headers:
                    metaint = int(response.headers['icy-metaint'])

                    while self.stream_active:
                        try:
                            # Skip audio data
                            chunk = response.raw.read(metaint)
                            if not chunk or len(chunk) < metaint:
                                break

                            # Read metadata length byte
                            meta_length_byte = response.raw.read(1)
                            if not meta_length_byte:
                                break

                            meta_length = ord(meta_length_byte) * 16

                            if meta_length > 0:
                                # Read metadata
                                meta_data = response.raw.read(meta_length).decode('utf-8', errors='ignore')

                                # Extract StreamTitle
                                if 'StreamTitle=' in meta_data:
                                    title_start = meta_data.find('StreamTitle=') + len('StreamTitle=')
                                    title_end = meta_data.find(';', title_start)
                                    if title_start > 0 and title_end > 0:
                                        song_info = meta_data[title_start:title_end].strip("'")
                                        if song_info and song_info != "Unknown":
                                            yield song_info
                            else:
                                # Skip empty metadata block
                                pass
                        except RecursionError:
                            # Handle recursion error if it occurs
                            self.log("Recursion error in stream processing, restarting stream...")
                            time.sleep(2)
                            break
                else:
                    # Without metaint, keep connection open but can't get new metadata
                    while self.stream_active:
                        # Read small chunks to keep connection alive
                        if not response.raw.read(8192):
                            break
                        time.sleep(0.5)
        except Exception as e:
            self.log(f"Stream error: {e}")
            yield None

if __name__ == "__main__":
    try:
        print("Starting Brainy Fred Server...")
        server = BrainyFredServer()

    except KeyboardInterrupt:
        print("Server stopped by user.")

    except Exception as e:
        print(f"Fatal error: {e}")
