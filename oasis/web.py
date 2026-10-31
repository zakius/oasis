"""Account pages: sign in with the Subsonic user, link YouTube Music, Last.fm and ListenBrainz, manage users."""
import json
import urllib.request
from html import escape
from urllib.parse import parse_qsl, quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from . import accounts
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
ol { padding-left: 1.2rem; } form.inline { display: inline; } td { padding: .2rem .6rem .2rem 0; }
"""


async def read_form(request: Request) -> dict:
    return dict(parse_qsl((await request.body()).decode()))


def page(title: str, body: str, message: str = "", error: str = "") -> HTMLResponse:
    notice = f'<p class="ok">{escape(message)}</p>' if message else ""
    notice += f'<p class="error">{escape(error)}</p>' if error else ""
    return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        f'<title>{escape(title)} · oasis</title><style>{STYLE}</style></head><body><h1>{escape(title)}</h1>'
                        f'{notice}{body}</body></html>')


def back(path: str, message: str = "", error: str = "") -> RedirectResponse:
    query = f"?message={quote(message)}" if message else f"?error={quote(error)}" if error else ""
    return RedirectResponse(path + query, status_code=303)


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
        if api.library:
            try:
                lidarr = api.library.lidarr
                queue = lidarr.call("GET", "queue?pageSize=50&includeAlbum=true&includeArtist=true")["records"]
                missing = lidarr.call("GET", "wanted/missing?pageSize=1&monitored=true")["totalRecords"]
                downloading = "".join(
                    f'<li>{escape(q.get("artist", {}).get("artistName", ""))} – {escape(q.get("album", {}).get("title", q["title"]))} '
                    f'<span class="muted">{100 - round(100 * q["sizeleft"] / q["size"]) if q.get("size") else 0}%</span></li>' for q in queue)
                lines.append(f'Downloading {len(queue)}{":" if queue else "."}<ul>{downloading}</ul>' if queue else "Nothing downloading.")
                lines.append(f'{missing} more albums Lidarr is looking for.')
            except Exception as e:
                lines.append(f'<span class="error">Lidarr didn\'t answer: {escape(str(e))}</span>')
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
