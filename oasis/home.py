"""Home's dial (oasisHome, docs/oasis-extension.md): songs from the user's plays and from every playlist made for them,
in four stops from what they play now to what they have never played. Each song says why it is there."""
import json
import random
import time

from . import playlists
from .mixes import playlist_order
from .ready import FAVOURITE, WINDOW

SIZE = 40  # songs per stop
STOPS = {
    "repeat": ("On repeat", "Your most played of the last three months"),
    "while": ("Been a while", "Songs you played often, and not for three months"),
    "blend": ("Blend", "Songs your mixes share, played or not"),
    "new": ("New to you", "Songs in your mixes you have never played"),
}


def holders(db, user: str) -> dict[str, list[str]]:
    """Each song in the user's generated playlists (mixes, stations, radios), with the names of those holding it, in the
    order the apps list them."""
    rows = [r for r in db.execute("select id, name, mix, radio from playlist where owner = ? and hidden = 0", (user,))
            if playlists.made_for(r["mix"]) or r["radio"]]
    held = {}
    for r in sorted(rows, key=playlist_order):
        for (track_id,) in db.execute("select track_id from playlist_entry where playlist_id = ? order by pos", (r["id"],)):
            names = held.setdefault(track_id, [])
            if r["name"] not in names:
                names.append(r["name"])
    return held


def stops(db, user: str, plays) -> list[tuple[str, list[tuple[str, str, list[str]]]]]:
    """The stops that have songs, in order, each as (kind, [(track id, why, the generated playlists holding it)]). plays(track row) says whether a song plays
    for the user. The mixes' songs are shuffled the same way all day."""
    since = time.time() - WINDOW
    repeat = [(r["track_id"], f"{plays_count(r['n'])} lately") for r in db.execute(
        "select track_id, count(*) as n from play where user = ? and played > ? group by track_id order by n desc, max(played) desc",
        (user, since))]
    left = [(r["id"], f"{plays_count(r['play_count'])}, last in {time.strftime('%B %Y', time.localtime(r['played']))}") for r in db.execute(
        "select id, play_count, played from stat where user = ? and play_count >= ? and played <= ? "
        "order by play_count desc, played desc", (user, FAVOURITE, since))]
    held = holders(db, user)
    shuffled = list(held)
    random.Random(f"{user} {time.strftime('%Y-%m-%d')}").shuffle(shuffled)
    blend = [(id, in_mixes(held[id])) for id in sorted(shuffled, key=lambda id: -len(held[id]))]  # stable: shared first
    found = [("repeat", repeat), ("while", left), ("blend", blend), ("new", [(id, in_mixes(held[id])) for id in shuffled])]

    ids = list({id for _, songs in found for id, _ in songs})
    tracks = {r["id"]: r for r in db.execute(
        "select t.id, t.source, t.ref, s.play_count from track t left join stat s on s.id = t.id and s.user = ? "
        "where t.id in (select value from json_each(?))", (user, json.dumps(ids)))}

    def kept(kind: str, songs: list[tuple[str, str]]) -> list[tuple[str, str, list[str]]]:
        usable = [(id, why, held.get(id, [])) for id, why in songs if id in tracks and tracks[id]["source"] != "missing"
                  and not (kind == "new" and tracks[id]["play_count"]) and plays(tracks[id])]
        return usable[:SIZE]

    result = []
    for kind, songs in found:
        songs = kept(kind, songs)
        if songs:
            result.append((kind, songs))
    return result


def plays_count(n: int) -> str:
    return f"{n} play" + ("" if n == 1 else "s")


def in_mixes(names: list[str]) -> str:
    """Where a song is, as a reason: "My Supermix", "My Supermix + 2 more"."""
    return names[0] + (f" + {len(names) - 1} more" if len(names) > 1 else "")
