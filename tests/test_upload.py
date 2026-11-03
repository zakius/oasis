import json
import subprocess
import time

import mutagen
import pytest

from conftest import PNG, call, make_client
from oasis import accounts, importer, musicbrainz
from oasis.db import connect
from test_importer import edition

AUTH = {"u": "admin", "p": "secret", "f": "json"}


def encode(path, seconds=2, *more):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=duration={seconds}", *more, str(path)], check=True)
    return path.read_bytes()


@pytest.fixture
def root(tmp_path):
    (tmp_path / "music").mkdir()
    return tmp_path / "music"


@pytest.fixture
def client(root, tmp_path, monkeypatch):
    (tmp_path / "caa").mkdir()
    monkeypatch.setattr(importer, "COVER_ART", (tmp_path / "caa").as_uri() + "/{}.png")  # the Cover Art Archive, a file per release
    with make_client(root, tmp_path / "oasis.db") as c:
        yield c


def send(client, upload, number, data, offset=0, size=None, suffix="flac", disc=1, **auth):
    return client.post("/rest/uploadTrack", params={**AUTH, **auth, "uploadId": upload, "disc": disc, "number": number, "offset": offset,
                                                    "size": len(data) + offset if size is None else size, "suffix": suffix},
                       content=data).json()["subsonic-response"]


def tags(*titles, album="First", artist="Beta"):
    return {"album": album, "albumArtist": artist, "year": 2001, "genre": "Rock",
            "track": [{"disc": 1, "number": n, "title": t} for n, t in enumerate(titles, 1)]}


def upload(client, tmp_path, given, suffix="flac", **commit):
    """Creates an upload of the tags, sends a file per track and commits it."""
    made = call(client, "createUpload", tags=json.dumps(given))["upload"]
    for t in made["track"]:
        assert send(client, made["id"], t["number"], encode(tmp_path / f"src/{t['number']}.{suffix}"), suffix=suffix)["status"] == "ok"
    return call(client, "commitUpload", uploadId=made["id"], **commit)


def test_an_upload_with_the_clients_tags_lands_tagged_named_and_with_its_cover(client, root, tmp_path):
    made = call(client, "createUpload", tags='{"album": "First", "albumArtist": "Beta", "year": 2001, "genre": "Rock", "track": '
                                             '[{"disc": 1, "number": 1, "title": "One"}, {"disc": 1, "number": 2, "title": "Two: Live", "artist": "Beta & Gamma"}]}')
    assert made["status"] == "ok" and made["upload"]["track"] == [{"disc": 1, "number": 1}, {"disc": 1, "number": 2}] and made["upload"]["expires"]
    id = made["upload"]["id"]
    for n in (1, 2):
        assert send(client, id, n, encode(tmp_path / f"src/{n}.flac"))["status"] == "ok"
    assert client.post("/rest/uploadCover", params={**AUTH, "uploadId": id}, content=PNG).json()["subsonic-response"]["status"] == "ok"
    album = call(client, "commitUpload", uploadId=id)["album"]
    assert (album["name"], album["artist"], album["year"], album["songCount"]) == ("First", "Beta", 2001, 2)
    assert [(s["title"], s["artist"], s["track"], s["path"]) for s in album["song"]] == [
        ("One", "Beta", 1, "Beta/2001 - First/01 - One.flac"), ("Two: Live", "Beta & Gamma", 2, "Beta/2001 - First/02 - Two - Live.flac")]
    tagged = mutagen.File(root / "Beta/2001 - First/02 - Two - Live.flac")
    assert {k: tagged[k] for k in ("title", "artist", "albumartist", "album", "date", "genre", "tracknumber", "tracktotal", "discnumber")} == {
        "title": ["Two: Live"], "artist": ["Beta & Gamma"], "albumartist": ["Beta"], "album": ["First"], "date": ["2001"], "genre": ["Rock"],
        "tracknumber": ["2"], "tracktotal": ["2"], "discnumber": ["1"]}
    assert (root / "Beta/2001 - First/cover.png").read_bytes() == PNG
    assert client.get(f"/rest/getCoverArt", params={**AUTH, "id": album["id"]}).content == PNG
    assert call(client, "getUpload", id=id)["error"]["code"] == 70
    assert not (tmp_path / "uploads" / id).exists()


def test_a_musicbrainz_release_tags_one_of_its_discs_with_the_cover_art_archives_cover(client, root, tmp_path, monkeypatch):
    release = edition("rel-1", [("One", 2), ("Two", 2)], [("Three", 2)])
    asked = []
    monkeypatch.setattr(musicbrainz, "get", lambda url, *args: asked.append(url) or release)
    (tmp_path / "caa/rel-1.png").write_bytes(PNG)
    made = call(client, "createUpload", releaseId="rel-1", disc=2)["upload"]
    assert asked[0].startswith("https://musicbrainz.org/ws/2/release/rel-1?") and made["track"] == [{"disc": 2, "number": 1}]
    send(client, made["id"], 1, encode(tmp_path / "three.flac"), disc=2)
    album = call(client, "commitUpload", uploadId=made["id"])["album"]
    assert [(s["title"], s["discNumber"], s["path"]) for s in album["song"]] == [("Three", 2, "Beta/2001 - First/2-01 - Three.flac")]
    tagged = mutagen.File(root / "Beta/2001 - First/2-01 - Three.flac")
    assert (tagged["musicbrainz_albumid"], tagged["discnumber"], tagged["disctotal"], tagged["label"], tagged["releasecountry"]) == \
           (["rel-1"], ["2"], ["2"], ["Label"], ["XW"])
    assert (root / "Beta/2001 - First/cover.png").read_bytes() == PNG
    assert call(client, "createUpload", releaseId="rel-1", disc=3)["error"]["code"] == 70


def test_lookup_disc_lists_the_disc_ids_releases_first_with_the_disc_it_is(client, monkeypatch):
    exact = edition("exact", [("One", 2)], [("東京", 200), ("Two", 3)], title="東京")
    exact["media"][1]["discs"] = [{"id": "disc-1"}]
    fuzzy = edition("fuzzy", [("A", 2), ("B", 3)])
    asked = []
    monkeypatch.setattr(musicbrainz, "get", lambda url, *args: asked.append(url) or {"releases": [fuzzy, exact]})
    found = call(client, "lookupDisc", discId="disc-1", toc="1 2 30000 150 15000", romanization="both")["discLookup"]["release"]
    assert asked == ["https://musicbrainz.org/ws/2/discid/disc-1?inc=artist-credits+recordings+labels&cdstubs=no&fmt=json&toc=1+2+30000+150+15000"]
    assert [(r["id"], r["discNumber"], r["discCount"]) for r in found] == [("exact", 2, 2), ("fuzzy", 1, 1)]
    assert {k: v for k, v in found[0].items() if k != "track"} == {
        "id": "exact", "title": "東京", "titleLatin": "Tōkyō", "artist": "Beta", "year": 2001, "date": "2001-02-03", "country": "XW", "label": "Label",
        "catalogNumber": "CAT-1", "barcode": "123", "discCount": 2, "discNumber": 2}
    assert found[0]["track"][0] == {"number": 1, "title": "東京", "titleLatin": "Tōkyō", "artist": "Beta", "duration": 200, "musicBrainzId": "rec-東京"}
    monkeypatch.setattr(musicbrainz, "get", lambda url, *args: (_ for _ in ()).throw(LookupError(url)))
    assert call(client, "lookupDisc", discId="unknown")["discLookup"] == {"release": []}


def test_an_interrupted_track_resumes_from_what_was_received(client, root, tmp_path):
    made = call(client, "createUpload", tags='{"albumArtist": "Beta", "album": "", "track": [{"number": 1, "title": "Single"}]}')["upload"]
    data = encode(tmp_path / "one.flac")
    assert send(client, made["id"], 1, data[:1000], size=len(data))["uploadTrack"] == {"received": 1000}
    assert call(client, "getUpload", id=made["id"])["upload"]["track"] == [{"disc": 1, "number": 1, "received": 1000, "size": len(data)}]
    assert call(client, "commitUpload", uploadId=made["id"])["error"] == {"code": 0, "message": "Track 1-1 isn't complete"}
    wrong = send(client, made["id"], 1, data[2000:], offset=2000)
    assert wrong["error"]["code"] == 0 and "1000" in wrong["error"]["message"]
    assert send(client, made["id"], 1, data[1000:], offset=1000)["uploadTrack"] == {"received": len(data)}
    album = call(client, "commitUpload", uploadId=made["id"])["album"]
    assert [s["path"] for s in album["song"]] == ["Beta/Single/01 - Single.flac"]  # loose singles: an album of the track's name


def test_a_suffix_oasis_doesnt_take_and_content_of_another_format_are_refused(client, tmp_path):
    made = call(client, "createUpload", tags='{"albumArtist": "Beta", "album": "First", "track": [{"number": 1, "title": "One"}]}')["upload"]
    assert send(client, made["id"], 1, encode(tmp_path / "one.wav"), suffix="wav")["error"]["code"] == 0
    refused = send(client, made["id"], 1, encode(tmp_path / "one.mp3"), suffix="flac")
    assert refused["error"] == {"code": 0, "message": "Track 1-1 isn't a flac file"}
    assert call(client, "getUpload", id=made["id"])["upload"]["track"][0]["received"] == 0
    assert not list((tmp_path / "uploads" / made["id"]).glob("1-1.*"))
    assert send(client, made["id"], 2, b"x")["error"]["code"] == 70


def test_an_album_already_in_the_library_is_refused_unless_replaced(client, root, tmp_path):
    assert upload(client, tmp_path, tags("One"))["status"] == "ok"
    again = upload(client, tmp_path, tags("One"), suffix="mp3")
    assert again["error"] == {"code": 0, "message": "already in the library", "duplicate": True}
    assert sorted(p.name for p in (root / "Beta/2001 - First").iterdir()) == ["01 - One.flac"]
    album = upload(client, tmp_path, tags("One"), suffix="mp3", replace="true")["album"]
    assert [s["suffix"] for s in album["song"]] == ["mp3"]
    assert sorted(p.name for p in (root / "Beta/2001 - First").iterdir()) == ["01 - One.mp3"]


def test_a_lossless_upload_replaces_the_librarys_lossy_copy(client, root, tmp_path):
    upload(client, tmp_path, tags("One", "Two"), suffix="mp3")
    album = upload(client, tmp_path, tags("One", "Two"))["album"]
    assert [s["suffix"] for s in album["song"]] == ["flac", "flac"]
    assert sorted(p.name for p in (root / "Beta/2001 - First").iterdir()) == ["01 - One.flac", "02 - Two.flac"]
    assert upload(client, tmp_path, tags("One", "Two"), suffix="mp3")["error"]["message"] == "already in the library"


def test_an_upgrade_keeps_the_library_tracks_it_does_not_replace(client, root, tmp_path):
    upload(client, tmp_path, tags("One", "Two"), suffix="mp3")
    upload(client, tmp_path, tags("One"))
    assert sorted(p.name for p in (root / "Beta/2001 - First").iterdir()) == ["01 - One.flac", "02 - Two.mp3"]


def test_a_failed_placement_leaves_the_library_as_it_was_and_the_upload_for_a_retry(client, root, tmp_path, monkeypatch):
    upload(client, tmp_path, tags("One", "Two"), suffix="mp3")
    made = call(client, "createUpload", tags=json.dumps(tags("One", "Two")))["upload"]
    for n in (1, 2):
        send(client, made["id"], n, encode(tmp_path / f"src/{n}.flac"))
    real, copies = importer.copy, []

    def failing(source, target, over=False):  # the second file into the library fails
        if root in target.parents:
            if copies:
                raise OSError("disk full")
            copies.append(target)
        real(source, target, over)
    monkeypatch.setattr("oasis.upload.copy", failing)
    assert call(client, "commitUpload", uploadId=made["id"])["error"]["message"].startswith("Couldn't place the upload")
    assert sorted(p.name for p in (root / "Beta/2001 - First").iterdir()) == ["01 - One.mp3", "02 - Two.mp3"]
    monkeypatch.setattr("oasis.upload.copy", real)
    assert [s["suffix"] for s in call(client, "commitUpload", uploadId=made["id"])["album"]["song"]] == ["flac", "flac"]


def test_cancel_deletes_the_uploads_files_and_state(client, tmp_path):
    made = call(client, "createUpload", tags='{"albumArtist": "Beta", "album": "First", "track": [{"number": 1, "title": "One"}]}')["upload"]
    send(client, made["id"], 1, encode(tmp_path / "one.flac"))
    assert (tmp_path / "uploads" / made["id"]).exists()
    assert call(client, "cancelUpload", uploadId=made["id"])["status"] == "ok"
    assert not (tmp_path / "uploads" / made["id"]).exists()
    assert call(client, "getUpload", id=made["id"])["error"]["code"] == 70


def test_only_admins_have_upload_role_and_may_upload(client, tmp_path):
    db = connect(tmp_path / "oasis.db")
    accounts.create(db, "guest", "pw")
    guest = {"u": "guest", "p": "pw"}
    assert call(client, "getUser", username="guest")["user"]["uploadRole"] is False
    assert call(client, "getUser")["user"]["uploadRole"] is True
    for method in ("lookupDisc", "createUpload", "getUpload", "cancelUpload", "commitUpload"):
        assert call(client, method, **guest)["error"]["code"] == 50
    made = call(client, "createUpload", tags='{"albumArtist": "Beta", "album": "First", "track": [{"number": 1, "title": "One"}]}')["upload"]
    assert send(client, made["id"], 1, b"fLaC", **guest)["error"]["code"] == 50


def test_uploads_abandoned_a_day_ago_are_deleted(client, tmp_path):
    made = [call(client, "createUpload", tags='{"albumArtist": "Beta", "album": "First", "track": [{"number": 1, "title": "One"}]}')["upload"]
            for _ in range(2)]
    for m in made:
        send(client, m["id"], 1, b"fLaC", size=100)
    db = connect(tmp_path / "oasis.db")
    db.execute("update upload set touched = ? where id = ?", (time.time() - 25 * 3600, made[0]["id"]))
    client.app.state.api.recommender.chores[0](db)
    assert not (tmp_path / "uploads" / made[0]["id"]).exists() and (tmp_path / "uploads" / made[1]["id"]).exists()
    assert [r[0] for r in db.execute("select id from upload")] == [made[1]["id"]]
    assert db.execute("select count(*) from upload_track").fetchone()[0] == 1


def test_the_extension_is_advertised_and_other_requests_are_as_they_were(client):
    extensions = {e["name"]: e["versions"] for e in call(client, "getOpenSubsonicExtensions")["openSubsonicExtensions"]}
    assert extensions == {"formPost": [1], "songLyrics": [1, 2], "transcodeOffset": [1], "romanization": [1], "upload": [1], "shareUrl": [1], "oasis": [1, 2, 3]}
    form = client.post("/rest/getUser", data={**AUTH, "username": "admin"}).json()["subsonic-response"]
    assert form["status"] == "ok" and form["user"]["username"] == "admin"
    assert client.post("/rest/getUser", params={"f": "json"}, data={**AUTH}).json()["subsonic-response"]["user"]["adminRole"] is True


def test_loose_singles_each_land_as_a_single_of_their_own(client, root, tmp_path):
    given = {"album": "", "albumArtist": "Beta", "year": "2001", "track": [{"number": 1, "title": "One"}, {"number": 2, "title": "Two"}]}
    result = upload(client, tmp_path, given)
    assert [(a["name"], a["songCount"]) for a in result["albums"]] == [("One", 1), ("Two", 1)] and result["album"] == result["albums"][0]
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*.flac")) == ["Beta/2001 - One/01 - One.flac", "Beta/2001 - Two/01 - Two.flac"]
    assert mutagen.File(root / "Beta/2001 - Two/01 - Two.flac")["tracknumber"] == ["1"]
    assert upload(client, tmp_path, given, suffix="mp3")["error"]["duplicate"] is True


def test_a_tracks_artists_array_wins_and_the_year_may_be_a_numeric_string(client, root, tmp_path):
    given = {**tags("One"), "year": "2001", "albumArtist": None}
    given["track"][0]["artists"] = ["Beta", "Gamma"]
    album = upload(client, tmp_path, given)["album"]
    assert (album["artist"], album["year"]) == ("Beta, Gamma", 2001)
    assert mutagen.File(root / "Beta, Gamma/2001 - First/01 - One.flac")["artists"] == ["Beta", "Gamma"]
    assert call(client, "createUpload", tags=json.dumps({**tags("One"), "year": "MMI"}))["error"]["message"] == "year must be a number"


def test_a_cover_that_cant_be_added_leaves_the_upload_committed(client, root, tmp_path, monkeypatch):
    def failing(*args):
        raise OSError("read-only cover")
    monkeypatch.setattr(client.app.state.api.uploads.importer, "cover", failing)
    made = call(client, "createUpload", tags=json.dumps(tags("One")))["upload"]
    send(client, made["id"], 1, encode(tmp_path / "one.flac"))
    assert call(client, "commitUpload", uploadId=made["id"])["album"]["songCount"] == 1
    assert [p.name for p in (root / "Beta/2001 - First").iterdir()] == ["01 - One.flac"] and not (tmp_path / "uploads" / made["id"]).exists()


def test_a_two_disc_upgrade_replaces_the_lossy_copy_on_each_disc(client, root, tmp_path):
    def two_discs(suffix, second):
        made = call(client, "createUpload", tags=json.dumps({"album": "First", "albumArtist": "Beta", "year": 2001, "track": [
            {"disc": 1, "number": 1, "title": "One"}, {"disc": 2, "number": 1, "title": second}]}))["upload"]
        for d in (1, 2):
            send(client, made["id"], 1, encode(tmp_path / f"src/{d}.{suffix}"), suffix=suffix, disc=d)
        return call(client, "commitUpload", uploadId=made["id"])
    two_discs("mp3", "Three")
    assert [s["suffix"] for s in two_discs("flac", "Three (Remaster)")["album"]["song"]] == ["flac", "flac"]
    assert sorted(p.name for p in (root / "Beta/2001 - First").iterdir()) == ["1-01 - One.flac", "2-01 - Three (Remaster).flac"]


def test_a_track_too_big_or_without_room_for_it_is_refused(client, tmp_path, monkeypatch):
    import shutil
    from types import SimpleNamespace
    made = call(client, "createUpload", tags=json.dumps(tags("One")))["upload"]
    assert "at most" in send(client, made["id"], 1, b"x", size=(4 << 30) + 1)["error"]["message"]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=99))
    assert send(client, made["id"], 1, b"x", size=100)["error"] == {"code": 0, "message": "Too little disk space: 100 bytes needed, 99 free"}
    assert call(client, "getUpload", id=made["id"])["upload"]["track"][0]["received"] == 0


def test_a_file_being_received_or_an_upload_being_committed_takes_no_other_request_for_it(client, tmp_path):
    uploads = client.app.state.api.uploads
    made = call(client, "createUpload", tags=json.dumps(tags("One", "Two")))["upload"]
    data = encode(tmp_path / "one.flac")
    uploads.busy.add((made["id"], (1, 1)))
    assert send(client, made["id"], 1, data)["error"]["message"] == "Another request is sending this file or committing the upload"
    assert send(client, made["id"], 2, data)["status"] == "ok"  # another track goes meanwhile
    assert call(client, "commitUpload", uploadId=made["id"])["error"]["message"] == "Another request is sending this file or committing the upload"
    uploads.busy.clear()
    uploads.busy.add((made["id"], "commit"))
    assert send(client, made["id"], 1, data)["error"]["code"] == 0
    assert client.post("/rest/uploadCover", params={**AUTH, "uploadId": made["id"]}, content=PNG).json()["subsonic-response"]["error"]["code"] == 0
    uploads.busy.clear()
    assert send(client, made["id"], 1, data)["status"] == "ok"


def test_a_cover_over_the_cap_is_refused(client, tmp_path, monkeypatch):
    monkeypatch.setattr("oasis.upload.MAX_COVER", len(PNG) - 1)
    made = call(client, "createUpload", tags=json.dumps(tags("One")))["upload"]
    refused = client.post("/rest/uploadCover", params={**AUTH, "uploadId": made["id"]}, content=PNG).json()["subsonic-response"]
    assert refused["error"]["code"] == 0 and "at most" in refused["error"]["message"] and not list((tmp_path / "uploads" / made["id"]).iterdir())


def test_names_that_clean_to_nothing_land_in_a_folder_of_their_own(client, root, tmp_path):
    upload(client, tmp_path, {**tags("One", album=".."), "albumArtist": "..", "year": None})
    assert [str(p.relative_to(root)) for p in root.rglob("*.flac")] == ["Unknown Artist/Unknown Album/01 - One.flac"]


def test_bad_tags_and_numbers_are_refused_not_failed(client, tmp_path):
    for given in ({**tags("One"), "album": "Fir\0st"}, {**tags("One"), "albumArtist": ["Beta"]}, {**tags("One"), "track": [{"number": 1, "title": 5}]},
                  {**tags("One"), "track": [{"number": 1, "title": "One", "artists": ["Beta", None]}]}, {**tags("One"), "track": ["One"]}, ["One"]):
        assert call(client, "createUpload", tags=json.dumps(given))["error"]["code"] == 0, given
    assert call(client, "createUpload", releaseId="rel-1", disc="two")["error"] == {"code": 0, "message": "disc must be an integer"}
    made = call(client, "createUpload", tags=json.dumps(tags("One")))["upload"]
    assert send(client, made["id"], 1, b"x", offset="x", size=1)["error"] == {"code": 0, "message": "disc, number, offset and size must be integers"}


def test_cleaning_spares_an_upload_with_a_request_in_progress(client, tmp_path):
    uploads = client.app.state.api.uploads
    made = [call(client, "createUpload", tags=json.dumps(tags("One")))["upload"]["id"] for _ in range(2)]
    db = connect(tmp_path / "oasis.db")
    db.execute("update upload set touched = ?", (time.time() - 25 * 3600,))
    uploads.busy.add((made[0], (1, 1)))
    uploads.clean(db)
    assert sorted(r[0] for r in db.execute("select id from upload")) == [made[0]]
    uploads.busy.clear()
    uploads.clean(db)
    assert not db.execute("select id from upload").fetchall()


def test_a_lossy_file_that_doesnt_decode_is_refused(client, tmp_path):
    made = call(client, "createUpload", tags=json.dumps(tags("One")))["upload"]
    data = bytearray(encode(tmp_path / "one.mp3", 5))
    data[len(data) // 3:len(data) // 3 + 3000] = bytes(3000)  # a header intact, frames gone
    assert send(client, made["id"], 1, bytes(data), suffix="mp3")["status"] == "ok"
    assert call(client, "commitUpload", uploadId=made["id"])["error"] == {"code": 0, "message": "Track 1-1 doesn't decode"}
