"""Account pages: sign in with the Subsonic user, link YouTube Music, Last.fm and ListenBrainz, manage users. Music
pages: find any artist or album on MusicBrainz, follow or want it, and see what oasis is looking for."""
import json
from collections import Counter
from pathlib import Path
import re
import time
import urllib.request
from html import escape
from urllib.parse import parse_qsl, quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import accounts, acquire, importer, musicbrainz, releases
from .acquire import MB_IDS, TYPES
from .artists import linked
from .forward import LastFm

COOKIE = "oasis_session"

LASTFM_APP_STEPS = """<p>First connect oasis itself to Last.fm (once, for every user):</p><ol>
<li>Signed in to Last.fm, open <a href="https://www.last.fm/api/account/create" target="_blank">last.fm/api/account/create</a>.</li>
<li>Fill in any application name, for example <i>oasis</i>; leave the callback URL empty.</li>
<li>Copy the <b>API key</b> and <b>Shared secret</b> it shows here.</li></ol>
<form method="post" action="/account/lastfm/app"><label>API key<input type="text" name="key"></label>
<label>Shared secret<input type="password" name="secret"></label><button>Connect</button></form>"""

STYLE = """
body { font: 16px/1.5 system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; color: #1d1d1f; background: #fafafa; }
@media (prefers-color-scheme: dark) { body { color: #e8e8e8; background: #161616; } section { background: #202020 !important; } }
h1 { font-size: 1.6rem; } h2 { font-size: 1.15rem; margin: 0 0 .5rem; }
section { background: #fff; border-radius: 12px; padding: 1rem 1.25rem; margin: 1rem 0; box-shadow: 0 1px 3px #0002; }
input, textarea, button { font: inherit; } textarea { width: 100%; min-height: 6rem; box-sizing: border-box; }
input[type=text], input[type=password] { width: 100%; box-sizing: border-box; margin: .2rem 0 .6rem; padding: .4rem; }
button { padding: .4rem 1rem; border-radius: 8px; border: 1px solid #888; cursor: pointer; }
.ok { color: #1a7f37; } .error { color: #c62828; } .muted { opacity: .7; font-size: .9rem; }
ol { padding-left: 1.2rem; } form.inline { display: inline; } td { padding: .2rem .6rem .2rem 0; vertical-align: top; }
li { margin: .25rem 0; } form.inline button { padding: .1rem .6rem; font-size: .85rem; }
body.wide { max-width: 72rem; } a { color: #2f6fd0; } @media (prefers-color-scheme: dark) { a { color: #7fb0ff; } }
table.grid { width: 100%; border-collapse: collapse; font-size: .93rem; }
table.grid th { text-align: left; font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; opacity: .65; padding: .35rem .5rem;
                border-bottom: 1px solid #8885; font-weight: 600; }
table.grid td { padding: .4rem .5rem; border-bottom: 1px solid #8882; vertical-align: middle; }
table.grid tr:hover td { background: #8881; } table.grid tr.dim td { opacity: .55; }
.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; } .actions { text-align: right; white-space: nowrap; }
.actions form { display: inline; } .actions form + form { margin-left: 1.5rem; } .actions button, .bulk button { padding: .15rem .6rem; font-size: .8rem; }
.pill { display: inline-block; padding: 0 .55rem; border-radius: 999px; font-size: .78rem; font-weight: 500; background: #8882; white-space: nowrap; }
.pill.bad { background: #c628282e; } .pill.good { background: #1a7f372e; } .pill.busy { background: #1565c02e; }
details > summary { cursor: pointer; list-style: none; } details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▸"; display: inline-block; width: 1em; transition: transform .15s; opacity: .6; }
details[open] > summary::before { transform: rotate(90deg); }
details.group { margin: 1.75rem 0; border: 1px solid #8883; border-radius: 10px; overflow: hidden; }
details.group > summary { display: flex; gap: .5rem; align-items: center; font-weight: 600; padding: .55rem .75rem; background: #8881; }
details.group[open] > summary { border-bottom: 1px solid #8883; } details.group table.grid tr:last-child td { border-bottom: 0; }
details.group table.grid :is(td, th):first-child { padding-left: .75rem; } details.group table.grid :is(td, th):last-child { padding-right: .75rem; }
details.group > .bulk { display: flex; justify-content: flex-end; padding: 1rem .75rem; border-top: 1px solid #8883; background: #8881; }
details.group > .bulk button { background: none; border: 1px solid currentColor; padding: .25rem .8rem; font-size: .85rem; color: #c62828; }
@media (prefers-color-scheme: dark) { details.group > .bulk button { color: #ef8a8a; } }
details.artist { border-bottom: 1px solid #8883; padding: .35rem 0; } details.artist > summary, details.album > summary {
  display: flex; flex-wrap: wrap; gap: .25rem .75rem; align-items: baseline; }
details.artist > summary b { font-size: 1.05rem; } details.album { margin: .2rem 0 .2rem 1.2rem; }
.hero { display: flex; gap: 1.25rem; align-items: flex-start; } .hero > div { flex: 1; min-width: 0; }
.cover { width: 11rem; height: 11rem; object-fit: cover; border-radius: 8px; background: #8882; flex: none; }
.cover.small { width: 2.6rem; height: 2.6rem; border-radius: 4px; } td.thumb { width: 2.6rem; padding-right: 0; }
dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: .2rem 1rem; margin: 0; } dl.meta dt { opacity: .6; }
dl.meta dd { margin: 0; overflow-wrap: anywhere; } code { font-size: .85em; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); gap: 1rem; }
.card { display: flex; flex-direction: column; gap: .15rem; color: inherit; text-decoration: none; min-width: 0; }
.card .cover { width: 100%; height: auto; aspect-ratio: 1; } .card b { font-weight: 600; overflow-wrap: anywhere; }
a.card:hover b { text-decoration: underline; } .card.dim { opacity: .55; } .card .pill { align-self: flex-start; }
.dim-row { opacity: .6; display: flex; flex-wrap: wrap; gap: .25rem .75rem; align-items: baseline; padding-left: 1em; }
details.album > table { margin: .3rem 0 .6rem 1em; width: calc(100% - 1em); } .right { margin-left: auto; }
.filters { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; } .filters input[type=text] { width: auto; flex: 1 1 12rem; margin: 0; }
.filters select { font: inherit; padding: .35rem; max-width: 100%; }
@media (max-width: 640px) {
  body { margin: 1rem auto; } section { padding: .8rem .9rem; }
  table.grid thead { display: none; } table.grid, table.grid tbody, table.grid tr { display: block; width: 100%; box-sizing: border-box; }
  table.grid tr { border-bottom: 1px solid #8884; padding: .45rem 0; } table.grid td { display: inline-block; border: 0; padding: .05rem .6rem .05rem 0; }
  table.grid td:empty { display: none; } table.grid td.lead, table.grid td.actions, table.grid td.block { display: block; width: 100%; box-sizing: border-box; }
  table.grid td.lead { font-weight: 600; } table.grid td[data-unit]::after { content: " " attr(data-unit); opacity: .6; }
  table.tracks tr { display: grid; grid-template-columns: 2rem 1fr auto; column-gap: .4rem; }
  table.tracks td:nth-child(1) { grid-column: 1; grid-row: 1; } table.tracks td.lead { grid-column: 2; grid-row: 1; }
  table.tracks td:nth-child(4) { grid-column: 3; grid-row: 1; } table.tracks td:nth-child(3) { grid-column: 2; grid-row: 2; }
  table.tracks td:nth-child(5) { grid-column: 3; grid-row: 2; } table.tracks td { padding-right: 0; }
  details.group table.grid tr { padding: .7rem .75rem; } details.group table.grid td:first-child { padding-left: 0; } details.group table.grid tr:last-child { border-bottom: 0; }
  .num, .actions { text-align: left; } .actions { padding-top: .5rem !important; } td.actions { display: flex !important; justify-content: space-between; }
  details.album { margin-left: .4rem; }
  details.album > table { margin-left: 0; width: 100%; }
  .hero { flex-direction: column; } .hero > .cover { width: 100%; height: auto; aspect-ratio: 1; max-width: 22rem; }
  .cards { grid-template-columns: repeat(2, 1fr); gap: .75rem; } td.thumb { display: none !important; }
}
"""


STATES = {"added": "waiting for TorBox", "downloading": "TorBox downloading", "fetching": "copying here", "importing": "importing"}
NO_COVER = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#8883"/>'
            '<text x="50" y="64" font-size="42" text-anchor="middle" fill="#8889">♪</text></svg>')
confirm = lambda question: f' onsubmit="return confirm({escape(json.dumps(question, ensure_ascii=False))})"'  # for buttons that drop things
HIDE_BROKEN = 'onerror="this.removeAttribute(\'src\')"'  # a cover that fails shows the empty frame, not a broken image
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MUSIC_NAV = ('<p><a href="/account">Your account</a> · <a href="/music/library">Library</a> · <a href="/music">Find music</a> · '
             '<a href="/music/wanted">Wanted</a></p>')


def length(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02}"


def size(n) -> str:
    return f"{(n or 0) / 1e9:.1f} GB" if (n or 0) >= 1e9 else f"{(n or 0) / 1e6:.0f} MB"


def ago(ts: float) -> str:
    minutes = int((time.time() - ts) // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


async def read_form(request: Request) -> dict:
    return dict(parse_qsl((await request.body()).decode()))


def page(title: str, body: str, message: str = "", error: str = "", wide: bool = False) -> HTMLResponse:
    notice = f'<p class="ok">{escape(message)}</p>' if message else ""
    notice += f'<p class="error">{escape(error)}</p>' if error else ""
    return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        f'<title>{escape(title)} · oasis</title><style>{STYLE}</style></head><body{' class="wide"' if wide else ""}><h1>{escape(title)}</h1>'
                        f'{notice}{body}</body></html>')


def back(path: str, message: str = "", error: str = "") -> RedirectResponse:
    query = f"message={quote(message)}" if message else f"error={quote(error)}" if error else ""
    return RedirectResponse(path + ("&" if "?" in path else "?") + query if query else path, status_code=303)


def details(*parts) -> str:
    return '<span class="muted">' + escape(" · ".join(str(p) for p in parts if p)) + '</span>'


def about(a: dict) -> str:
    """A MusicBrainz artist's type, country, years and disambiguation."""
    life = a.get("life-span") or {}
    return details(a.get("type"), a.get("country"), "–".join(filter(None, [(life.get(k) or "")[:4] for k in ("begin", "end")])), a.get("disambiguation"))


def credited(g: dict) -> str:
    return "".join(c["name"] + (c.get("joinphrase") or "") for c in g.get("artist-credit") or [])


def routes(api) -> list[Route]:
    def with_db(fn):
        def run(*args):
            db = api._db()
            try:
                return fn(db, *args)
            finally:
                db.close()
        return run

    def signed_in(handler, admin: bool = False):
        """Runs handler(db, request, user, form) for a signed-in user, else sends them to sign in."""
        async def endpoint(request: Request):
            form = await read_form(request) if request.method == "POST" else {}

            def run(db):
                user = accounts.session_user(db, request.cookies.get(COOKIE))
                if user is None:
                    return RedirectResponse("/login", status_code=303)
                if admin and not user["admin"]:
                    return back("/account", error="Only admins can manage users.")
                return handler(db, request, user, form)
            return await run_in_threadpool(with_db(run))
        return endpoint

    async def login(request: Request):
        if request.method == "GET":
            return page("Sign in", '<section><form method="post"><label>User<input type="text" name="name" autofocus></label>'
                        '<label>Password<input type="password" name="password"></label><button>Sign in</button></form>'
                        '<p class="muted">The same user and password as in your Subsonic apps.</p></section>',
                        error=request.query_params.get("error", ""))
        form = await read_form(request)

        def run(db):
            user = accounts.get(db, form.get("name", ""))
            if user is None or user["password"] != form.get("password"):
                return back("/login", error="Wrong user or password.")
            response = RedirectResponse("/account", status_code=303)
            response.set_cookie(COOKIE, accounts.new_session(db, user["name"]), max_age=accounts.SESSION_DAYS * 86400,
                                httponly=True, samesite="lax")
            return response
        return await run_in_threadpool(with_db(run))

    def logout(db, request, user, form):
        db.execute("delete from session where token = ?", (request.cookies.get(COOKIE),))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    def account(db, request, user, form):
        q = request.query_params
        youtube = (f'<p class="ok">Linked as <b>{escape(user["ytm_name"] or "")}</b>. Your mixes show up as playlists and your plays '
                   f'go to its history.</p><form method="post" action="/account/youtube/unlink"><button>Unlink</button></form>'
                   if user["ytm_auth"] else YOUTUBE_STEPS)
        if not api.ytm:
            youtube = '<p class="muted">YouTube Music is off on this server (<code>[ytmusic] enabled</code>).</p>'
        lastfm = ((LASTFM_APP_STEPS if user["admin"] else '<p class="muted">An admin needs to connect oasis to Last.fm first.</p>')
                  if not (api.config.lastfm_key and api.config.lastfm_secret) else
                  f'<p class="ok">Linked as <b>{escape(user["lastfm_name"] or "")}</b>.</p><form method="post" '
                  f'action="/account/lastfm/unlink"><button>Unlink</button></form>' if user["lastfm_session"] else
                  '<p><a href="/account/lastfm"><button type="button">Sign in with Last.fm</button></a></p>')
        listenbrainz = (f'<p class="ok">Linked.</p><form method="post" action="/account/listenbrainz"><input type="hidden" name="token">'
                        f'<button>Unlink</button></form>' if user["listenbrainz_token"] else
                        '<form method="post" action="/account/listenbrainz"><label>User token from '
                        '<a href="https://listenbrainz.org/settings/" target="_blank">listenbrainz.org/settings</a>'
                        '<input type="text" name="token"></label><button>Link</button></form>')
        admin = '<p><a href="/admin">Manage users</a> · <a href="/admin/artists">Artist settings</a></p>' if user["admin"] else ""
        admin = '<p><a href="/music">Find music</a> · <a href="/music/wanted">Wanted</a></p>' + admin
        body = (f'<div>Signed in as <b>{escape(user["name"])}</b>. <form class="inline" method="post" action="/logout">'
                f'<button>Sign out</button></form></div>{admin}{status(db)}'
                f'<section><h2>YouTube Music</h2>{youtube}</section>'
                f'<section><h2>Last.fm</h2><p class="muted">Your plays are scrobbled here. Turn off scrobbling in your apps, '
                f'or plays count twice.</p>{lastfm}</section>'
                f'<section><h2>ListenBrainz</h2>{listenbrainz}</section>'
                f'<section><h2>Password</h2>' + (
                    '<p class="muted">Set in oasis.toml.</p>' if user["name"] in api.config.users else
                    '<form method="post" action="/account/password"><label>New password<input type="password" name="password">'
                    '</label><button>Change</button></form>') + '</section>')
        return page("Your oasis account", body, q.get("message", ""), q.get("error", ""))

    def link_youtube(db, request, user, form):
        try:
            name = api._link_youtube(db, user["name"], accounts.youtube_headers(form.get("headers", "")))
        except ValueError as e:
            return back("/account", error=str(e))
        except Exception as e:  # Fail from the API
            return back("/account", error=getattr(e, "message", str(e)))
        return back("/account", message=f"YouTube Music linked as {name}. Your mixes are loading.")

    def unlink_youtube(db, request, user, form):
        accounts.set_field(db, user["name"], ytm_auth=None, ytm_name=None)
        api._drop_mixes(db, user["name"])
        return back("/account", message="YouTube Music unlinked.")

    def lastfm_service():
        return LastFm(api.config.lastfm_key, api.config.lastfm_secret, None)

    def lastfm_start(db, request, user, form):
        return RedirectResponse(lastfm_service().auth_url(str(request.url_for("lastfm_callback"))), status_code=303)

    def lastfm_callback(db, request, user, form):
        try:
            key, name = lastfm_service().sign_in(request.query_params.get("token", ""))
        except Exception as e:
            return back("/account", error=f"Last.fm sign-in failed: {e}")
        accounts.set_field(db, user["name"], lastfm_session=key, lastfm_name=name)
        return back("/account", message=f"Last.fm linked as {name}.")

    def lastfm_app(db, request, user, form):
        key, secret = form.get("key", "").strip(), form.get("secret", "").strip()
        try:
            api._set_lastfm_app(db, key, secret)
        except Exception as e:
            return back("/account", error=f"Last.fm didn't accept these: {e}")
        return back("/account", message="oasis is connected to Last.fm. Now sign in with your Last.fm account.")

    def unlink_lastfm(db, request, user, form):
        accounts.set_field(db, user["name"], lastfm_session=None, lastfm_name=None)
        return back("/account", message="Last.fm unlinked.")

    def link_listenbrainz(db, request, user, form):
        token = form.get("token", "").strip()
        if token:
            request_ = urllib.request.Request("https://api.listenbrainz.org/1/validate-token", headers={"Authorization": "Token " + token})
            try:
                with urllib.request.urlopen(request_, timeout=15) as response:
                    result = json.load(response)
            except Exception as e:
                return back("/account", error=f"ListenBrainz didn't answer: {e}")
            if not result.get("valid"):
                return back("/account", error="ListenBrainz says that token isn't valid.")
        accounts.set_field(db, user["name"], listenbrainz_token=token or None)
        return back("/account", message="ListenBrainz linked." if token else "ListenBrainz unlinked.")

    def change_password(db, request, user, form):
        if user["name"] in api.config.users:
            return back("/account", error="This password is set in oasis.toml.")
        if not form.get("password"):
            return back("/account", error="A password is needed.")
        accounts.set_field(db, user["name"], password=form["password"])
        return back("/account", message="Password changed. Update it in your apps.")

    def status(db) -> str:
        """What oasis is busy with: downloads, the MusicBrainz fill and dedupe."""
        count = lambda sql: db.execute(sql).fetchone()[0]
        songs, albums, artists = db.execute("select count(*), count(distinct album_id), count(distinct artist_id) from track "
                                            "where source = 'local'").fetchone()
        lines = [f"{songs} songs, {albums} albums, {artists} artists in the library."]
        active = db.execute("select title, state, created from download where state not in ('done', 'failed') order by created").fetchall()
        lines.append(f'Downloading {len(active)}:<ul>' + "".join(
            f'<li>{escape(r["title"])} <span class="muted">{STATES.get(r["state"], r["state"])}, {ago(r["created"])}</span></li>'
            for r in active) + '</ul>' if active else "Nothing downloading.")
        recent = db.execute("select title, state, error, updated from download where state in ('done', 'failed') "
                            "order by updated desc limit 5").fetchall()
        if recent:
            lines.append('Lately:<ul>' + "".join(
                f'<li>{"✓" if r["state"] == "done" else "✗"} {escape(r["title"])} <span class="muted">{ago(r["updated"])}'
                f'{": " + escape(r["error"]) if r["error"] and r["state"] == "failed" else ""}</span></li>' for r in recent) + '</ul>')
        if api.library:
            lines.append(f'{api.library.still_to_find(db)} more albums still to find.')
        filled = count("select count(*) from track where source = 'local' and credits_at is not null")
        known = count("select count(*) from track where source = 'local' and mb_release is not null")
        artists_done = count("select count(*) from mb_artist where fetched is not null and mbid in (select mbid from credit)")
        artists_all = count("select count(*) from mb_artist where mbid in (select mbid from credit)")
        lines.append(f'MusicBrainz: credits for {filled} of {known} songs, names and links for {artists_done} of {artists_all} artists.')
        shared = db.execute("select count(*), coalesce(sum(t.size), 0) from audio_shared a join track t on t.ref = a.path").fetchone()
        if api.config.dedupe:
            lines.append(f'Dedupe: {shared[0]} copies share another file\'s audio, saving {shared[1] / 1e9:.1f} GB.')
        return '<section><h2>Status</h2>' + "".join(f'<p>{line}</p>' for line in lines) + '</section>'

    def artist_settings(db, request, user, form):
        q, settings = request.query_params, api._artist_settings(db)
        lines = lambda key: escape("\n".join("; ".join(group) for group in settings.get(key, [])))
        body = ('<p><a href="/account">Back to your account</a></p><form method="post" action="/admin/artists">'
                '<section><h2>Related artists</h2><p class="muted">One group per line, names separated by <code>;</code>. '
                'Separate artists that belong together: they come first among each other\'s similar artists and mix into each '
                f'other\'s radios. Clients only ever see them as related.</p><textarea name="related">{lines("related")}</textarea></section>'
                '<section><h2>Same artist</h2><p class="muted">One artist per line: the name to show, then its other names, '
                'separated by <code>;</code>. For names MusicBrainz doesn\'t know belong together.</p>'
                f'<textarea name="merge">{lines("merge")}</textarea></section>'
                '<section><h2>Keep separate</h2><p class="muted">One name per line. MusicBrainz lists it as another name of '
                'some artist, but here it\'s someone else.</p>'
                f'<textarea name="separate">{escape(chr(10).join(settings.get("separate", [])))}</textarea></section>'
                '<button>Save</button></form>')
        return page("Artist settings", body, q.get("message", ""), q.get("error", ""))

    def save_artist_settings(db, request, user, form):
        groups = lambda key: [names for line in form.get(key, "").splitlines() if len(names := [n.strip() for n in line.split(";") if n.strip()]) > 1]
        settings = {"related": groups("related"), "merge": groups("merge"),
                    "separate": [n.strip() for n in form.get("separate", "").splitlines() if n.strip()]}
        api._set_artist_settings(db, settings)
        return back("/admin/artists", message="Saved. Artist pages, similar artists and radios use it now.")

    def admin(db, request, user, form):
        q = request.query_params
        rows = "".join(
            f'<tr><td><b>{escape(r["name"])}</b>{" (admin)" if r["admin"] else ""}</td>'
            f'<td class="muted">{"YouTube Music " if r["ytm_auth"] else ""}{"Last.fm " if r["lastfm_session"] else ""}'
            f'{"ListenBrainz" if r["listenbrainz_token"] else ""}</td><td>' + (
                '<span class="muted">from oasis.toml</span>' if r["name"] in api.config.users else
                f'<form class="inline" method="post" action="/admin/users/{quote(r["name"])}/delete"><button>Delete</button></form>') +
            '</td></tr>' for r in db.execute("select * from user order by name"))
        body = (f'<p><a href="/account">Back to your account</a></p><section><h2>Users</h2><table>{rows}</table></section>'
                '<section><h2>New user</h2><form method="post" action="/admin/users"><label>User<input type="text" name="name">'
                '</label><label>Password<input type="password" name="password"></label><label><input type="checkbox" '
                'name="admin"> Admin</label><p><button>Create</button></p></form></section>')
        return page("Users", body, q.get("message", ""), q.get("error", ""))

    def create_user(db, request, user, form):
        try:
            accounts.create(db, form.get("name", "").strip(), form.get("password", ""), bool(form.get("admin")))
        except ValueError as e:
            return back("/admin", error=str(e))
        return back("/admin", message=f"Created {form['name'].strip()}. They can sign in here to link their services.")

    def delete_user(db, request, user, form):
        name = request.path_params["name"]
        if name in api.config.users:
            return back("/admin", error="Users from oasis.toml can only be removed there.")
        accounts.delete(db, name)
        return back("/admin", message=f"Deleted {name}.")

    def catalogue(handler):
        """A music page, for any signed-in user: it needs oasis's own catalogue (acquire.py)."""
        def run(db, request, user, form):
            if not api.library:
                return page("Find music", MUSIC_NAV, error="Acquisition is off: it needs [torbox] with downloads and [prowlarr] in oasis.toml.")
            return handler(db, request, user, form)
        return signed_in(run)

    def mine(db, user, kind: str, mbid: str) -> list[str]:
        """The ids the user wants the artist or album under."""
        ids = acquire.refs(db, kind, mbid)
        return [r[0] for r in db.execute(f"select ref from wanted where user = ? and kind = ? and ref in ({','.join('?' * len(ids))})",
                                         (user["name"], kind, *ids))]

    def want_button(kind: str, mbid: str, on: bool, here: str) -> str:
        label = {("album", True): "Want", ("album", False): "Unwant", ("artist", True): "Follow", ("artist", False): "Unfollow"}[kind, on]
        return (f'<form class="inline" method="post" action="/music/want"><input type="hidden" name="kind" value="{kind}">'
                f'<input type="hidden" name="mbid" value="{escape(mbid)}"><input type="hidden" name="on" value="{"1" if on else ""}">'
                f'<input type="hidden" name="next" value="{escape(here)}"><button>{label}</button></form>')

    def followed(db, mbid: str) -> bool:
        ids = acquire.refs(db, "artist", mbid)
        return bool(db.execute(f"select 1 from wanted where kind = 'artist' and ref in ({','.join('?' * len(ids))})", list(ids)).fetchone())

    def artist_state(db, mbid: str) -> str:
        return " ".join(filter(None, ["in the library" if acquire.library_id(db, MB_IDS["artist"] + mbid) else "",
                                      "followed" if followed(db, mbid) else ""]))

    def album_line(db, user, g: dict, here: str, by: bool = True) -> str:
        row = db.execute("select state, error from want_album where rg = ?", (g["id"],)).fetchone()
        state = "in the library" if acquire.library_id(db, MB_IDS["album"] + g["id"]) or row and row["state"] == "have" else \
            {"missing": "wanted", "downloading": "downloading", "skipped": f"left out: {row['error']}"}[row["state"]] if row else ""
        credit = g.get("artist-credit") or []
        artist = f' — <a href="/music/artist/{credit[0]["artist"]["id"]}">{escape(credited(g))}</a>' if by and credit else ""
        info = details(g.get("primary-type"), *(g.get("secondary-types") or []), (g.get("first-release-date") or "")[:4], g.get("disambiguation"))
        return (f'<li>{escape(g["title"])}{artist} {info} <b>{escape(state)}</b> '
                f'{want_button("album", g["id"], not mine(db, user, "album", g["id"]), here)}</li>')

    def find_music(db, request, user, form):
        q = request.query_params
        text, kind, error = q.get("q", "").strip(), "album" if q.get("kind") == "album" else "artist", q.get("error", "")
        checked = lambda k: " checked" if k == kind else ""
        body = (f'{MUSIC_NAV}<section><form><input type="text" name="q" value="{escape(text)}" placeholder="An artist or album, in any '
                f'script" autofocus><label><input type="radio" name="kind" value="artist"{checked("artist")}> Artists</label> '
                f'<label><input type="radio" name="kind" value="album"{checked("album")}> Albums</label> <button>Search MusicBrainz</button>'
                f'</form></section>')
        if text:
            try:
                found = acquire.find(db, kind, text)
            except OSError as e:
                found, error = [], f"MusicBrainz didn't answer: {e}"
            here = f"/music?q={quote(text)}&kind={kind}"
            lines = [album_line(db, user, g, here) for g in found] if kind == "album" else [
                f'<li><a href="/music/artist/{a["id"]}">{escape(a["name"])}</a> {about(a)} <b>{artist_state(db, a["id"])}</b></li>' for a in found]
            body += f'<section><ul>{"".join(lines)}</ul></section>' if lines else '<p class="muted">MusicBrainz knows nothing by that name.</p>'
        return page("Find music", body, q.get("message", ""), error)

    def music_artist(db, request, user, form):
        q, mbid = request.query_params, request.path_params["mbid"]
        try:
            if not MBID.fullmatch(mbid):
                raise LookupError(mbid)
            info, groups = musicbrainz.get(releases.ALIASES.format(mbid), db, releases.KEEP_ALIASES), acquire.release_groups(db, mbid)
        except LookupError:
            return back("/music", error="MusicBrainz has no such artist.")
        except OSError as e:
            return back("/music", error=f"MusicBrainz didn't answer: {e}")
        here = f"/music/artist/{mbid}"
        body = (f'{MUSIC_NAV}<section><p>{about(info)}</p><p><b>{artist_state(db, mbid)}</b> '
                f'{want_button("artist", mbid, not mine(db, user, "artist", mbid), here)}</p><p class="muted">Following wants its albums, EPs and '
                f'singles, new ones too, except repackages, singles whose songs are on its albums and what has no official release.</p></section>')
        for kind in TYPES:
            rows = sorted((g for g in groups if g.get("primary-type") == kind), key=lambda g: g.get("first-release-date") or "9999")
            if rows:
                body += f'<section><h2>{kind}s</h2><ul>{"".join(album_line(db, user, g, here, by=False) for g in rows)}</ul></section>'
        return page(info["name"], body, q.get("message", ""), q.get("error", ""))

    def set_want(db, request, user, form):
        kind, mbid, here = form.get("kind"), form.get("mbid", ""), form.get("next", "")
        here = here if here.startswith("/music") else "/music"
        if kind not in MB_IDS or not MBID.fullmatch(mbid):
            return back(here, error="Nothing to want.")
        if form.get("on"):  # under the library's id once it has it, as clients see it
            api._want(db, user["name"], kind, [acquire.library_id(db, MB_IDS[kind] + mbid) or MB_IDS[kind] + mbid], time.time())
            return back(here, message="Followed: its albums are searched for." if kind == "artist" else "Wanted: it is searched for.")
        api._want(db, user["name"], kind, mine(db, user, kind, mbid), None)
        return back(here, message="Unfollowed. Files already here stay." if kind == "artist" else "Unwanted. Files already here stay.")

    def wanted(db, request, user, form):
        q, acq = request.query_params, api.library
        artist_f, type_f, text = q.get("artist", ""), q.get("type", ""), q.get("q", "").strip()
        here = "/music/wanted" + ("?" + urlencode({k: v for k, v in (("artist", artist_f), ("type", type_f), ("q", text)) if v})
                                  if artist_f or type_f or text else "")
        active = {r["rg"]: r for r in db.execute("select d.hash, d.state, coalesce(t.rg, d.album_mbid) rg from download d left join download_target t "
                                                 "on t.hash = d.hash where d.state not in ('done', 'failed') and coalesce(t.rg, d.album_mbid) is not null")}

        def place(r) -> tuple[str, str]:
            """The group a wanted album goes in, and what to say about it."""
            if d := active.get(r["rg"]):
                seen = api.downloads.seen.get(d["hash"]) if api.downloads else None
                return "downloading", (f'<span class="pill busy">{seen[0]:.0%}</span>' if d["state"] == "downloading" and seen
                                       else f'<span class="pill busy">{STATES.get(d["state"], d["state"])}</span>')
            if r["state"] == "downloading":
                return "downloading", '<span class="pill busy">downloading</span>'
            if (r["error"] or "").startswith(("download failed", "search failed")):
                return "attention", f'<span class="error">{escape(r["error"])}</span>'
            if r["rg"] in acq.soon:
                return "next", '<span class="pill">in a minute or two</span>'
            return ("notfound", f'searched {ago(r["searched"])}') if r["searched"] else ("waiting", "")

        def action(rg: str, name: str, label: str, ask: str = "") -> str:
            return (f'<form method="post" action="/music/wanted/{escape(rg)}/{name}"{ask and confirm(ask)}><input type="hidden" name="next" value="{escape(here)}">'
                    f'<button>{label}</button></form>')

        where, params = ["state in ('missing', 'downloading')"], []
        if artist_f:
            where.append("artist_mbid = ?"), params.append(artist_f)
        if type_f:
            where.append("primary_type = ?"), params.append(type_f)
        if text:
            where.append("(title like ? or artist like ?)"), params.extend([f"%{text}%"] * 2)
        rows = db.execute(f"select * from want_album where {' and '.join(where)} order by reason != 'starred', artist collate nocase, "
                          "primary_type != 'Album', primary_type != 'EP', first_date", params).fetchall()
        groups: dict[str, list] = {}
        for r in rows:
            name, note = place(r)
            groups.setdefault(name, []).append((r, note))
        artists = db.execute("select artist_mbid, artist, count(*) n from want_album where state in ('missing', 'downloading') "
                             "group by artist_mbid order by artist collate nocase").fetchall()
        option = lambda value, label, chosen: f'<option value="{escape(value)}"{" selected" if value == chosen else ""}>{escape(label)}</option>'
        counts = dict(db.execute("select state, count(*) from want_album group by state").fetchall())
        body = (f'{MUSIC_NAV}<section><p>{counts.get("missing", 0)} albums to find, {counts.get("downloading", 0)} downloading · '
                f'{counts.get("have", 0)} wanted ones are here · {counts.get("skipped", 0)} of followed artists left out.</p>'
                f'<form class="filters"><select name="artist">{option("", "All artists", artist_f)}'
                + "".join(option(a["artist_mbid"], f'{a["artist"]} ({a["n"]})', artist_f) for a in artists)
                + f'</select><select name="type">{option("", "Any type", type_f)}'
                + "".join(option(t, t, type_f) for t in ("Album", "EP", "Single"))
                + f'</select><input type="text" name="q" value="{escape(text)}" placeholder="Title or artist"><button>Filter</button>'
                + (' <a href="/music/wanted">Clear</a>' if here != "/music/wanted" else "") + '</form>'
                + '<p class="muted">Giving up stops the search; files already here stay, and wanting it again brings it back.</p>')
        titles = [("attention", "Needs a look"), ("downloading", "Downloading"), ("next", "Searched next"),
                  ("notfound", "Not found yet"), ("waiting", "Waiting for a first search")]
        for key, title in titles:
            items = groups.get(key, [])
            if not items:
                continue
            bulk = (f'<form class="bulk" method="post" action="/music/wanted/give-up" '
                    f'{confirm(f"Give up on all {len(items)} in {title}?")}><input type="hidden" name="rgs" '
                    f'value="{",".join(r["rg"] for r, _ in items)}"><input type="hidden" name="next" value="{escape(here)}">'
                    f'<button>Give up on {"all " + str(len(items)) if len(items) > 1 else "it"}…</button></form>')
            table = "".join(
                f'<tr><td class="lead"><a href="/music/artist/{escape(r["artist_mbid"])}">{escape(r["artist"] or "")}</a> – '
                f'{escape(r["title"])}</td><td>{escape(r["primary_type"] or "")}'
                f'{" · " + escape(r["secondary_types"]) if r["secondary_types"] else ""}</td>'
                f'<td class="num">{escape((r["first_date"] or "")[:4])}</td>'
                f'<td>{"★ starred" if r["reason"] == "starred" else "followed"}</td><td>{note}</td>'
                f'<td class="actions">{action(r["rg"], "block", "Try another") if key == "downloading" else action(r["rg"], "search", "Search now")}'
                f'{action(r["rg"], "give-up", "Give up", f"Give up on {r['artist']} – {r['title']}?")}</td></tr>' for r, note in items)
            opened = " open" if key in ("attention", "downloading", "next") or len(items) <= 30 else ""
            body += (f'<details class="group"{opened}><summary>{title} <span class="pill">{len(items)}</span></summary>'
                     f'<table class="grid"><thead><tr><th>Album</th><th>Type</th><th class="num">Year</th><th>Why</th><th>State</th><th></th>'
                     f'</tr></thead><tbody>{table}</tbody></table>{bulk}</details>')
        if not rows:
            body += '<p class="muted">Nothing wanted matches.</p>' if here != "/music/wanted" else '<p class="muted">Nothing left to find.</p>'
        body += '</section>'
        lost = db.execute("select li.*, exists (select 1 from wanted w where w.kind = li.kind and w.ref = li.ref and w.user = ?) mine "
                          "from lidarr_item li where li.state = 'failed' and exists (select 1 from wanted w where w.kind = li.kind "
                          "and w.ref = li.ref)", (user["name"],)).fetchall()
        if lost:
            names = {"album": ("select album_artist || ' – ' || album from track where album_id = ?",
                               "select a.name || ' – ' || r.title from ytm_release r join ytm_artist a using (artist_id) where r.album_id = ?"),
                     "artist": ("select album_artist from track where artist_id = ?", "select name from ytm_artist where artist_id = ?")}
            name = lambda r: next((row[0] for sql in names[r["kind"]] if (row := db.execute(sql, (r["ref"],)).fetchone())), r["ref"])
            body += ('<section><h2>Not found on MusicBrainz</h2><p class="muted">Starred, but oasis can\'t tell which MusicBrainz '
                     'artist or album it is; it asks again later. Find it and want it from there instead.</p><table class="grid"><tbody>' + "".join(
                         f'<tr><td class="lead">{escape(name(r))}</td><td>{escape(r["kind"])}</td><td><span class="error">{escape(r["error"] or "")}</span></td>'
                         f'<td class="actions">' + (
                             f'<form method="post" action="/music/unwant"{confirm(f"Give up on {name(r)}?")}><input type="hidden" name="kind" value="{r["kind"]}">'
                             f'<input type="hidden" name="ref" value="{escape(r["ref"])}"><button>Give up</button></form>' if r["mine"] else "")
                         + '</td></tr>' for r in lost) + '</tbody></table></section>')
        return page("Wanted", body, q.get("message", ""), q.get("error", ""), wide=True)

    def music_library(db, request, user, form):
        """Every artist with music here or wanted, one row each; their pages go deeper."""
        q = request.query_params
        text, show = q.get("q", "").strip(), q.get("show", "")
        artists = library_artists(db)
        rows = ""
        for key, a in sorted(artists.items(), key=lambda kv: (kv[1]["name"] or "").casefold()):
            if text and text.casefold() not in (a["name"] or "").casefold() or show == "here" and not a["albums"] or show == "wanted" and not a["wanted"]:
                continue
            genres = ", ".join(a["genres"][:2])
            rows += (f'<tr><td class="thumb">{picture(a["cover"], small=True)}</td><td class="lead"><a href="/music/library/artist/{quote(key)}">'
                     f'{escape(a["name"] or "")}</a>{" <span class=pill>followed</span>" if a["followed"] else ""}'
                     f'<div class="muted">{escape(genres)}</div></td>'
                     f'<td class="num" data-unit="here">{a["albums"] or ""}</td><td class="num" data-unit="wanted">{a["wanted"] or ""}</td>'
                     f'<td class="num" data-unit="songs">{a["songs"] or ""}</td>'
                     f'<td class="num">{length(a["seconds"]) if a["seconds"] else ""}</td><td class="num">{size(a["bytes"]) if a["bytes"] else ""}</td></tr>')
        options = "".join(f'<option value="{v}"{" selected" if v == show else ""}>{label}</option>'
                          for v, label in (("", "Everything"), ("here", "With music here"), ("wanted", "With wanted albums")))
        body = (f'{MUSIC_NAV}<section><form class="filters"><input type="text" name="q" value="{escape(text)}" placeholder="Artist">'
                f'<select name="show">{options}</select><button>Show</button></form></section><section>'
                + (f'<table class="grid"><thead><tr><th></th><th>Artist</th><th class="num">Albums here</th><th class="num">Wanted</th>'
                   f'<th class="num">Songs</th><th class="num">Length</th><th class="num">Size</th></tr></thead><tbody>{rows}</tbody></table>'
                   if rows else '<p class="muted">Nothing matches.</p>') + '</section>')
        return page("Library", body, q.get("message", ""), q.get("error", ""), wide=True)

    def library_artists(db) -> dict[str, dict]:
        """Artists by their page key (MusicBrainz id when known, else oasis id) with what's here and what's wanted."""
        mbid_of = dict(db.execute("select artist_id, mbid from mb_artist").fetchall())
        followed = {r[0] for r in db.execute("select ref from wanted where kind = 'artist'")}
        artists: dict[str, dict] = {}
        for r in db.execute("select artist_id, min(album_artist) name, count(distinct album_id) albums, count(*) songs, sum(duration) seconds, "
                            "sum(size) bytes, max(album_id) album from track where source = 'local' group by artist_id"):
            key = mbid_of.get(r["artist_id"]) or r["artist_id"]
            artists[key] = {**dict(r), "wanted": 0, "followed": r["artist_id"] in followed, "cover": r["album"], "genres": []}
        for r in db.execute("select artist_mbid, min(artist) name, count(*) n from want_album where state in ('missing', 'downloading') "
                            "group by artist_mbid"):
            a = artists.setdefault(r["artist_mbid"], {"name": r["name"], "albums": 0, "songs": 0, "seconds": 0, "bytes": 0, "cover": None,
                                                      "followed": False, "genres": []})
            a["wanted"] = r["n"]
        for key, a in artists.items():
            if MBID.fullmatch(key):
                a["followed"] = a["followed"] or bool(acquire.refs(db, "artist", key) & followed)
        for artist_id, genres in db.execute("select artist_id, group_concat(genres, '|') from track where source = 'local' and genres is not null "
                                             "group by artist_id"):
            counts = Counter(g for chunk in genres.split("|") for g in json.loads(chunk))
            if a := artists.get(mbid_of.get(artist_id) or artist_id):
                a["genres"] = [g for g, _ in counts.most_common(3)]
        return artists

    def picture(album_id: str | None, small: bool = False) -> str:
        cls = "cover small" if small else "cover"
        return (f'<img class="{cls}" loading="lazy" alt="" src="/music/cover/{quote(album_id)}" {HIDE_BROKEN}>' if album_id else f'<div class="{cls}"></div>')

    def music_cover(db, request, user, form):
        try:
            return api.getCoverArt(db, type("P", (), {"need": lambda self, key: request.path_params["id"]})(), user)
        except Exception:  # no cover anywhere: a quiet placeholder, not a broken image
            return Response(NO_COVER, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    def library_artist(db, request, user, form):
        key = request.path_params["key"]
        ids = [key] if key.startswith("ar-") else [r[0] for r in db.execute("select artist_id from mb_artist where mbid = ?", (key,))]
        mbid = key if MBID.fullmatch(key) else db.execute("select mbid from mb_artist where artist_id = ?", (key,)).fetchone()
        mbid = mbid if isinstance(mbid, str) or mbid is None else mbid[0]
        marks = ",".join("?" * len(ids)) or "null"
        albums = db.execute(f"select album_id, min(album) album, min(year) year, min(original_date) original, min(release_types) types, count(*) songs, "
                            f"sum(duration) seconds, sum(size) bytes, group_concat(distinct upper(suffix)) formats, min(album_artist) artist "
                            f"from track where source = 'local' and artist_id in ({marks}) group by album_id order by year, album", ids).fetchall()
        wanted = db.execute("select * from want_album where artist_mbid = ? and state in ('missing', 'downloading') order by first_date", (mbid,)).fetchall() if mbid else []
        name = (albums[0]["artist"] if albums else wanted[0]["artist"] if wanted else
                (db.execute("select name from mb_artist where mbid = ?", (mbid,)).fetchone() or [key])[0])
        info = db.execute(f"select * from ytm_artist where artist_id in ({marks}) and description is not null limit 1", ids).fetchone()
        about = db.execute("select type from mb_artist where mbid = ?", (mbid,)).fetchone() if mbid else None
        downloading = {r[0] for r in db.execute("select coalesce(t.rg, d.album_mbid) from download d left join download_target t on t.hash = d.hash "
                                                "where d.state not in ('done', 'failed')")}
        groups: dict[str, list] = {}
        for a in albums:
            kind = ((a["types"] or "album").split(";")[0].strip() or "album").title()
            groups.setdefault("EP" if kind == "Ep" else kind, []).append(card(a))
        for w in wanted:
            state = ('<span class="pill busy">downloading</span>' if w["state"] == "downloading" or w["rg"] in downloading else
                     f'<span class="pill">not found yet · {ago(w["searched"])}</span>' if w["searched"] else '<span class="pill">wanted</span>')
            groups.setdefault(w["primary_type"] or "Album", []).append(
                f'<div class="card dim"><img class="cover" loading="lazy" alt="" src="/music/cover/al-mb-{escape(w["rg"])}" '
                f'{HIDE_BROKEN}>'
                f'<b>{escape(w["title"])}</b><span class="muted">{escape((w["first_date"] or "")[:4])}</span>{state}</div>')
        links = "".join(f'<a href="/music/library/artist/{quote(other)}">{escape(n)}</a> <span class="muted">{kind}</span> · '
                        for other, kind in linked(db, ids[0]) if (n := link_name(db, other))) if ids else ""
        appears = db.execute(f"select distinct t.album_id, t.album, t.album_artist, group_concat(distinct c.role) roles from credit c "
                             f"join track t on t.id = c.track_id where c.artist_id in ({marks}) and t.artist_id not in ({marks}) "
                             f"and c.role != 'albumartist' group by t.album_id order by t.year", ids + ids).fetchall() if ids else []
        facts = [("Type", about[0] if about and about[0] else None), ("Albums here", len(albums) or None), ("Wanted", len(wanted) or None),
                 ("Songs", sum(a["songs"] for a in albums) or None), ("Length", length(sum(a["seconds"] or 0 for a in albums)) if albums else None),
                 ("Size", size(sum(a["bytes"] or 0 for a in albums)) if albums else None),
                 ("MusicBrainz", f'<a href="https://musicbrainz.org/artist/{mbid}">{mbid[:8]}…</a> · <a href="/music/artist/{mbid}">full discography</a>'
                  if mbid else None), ("Related", links.rstrip(" · ") or None)]
        image = (f'<img class="cover" loading="lazy" alt="" src="{escape(info["image"])}" {HIDE_BROKEN}>'
                 if info and info["image"]
                 else picture(albums[-1]["album_id"] if albums else None))
        body = (f'{MUSIC_NAV}<section class="hero">{image}<div>'
                f'<dl class="meta">{dl(facts)}</dl>'
                f'{"<p class=muted>" + escape(info["description"][:600]) + "</p>" if info else ""}</div></section>')
        for kind in ("Album", "EP", "Single", "Compilation", "Soundtrack", "Live", "Other"):
            if cards := groups.pop(kind, None):
                body += f'<section><h2>{kind}s</h2><div class="cards">{"".join(cards)}</div></section>'
        for kind, cards in groups.items():
            body += f'<section><h2>{escape(kind)}</h2><div class="cards">{"".join(cards)}</div></section>'
        if appears:
            body += ('<section><h2>Also appears on</h2><table class="grid"><tbody>' + "".join(
                f'<tr><td class="lead"><a href="/music/library/album/{quote(r["album_id"])}">{escape(r["album"])}</a> '
                f'<span class="muted">{escape(r["album_artist"])}</span></td><td>{escape(r["roles"].replace(",", ", "))}</td></tr>'
                for r in appears) + '</tbody></table></section>')
        return page(name, body, wide=True)

    def link_name(db, artist_id: str) -> str | None:
        row = db.execute("select album_artist from track where artist_id = ? limit 1", (artist_id,)).fetchone() or \
            db.execute("select name from credit where artist_id = ? limit 1", (artist_id,)).fetchone() or \
            db.execute("select name from mb_artist where artist_id = ?", (artist_id,)).fetchone()
        return row[0] if row else None

    def card(a) -> str:
        meta = " · ".join(filter(None, [str(a["year"] or ""), f'{a["songs"]} songs', length(a["seconds"]), a["formats"]]))
        return (f'<a class="card" href="/music/library/album/{quote(a["album_id"])}">{picture(a["album_id"])}<b>{escape(a["album"])}</b>'
                f'<span class="muted">{escape(meta)}</span></a>')

    def dl(facts) -> str:
        return "".join(f'<dt>{escape(k)}</dt><dd>{v}</dd>' for k, v in facts if v not in (None, "", 0))

    def library_album(db, request, user, form):
        album_id = request.path_params["id"]
        tracks = db.execute("select t.*, s.play_count plays, s.played, s.starred, s.rating from track t left join stat s on s.id = t.id "
                            "where t.album_id = ? order by t.disc_no, t.track_no, t.title", (album_id,)).fetchall()
        if not tracks:
            return back("/music/library", error="That album isn't here.")
        t = tracks[0]
        from .api import credits_of  # api imports this module
        credits = credits_of(db, [r["id"] for r in tracks])
        artist_key = (db.execute("select mbid from mb_artist where artist_id = ?", (t["artist_id"],)).fetchone() or [t["artist_id"]])[0]
        genres = Counter(g for r in tracks if r["genres"] for g in json.loads(r["genres"]))
        lossless = all((r["suffix"] or "") in importer.LOSSLESS for r in tracks)
        bitrates = sorted({r["bitrate"] for r in tracks if r["bitrate"]})
        fmt = ", ".join(sorted({(r["suffix"] or "").upper() for r in tracks})) + ("" if lossless else f' {bitrates[0]}–{bitrates[-1]} kbps'
                                                                                   if len(bitrates) > 1 else f' {bitrates[0]} kbps' if bitrates else "")
        facts = [("Artist", f'<a href="/music/library/artist/{quote(artist_key)}">{escape(t["album_artist"])}</a>'),
                 ("Released", escape(t["original_date"] or str(t["year"] or ""))), ("Type", escape((t["release_types"] or "").replace(";", ","))),
                 ("Label", escape(t["label"] or "")), ("Genres", escape(", ".join(g for g, _ in genres.most_common(5)))),
                 ("Songs", len(tracks)), ("Discs", max((r["disc_no"] or 1) for r in tracks) if max((r["disc_no"] or 1) for r in tracks) > 1 else None),
                 ("Length", length(sum(r["duration"] or 0 for r in tracks))), ("Format", escape(fmt)), ("Size", size(sum(r["size"] or 0 for r in tracks))),
                 ("Plays", sum(r["plays"] or 0 for r in tracks) or None),
                 ("MusicBrainz", f'<a href="https://musicbrainz.org/release/{escape(t["mb_release"])}">release</a>' if t["mb_release"] else None),
                 ("Folder", f'<code>{escape(str(Path(t["ref"]).parent))}</code>' if t["source"] == "local" else None)]
        multi = len({r["disc_no"] for r in tracks}) > 1
        rows = ""
        for r in tracks:
            mine = credits.get(r["id"], [])
            by = ", ".join(dict.fromkeys(c["name"] for c in mine if c["role"] == "artist")) or r["artist"]
            writers = ", ".join(dict.fromkeys(c["name"] for c in mine if c["role"] in ("composer", "writer", "lyricist")))
            number = f'{r["disc_no"]}.{r["track_no"]}' if multi else str(r["track_no"] or "")
            rows += (f'<tr><td class="num">{number}</td><td class="lead"><a href="/music/library/song/{quote(r["id"])}">{escape(r["title"])}</a>'
                     f'{" ★" if r["starred"] else ""}{f"<div class=muted>{escape(by)}</div>" if by and by != t["album_artist"] else ""}</td>'
                     f'<td class="muted">{escape(writers)}</td><td class="num">{length(r["duration"])}</td>'
                     f'<td class="num" data-unit="plays">{r["plays"] or ""}</td></tr>')
        body = (f'{MUSIC_NAV}<section class="hero">{picture(album_id)}<dl class="meta">{dl(facts)}</dl></section>'
                f'<section><table class="grid tracks"><thead><tr><th class="num">#</th><th>Title</th><th>Written by</th><th class="num">Length</th>'
                f'<th class="num">Plays</th></tr></thead><tbody>{rows}</tbody></table></section>')
        return page(t["album"], body, wide=True)

    def library_song(db, request, user, form):
        r = db.execute("select t.*, s.play_count plays, s.played, s.starred, s.rating from track t left join stat s on s.id = t.id where t.id = ?",
                       (request.path_params["id"],)).fetchone()
        if r is None:
            return back("/music/library", error="That song isn't here.")
        from .api import credits_of  # api imports this module
        mine = credits_of(db, [r["id"]]).get(r["id"], [])
        roles: dict[str, list] = {}
        for c in mine:
            if c["role"] not in ("albumartist",):
                who = f'<a href="/music/library/artist/{quote(c["mbid"] or c["artist_id"])}">{escape(c["name"])}</a>'
                roles.setdefault(c["role"], []).append(who + (f' <span class="muted">{escape(c["sub_role"])}</span>' if c["sub_role"] else ""))
        order = ["artist", "performer", "composer", "lyricist", "writer", "arranger", "conductor", "producer", "remixer", "mixer", "engineer"]
        credit_facts = [(role.title() + "s" if len(roles[role]) > 1 else role.title(), ", ".join(roles[role]))
                        for role in sorted(roles, key=lambda x: order.index(x) if x in order else 99)]
        shared = db.execute("select kept from audio_shared where path = ?", (r["ref"],)).fetchone()
        lyrics = db.execute("select found is not null and found != '' from lyrics where track_id = ?", (r["id"],)).fetchone()
        facts = [("Album", f'<a href="/music/library/album/{quote(r["album_id"])}">{escape(r["album"])}</a>'
                 + (f' · disc {r["disc_no"]}' if (r["disc_no"] or 1) > 1 else "") + (f' · track {r["track_no"]}' if r["track_no"] else "")),
                 *credit_facts, ("Length", length(r["duration"])), ("Genres", escape(", ".join(json.loads(r["genres"]) if r["genres"] else [r["genre"] or ""]))),
                 ("ISRC", escape(r["isrc"] or "")),
                 ("MusicBrainz", f'<a href="https://musicbrainz.org/recording/{escape(r["mb_recording"])}">recording</a>' if r["mb_recording"] else None),
                 ("Format", escape(f'{(r["suffix"] or "").upper()} · {r["bitrate"]} kbps · {size(r["size"])}')),
                 ("Plays", f'{r["plays"] or 0}' + (f' · last {ago(r["played"])}' if r["played"] else "")),
                 ("Starred", "★" if r["starred"] else None), ("Lyrics", ("found" if lyrics[0] else "none found") if lyrics else None),
                 ("Shares audio with", f'<code>{escape(shared[0])}</code>' if shared else None),
                 ("File", f'<code>{escape(r["ref"])}</code>' if r["source"] == "local" else None)]
        body = f'{MUSIC_NAV}<section class="hero">{picture(r["album_id"])}<dl class="meta">{dl(facts)}</dl></section>'
        return page(r["title"], body, wide=True)

    def give_up(db, user, rg: str):
        api._want(db, user["name"], "album", mine(db, user, "album", rg), None)
        db.execute("insert or replace into given_up values (?, ?)", (rg, time.time()))
        db.execute("update want_album set state = 'skipped', error = 'given up' where rg = ? and reason = 'followed'", (rg,))

    def wanted_action(db, request, user, form):
        rg, action = request.path_params["rg"], request.path_params["action"]
        here = form.get("next", "") if form.get("next", "").startswith("/music/wanted") else "/music/wanted"
        row = db.execute("select * from want_album where rg = ?", (rg,)).fetchone()
        if row is None or action not in ("search", "block", "give-up"):
            return back(here, error="That album isn't wanted any more.")
        name = f'{row["artist"]} – {row["title"]}'
        if action == "block" and not (api.downloads and api.downloads.block(db, rg)):
            return back(here, error=f"Nothing is downloading for {name} that can still be stopped.")
        if action in ("search", "block"):
            api.library.soon.add(rg)
            api.library.wake.set()
            return back(here, message=f"{name} is searched next, in a minute or two." if action == "search" else
                        f"Blocked that release of {name}; the next search picks another.")
        give_up(db, user, rg)
        return back(here, message=f"Gave up on {name}. Files already here stay.")

    def wanted_give_up(db, request, user, form):
        here = form.get("next", "") if form.get("next", "").startswith("/music/wanted") else "/music/wanted"
        rgs = [rg for rg in form.get("rgs", "").split(",") if MBID.fullmatch(rg)]
        for rg in rgs:
            give_up(db, user, rg)
        return back(here, message=f"Gave up on {len(rgs)} album{'s' if len(rgs) != 1 else ''}. Files already here stay.")

    def unwant(db, request, user, form):
        if form.get("kind") in MB_IDS:
            api._want(db, user["name"], form["kind"], [form.get("ref", "")], None)
        return back("/music/wanted", message="Given up.")

    async def home(request: Request):
        return RedirectResponse("/account")

    return [
        Route("/", home),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/admin/artists", signed_in(artist_settings, admin=True)),
        Route("/admin/artists", signed_in(save_artist_settings, admin=True), methods=["POST"]),
        Route("/logout", signed_in(logout), methods=["POST"]),
        Route("/account", signed_in(account)),
        Route("/account/youtube", signed_in(link_youtube), methods=["POST"]),
        Route("/account/youtube/unlink", signed_in(unlink_youtube), methods=["POST"]),
        Route("/account/lastfm", signed_in(lastfm_start)),
        Route("/account/lastfm/callback", signed_in(lastfm_callback), name="lastfm_callback"),
        Route("/account/lastfm/app", signed_in(lastfm_app, admin=True), methods=["POST"]),
        Route("/account/lastfm/unlink", signed_in(unlink_lastfm), methods=["POST"]),
        Route("/account/listenbrainz", signed_in(link_listenbrainz), methods=["POST"]),
        Route("/account/password", signed_in(change_password), methods=["POST"]),
        Route("/admin", signed_in(admin, admin=True)),
        Route("/music", catalogue(find_music)),
        Route("/music/artist/{mbid}", catalogue(music_artist)),
        Route("/music/want", catalogue(set_want), methods=["POST"]),
        Route("/music/unwant", catalogue(unwant), methods=["POST"]),
        Route("/music/wanted", catalogue(wanted)),
        Route("/music/library", catalogue(music_library)),
        Route("/music/library/artist/{key}", catalogue(library_artist)),
        Route("/music/library/album/{id}", catalogue(library_album)),
        Route("/music/library/song/{id}", catalogue(library_song)),
        Route("/music/cover/{id}", catalogue(music_cover)),
        Route("/music/wanted/give-up", catalogue(wanted_give_up), methods=["POST"]),
        Route("/music/wanted/{rg}/{action:str}", catalogue(wanted_action), methods=["POST"]),
        Route("/admin/users", signed_in(create_user, admin=True), methods=["POST"]),
        Route("/admin/users/{name}/delete", signed_in(delete_user, admin=True), methods=["POST"]),
    ]


YOUTUBE_STEPS = """
<p>oasis signs in to YouTube Music with your browser's sign-in. This takes a minute, once:</p>
<ol>
<li>Open a <b>private window</b>, go to <a href="https://music.youtube.com" target="_blank">music.youtube.com</a> and sign in.</li>
<li>Press <b>F12</b>, open the <b>Network</b> tab and type <code>browse</code> in its filter box. Click <b>Home</b> on the page.</li>
<li>Right-click the <code>browse</code> request and choose <b>Copy → Copy as cURL</b> (or copy its request headers, or just the
<code>Cookie</code> value).</li>
<li>Paste it below, then close the private window <b>without signing out</b>: signing out ends the sign-in oasis uses.</li>
</ol>
<form method="post" action="/account/youtube"><textarea name="headers" placeholder="curl 'https://music.youtube.com/youtubei/v1/browse?...' -H 'cookie: ...'"></textarea>
<p><button>Link YouTube Music</button></p></form>
<p class="muted">The roma app can link it for you with its own Google sign-in.</p>
"""
