"""A tiny in-memory Subsonic server for tests. Not part of the plugin."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SONGS = {
    "s1": {"id": "s1", "title": "Silent Night", "artist": "Choir", "album": "Carols",
           "genre": "Christmas", "genres": [{"name": "Christmas"}], "path": "Choir/Carols/01.flac"},
    "s2": {"id": "s2", "title": "Back in Black", "artist": "AC/DC", "album": "Back in Black",
           "genre": "Rock", "genres": [{"name": "Rock"}], "path": "ACDC/BiB/01.flac"},
    "s3": {"id": "s3", "title": "Jingle Bell Rock", "artist": "Bobby Helms", "album": "A Very Merry Christmas",
           "genre": "Pop", "genres": [{"name": "Pop"}], "path": "Various/Merry/03.flac"},
    "s4": {"id": "s4", "title": "Moonlight Sonata", "artist": "Beethoven", "album": "Sonatas",
           "genre": "Classical; Piano", "genres": [{"name": "Classical"}, {"name": "Piano"}],
           "path": "Beethoven/Sonatas/14.flac"},
    "s5": {"id": "s5", "title": "Superstition", "artist": "Stevie Wonder", "album": "Talking Book",
           "genre": "R&B/Soul", "genres": [], "path": "Stevie/TB/02.flac"},
    "s6": {"id": "s6", "title": "Holiday", "artist": "Green Day", "album": "American Idiot",
           "genre": "Rock", "genres": [{"name": "Rock"}], "path": "GreenDay/AI/03.flac"},
    "s7": {"id": "s7", "title": "Feliz Navidad", "artist": "Jose Feliciano", "album": "Hits",
           "genre": "Latin", "genres": [{"name": "Latin"}], "path": "xmas/Feliciano/01.flac"},
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_id = 100
        self.playlists = {}
        self.calls = []

    def add(self, name, song_ids, owner="audiomuse"):
        pid = str(self.next_id)
        self.next_id += 1
        self.playlists[pid] = {"id": pid, "name": name, "owner": owner, "songs": list(song_ids)}
        return pid


def make_handler(state, user="audiomuse", password="secret", api_key=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, payload):
            body = json.dumps({"subsonic-response": payload}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def _ok(self, **extra):
            self._send({"status": "ok", "version": "1.16.1", **extra})

        def _fail(self, code, msg):
            self._send({"status": "failed", "error": {"code": code, "message": msg}})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            self._handle(body)

        def do_GET(self):
            self._handle("")

        def _handle(self, body):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            for k, v in parse_qs(body).items():
                q.setdefault(k, []).extend(v)
            endpoint = url.path.rsplit("/", 1)[-1].removesuffix(".view")
            authed = (api_key and q.get("apiKey", [""])[0] == api_key) or (
                q.get("u", [""])[0] == user
                and q.get("p", [""])[0] == "enc:" + password.encode().hex()
            )
            if not authed:
                return self._fail(40, "Wrong username or password")
            with state.lock:
                state.calls.append((endpoint, q))
                return self._dispatch(endpoint, q)

        def _dispatch(self, endpoint, q):
            pls = state.playlists
            if endpoint == "ping":
                return self._ok()
            if endpoint == "getGenres":
                counts = {}
                for s in SONGS.values():
                    names = [g["name"] for g in s["genres"]] or [s["genre"]]
                    for n in names:
                        counts[n] = counts.get(n, 0) + 1
                return self._ok(genres={"genre": [
                    {"value": n, "songCount": c, "albumCount": 1} for n, c in counts.items()]})
            if endpoint == "getPlaylists":
                return self._ok(playlists={"playlist": [
                    {"id": p["id"], "name": p["name"], "owner": p["owner"], "songCount": len(p["songs"])}
                    for p in pls.values()]})
            if endpoint == "getPlaylist":
                p = pls.get(q.get("id", [""])[0])
                if not p:
                    return self._fail(70, "Playlist not found")
                return self._ok(playlist={"id": p["id"], "name": p["name"], "songCount": len(p["songs"]),
                                          "entry": [SONGS[s] for s in p["songs"]]})
            if endpoint == "updatePlaylist":
                p = pls.get(q.get("playlistId", [""])[0])
                if not p:
                    return self._fail(70, "Playlist not found")
                remove = {int(i) for i in q.get("songIndexToRemove", [])}
                p["songs"] = [s for i, s in enumerate(p["songs"]) if i not in remove]
                p["songs"] += q.get("songIdToAdd", [])
                return self._ok()
            if endpoint == "createPlaylist":
                pid = q.get("playlistId", [None])[0]
                if pid:
                    pls[pid]["songs"] = q.get("songId", [])
                else:
                    pid = state.add(q.get("name", [""])[0], q.get("songId", []))
                return self._ok(playlist={"id": pid})
            if endpoint == "deletePlaylist":
                pls.pop(q.get("id", [""])[0], None)
                return self._ok()
            if endpoint == "getMusicFolders":
                return self._ok(musicFolders={"musicFolder": [{"id": 1, "name": "Music"}]})
            if endpoint == "search3":
                offset = int(q.get("songOffset", ["0"])[0])
                songs = list(SONGS.values())[offset:offset + int(q.get("songCount", ["500"])[0])]
                return self._ok(searchResult3={"song": songs})
            return self._fail(0, f"unsupported endpoint {endpoint}")

    return Handler


def serve(port=0, host="127.0.0.1", **kw):
    state = State()
    server = ThreadingHTTPServer((host, port), make_handler(state, **kw))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":
    import sys
    srv, st = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 4533, host="0.0.0.0")
    st.add("Driving Rock_automatic", ["s2", "s1", "s6", "s3"])
    st.add("Late Night Piano_automatic (1)", ["s4", "s5", "s4"])
    st.add("Late Night Piano_automatic (2)", ["s5", "s7"])
    st.add("My Favourites", ["s1", "s4"], owner="william")
    print("fake navidrome on", srv.server_address, flush=True)
    threading.Event().wait()
