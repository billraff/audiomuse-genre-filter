# Playlist Genre Filter - an AudioMuse-AI plugin
# SPDX-License-Identifier: AGPL-3.0-only

"""Pure logic: a minimal Subsonic client and the track matching rules.

Nothing here imports AudioMuse-AI, so it can be tested on its own and does not
break when the core app's internals change.
"""

import json
import re
import urllib.parse
import urllib.request

CLIENT_NAME = "AudioMuse-PlaylistGenreFilter"
API_VERSION = "1.16.1"
REMOVE_BATCH = 100
TIMEOUT = 60

_GENRE_SPLIT = re.compile(r"\s*[;/,|]\s*")


class SubsonicError(RuntimeError):
    pass


class Subsonic:
    """Just the Subsonic calls this plugin needs, with the same auth core uses."""

    def __init__(self, url, user="", password="", api_key=""):
        if not url:
            raise SubsonicError("Navidrome URL is not configured for this server.")
        self.base = url.rstrip("/")
        if api_key:
            self.auth = {"apiKey": api_key}
        elif user and password:
            self.auth = {"u": user, "p": "enc:" + password.encode("utf-8").hex()}
        else:
            raise SubsonicError("Navidrome credentials are missing (need an API key or user and password).")

    @classmethod
    def from_creds(cls, creds):
        creds = creds or {}
        return cls(
            creds.get("url") or "",
            user=creds.get("user") or "",
            password=creds.get("password") or "",
            api_key=creds.get("api_key") or "",
        )

    def call(self, endpoint, params=()):
        query = list(self.auth.items()) + [("v", API_VERSION), ("c", CLIENT_NAME), ("f", "json")]
        query += [(k, str(v)) for k, v in params]
        url = f"{self.base}/rest/{endpoint}?{urllib.parse.urlencode(query)}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                data = json.load(resp).get("subsonic-response", {})
        except Exception as exc:
            raise SubsonicError(f"{endpoint}: {_redact(exc)}") from None
        if data.get("status") != "ok":
            err = data.get("error") or {}
            raise SubsonicError(f"{endpoint}: {err.get('code')} {err.get('message')}")
        return data

    def genres(self):
        data = self.call("getGenres")
        return [
            {"name": g.get("value") or "", "songs": int(g.get("songCount") or 0)}
            for g in (data.get("genres") or {}).get("genre") or []
            if g.get("value")
        ]

    def playlists(self):
        data = self.call("getPlaylists")
        return (data.get("playlists") or {}).get("playlist") or []

    def playlist_entries(self, playlist_id):
        data = self.call("getPlaylist", [("id", playlist_id)])
        return (data.get("playlist") or {}).get("entry") or []

    def remove_indices(self, playlist_id, indices):
        ordered = sorted(set(indices), reverse=True)
        for start in range(0, len(ordered), REMOVE_BATCH):
            batch = ordered[start:start + REMOVE_BATCH]
            params = [("playlistId", playlist_id)] + [("songIndexToRemove", i) for i in batch]
            self.call("updatePlaylist", params)

    def delete_playlist(self, playlist_id):
        self.call("deletePlaylist", [("id", playlist_id)])


_SECRET = re.compile(r"(?i)([?&](?:p|t|s|apiKey)=)[^&\s]*")


def _redact(exc):
    return _SECRET.sub(r"\1[REDACTED]", str(exc))


def clean_list(values):
    """Trim, drop blanks and case-insensitive duplicates, keep first spelling."""
    seen, out = set(), []
    for value in values or []:
        value = str(value).strip()
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            out.append(value)
    return out


def split_patterns(text):
    return clean_list(re.split(r"[,\n]", text or ""))


def song_genres(song):
    """Every genre a Subsonic song entry carries, lower-cased.

    Keeps the raw `genre` string whole (so "R&B/Soul" still matches a checklist
    entry of that name) and also its parts split on ; / , |, plus the OpenSubsonic
    `genres` list that holds every genre of a multi-genre file.
    """
    names = set()
    raw = (song.get("genre") or "").strip()
    if raw:
        names.add(raw)
        names.update(_GENRE_SPLIT.split(raw))
    for item in song.get("genres") or []:
        name = (item.get("name") or "").strip() if isinstance(item, dict) else ""
        if name:
            names.add(name)
    return {n.casefold() for n in names if n}


def block_reason(song, excluded_genres, patterns):
    """Why a playlist entry should go, or None to keep it.

    `excluded_genres` match a song genre exactly (ignoring case). `patterns` are
    words matched anywhere in the album title or file path (ignoring case).
    """
    excluded = {g.casefold() for g in excluded_genres}
    hit = sorted(song_genres(song) & excluded)
    if hit:
        return "genre: " + ", ".join(hit)
    album = (song.get("album") or "").casefold()
    path = (song.get("path") or "").casefold()
    for pattern in patterns:
        needle = pattern.casefold()
        if needle in album:
            return f"album matches '{pattern}'"
        if needle in path:
            return f"path matches '{pattern}'"
    return None


def name_matcher(suffix):
    """Match AudioMuse's clustering playlists: '<name><suffix>' or '<name><suffix> (2)'."""
    suffix = (suffix or "").strip() or "_automatic"
    return re.compile(re.escape(suffix) + r"(\s*\(\d+\))?$", re.IGNORECASE)


def filter_server(client, *, excluded_genres, patterns, suffix, apply, min_tracks=0,
                  owner=None, sample_limit=25, log=None):
    """Check every clustering playlist on one server and remove blocked tracks.

    Only playlists owned by `owner` (the account AudioMuse-AI uses) are touched,
    when the owner is known; other users' public playlists are left alone.
    Returns a summary dict. With apply=False nothing on the server changes.
    """
    matcher = name_matcher(suffix)
    summary = {"checked": 0, "changed": 0, "removed": 0, "deleted": 0, "playlists": []}
    for pl in client.playlists():
        name = pl.get("name") or ""
        if not matcher.search(name):
            continue
        if owner and pl.get("owner") and pl["owner"].casefold() != owner.casefold():
            continue
        summary["checked"] += 1
        entries = client.playlist_entries(pl["id"])
        hits = []
        for idx, song in enumerate(entries):
            reason = block_reason(song, excluded_genres, patterns)
            if reason:
                hits.append((idx, song, reason))
        if not hits:
            continue

        kept = len(entries) - len(hits)
        delete = bool(min_tracks) and kept < int(min_tracks)
        if apply:
            if delete:
                client.delete_playlist(pl["id"])
            else:
                client.remove_indices(pl["id"], [idx for idx, _, _ in hits])
        summary["changed"] += 1
        summary["removed"] += len(hits)
        summary["deleted"] += int(delete)
        summary["playlists"].append({
            "name": name,
            "total": len(entries),
            "removed": len(hits),
            "deleted": delete,
            "tracks": [
                {
                    "artist": song.get("artist") or "",
                    "title": song.get("title") or "",
                    "reason": reason,
                }
                for _, song, reason in hits[:sample_limit]
            ],
        })
        if log:
            log(name, len(hits), len(entries), delete)
    return summary
