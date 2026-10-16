# Playlist Genre Filter for AudioMuse-AI

An [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) plugin that removes
unwanted genres from the playlists clustering creates.

Clustering uses every song in your library. If you have Christmas or classical music
in the same library, those tracks end up in the generated `_automatic` playlists.
This plugin checks those playlists after clustering and takes out every track whose
genre you excluded. It also removes tracks whose album title or file path contains a
word you choose, which catches holiday albums tagged as Pop or Rock.

It runs on the stock AudioMuse-AI image, so you don't need a custom build.

## What it does and doesn't do

- **It cleans the generated playlists after clustering.** It does not change clustering
  itself. Excluded songs still take part in clustering, so they can shape the
  clusters before they are removed. A cluster made mostly of excluded songs ends up as
  a small playlist. You can delete those automatically (see Advanced below).
- **It only touches playlists whose names end in `_automatic`** (or `_automatic (2)`
  and so on). Your own playlists are never read or changed.
- **It doesn't affect Instant Mix** or other similar-song features. Those come from the
  Navidrome plugin and AudioMuse's live analysis, not from these playlists.
- **It supports Navidrome only for now.** Jellyfin, Emby, Plex and Lyrion servers are
  skipped with a log line.

## Requirements

- AudioMuse-AI 3.2.0 or newer (tested against 3.6.x).
- A Navidrome server configured under Music Servers.
- A persistent plugins volume on **both** the Flask and worker containers. Without it,
  AudioMuse re-downloads plugins at every start. The official `docker-compose.yaml`
  already includes these volumes. If yours doesn't, add them:

```yaml
  audiomuse-ai-flask:
    volumes:
      - plugins-flask:/app/plugin/installed

  audiomuse-ai-worker:
    volumes:
      - plugins-worker:/app/plugin/installed

volumes:
  plugins-flask:
  plugins-worker:
```

## Install

1. In AudioMuse-AI, open **Plugins → Repositories** and add:
   `https://raw.githubusercontent.com/YOUR_GITHUB_USER/audiomuse-genre-filter/main/manifest.json`
2. Open the **Catalog** tab, find **Playlist Genre Filter** and click **Install**.
3. Click **Apply now (restart)**.

## Use

Open **Playlist Genre Filter** from the menu (admins only), or use the Settings button on
the Plugins page.

1. **Genres to exclude:** tick genres from the checklist. It lists every genre tag on
   your Navidrome server with its song count, and has a search box. Matching is exact
   and ignores case, so ticking `Rock` won't catch "Jingle Bell Rock" tagged Pop. Files
   with several genres are matched on any of them.
2. **Album and path patterns:** enter words such as `christmas, xmas`. These are matched
   anywhere in the album title or file path, ignoring case.
3. **Mode:** leave this on **Preview** first and click **Save and run now**. The Recent
   runs table lists every track the filter would remove and why. When the list looks
   right, switch to **Apply** and run again.
4. **Schedule it:** go to **Administration → Scheduled Tasks**, find
   `plugin.playlist_genre_filter.filter` and enable it. It's added disabled, set to run
   every 30 minutes, and you can change the schedule. AudioMuse won't start it while
   clustering or analysis is running, so it simply catches up afterwards. A run with
   nothing to remove takes a few seconds and changes nothing.

### Advanced

- **Playlist name suffix:** change this only if your clustering playlists use a
  different suffix.
- **Delete playlists left with fewer than N tracks:** this applies only in Apply mode.
  A playlist that drops below N tracks after filtering is deleted instead of left nearly
  empty. The default of 0 never deletes anything.

## How it works

The plugin uses only the documented plugin API (`plugin.api`): settings, its own run-log
table, a scheduled task and a page. It reads the Navidrome connection details from
AudioMuse's music server list and talks to Navidrome directly over the Subsonic API:
`getPlaylists`, `getPlaylist`, `updatePlaylist` and `deletePlaylist`. It doesn't import
AudioMuse's internal modules, so core updates are unlikely to break it.

Removing a track from a playlist doesn't touch the song itself. If you rerun clustering,
the playlists are rebuilt from scratch, and the next filter run cleans them again.

## Development

```
python3 -m unittest discover -s tests
```

`tests/fake_navidrome.py` is a small in-memory Subsonic server used by the tests. You can
also run it on its own (`python3 tests/fake_navidrome.py 4533`) to try the plugin against
a local AudioMuse-AI without a real library.

### Releasing a new version

Add a new entry at the **top** of `versions` in `plugins/PlaylistGenreFilter/plugin.json`
with a higher `version` and a `changelog`, then push. The GitHub Actions workflow builds
the zip into `dist/` and fills in its `sourceUrl` and `checksum`. Never change a version
that is already published. AudioMuse picks up the new version within about an hour.

## License

AGPL-3.0, the same license as AudioMuse-AI. The community plugin catalog requires it.
