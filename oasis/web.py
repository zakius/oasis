"""Account pages: sign in with the Subsonic user, link YouTube Music, Last.fm and ListenBrainz, manage users. Music
pages: find any artist or album on MusicBrainz, follow or want it, and see what oasis is looking for."""
import json
import re
import time
import urllib.request
from html import escape
from urllib.parse import parse_qsl, quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from . import accounts, acquire, musicbrainz, releases
from .acquire import MB_IDS, TYPES
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
"""


STATES = {"added": "waiting for TorBox", "downloading": "TorBox downloading", "fetching": "copying here", "importing": "importing"}
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MUSIC_NAV = '<p><a href="/account">Your account</a> · <a href="/music">Find music</a> · <a href="/music/wanted">Wanted</a></p>'


def ago(ts: float) -> str:
    minutes = int((time.time() - ts) // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


async def read_form(request: Request) -> dict:
    return dict(parse_qsl((await request.body()).decode()))


def page(title: str, body: str, message: str = "", error: str = "") -> HTMLResponse:
    notice = f'<p class="ok">{escape(message)}</p>' if message else ""
    notice += f'<p class="error">{escape(error)}</p>' if error else ""
    return HTMLResponse(f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                        f'<title>{escape(title)} · oasis</title><style>{STYLE}</style></head><body><h1>{escape(title)}</h1>'
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
        active = {r["rg"]: r for r in db.execute("select d.hash, d.state, coalesce(t.rg, d.album_mbid) rg from download d left join download_target t "
                                                 "on t.hash = d.hash where d.state not in ('done', 'failed') and coalesce(t.rg, d.album_mbid) is not null")}

        def state(r) -> str:
            if d := active.get(r["rg"]):
                seen = api.downloads.seen.get(d["hash"]) if api.downloads else None
                return f"downloading {seen[0]:.0%}" if d["state"] == "downloading" and seen else STATES.get(d["state"], d["state"])
            if r["state"] == "downloading":
                return "downloading"
            if r["rg"] in acq.soon:
                return "searched next"
            if (r["error"] or "").startswith(("download failed", "search failed")):
                return f'<span class="error">{escape(r["error"])}</span>'
            return f'not found yet, searched {ago(r["searched"])}' if r["searched"] else "waiting for its first search"

        action = lambda rg, name, label: (f'<form class="inline" method="post" action="/music/wanted/{escape(rg)}/{name}">'
                                          f'<button>{label}</button></form>')
        rows = db.execute("select * from want_album where state in ('missing', 'downloading') order by state != 'downloading', "
                          "reason != 'starred', artist collate nocase, first_date").fetchall()
        lines = "".join(
            f'<tr><td><a href="/music/artist/{escape(r["artist_mbid"])}">{escape(r["artist"] or "")}</a> – {escape(r["title"])} '
            f'{details(r["primary_type"], (r["first_date"] or "")[:4], "starred" if r["reason"] == "starred" else "")}</td><td>{state(r)}</td><td>'
            + (action(r["rg"], "search", "Search now") if r["state"] == "missing" else action(r["rg"], "block", "Block, try another"))
            + action(r["rg"], "give-up", "Give up") + '</td></tr>' for r in rows)
        counts = dict(db.execute("select state, count(*) from want_album group by state").fetchall())
        body = (f'{MUSIC_NAV}<section><p>{counts.get("missing", 0)} albums to find, {counts.get("downloading", 0)} downloading; '
                f'{counts.get("have", 0)} wanted ones are here, {counts.get("skipped", 0)} of followed artists are left out.</p>'
                f'<p class="muted">Giving up stops the search; files already here stay.</p>'
                f'<table>{lines}</table></section>')
        lost = db.execute("select li.*, exists (select 1 from wanted w where w.kind = li.kind and w.ref = li.ref and w.user = ?) mine "
                          "from lidarr_item li where li.state = 'failed' and exists (select 1 from wanted w where w.kind = li.kind "
                          "and w.ref = li.ref)", (user["name"],)).fetchall()
        if lost:
            names = {"album": ("select album_artist || ' – ' || album from track where album_id = ?",
                               "select a.name || ' – ' || r.title from ytm_release r join ytm_artist a using (artist_id) where r.album_id = ?"),
                     "artist": ("select album_artist from track where artist_id = ?", "select name from ytm_artist where artist_id = ?")}
            name = lambda r: next((row[0] for sql in names[r["kind"]] if (row := db.execute(sql, (r["ref"],)).fetchone())), r["ref"])
            body += ('<section><h2>Not found on MusicBrainz</h2><p class="muted">Starred, but oasis can\'t tell which MusicBrainz '
                     'artist or album it is; it asks again later. Find it above and want it from there instead.</p><ul>' + "".join(
                         f'<li>{escape(name(r))} {details(r["kind"])} <span class="error">{escape(r["error"] or "")}</span> ' + (
                             f'<form class="inline" method="post" action="/music/unwant"><input type="hidden" name="kind" value="{r["kind"]}">'
                             f'<input type="hidden" name="ref" value="{escape(r["ref"])}"><button>Give up</button></form>' if r["mine"] else "")
                         + '</li>' for r in lost) + '</ul></section>')
        return page("Wanted", body, q.get("message", ""), q.get("error", ""))

    def wanted_action(db, request, user, form):
        rg, action = request.path_params["rg"], request.path_params["action"]
        row = db.execute("select * from want_album where rg = ?", (rg,)).fetchone()
        if row is None or action not in ("search", "block", "give-up"):
            return back("/music/wanted", error="That album isn't wanted any more.")
        name = f'{row["artist"]} – {row["title"]}'
        if action == "block" and not (api.downloads and api.downloads.block(db, rg)):
            return back("/music/wanted", error=f"Nothing is downloading for {name} that can still be stopped.")
        if action in ("search", "block"):
            api.library.soon.add(rg)
            api.library.wake.set()
            return back("/music/wanted", message=f"{name} is searched next, in a minute or two." if action == "search" else
                        f"Blocked that release of {name}; the next search picks another.")
        api._want(db, user["name"], "album", mine(db, user, "album", rg), None)
        db.execute("insert or replace into given_up values (?, ?)", (rg, time.time()))
        db.execute("update want_album set state = 'skipped', error = 'given up' where rg = ? and reason = 'followed'", (rg,))
        return back("/music/wanted", message=f"Gave up on {name}. Files already here stay.")

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
