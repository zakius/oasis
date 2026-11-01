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
<label>Shared secret<input type="password" name="secret"></label><button class="primary">Connect</button></form>"""

STYLE = """
:root { color-scheme: light dark; --bg: #f4f5f7; --surface: #fff; --raised: #eceef2; --line: #e1e4ea; --text: #14171c; --muted: #5b6472;
        --accent: #0d9488; --link: #0f766e; --on-accent: #fff; --good: #15803d; --busy: #4f46e5; --bad: #dc2626; --star: #d97706;
        --shadow: 0 1px 2px #0000000d; --pad: 1.25rem; --radius: 14px; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0f1115; --surface: #171a20; --raised: #22262e; --line: #2a2e37; --text: #e8eaee; --muted: #969eac; --accent: #2dd4bf;
          --link: #5eead4; --on-accent: #042f2c; --good: #4ade80; --busy: #a5b4fc; --bad: #f87171; --star: #fbbf24; --shadow: none; } }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--text); background: var(--bg);
       -webkit-text-size-adjust: 100%; -webkit-tap-highlight-color: transparent; }
main { max-width: 48rem; margin: 0 auto; padding: 2rem 1.5rem 5rem; } .wide main { max-width: 76rem; }
a { color: var(--link); text-decoration: none; } a:hover { text-decoration: underline; }
h1 { font-size: 1.85rem; line-height: 1.2; letter-spacing: -.02em; margin: 0 0 1.5rem; overflow-wrap: anywhere; }
h2 { font-size: 1.05rem; letter-spacing: -.01em; margin: 0 0 .75rem; } main > h2 { margin-top: 2rem; }
p { margin: 0 0 .75rem; } :is(p, ul, ol, form):last-child { margin-bottom: 0; }
ol { padding-left: 1.3rem; } li { margin: .3rem 0; } code { font-size: .85em; overflow-wrap: anywhere; }
.bar { position: sticky; top: 0; z-index: 5; border-bottom: 1px solid var(--line); background: var(--bg); }
.bar > div { max-width: 76rem; margin: 0 auto; padding: 0 1.5rem; height: 3.75rem; display: flex; align-items: center; gap: 2.5rem; }
.brand { display: flex; align-items: center; gap: .55rem; margin: 0; font-size: 1.15rem; font-weight: 700; letter-spacing: -.02em; color: var(--text); }
.brand:hover { text-decoration: none; } .brand svg { width: 1.65rem; height: 1.65rem; } .logo circle { fill: var(--accent); } .logo path { stroke: var(--on-accent); }
.tabs { display: flex; gap: .25rem; }
.tabs a { display: flex; align-items: center; gap: .5rem; padding: .45rem .85rem; border-radius: 999px; color: var(--muted); font-size: .95rem; font-weight: 600; }
.tabs a:hover { color: var(--text); background: var(--raised); text-decoration: none; }
.tabs a[aria-current] { color: var(--text); background: var(--raised); } .tabs a[aria-current] svg { color: var(--link); }
.tabs svg { width: 1.2rem; height: 1.2rem; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
section { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: var(--pad); margin: 0 0 1rem; box-shadow: var(--shadow); }
section.plain { background: none; border: 0; border-radius: 0; padding: 0; margin: 0 0 2.5rem; box-shadow: none; }
.split { display: grid; grid-template-columns: minmax(0, 1fr) 22rem; gap: 0 1.5rem; align-items: start; }
input, select, textarea, button { font: inherit; color: inherit; }
input[type=text], input[type=password], select, textarea { display: block; width: 100%; min-height: 2.75rem; margin: .35rem 0 0; padding: .55rem .8rem;
  border: 1px solid var(--line); border-radius: 10px; background: var(--bg); color: var(--text); font-size: 1rem; font-weight: 400; }
textarea { min-height: 8rem; resize: vertical; line-height: 1.45; }
select { appearance: none; padding-right: 2.25rem; cursor: pointer; background: var(--bg) no-repeat right .8rem center / .75rem
  url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%238a93a0' stroke-width='2'/%3E%3C/svg%3E"); }
:is(input, select, textarea):focus { outline: 2px solid var(--accent); outline-offset: -2px; } ::placeholder { color: var(--muted); opacity: .8; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
label { display: block; margin: 0 0 1rem; font-size: .875rem; font-weight: 600; color: var(--muted); }
label.check { display: flex; align-items: center; gap: .6rem; color: var(--text); font-size: 1rem; font-weight: 400; }
input[type=checkbox], input[type=radio] { width: 1.15rem; height: 1.15rem; margin: 0; accent-color: var(--accent); }
button, .button { display: inline-flex; align-items: center; justify-content: center; gap: .4rem; min-height: 2.25rem; padding: .35rem .95rem;
  border: 1px solid var(--line); border-radius: 10px; background: var(--raised); color: var(--text); font-size: .9rem; font-weight: 600;
  line-height: 1.2; white-space: nowrap; cursor: pointer; }
button:hover, .button:hover { border-color: var(--muted); text-decoration: none; } button:disabled { opacity: .55; cursor: progress; }
:is(button, .button).primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
:is(button, .button).primary:hover { filter: brightness(1.08); }
button.danger { background: none; color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, transparent); }
button.danger:hover { border-color: var(--bad); background: color-mix(in srgb, var(--bad) 10%, transparent); }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; } .row > .right { margin-left: auto; } .row > p { margin: 0; }
section > .row + .row { margin-top: 1rem; } form.inline { display: inline; }
.pill { display: inline-block; padding: .1rem .55rem; border-radius: 999px; font-size: .78rem; font-weight: 600; line-height: 1.45; white-space: nowrap;
        vertical-align: .08em; color: var(--muted); background: color-mix(in srgb, currentColor 14%, transparent); }
.muted { color: var(--muted); font-size: .875rem; } .good, .ok { color: var(--good); } .busy { color: var(--busy); } .bad, .error { color: var(--bad); }
.accent { color: var(--link); } .star { color: var(--star); } .ok::before { content: "✓ "; } a.pill:hover { text-decoration: none; filter: brightness(1.15); }
.meter { display: inline-block; width: 4rem; height: .4rem; margin-right: .5rem; border-radius: 999px; overflow: hidden;
  vertical-align: .1em; background: color-mix(in srgb, var(--busy) 20%, transparent); } .meter i { display: block; height: 100%; background: var(--busy); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr)); gap: .75rem; margin: 0 0 1.25rem; }
.stats > div { padding: .85rem 1rem; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); }
.stats b { display: block; font-size: 1.6rem; line-height: 1.15; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.stats span { color: var(--muted); font-size: .85rem; }
ul.rows { list-style: none; padding: 0; margin: 0 calc(-1 * var(--pad)); }
ul.rows > li { display: flex; align-items: center; gap: .85rem; margin: 0; padding: .7rem var(--pad); }
ul.rows > li + li { border-top: 1px solid var(--line); } section > :is(ul.rows, table.grid):first-child { margin-top: -.5rem; }
section > ul.rows:last-child { margin-bottom: -.5rem; }
.grow { flex: 1; min-width: 0; overflow-wrap: anywhere; } ul.rows .grow > a:first-child { font-weight: 600; }
.mark { flex: none; width: 1.25rem; text-align: center; font-weight: 700; }
table.grid { width: 100%; border-collapse: collapse; }
section > table.grid { width: calc(100% + 2 * var(--pad)); margin: 0 calc(-1 * var(--pad)) -.5rem; }
table.grid th { padding: .55rem .6rem; border-bottom: 1px solid var(--line); text-align: left; font-size: .72rem; font-weight: 600; text-transform: uppercase;
                letter-spacing: .06em; color: var(--muted); white-space: nowrap; } table.grid th.num { text-align: right; }
table.grid td { padding: .65rem .6rem; border-top: 1px solid var(--line); vertical-align: middle; } table.grid tbody tr:first-child td { border-top: 0; }
table.grid :is(td, th):first-child { padding-left: var(--pad); } table.grid :is(td, th):last-child { padding-right: var(--pad); }
table.grid tbody tr:hover { background: color-mix(in srgb, var(--raised) 50%, transparent); } td.lead b { font-weight: 600; }
tr:target { background: color-mix(in srgb, var(--accent) 16%, transparent) !important; } tr[id] { scroll-margin-top: 30vh; }
.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; } td.thumb { width: 1px; padding-right: .2rem !important; }
td.meta { color: var(--muted); font-size: .9rem; white-space: nowrap; } a.stretch::after { content: ""; position: absolute; inset: 0; }
a.stretch, table.tracks td.lead > a { color: var(--text); }
table.thumbs tr { position: relative; } table.tracks td:first-child { width: 2.5rem; color: var(--muted); }
.actions { text-align: right; white-space: nowrap; } .actions form { display: inline-block; } .actions form + form { margin-left: 1.25rem; }
.actions button { min-height: 2rem; padding: .25rem .75rem; font-size: .85rem; }
details > summary { cursor: pointer; list-style: none; } details > summary::-webkit-details-marker { display: none; }
details.group { margin: 0 0 1rem; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); }
details.group > summary { display: flex; align-items: center; gap: .65rem; min-height: 3.25rem; padding: .7rem var(--pad); font-weight: 650; user-select: none; }
details.group > summary::after { content: ""; width: .5rem; height: .5rem; margin: -.25rem .2rem 0 auto; border: solid var(--muted); border-width: 0 2px 2px 0;
                                 transform: rotate(45deg); transition: transform .15s; }
details.group[open] > summary::after { margin-top: .25rem; transform: rotate(225deg); } details.group[open] > summary { border-bottom: 1px solid var(--line); }
details.group > .bulk { display: flex; justify-content: flex-end; padding: .85rem var(--pad); border-top: 1px solid var(--line); }
.dot { flex: none; width: .55rem; height: .55rem; border-radius: 50%; background: currentColor; } .count { color: var(--muted); font-weight: 500; }
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; margin: 0 0 1rem; } .filters > * { margin: 0 !important; }
.filters :is(select, input) { width: auto; min-width: 0; flex: 1 1 10rem; }
.filters select[name=artist] { flex: 2 1 14rem; } .filters select[name=type] { flex: 0 1 8.5rem; }
.search { margin: 0 0 1.25rem; } .search > input { margin: 0 0 .75rem; } .facts > .row { margin: 0 0 1.1rem; }
.seg { display: inline-flex; gap: 2px; padding: 3px; border-radius: 12px; background: var(--raised); }
.seg label { margin: 0; font-size: .9rem; } .seg input { position: absolute; opacity: 0; pointer-events: none; }
.seg span { display: block; padding: .4rem 1rem; border-radius: 9px; cursor: pointer; }
.seg input:checked + span { background: var(--surface); color: var(--text); box-shadow: 0 1px 3px #0003; }
.seg input:focus-visible + span { outline: 2px solid var(--accent); }
.cover { display: block; flex: none; width: 13rem; height: auto; aspect-ratio: 1; object-fit: cover; border-radius: 10px; background: var(--raised); }
.cover.small { width: 2.75rem; border-radius: 6px; } .cover.round { border-radius: 50%; }
.hero { display: grid; grid-template: "art head" auto "art facts" 1fr / 13rem minmax(0, 1fr); gap: .75rem 2rem; margin: 0 0 2.5rem; }
.hero > .cover { grid-area: art; width: 100%; box-shadow: 0 8px 28px #0000003a; } .hero > header { grid-area: head; align-self: end; }
.hero > .facts { grid-area: facts; } .hero h1 { margin: 0 0 .35rem; font-size: 2.25rem; } .hero .sub { margin: 0; color: var(--muted); }
.kicker { margin: 0 0 .3rem; font-size: .75rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: .3rem 1.25rem; margin: 0 0 1rem; font-size: .925rem; }
dl.meta dt { color: var(--muted); } dl.meta dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr)); gap: 1.5rem 1.1rem; }
.card { display: flex; flex-direction: column; gap: .1rem; min-width: 0; color: inherit; } a.card:hover { text-decoration: none; }
.card .cover { width: 100%; margin-bottom: .5rem; box-shadow: 0 2px 10px #00000024; transition: transform .15s; } a.card:hover .cover { transform: translateY(-2px); }
.card b { display: -webkit-box; overflow: hidden; -webkit-line-clamp: 2; -webkit-box-orient: vertical; font-size: .95rem; font-weight: 600;
          line-height: 1.3; overflow-wrap: anywhere; }
a.card:hover b { text-decoration: underline; } .card .muted { font-size: .8rem; } .card .pill { align-self: flex-start; margin-top: .3rem; }
.card.dim .cover { opacity: .4; filter: grayscale(.7); } .card.dim b { color: var(--muted); }
.login { max-width: 23rem; margin: 10vh auto 0; padding: 2rem 1.75rem; } .login h1 { justify-content: center; margin-bottom: 1.75rem; font-size: 1.75rem; }
.login button { width: 100%; min-height: 2.75rem; margin-bottom: 1rem; }
.toast { position: fixed; z-index: 9; left: 50%; bottom: 1.5rem; width: max-content; max-width: min(34rem, calc(100% - 2rem)); margin: 0;
         padding: .8rem 1.1rem .8rem 1rem; border-left: 4px solid var(--good); border-radius: 12px; background: var(--text); color: var(--bg);
         box-shadow: 0 10px 30px #00000040; font-size: .95rem; cursor: pointer; transform: translateX(-50%); animation: rise .2s ease-out, fade 1s 6s forwards; }
.toast.bad { border-left-color: var(--bad); animation: rise .2s ease-out; }
@keyframes rise { from { opacity: 0; transform: translate(-50%, .75rem); } } @keyframes fade { to { opacity: 0; visibility: hidden; } }
@media (max-width: 960px) { .split { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 640px) {
  :root { --pad: 1rem; } body { padding-bottom: calc(4.5rem + env(safe-area-inset-bottom)); } main { padding: 1.25rem 1rem 2rem; }
  h1 { font-size: 1.6rem; margin-bottom: 1.1rem; }
  .bar { position: fixed; top: auto; bottom: 0; left: 0; right: 0; padding-bottom: env(safe-area-inset-bottom); border-top: 1px solid var(--line);
         border-bottom: 0; background: var(--surface); }
  .bar > div { height: auto; padding: 0; } .bar .brand { display: none; } .tabs { flex: 1; gap: 0; }
  .tabs a { flex: 1; flex-direction: column; gap: .2rem; padding: .5rem 0 .55rem; border-radius: 0; background: none !important; font-size: .72rem; }
  .tabs svg { width: 3.6rem; height: 1.95rem; padding: .3rem 0; border-radius: 999px; }
  .tabs a[aria-current] svg { background: color-mix(in srgb, var(--accent) 22%, transparent); }
  .toast { bottom: calc(5.5rem + env(safe-area-inset-bottom)); }
  button, .button, .actions button { min-height: 2.75rem; padding: .5rem 1.1rem; font-size: .9rem; } .seg span { padding: .65rem 1rem; }
  .stats { grid-template-columns: repeat(4, 1fr); gap: .5rem; } .stats > div { padding: .6rem .65rem; } .stats b { font-size: 1.2rem; }
  .stats span { display: block; font-size: .72rem; line-height: 1.25; }
  .filters select[name=artist] { flex-basis: 100%; } .filters input { flex-basis: 6rem; }
  table.grid thead { display: none; } table.grid, table.grid tbody, table.grid tr { display: block; } table.grid { width: auto; }
  table.grid tr { padding: .8rem var(--pad); border-top: 1px solid var(--line); } table.grid tr:first-child { border-top: 0; }
  table.grid td { display: inline-block; margin-right: .75rem; padding: 0 !important; border: 0; } table.grid td:empty { display: none; }
  table.grid :is(td.lead, td.state, td.actions) { display: block; margin-right: 0; } td.lead + td { margin-top: .1rem; }
  table.grid td[data-unit]::after { content: " " attr(data-unit); color: var(--muted); } .num { text-align: left; }
  table.thumbs td.num { margin-right: .55rem; font-size: .8125rem; }
  table.thumbs tr { min-height: 4.4rem; padding-left: calc(var(--pad) + 3.6rem); } table.thumbs td.thumb { position: absolute; left: var(--pad); top: .8rem; }
  td.actions { display: flex !important; justify-content: space-between; gap: 1rem; margin-top: .65rem; } table.thumbs td.actions { margin-left: -3.6rem; }
  table.wanted td.lead { display: inline; } table.wanted td.lead b { display: block; } table.wanted td.lead div { display: inline; }
  table.wanted td.meta { display: inline; margin: 0; font-size: .875rem; } table.wanted td.meta::before { content: " · "; } table.wanted td.state { margin-top: .3rem; }
  table.tracks tr { display: grid; grid-template-columns: 1.75rem minmax(0, 1fr) auto; column-gap: .6rem; align-items: baseline; }
  table.tracks td { margin: 0; } table.tracks td:nth-child(1) { grid-row: 1 / 3; } table.tracks td:nth-child(4) { grid-area: 1 / 3; }
  table.tracks td:nth-child(3) { grid-area: 2 / 2; font-size: .8rem; } table.tracks td:nth-child(5) { grid-area: 2 / 3; font-size: .8rem; text-align: right; }
  .hero { grid-template: "art head" auto "facts facts" auto / 7.5rem minmax(0, 1fr); gap: 1.25rem 1rem; margin-bottom: 2rem; }
  .hero > header { align-self: center; } .hero h1 { font-size: 1.4rem; } .hero .sub { font-size: .9rem; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem .85rem; }
}
"""


STATES = {"added": "waiting for TorBox", "downloading": "TorBox downloading", "fetching": "copying here", "importing": "importing"}
NO_COVER = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#8883"/>'
            '<text x="50" y="64" font-size="42" text-anchor="middle" fill="#8889">♪</text></svg>')
confirm = lambda question: f' onsubmit="return confirm({escape(json.dumps(question, ensure_ascii=False))})"'  # for buttons that drop things
HIDE_BROKEN = 'onerror="this.removeAttribute(\'src\')"'  # a cover that fails shows the empty frame, not a broken image
MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TABS = [("home", "/", "Dashboard", "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"),
        ("library", "/music/library", "Library", "M5 4v16M9.5 4v16M14 4.5l5.5 15"),
        ("wanted", "/music/wanted", "Wanted", "M12 3v12m-5-5 5 5 5-5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"),
        ("find", "/music", "Find music", "M10.5 3.5a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm5 12 5 5")]
LOGO = ('<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="15"/><path d="M8 13.5c2.7-2.2 5.3-2.2 8 0s5.3 2.2 '
        '8 0M8 19.5c2.7-2.2 5.3-2.2 8 0s5.3 2.2 8 0" fill="none" stroke-width="2.4" stroke-linecap="round"/></svg>')
FAVICON = "data:image/svg+xml," + quote(LOGO.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"').replace("<circle", '<circle fill="#14b8a6"')
                                        .replace('fill="none"', 'fill="none" stroke="#fff"'))


def length(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02}"


def size(n) -> str:
    return f"{(n or 0) / 1e9:.1f} GB" if (n or 0) >= 1e9 else f"{(n or 0) / 1e6:.0f} MB"


def until(ts: float | None) -> str:
    minutes = int(((ts or 0) - time.time()) // 60)
    return "soon" if minutes < 2 else f"in {minutes} min" if minutes < 120 else f"in {minutes // 60} h" if minutes < 2880 else f"in {minutes // 1440} days"


def ago(ts: float) -> str:
    minutes = int((time.time() - ts) // 60)
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


async def read_form(request: Request) -> dict:
    return dict(parse_qsl((await request.body()).decode()))


# Forms post in the background and the page's content is swapped for what the server sends back, toast and all; pages
# that show changing state (live) fetch themselves again every 10 s while visible. Without JavaScript, all still works.
SCRIPT = """(() => {
  const main = () => document.getElementById("main");
  const clean = url => { url = new URL(url); url.searchParams.delete("message"); url.searchParams.delete("error"); url.hash = ""; return url; };
  history.replaceState(null, "", clean(location.href));
  function swap(doc) {
    const next = doc.getElementById("main"), open = {};
    if (!next) return;
    main().querySelectorAll("details[id]").forEach(d => open[d.id] = d.open);
    next.querySelectorAll("details[id]").forEach(d => { if (d.id in open) d.open = open[d.id]; });
    main().replaceChildren(...next.childNodes);
    document.title = doc.title;
  }
  async function load(url, options) {
    const response = await fetch(url, options);
    if (clean(response.url).pathname !== location.pathname) return location.assign(response.url);  // signed out, or a page elsewhere
    return new DOMParser().parseFromString(await response.text(), "text/html");
  }
  document.addEventListener("submit", async e => {
    const form = e.target;
    if (e.defaultPrevented || form.method !== "post") return;
    e.preventDefault();
    form.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      const doc = await load(form.action, {method: "POST", body: new URLSearchParams(new FormData(form))});
      if (doc) {
        swap(doc);
        document.querySelectorAll(".toast").forEach(t => t.remove());
        document.body.append(...doc.querySelectorAll(".toast"));
      }
    } catch { form.submit(); }
  });
  if (document.body.dataset.live) setInterval(async () => {
    const typing = [...main().querySelectorAll("input:not([type=hidden]), textarea")].some(i => i.value !== i.defaultValue);
    if (document.hidden || typing || document.activeElement?.matches("input, textarea, select")) return;
    try { const doc = await load(clean(location.href)); if (doc) swap(doc); } catch {}
  }, 10000);
})();"""


def page(title: str, body: str, message: str = "", error: str = "", wide: bool = False, live: bool = False, tab: str | None = None,
         heading: bool = True) -> HTMLResponse:
    """tab: the navigation tab the page is under, None for no navigation (signing in); heading=False when body brings its own h1."""
    notice = f'<p class="toast" onclick="this.remove()">{escape(message)}</p>' if message else ""
    notice += f'<p class="toast bad" onclick="this.remove()">{escape(error)}</p>' if error else ""
    tabs = "".join(f'<a href="{href}"{' aria-current="page"' if key == tab else ""}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="{d}"/></svg>'
                   f'{label}</a>' for key, href, label, d in TABS)
    nav = f'<nav class="bar"><div><a class="brand" href="/">{LOGO}oasis</a><div class="tabs">{tabs}</div></div></nav>' if tab else ""
    return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, '
                        f'viewport-fit=cover"><meta name="theme-color" content="#f4f5f7" media="(prefers-color-scheme: light)"><meta name="theme-color" '
                        f'content="#0f1115" media="(prefers-color-scheme: dark)"><link rel="icon" href="{FAVICON}"><title>{escape(title)} · oasis</title>'
                        f'<style>{STYLE}</style></head><body{' class="wide"' if wide else ""}{' data-live="1"' if live else ""}>{nav}<main id="main">'
                        f'{f"<h1>{escape(title)}</h1>" if heading else ""}{body}</main>{notice}<script>{SCRIPT}</script></body></html>')


def back(path: str, message: str = "", error: str = "", anchor: str = "") -> RedirectResponse:
    """anchor: the element to scroll back to, so a phone stays where the button was."""
    query = f"message={quote(message)}" if message else f"error={quote(error)}" if error else ""
    return RedirectResponse((path + ("&" if "?" in path else "?") + query if query else path) + (f"#{anchor}" if anchor else ""), status_code=303)


def pill(text: str, tone: str = "") -> str:
    return f'<span class="pill{" " + tone if tone else ""}">{escape(text)}</span>'


def cover_of(rg: str) -> str:
    """A MusicBrainz release group's cover, from the Cover Art Archive until the album is here."""
    return f'<img class="cover small" loading="lazy" alt="" src="/music/cover/al-mb-{escape(rg)}" {HIDE_BROKEN}>'


def hero(image: str, kicker: str, title: str, sub: str, facts: str) -> str:
    """The head of an artist, album or song page: its picture beside what it is and its name, then the facts about it."""
    return (f'<div class="hero">{image}<header><p class="kicker">{kicker}</p><h1>{escape(title)}</h1><p class="sub">{sub}</p></header>'
            f'<div class="facts">{facts}</div></div>')


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
                    return back("/", error="Only admins can manage users.")
                return handler(db, request, user, form)
            return await run_in_threadpool(with_db(run))
        return endpoint

    async def login(request: Request):
        if request.method == "GET":
            return page("Sign in", f'<section class="login"><h1 class="brand">{LOGO}oasis</h1><form method="post"><label>User<input type="text" '
                        'name="name" autocomplete="username" autofocus></label><label>Password<input type="password" name="password" '
                        'autocomplete="current-password"></label><button class="primary">Sign in</button></form>'
                        '<p class="muted">The same user and password as in your Subsonic apps.</p></section>',
                        error=request.query_params.get("error", ""), heading=False)
        form = await read_form(request)

        def run(db):
            user = accounts.get(db, form.get("name", ""))
            if user is None or user["password"] != form.get("password"):
                return back("/login", error="Wrong user or password.")
            response = RedirectResponse("/", status_code=303)
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
                   f'go to its history.</p><form method="post" action="/account/youtube/unlink"{confirm("Unlink YouTube Music?")}>'
                   '<button class="danger">Unlink</button></form>'
                   if user["ytm_auth"] else YOUTUBE_STEPS)
        if not api.ytm:
            youtube = '<p class="muted">YouTube Music is off on this server (<code>[ytmusic] enabled</code>).</p>'
        lastfm = ((LASTFM_APP_STEPS if user["admin"] else '<p class="muted">An admin needs to connect oasis to Last.fm first.</p>')
                  if not (api.config.lastfm_key and api.config.lastfm_secret) else
                  f'<p class="ok">Linked as <b>{escape(user["lastfm_name"] or "")}</b>.</p><form method="post" '
                  f'action="/account/lastfm/unlink"{confirm("Unlink Last.fm?")}><button class="danger">Unlink</button></form>' if user["lastfm_session"] else
                  '<a class="button primary" href="/account/lastfm">Sign in with Last.fm</a>')
        listenbrainz = (f'<p class="ok">Linked.</p><form method="post" action="/account/listenbrainz"{confirm("Unlink ListenBrainz?")}><input type="hidden" name="token">'
                        f'<button class="danger">Unlink</button></form>' if user["listenbrainz_token"] else
                        '<form method="post" action="/account/listenbrainz"><label>User token from '
                        '<a href="https://listenbrainz.org/settings/" target="_blank">listenbrainz.org/settings</a>'
                        '<input type="text" name="token"></label><button class="primary">Link</button></form>')
        admin = '<a class="button" href="/admin">Users</a><a class="button" href="/admin/artists">Artist settings</a>' if user["admin"] else ""
        body = (f'<div class="split"><div>{status(db)}</div><div><section><h2>Account</h2><div class="row"><p>Signed in as <b>{escape(user["name"])}</b>.</p>'
                f'<form class="right" method="post" action="/logout"><button>Sign out</button></form></div>{f"<div class=row>{admin}</div>" if admin else ""}</section>'
                f'<section><h2>YouTube Music</h2>{youtube}</section>'
                f'<section><h2>Last.fm</h2><p class="muted">Your plays are scrobbled here. Turn off scrobbling in your apps, '
                f'or plays count twice.</p>{lastfm}</section>'
                f'<section><h2>ListenBrainz</h2>{listenbrainz}</section>'
                f'<section><h2>Password</h2>' + (
                    '<p class="muted">Set in oasis.toml.</p>' if user["name"] in api.config.users else
                    f'<form method="post" action="/account/password"{confirm("Change your password? Apps signed in with the old one stop working until you update them.")}><label>New password<input type="password" name="password">'
                    '</label><button>Change</button></form>') + '</section></div></div>')
        return page("Dashboard", body, q.get("message", ""), q.get("error", ""), wide=True, live=True, tab="home")

    def link_youtube(db, request, user, form):
        try:
            name = api._link_youtube(db, user["name"], accounts.youtube_headers(form.get("headers", "")))
        except ValueError as e:
            return back("/", error=str(e))
        except Exception as e:  # Fail from the API
            return back("/", error=getattr(e, "message", str(e)))
        return back("/", message=f"YouTube Music linked as {name}. Your mixes are loading.")

    def unlink_youtube(db, request, user, form):
        accounts.set_field(db, user["name"], ytm_auth=None, ytm_name=None)
        api._drop_mixes(db, user["name"])
        return back("/", message="YouTube Music unlinked.")

    def lastfm_service():
        return LastFm(api.config.lastfm_key, api.config.lastfm_secret, None)

    def lastfm_start(db, request, user, form):
        return RedirectResponse(lastfm_service().auth_url(str(request.url_for("lastfm_callback"))), status_code=303)

    def lastfm_callback(db, request, user, form):
        try:
            key, name = lastfm_service().sign_in(request.query_params.get("token", ""))
        except Exception as e:
            return back("/", error=f"Last.fm sign-in failed: {e}")
        accounts.set_field(db, user["name"], lastfm_session=key, lastfm_name=name)
        return back("/", message=f"Last.fm linked as {name}.")

    def lastfm_app(db, request, user, form):
        key, secret = form.get("key", "").strip(), form.get("secret", "").strip()
        try:
            api._set_lastfm_app(db, key, secret)
        except Exception as e:
            return back("/", error=f"Last.fm didn't accept these: {e}")
        return back("/", message="oasis is connected to Last.fm. Now sign in with your Last.fm account.")

    def unlink_lastfm(db, request, user, form):
        accounts.set_field(db, user["name"], lastfm_session=None, lastfm_name=None)
        return back("/", message="Last.fm unlinked.")

    def link_listenbrainz(db, request, user, form):
        token = form.get("token", "").strip()
        if token:
            request_ = urllib.request.Request("https://api.listenbrainz.org/1/validate-token", headers={"Authorization": "Token " + token})
            try:
                with urllib.request.urlopen(request_, timeout=15) as response:
                    result = json.load(response)
            except Exception as e:
                return back("/", error=f"ListenBrainz didn't answer: {e}")
            if not result.get("valid"):
                return back("/", error="ListenBrainz says that token isn't valid.")
        accounts.set_field(db, user["name"], listenbrainz_token=token or None)
        return back("/", message="ListenBrainz linked." if token else "ListenBrainz unlinked.")

    def change_password(db, request, user, form):
        if user["name"] in api.config.users:
            return back("/", error="This password is set in oasis.toml.")
        if not form.get("password"):
            return back("/", error="A password is needed.")
        accounts.set_field(db, user["name"], password=form["password"])
        return back("/", message="Password changed. Update it in your apps.")

    def progress(hash: str, state: str) -> str:
        """How far a download is: TorBox's share done while it downloads, else the step it is at."""
        seen = api.downloads.seen.get(hash) if api.downloads and state == "downloading" else None
        return (f'<span class="meter"><i style="width:{seen[0]:.0%}"></i></span><span class="busy">{seen[0]:.0%}</span>' if seen else
                f'<span class="pill busy">{STATES.get(state, state)}</span>')

    def status(db) -> str:
        """What oasis is busy with: downloads, the MusicBrainz fill and dedupe."""
        count = lambda sql: db.execute(sql).fetchone()[0]
        songs, albums, artists = db.execute("select count(*), count(distinct album_id), count(distinct artist_id) from track "
                                            "where source = 'local'").fetchone()
        tiles = [(songs, "songs"), (albums, "albums"), (artists, "artists"), *([(api.library.still_to_find(db), "albums to find")] if api.library else [])]
        html = '<div class="stats">' + "".join(f'<div><b>{n}</b><span>{label}</span></div>' for n, label in tiles) + '</div>'
        active = db.execute("select hash, title, state, created from download where state not in ('done', 'failed') order by created").fetchall()
        html += (f'<section><h2>Downloading <span class="count">{len(active)}</span></h2><ul class="rows">' + "".join(
            f'<li><div class="grow">{escape(r["title"])}<div class="muted">{progress(r["hash"], r["state"])} {ago(r["created"])}</div></div></li>'
            for r in active) + '</ul></section>' if active else '<section><h2>Downloading</h2><p class="muted">Nothing downloading.</p></section>')
        recent = db.execute("select title, state, error, updated from download where state in ('done', 'failed') "
                            "order by updated desc limit 5").fetchall()
        if recent:
            html += '<section><h2>Lately</h2><ul class="rows">' + "".join(
                f'<li>{"<span class=\"mark good\">✓</span>" if r["state"] == "done" else "<span class=\"mark bad\">✗</span>"}<div class="grow">'
                f'{escape(r["title"])}<div class="muted">{ago(r["updated"])}{": " + escape(r["error"]) if r["error"] and r["state"] == "failed" else ""}'
                f'</div></div></li>' for r in recent) + '</ul></section>'
        filled = count("select count(*) from track where source = 'local' and credits_at is not null")
        known = count("select count(*) from track where source = 'local' and mb_release is not null")
        artists_done = count("select count(*) from mb_artist where fetched is not null and mbid in (select mbid from credit)")
        artists_all = count("select count(*) from mb_artist where mbid in (select mbid from credit)")
        html += (f'<section><h2>Background</h2><p class="muted">MusicBrainz: credits for {filled} of {known} songs, names and links for '
                 f'{artists_done} of {artists_all} artists.</p>')
        shared = db.execute("select count(*), coalesce(sum(t.size), 0) from audio_shared a join track t on t.ref = a.path").fetchone()
        if api.config.dedupe:
            html += f'<p class="muted">Dedupe: {shared[0]} copies share another file\'s audio, saving {shared[1] / 1e9:.1f} GB.</p>'
        return html + '</section>'

    def artist_settings(db, request, user, form):
        q, settings = request.query_params, api._artist_settings(db)
        lines = lambda key: escape("\n".join("; ".join(group) for group in settings.get(key, [])))
        body = ('<form method="post" action="/admin/artists">'
                '<section><h2>Related artists</h2><p class="muted">One group per line, names separated by <code>;</code>. '
                'Separate artists that belong together: they come first among each other\'s similar artists and mix into each '
                f'other\'s radios. Clients only ever see them as related.</p><textarea name="related">{lines("related")}</textarea></section>'
                '<section><h2>Same artist</h2><p class="muted">One artist per line: the name to show, then its other names, '
                'separated by <code>;</code>. For names MusicBrainz doesn\'t know belong together.</p>'
                f'<textarea name="merge">{lines("merge")}</textarea></section>'
                '<section><h2>Keep separate</h2><p class="muted">One name per line. MusicBrainz lists it as another name of '
                'some artist, but here it\'s someone else.</p>'
                f'<textarea name="separate">{escape(chr(10).join(settings.get("separate", [])))}</textarea></section>'
                '<button class="primary">Save</button></form>')
        return page("Artist settings", body, q.get("message", ""), q.get("error", ""), tab="home")

    def save_artist_settings(db, request, user, form):
        groups = lambda key: [names for line in form.get(key, "").splitlines() if len(names := [n.strip() for n in line.split(";") if n.strip()]) > 1]
        settings = {"related": groups("related"), "merge": groups("merge"),
                    "separate": [n.strip() for n in form.get("separate", "").splitlines() if n.strip()]}
        api._set_artist_settings(db, settings)
        return back("/admin/artists", message="Saved. Artist pages, similar artists and radios use it now.")

    def admin(db, request, user, form):
        q = request.query_params
        rows = "".join(
            f'<tr><td class="lead"><b>{escape(r["name"])}</b>{" <span class=pill>admin</span>" if r["admin"] else ""}</td>'
            f'<td class="muted">{" · ".join(name for name, on in (("YouTube Music", r["ytm_auth"]), ("Last.fm", r["lastfm_session"]),
                                                                    ("ListenBrainz", r["listenbrainz_token"])) if on)}</td><td class="actions">' + (
                '<span class="muted">from oasis.toml</span>' if r["name"] in api.config.users else
                f'<form method="post" action="/admin/users/{quote(r["name"])}/delete"{confirm(f"Delete the user {r['name']}?")}><button class="danger">Delete</button></form>') +
            '</td></tr>' for r in db.execute("select * from user order by name"))
        body = (f'<section><h2>Users</h2><table class="grid"><tbody>{rows}</tbody></table></section>'
                '<section><h2>New user</h2><form method="post" action="/admin/users"><label>User<input type="text" name="name">'
                '</label><label>Password<input type="password" name="password"></label><label class="check"><input type="checkbox" '
                'name="admin"> Admin</label><button class="primary">Create</button></form></section>')
        return page("Users", body, q.get("message", ""), q.get("error", ""), tab="home")

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
                return page("Find music", "", error="Acquisition is off: it needs [torbox] with downloads and [prowlarr] in oasis.toml.", tab="find")
            return handler(db, request, user, form)
        return signed_in(run)

    def mine(db, user, kind: str, mbid: str) -> list[str]:
        """The ids the user wants the artist or album under."""
        ids = acquire.refs(db, kind, mbid)
        return [r[0] for r in db.execute(f"select ref from wanted where user = ? and kind = ? and ref in ({','.join('?' * len(ids))})",
                                         (user["name"], kind, *ids))]

    def want_button(kind: str, mbid: str, on: bool, here: str) -> str:
        label = {("album", True): "Want", ("album", False): "Unwant", ("artist", True): "Follow", ("artist", False): "Unfollow"}[kind, on]
        ask = "" if on else confirm(f"{label}? Its search stops; files already here stay.")
        return (f'<form class="inline" method="post" action="/music/want"{ask}><input type="hidden" name="kind" value="{kind}">'
                f'<input type="hidden" name="mbid" value="{escape(mbid)}"><input type="hidden" name="on" value="{"1" if on else ""}">'
                f'<input type="hidden" name="next" value="{escape(here)}"><button{' class="danger"' if not on else ' class="primary"' if kind == "artist" else ""}>'
                f'{label}</button></form>')

    def followed(db, mbid: str) -> bool:
        ids = acquire.refs(db, "artist", mbid)
        return bool(db.execute(f"select 1 from wanted where kind = 'artist' and ref in ({','.join('?' * len(ids))})", list(ids)).fetchone())

    def artist_state(db, mbid: str) -> str:
        return (pill("in the library", "good") + " " if acquire.library_id(db, MB_IDS["artist"] + mbid) else ""
                ) + (pill("followed", "accent") if followed(db, mbid) else "")

    def album_line(db, user, g: dict, here: str, by: bool = True) -> str:
        row = db.execute("select state, error from want_album where rg = ?", (g["id"],)).fetchone()
        state = pill("in the library", "good") if acquire.library_id(db, MB_IDS["album"] + g["id"]) or row and row["state"] == "have" else \
            {"missing": pill("wanted", "accent"), "downloading": pill("downloading", "busy"),
             "skipped": f'<span class="muted">left out: {escape(row["error"] or "")}</span>'}[row["state"]] if row else ""
        credit = g.get("artist-credit") or []
        artist = f' — <a href="/music/artist/{credit[0]["artist"]["id"]}">{escape(credited(g))}</a>' if by and credit else ""
        info = details(g.get("primary-type"), *(g.get("secondary-types") or []), (g.get("first-release-date") or "")[:4], g.get("disambiguation"))
        return (f'<li>{cover_of(g["id"])}<div class="grow"><b>{escape(g["title"])}</b>{artist}<div>{info} {state}</div></div>'
                f'{want_button("album", g["id"], not mine(db, user, "album", g["id"]), here)}</li>')

    def find_music(db, request, user, form):
        q = request.query_params
        text, kind, error = q.get("q", "").strip(), "album" if q.get("kind") == "album" else "artist", q.get("error", "")
        checked = lambda k: " checked" if k == kind else ""
        body = (f'<form class="search"><input type="text" name="q" value="{escape(text)}" placeholder="An artist or album, in any script" '
                f'aria-label="Artist or album"{"" if text else " autofocus"}><div class="row"><div class="seg"><label><input type="radio" name="kind" '
                f'value="artist"{checked("artist")}><span>Artists</span></label><label><input type="radio" name="kind" value="album"{checked("album")}>'
                f'<span>Albums</span></label></div><button class="primary right">Search MusicBrainz</button></div></form>')
        if text:
            try:
                found = acquire.find(db, kind, text)
            except OSError as e:
                found, error = [], f"MusicBrainz didn't answer: {e}"
            here = f"/music?q={quote(text)}&kind={kind}"
            lines = [album_line(db, user, g, here) for g in found] if kind == "album" else [
                f'<li><div class="grow"><a href="/music/artist/{a["id"]}">{escape(a["name"])}</a><div>{about(a)}</div></div>{artist_state(db, a["id"])}</li>'
                for a in found]
            body += f'<section><ul class="rows">{"".join(lines)}</ul></section>' if lines else '<p class="muted">MusicBrainz knows nothing by that name.</p>'
        return page("Find music", body, q.get("message", ""), error, tab="find")

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
        body = (f'<section><div class="row"><div class="grow">{about(info)}<div>{artist_state(db, mbid)}</div></div>'
                f'{want_button("artist", mbid, not mine(db, user, "artist", mbid), here)}</div><p class="muted">Following wants its albums, EPs and '
                f'singles, new ones too, except repackages, singles whose songs are on its albums and what has no official release.</p></section>')
        for kind in TYPES:
            rows = sorted((g for g in groups if g.get("primary-type") == kind), key=lambda g: g.get("first-release-date") or "9999")
            if rows:
                body += f'<section><h2>{kind}s</h2><ul class="rows">{"".join(album_line(db, user, g, here, by=False) for g in rows)}</ul></section>'
        return page(info["name"], body, q.get("message", ""), q.get("error", ""), tab="find")

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
                return "downloading", progress(d["hash"], d["state"])
            if r["state"] == "downloading":
                return "downloading", pill("downloading", "busy")
            if r["rg"] in acq.soon:
                return "next", pill("in a minute or two", "accent")
            if (r["error"] or "").startswith(("download failed", "search failed")):
                return "attention", f'<span class="error">{escape(r["error"])}</span> <span class="muted">· tries again {until(r["next_search"])}</span>'
            return ("notfound", f'<span class="muted">searched {ago(r["searched"])}</span>') if r["searched"] else ("waiting", "")

        def action(rg: str, name: str, label: str, ask: str = "") -> str:
            return (f'<form method="post" action="/music/wanted/{escape(rg)}/{name}"{ask and confirm(ask)}><input type="hidden" name="next" value="{escape(here)}">'
                    f'<button{' class="danger"' if ask and name == "give-up" else ""}>{label}</button></form>')

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
        tiles = [(counts.get("missing", 0), "albums to find"), (counts.get("downloading", 0), "downloading"), (counts.get("have", 0), "arrived"),
                 (counts.get("skipped", 0), "left out")]
        body = ('<div class="stats">' + "".join(f'<div><b>{n}</b><span>{label}</span></div>' for n, label in tiles) + '</div>'
                f'<form class="filters"><select name="artist" aria-label="Artist">{option("", "All artists", artist_f)}'
                + "".join(option(a["artist_mbid"], f'{a["artist"]} ({a["n"]})', artist_f) for a in artists)
                + f'</select><select name="type" aria-label="Type">{option("", "Any type", type_f)}'
                + "".join(option(t, t, type_f) for t in ("Album", "EP", "Single"))
                + f'</select><input type="text" name="q" value="{escape(text)}" placeholder="Title or artist" aria-label="Title or artist"><button>Filter</button>'
                + ('<a class="button" href="/music/wanted">Clear</a>' if here != "/music/wanted" else "") + '</form>'
                + '<p class="muted">Giving up stops the search; files already here stay, and wanting it again brings it back.</p>')
        titles = [("attention", "Failed, trying again"), ("downloading", "Downloading"), ("next", "Searched next"),
                  ("notfound", "Not found yet"), ("waiting", "Waiting for a first search")]
        for key, title in titles:
            items = groups.get(key, [])
            if not items:
                continue
            bulk = (f'<form class="bulk" method="post" action="/music/wanted/give-up" '
                    f'{confirm(f"Give up on all {len(items)} in {title}?")}><input type="hidden" name="rgs" '
                    f'value="{",".join(r["rg"] for r, _ in items)}"><input type="hidden" name="next" value="{escape(here)}">'
                    f'<button class="danger">Give up on {"all " + str(len(items)) if len(items) > 1 else "it"}…</button></form>')
            table = "".join(
                f'<tr id="rg-{escape(r["rg"])}"><td class="thumb">{cover_of(r["rg"])}</td><td class="lead"><b>{escape(r["title"])}</b><div class="muted">'
                f'<a href="/music/artist/{escape(r["artist_mbid"])}">{escape(r["artist"] or "")}</a></div></td><td class="meta">{escape(r["primary_type"] or "")}'
                f'{" · " + escape(r["secondary_types"]) if r["secondary_types"] else ""}</td>'
                f'<td class="meta num">{escape((r["first_date"] or "")[:4])}</td>'
                f'<td class="meta">{"<span class=star>★ starred</span>" if r["reason"] == "starred" else "followed"}</td><td class="state">{note}</td>'
                f'<td class="actions">{action(r["rg"], "block", "Try another", f"Drop this download of {r['artist']} – {r['title']} and try another release?") if key == "downloading" else action(r["rg"], "search", "Search now")}'
                f'{action(r["rg"], "give-up", "Give up", f"Give up on {r['artist']} – {r['title']}?")}</td></tr>' for r, note in items)
            opened = " open" if key in ("attention", "downloading", "next") or len(items) <= 30 else ""
            tone = {"attention": "bad", "downloading": "busy", "next": "accent"}.get(key, "muted")
            body += (f'<details class="group" id="group-{key}"{opened}><summary><span class="dot {tone}"></span>{title}<span class="count">{len(items)}</span>'
                     f'</summary><table class="grid thumbs wanted"><thead><tr><th></th><th>Album</th><th>Type</th><th class="num">Year</th><th>Why</th>'
                     f'<th>State</th><th></th></tr></thead><tbody>{table}</tbody></table>{bulk}</details>')
        if not rows:
            body += f'<section><p class="muted">{"Nothing wanted matches." if here != "/music/wanted" else "Nothing left to find."}</p></section>'
        lost = db.execute("select wi.*, exists (select 1 from wanted w where w.kind = wi.kind and w.ref = wi.ref and w.user = ?) mine "
                          "from want_item wi where wi.state = 'failed' and exists (select 1 from wanted w where w.kind = wi.kind "
                          "and w.ref = wi.ref)", (user["name"],)).fetchall()
        if lost:
            names = {"album": ("select album_artist || ' – ' || album from track where album_id = ?",
                               "select a.name || ' – ' || r.title from ytm_release r join ytm_artist a using (artist_id) where r.album_id = ?"),
                     "artist": ("select album_artist from track where artist_id = ?", "select name from ytm_artist where artist_id = ?")}
            name = lambda r: next((row[0] for sql in names[r["kind"]] if (row := db.execute(sql, (r["ref"],)).fetchone())), r["ref"])
            body += ('<section><h2>Not found on MusicBrainz</h2><p class="muted">Starred, but oasis can\'t tell which MusicBrainz '
                     'artist or album it is; it asks again later. Find it and want it from there instead.</p><table class="grid"><tbody>' + "".join(
                         f'<tr><td class="lead"><b>{escape(name(r))}</b></td><td class="meta">{escape(r["kind"])}</td><td class="state"><span class="error">'
                         f'{escape(r["error"] or "")}</span></td><td class="actions">' + (
                             f'<form method="post" action="/music/unwant"{confirm(f"Give up on {name(r)}?")}><input type="hidden" name="kind" value="{r["kind"]}">'
                             f'<input type="hidden" name="ref" value="{escape(r["ref"])}"><button class="danger">Give up</button></form>' if r["mine"] else "")
                         + '</td></tr>' for r in lost) + '</tbody></table></section>')
        return page("Wanted", body, q.get("message", ""), q.get("error", ""), wide=True, live=True, tab="wanted")

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
            rows += (f'<tr><td class="thumb">{picture(a["cover"], "small round")}</td><td class="lead"><a class="stretch" href="/music/library/artist/{quote(key)}">'
                     f'<b>{escape(a["name"] or "")}</b></a>{" " + pill("followed", "accent") if a["followed"] else ""}'
                     f'<div class="muted">{escape(genres)}</div></td>'
                     f'<td class="num" data-unit="here">{a["albums"] or ""}</td><td class="num" data-unit="wanted">{a["wanted"] or ""}</td>'
                     f'<td class="num" data-unit="songs">{a["songs"] or ""}</td>'
                     f'<td class="num">{length(a["seconds"]) if a["seconds"] else ""}</td><td class="num">{size(a["bytes"]) if a["bytes"] else ""}</td></tr>')
        options = "".join(f'<option value="{v}"{" selected" if v == show else ""}>{label}</option>'
                          for v, label in (("", "Everything"), ("here", "With music here"), ("wanted", "With wanted albums")))
        body = (f'<form class="filters"><input type="text" name="q" value="{escape(text)}" placeholder="Artist" aria-label="Artist">'
                f'<select name="show" aria-label="Show">{options}</select><button>Show</button></form>'
                + (f'<section><table class="grid thumbs"><thead><tr><th></th><th>Artist</th><th class="num">Albums here</th><th class="num">Wanted</th>'
                   f'<th class="num">Songs</th><th class="num">Length</th><th class="num">Size</th></tr></thead><tbody>{rows}</tbody></table></section>'
                   if rows else '<section><p class="muted">Nothing matches.</p></section>'))
        return page("Library", body, q.get("message", ""), q.get("error", ""), wide=True, tab="library")

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

    def picture(album_id: str | None, look: str = "") -> str:
        cls = f"cover {look}".strip()
        return (f'<img class="{cls}" loading="lazy" alt="" src="/music/cover/{quote(album_id)}" {HIDE_BROKEN}>' if album_id else f'<div class="{cls}"></div>')

    def music_cover(db, request, user, form):
        try:
            return api.getCoverArt(db, type("P", (), {"need": lambda self, key: request.path_params["id"]})(), user)
        except Exception:  # no cover anywhere: a quiet placeholder, not a broken image
            return Response(NO_COVER, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    def library_artist(db, request, user, form):
        q, key = request.query_params, request.path_params["key"]
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
            state = (pill("downloading", "busy") if w["state"] == "downloading" or w["rg"] in downloading else
                     pill(f"not found yet · {ago(w['searched'])}") if w["searched"] else pill("wanted", "accent"))
            groups.setdefault(w["primary_type"] or "Album", []).append(
                f'<div class="card dim"><img class="cover" loading="lazy" alt="" src="/music/cover/al-mb-{escape(w["rg"])}" '
                f'{HIDE_BROKEN}>'
                f'<b>{escape(w["title"])}</b><span class="muted">{escape((w["first_date"] or "")[:4])}</span>{state}</div>')
        links = "".join(f'<a href="/music/library/artist/{quote(other)}">{escape(n)}</a> <span class="muted">{kind}</span> · '
                        for other, kind in linked(db, ids[0]) if (n := link_name(db, other))) if ids else ""
        appears = db.execute(f"select distinct t.album_id, t.album, t.album_artist, group_concat(distinct c.role) roles from credit c "
                             f"join track t on t.id = c.track_id where c.artist_id in ({marks}) and t.artist_id not in ({marks}) "
                             f"and c.role != 'albumartist' group by t.album_id order by t.year", ids + ids).fetchall() if ids else []
        counts = [f"{len(albums)} albums here" if albums else "", f"{len(wanted)} wanted" if wanted else "",
                  f'{sum(a["songs"] for a in albums)} songs' if albums else "", length(sum(a["seconds"] or 0 for a in albums)) if albums else "",
                  size(sum(a["bytes"] or 0 for a in albums)) if albums else ""]
        facts = [("MusicBrainz", f'<a href="https://musicbrainz.org/artist/{mbid}">{mbid[:8]}…</a>' if mbid else None), ("Related", links.rstrip(" · ") or None)]
        image = (f'<img class="cover round" loading="lazy" alt="" src="{escape(info["image"])}" {HIDE_BROKEN}>' if info and info["image"]
                 else picture(albums[-1]["album_id"] if albums else None, "round"))
        manage = f'<div class="row"><a class="button" href="/music/artist/{mbid}">Full discography</a></div>' if mbid else ""
        body = hero(image, "Artist" + (f" · {escape(about[0])}" if about and about[0] else ""), name, escape(" · ".join(filter(None, counts))),
                    f'{manage}<dl class="meta">{dl(facts)}</dl>{"<p class=muted>" + escape(info["description"][:600]) + "</p>" if info else ""}')
        for kind in ("Album", "EP", "Single", "Compilation", "Soundtrack", "Live", "Other"):
            if cards := groups.pop(kind, None):
                body += f'<section class="plain"><h2>{kind}s</h2><div class="cards">{"".join(cards)}</div></section>'
        for kind, cards in groups.items():
            body += f'<section class="plain"><h2>{escape(kind)}</h2><div class="cards">{"".join(cards)}</div></section>'
        if appears:
            body += ('<h2>Also appears on</h2><section><table class="grid"><tbody>' + "".join(
                f'<tr><td class="lead"><a href="/music/library/album/{quote(r["album_id"])}">{escape(r["album"])}</a> '
                f'<span class="muted">{escape(r["album_artist"])}</span></td><td class="meta">{escape(r["roles"].replace(",", ", "))}</td></tr>'
                for r in appears) + '</tbody></table></section>')
        return page(name, body, q.get("message", ""), q.get("error", ""), wide=True, tab="library", heading=False)

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
        facts = [("Released", escape(t["original_date"] or str(t["year"] or ""))),
                 ("Label", escape(t["label"] or "")), ("Genres", escape(", ".join(g for g, _ in genres.most_common(5)))),
                 ("Discs", max((r["disc_no"] or 1) for r in tracks) if max((r["disc_no"] or 1) for r in tracks) > 1 else None),
                 ("Format", escape(fmt)), ("Size", size(sum(r["size"] or 0 for r in tracks))),
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
                     f'{" <span class=star>★</span>" if r["starred"] else ""}{f"<div class=muted>{escape(by)}</div>" if by and by != t["album_artist"] else ""}</td>'
                     f'<td class="muted">{escape(writers)}</td><td class="num">{length(r["duration"])}</td>'
                     f'<td class="num" data-unit="plays">{r["plays"] or ""}</td></tr>')
        kinds = [k.strip() for k in (t["release_types"] or "album").split(";") if k.strip()]
        line = [f'by <a href="/music/library/artist/{quote(artist_key)}">{escape(t["album_artist"])}</a>', escape(str(t["year"] or "")),
                f"{len(tracks)} songs", length(sum(r["duration"] or 0 for r in tracks))]
        body = (hero(picture(album_id), escape(" · ".join(kinds)), t["album"], " · ".join(filter(None, line)), f'<dl class="meta">{dl(facts)}</dl>')
                + f'<section><table class="grid tracks"><thead><tr><th class="num">#</th><th>Title</th><th>Written by</th><th class="num">Length</th>'
                f'<th class="num">Plays</th></tr></thead><tbody>{rows}</tbody></table></section>')
        return page(t["album"], body, wide=True, tab="library", heading=False)

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
        facts = [*credit_facts, ("Genres", escape(", ".join(json.loads(r["genres"]) if r["genres"] else [r["genre"] or ""]))),
                 ("ISRC", escape(r["isrc"] or "")),
                 ("MusicBrainz", f'<a href="https://musicbrainz.org/recording/{escape(r["mb_recording"])}">recording</a>' if r["mb_recording"] else None),
                 ("Format", escape(f'{(r["suffix"] or "").upper()} · {r["bitrate"]} kbps · {size(r["size"])}')),
                 ("Plays", f'{r["plays"] or 0}' + (f' · last {ago(r["played"])}' if r["played"] else "")),
                 ("Lyrics", ("found" if lyrics[0] else "none found") if lyrics else None),
                 ("Shares audio with", f'<code>{escape(shared[0])}</code>' if shared else None),
                 ("File", f'<code>{escape(r["ref"])}</code>' if r["source"] == "local" else None)]
        line = [f'from <a href="/music/library/album/{quote(r["album_id"])}">{escape(r["album"])}</a>', f'disc {r["disc_no"]}' if (r["disc_no"] or 1) > 1 else "",
                f'track {r["track_no"]}' if r["track_no"] else "", length(r["duration"])]
        body = hero(picture(r["album_id"]), "Song" + (' · <span class="star">★ starred</span>' if r["starred"] else ""), r["title"],
                    " · ".join(filter(None, line)), f'<dl class="meta">{dl(facts)}</dl>')
        return page(r["title"], body, wide=True, tab="library", heading=False)

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
                        f"Blocked that release of {name}; the next search picks another.", anchor=f"rg-{rg}")
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

    return [
        Route("/", signed_in(account)),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/admin/artists", signed_in(artist_settings, admin=True)),
        Route("/admin/artists", signed_in(save_artist_settings, admin=True), methods=["POST"]),
        Route("/logout", signed_in(logout), methods=["POST"]),
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
<p><button class="primary">Link YouTube Music</button></p></form>
<p class="muted">The roma app can link it for you with its own Google sign-in.</p>
"""
