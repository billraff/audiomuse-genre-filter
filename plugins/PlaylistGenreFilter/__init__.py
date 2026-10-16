# Playlist Genre Filter - an AudioMuse-AI plugin
# SPDX-License-Identifier: AGPL-3.0-only

"""Remove unwanted genres from AudioMuse-AI's clustering playlists.

Clustering uses every song in the library, so holiday or classical tracks can
land in the generated playlists. This plugin checks those playlists on the
media server after clustering and takes out tracks whose genre (or album title
or file path) you excluded. It only uses the documented plugin API plus its own
small Subsonic client, so core updates should not break it.

Navidrome only for now; other server types are skipped with a log line.
"""

import html
import json

from flask import Blueprint, redirect, request, url_for

from plugin.api import (
    active_server_id,
    enqueue,
    get_db,
    get_setting,
    list_servers,
    logger,
    render_page,
    set_setting,
    table,
)

from .filtering import Subsonic, SubsonicError, clean_list, filter_server, split_patterns

PLUGIN_ID = "playlist_genre_filter"
TASK_NAME = "filter"
TASK_TYPE = f"plugin.{PLUGIN_ID}.{TASK_NAME}"
DEFAULT_CRON = "*/30 * * * *"
KEEP_RUNS = 30
SUPPORTED_TYPES = ("navidrome",)

DEFAULTS = {
    "genres": [],
    "patterns": [],
    "suffix": "_automatic",
    "mode": "preview",
    "min_tracks": 0,
}

bp = Blueprint(PLUGIN_ID, __name__)


# ---------------------------------------------------------------- settings

def _settings():
    return {
        "genres": clean_list(get_setting("genres", DEFAULTS["genres"])),
        "patterns": clean_list(get_setting("patterns", DEFAULTS["patterns"])),
        "suffix": (get_setting("suffix", DEFAULTS["suffix"]) or DEFAULTS["suffix"]).strip(),
        "mode": "apply" if get_setting("mode", DEFAULTS["mode"]) == "apply" else "preview",
        "min_tracks": max(0, int(get_setting("min_tracks", DEFAULTS["min_tracks"]) or 0)),
    }


# ---------------------------------------------------------------- run log

def migrate(db):
    cur = db.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS " + table("runs") + " ("
        "id SERIAL PRIMARY KEY, ran_at TIMESTAMP DEFAULT now(), server_name TEXT, "
        "mode TEXT, status TEXT, message TEXT, summary TEXT)"
    )
    cur.execute(
        "INSERT INTO cron (name, task_type, cron_expr, enabled) VALUES (%s, %s, %s, FALSE) "
        "ON CONFLICT (task_type) DO NOTHING",
        (TASK_TYPE, TASK_TYPE, DEFAULT_CRON),
    )
    db.commit()
    cur.close()


def _log_run(server_name, mode, status, message, summary=None):
    db = get_db()
    cur = db.cursor()
    try:
        runs = table("runs")
        cur.execute(
            "INSERT INTO " + runs + " (server_name, mode, status, message, summary) "
            "VALUES (%s, %s, %s, %s, %s)",
            (server_name, mode, status, message, json.dumps(summary) if summary else None),
        )
        cur.execute(
            "DELETE FROM " + runs + " WHERE id NOT IN "
            "(SELECT id FROM " + runs + " ORDER BY id DESC LIMIT %s)",
            (KEEP_RUNS,),
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("%s: could not record the run", PLUGIN_ID)
    finally:
        cur.close()


def _recent_runs(limit=10):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "SELECT to_char(ran_at, 'YYYY-MM-DD HH24:MI'), server_name, mode, status, message, summary "
            "FROM " + table("runs") + " ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()
    except Exception:
        db.rollback()
        return []
    finally:
        cur.close()


# ---------------------------------------------------------------- the task

def _active_server():
    current = active_server_id()
    servers = list_servers()
    if current:
        return next((s for s in servers if s["server_id"] == current), None)
    return next((s for s in servers if s.get("is_default")), None)


def filter_playlists():
    """Scheduled task. AudioMuse-AI runs it once per configured music server."""
    server = _active_server()
    if not server:
        return {"message": "No music server configured; nothing to do."}
    name = server.get("name") or server.get("server_id")
    if server.get("server_type") not in SUPPORTED_TYPES:
        msg = f"Skipped {name}: {server.get('server_type')} servers are not supported yet."
        logger.info("%s: %s", PLUGIN_ID, msg)
        return {"message": msg}

    cfg = _settings()
    if not cfg["genres"] and not cfg["patterns"]:
        msg = "Nothing to exclude yet. Pick genres or patterns in the plugin settings."
        logger.info("%s: %s", PLUGIN_ID, msg)
        return {"message": msg}

    apply = cfg["mode"] == "apply"

    def log_line(pl_name, removed, total, deleted):
        if deleted:
            verb = "Deleted" if apply else "Would delete"
            logger.info("%s: %s '%s' (%d of %d tracks excluded, fewer than %d left)",
                        PLUGIN_ID, verb, pl_name, removed, total, cfg["min_tracks"])
        else:
            verb = "Removed" if apply else "Would remove"
            logger.info("%s: %s %d of %d tracks from '%s'", PLUGIN_ID, verb, removed, total, pl_name)

    try:
        client = Subsonic.from_creds(server.get("creds"))
        summary = filter_server(
            client,
            excluded_genres=cfg["genres"],
            patterns=cfg["patterns"],
            suffix=cfg["suffix"],
            apply=apply,
            min_tracks=cfg["min_tracks"],
            owner=(server.get("creds") or {}).get("user") or None,
            log=log_line,
        )
    except SubsonicError as exc:
        msg = f"{name}: {exc}"
        logger.error("%s: %s", PLUGIN_ID, msg)
        _log_run(name, cfg["mode"], "error", msg)
        raise

    tracks = _plural(summary["removed"], "track")
    checked = _plural(summary["checked"], "playlist")
    if apply:
        msg = f"{name}: removed {tracks} from {summary['changed']} of {checked}"
    else:
        msg = f"{name}: preview, would remove {tracks} from {summary['changed']} of {checked}"
    if summary["deleted"]:
        small = _plural(summary["deleted"], "small playlist")
        msg += f" ({'deleted' if apply else 'would delete'} {small})"
    logger.info("%s: %s", PLUGIN_ID, msg)
    # A scheduled run that finds nothing is the normal case; note the time
    # instead of filling the run log with empty rows.
    if apply and not summary["changed"]:
        _note_clean_check(name)
    else:
        _log_run(name, cfg["mode"], "ok", msg, summary)
    return {"message": msg}


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def _note_clean_check(server_name):
    # Use the database clock, like the run log, so both show the same time zone.
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT to_char(now(), 'YYYY-MM-DD HH24:MI')")
        stamp = cur.fetchone()[0]
    finally:
        cur.close()
    checks = get_setting("last_clean_check", {}) or {}
    checks[server_name] = stamp
    set_setting("last_clean_check", checks)


# ---------------------------------------------------------------- pages

def _navidrome_servers():
    return [s for s in list_servers() if s.get("server_type") in SUPPORTED_TYPES]


def _server_genres():
    """Genres with song counts across every Navidrome server, plus any error text."""
    counts, errors = {}, []
    for server in _navidrome_servers():
        try:
            for g in Subsonic.from_creds(server.get("creds")).genres():
                key = g["name"].casefold()
                name, songs = counts.get(key, (g["name"], 0))
                counts[key] = (name, songs + g["songs"])
        except SubsonicError as exc:
            errors.append(f"{server.get('name')}: {exc}")
    return sorted(counts.values(), key=lambda x: x[0].casefold()), errors


_STYLE = """
<style>
.pgf{max-width:900px}
.pgf fieldset{border:1px solid var(--border-color);border-radius:8px;padding:1rem;margin:0 0 1rem}
.pgf legend{font-weight:600;padding:0 .4rem}
.pgf .hint{color:var(--text-muted);font-size:.9rem;margin:.25rem 0 .6rem}
.pgf input[type=text],.pgf input[type=number],.pgf textarea,.pgf input[type=search]{
  width:100%;box-sizing:border-box;padding:.45rem .6rem;border:1px solid var(--border-color);
  border-radius:6px;background:var(--bg-input);color:var(--text-main)}
.pgf .genres{max-height:320px;overflow:auto;border:1px solid var(--border-color);border-radius:6px;
  padding:.4rem .6rem;margin-top:.5rem;columns:2 220px}
.pgf .genres label{display:block;padding:.15rem 0;break-inside:avoid}
.pgf .count{color:var(--text-muted);font-size:.85rem}
.pgf .msg{padding:.6rem .8rem;border-radius:6px;margin-bottom:1rem;background:var(--status-idle-bg);color:var(--status-idle-text)}
.pgf .err{background:var(--status-failure-bg);color:var(--status-failure-text)}
.pgf .row{display:flex;gap:1rem;flex-wrap:wrap}.pgf .row>div{flex:1 1 220px}
.pgf .runs{overflow-x:auto}
.pgf table{border-collapse:collapse;width:100%;font-size:.92rem}
.pgf td,.pgf th{padding:.35rem .5rem;border-top:1px solid var(--border-color);text-align:left;vertical-align:top}
.pgf td{overflow-wrap:anywhere}.pgf td:first-child,.pgf td:nth-child(2){white-space:nowrap}
.pgf .choice{display:block;margin:.3rem 0}
.pgf details ul{margin:.3rem 0 .6rem 1.1rem;padding:0}
.pgf .actions{display:flex;gap:.6rem;flex-wrap:wrap}
</style>
"""

_SCRIPT = """
<script>
(function(){
  var box=document.getElementById('pgf-search');
  if(!box)return;
  box.addEventListener('input',function(){
    var q=box.value.toLowerCase();
    document.querySelectorAll('#pgf-genres label').forEach(function(l){
      l.style.display=l.dataset.name.indexOf(q)>-1?'':'none';
    });
  });
})();
</script>
"""


def _genre_checklist(saved):
    genres, errors = _server_genres()
    chosen = {g.casefold() for g in saved}
    on_server = {name.casefold() for name, _ in genres}
    rows = []
    for g in saved:
        if g.casefold() not in on_server:
            rows.append((g, None))
    rows += genres
    items = "".join(
        f'<label data-name="{html.escape(name.casefold())}">'
        f'<input type="checkbox" name="genre" value="{html.escape(name)}"'
        f'{" checked" if name.casefold() in chosen else ""}> {html.escape(name)} '
        f'<span class="count">{"(not on server)" if songs is None else f"({songs:,})"}</span></label>'
        for name, songs in rows
    )
    err = "".join(f'<div class="msg err">Could not read genres from {html.escape(e)}</div>' for e in errors)
    if not rows:
        items = '<p class="hint">No genres found. Check the Navidrome connection under Music Servers.</p>'
    return err + (
        '<input type="search" id="pgf-search" placeholder="Search genres">'
        f'<div class="genres" id="pgf-genres">{items}</div>'
    )


def _runs_html():
    checks = get_setting("last_clean_check", {}) or {}
    clean = "".join(
        f'<p class="hint">Last check with nothing to remove on {html.escape(str(server))}: '
        f'{html.escape(str(when))}</p>'
        for server, when in sorted(checks.items())
    )
    return clean + _runs_table()


def _runs_table():
    rows = _recent_runs()
    if not rows:
        return '<p class="hint">No runs yet.</p>'
    out = []
    for ran_at, server_name, mode, status, message, summary_json in rows:
        detail = ""
        if summary_json:
            summary = json.loads(summary_json)
            for pl in summary.get("playlists", []):
                tracks = "".join(
                    f"<li>{html.escape(t['artist'])} - {html.escape(t['title'])} "
                    f"<span class=\"count\">[{html.escape(t['reason'])}]</span></li>"
                    for t in pl.get("tracks", [])
                )
                more = pl["removed"] - len(pl.get("tracks", []))
                if more > 0:
                    tracks += f'<li class="count">... and {more} more</li>'
                label = "playlist deleted" if pl.get("deleted") else f"{pl['removed']} of {pl['total']}"
                detail += (f"<details><summary>{html.escape(pl['name'])} "
                           f"<span class=\"count\">({label})</span></summary><ul>{tracks}</ul></details>")
        badge = "Preview" if mode == "preview" else "Applied"
        if status != "ok":
            badge = "Error"
        out.append(
            f"<tr><td>{html.escape(ran_at or '')}</td><td>{badge}</td>"
            f"<td>{html.escape(message or '')}{detail}</td></tr>"
        )
    return ('<div class="runs"><table><tr><th>When</th><th>Mode</th><th>Result</th></tr>'
            + "".join(out) + "</table></div>")


@bp.route("/")
def home():
    return redirect(url_for(f"{PLUGIN_ID}.settings"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        extra = split_patterns(request.form.get("extra_genres", ""))
        set_setting("genres", clean_list(request.form.getlist("genre") + extra))
        set_setting("patterns", split_patterns(request.form.get("patterns", "")))
        set_setting("suffix", (request.form.get("suffix") or DEFAULTS["suffix"]).strip())
        set_setting("mode", "apply" if request.form.get("mode") == "apply" else "preview")
        try:
            set_setting("min_tracks", max(0, int(request.form.get("min_tracks") or 0)))
        except ValueError:
            set_setting("min_tracks", 0)
        msg = "Settings saved."
        if request.form.get("action") == "run":
            try:
                enqueue(filter_playlists, server_scope="all")
                msg = "Settings saved. Filter queued; refresh in a moment to see the result below."
            except Exception as exc:
                logger.info("%s: run now refused: %s", PLUGIN_ID, exc)
                msg = ("Settings saved, but the filter did not start because another job "
                       "(such as clustering or analysis) is running. Try again when it finishes.")
        return redirect(url_for(f"{PLUGIN_ID}.settings", msg=msg))

    cfg = _settings()
    msg = request.args.get("msg")
    notice = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    if not _navidrome_servers():
        notice += ('<div class="msg err">No Navidrome server is configured. This plugin '
                   'currently works with Navidrome only.</div>')

    preview_checked = "" if cfg["mode"] == "apply" else " checked"
    apply_checked = " checked" if cfg["mode"] == "apply" else ""
    body = (
        _STYLE + '<div class="pgf">' + notice +
        '<p class="hint">Removes tracks of the genres you pick from the playlists clustering '
        'creates. Clustering still uses every song; this cleans the playlists afterwards. '
        'Schedule it under Administration &gt; Scheduled Tasks as '
        f'<code>{TASK_TYPE}</code>; a run while clustering is busy waits its turn.</p>'
        '<form method="post">'

        '<fieldset><legend>Genres to exclude</legend>'
        '<p class="hint">Genre tags from your Navidrome server, with song counts.</p>'
        + _genre_checklist(cfg["genres"]) +
        '<p class="hint" style="margin-top:.8rem">Other genre names (comma-separated), '
        'for tags not listed above:</p>'
        '<input type="text" name="extra_genres" value="">'
        '</fieldset>'

        '<fieldset><legend>Album and path patterns</legend>'
        '<p class="hint">Words matched anywhere in album titles and file paths, ignoring case. '
        'Catches holiday albums whose tracks are tagged Pop or Rock. Comma-separated.</p>'
        f'<textarea name="patterns" rows="2" placeholder="christmas, xmas">'
        f'{html.escape(", ".join(cfg["patterns"]))}</textarea>'
        '</fieldset>'

        '<fieldset><legend>Mode</legend>'
        f'<label class="choice"><input type="radio" name="mode" value="preview"{preview_checked}> '
        'Preview: only list what would be removed</label>'
        f'<label class="choice"><input type="radio" name="mode" value="apply"{apply_checked}> '
        'Apply: remove the tracks from the playlists</label>'
        '<p class="hint">Start with Preview and check the results below before switching to Apply.</p>'
        '</fieldset>'

        '<fieldset><legend>Advanced</legend><div class="row">'
        '<div><label>Playlist name suffix<br>'
        f'<input type="text" name="suffix" value="{html.escape(cfg["suffix"])}"></label>'
        '<p class="hint">Only playlists ending in this (or this plus " (2)") are touched.</p></div>'
        '<div><label>Delete playlists left with fewer than<br>'
        f'<input type="number" min="0" name="min_tracks" value="{cfg["min_tracks"]}"></label>'
        '<p class="hint">Tracks after filtering. 0 keeps every playlist.</p></div>'
        '</div></fieldset>'

        '<div class="actions">'
        '<button type="submit" name="action" value="save" class="btn btn-primary">Save</button>'
        '<button type="submit" name="action" value="run" class="btn btn-primary">Save and run now</button>'
        '</div></form>'

        '<h3 style="margin-top:2rem">Recent runs</h3>' + _runs_html() +
        '</div>' + _SCRIPT
    )
    return render_page(body, title="Playlist Genre Filter")


def register(ctx):
    ctx.on_install(migrate)
    ctx.add_blueprint(bp)
    ctx.add_menu_item("Playlist Genre Filter", f"{PLUGIN_ID}.home", admin_only=True)
    ctx.add_cron_task(TASK_NAME, filter_playlists)
