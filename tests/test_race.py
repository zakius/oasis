"""Recommender.find asks every service at once and takes the best answer, waiting a grace for better ones."""
import logging
import time

import pytest

from oasis import recommend
from oasis.db import connect
from oasis.recommend import Recommender, rank

SLACK = 0.2  # seconds a timing may be late by on a busy machine


class Service:
    """A streaming service that answers after delay seconds, with the song or nothing."""

    def __init__(self, name, delay, has=True, suffix="mp3", bitrate=128, fails=False):
        self.name = self.title = name
        self.delay, self.has, self.suffix, self.bitrate, self.fails = delay, has, suffix, bitrate, fails
        self.prefix, self.content_type, self.asked = name[:2] + "-", "audio/mpeg", 0

    def find(self, artist, title):
        self.asked += 1
        time.sleep(self.delay)
        if self.fails:
            raise OSError("down")
        return {"id": "1", "title": title} if self.has else None


class Ytm(Service):
    def find(self, artist, title, duration=None):
        return super().find(artist, title)


class Clips(Service):
    def find(self, artist, title):
        return "7" if super().find(artist, title) else None


@pytest.fixture
def finder(tmp_path):
    connect(tmp_path / "oasis.db").close()  # made, as a server's is before it asks
    return Recommender(lambda: connect(tmp_path / "oasis.db"), None, lambda: None, None)


def timed(finder, *services, ytm=None, previews=(), deadline=None):
    """What find answers, the service that gave it, and how long it took."""
    start = time.monotonic()
    found = finder.find(ytm, list(services), "Artist", "Song", list(previews),
                        start + deadline if deadline else None)
    took = time.monotonic() - start
    return found, (found[0] if found and found[0] in ("ytm", "preview") else found and found[0].name), took


def test_answers_rank_lossless_then_lossy_by_bitrate_with_youtube_winning_ties_then_previews():
    flac, aac, mp3 = Service("q", 0, suffix="flac", bitrate=1411), Service("s", 0, bitrate=256), Service("b", 0, bitrate=128)
    answers = [("preview", "clips:1"), (mp3, {}), ("ytm", {}), (aac, {}), (flac, {}), (mp3, {"bitrate": 320})]
    assert sorted(answers, key=rank, reverse=True) == [(flac, {}), (mp3, {"bitrate": 320}), (aac, {}), ("ytm", {}), (mp3, {}), ("preview", "clips:1")]


def test_a_full_lossless_song_ends_the_wait_at_once(finder):
    # The lossy song at 0.5 s has a grace till 0.65 s, and the lossless one ends it at 0.55 s.
    found, by, took = timed(finder, Service("lossy", 0.5), Service("flac", 0.55, suffix="flac", bitrate=1411), Service("slow", 5, bitrate=320))
    assert by == "flac" and took < 0.55 + SLACK / 2


def test_a_better_answer_within_the_grace_wins_and_one_after_it_is_not_waited_for(finder):
    # The first answer comes at 0.5 s: a better one is waited for till 0.65 s.
    found, by, took = timed(finder, Service("first", 0.5), Service("better", 0.6, bitrate=256), Service("late", 3, suffix="flac", bitrate=1411))
    assert by == "better" and 0.6 + 0.3 * 0.6 - 0.05 < took < 0.6 + 0.3 * 0.6 + SLACK  # the grace starts again from 0.6 s
    found, by, took = timed(finder, Service("first2", 0.5), Service("better2", 0.9, bitrate=256))
    assert by == "first2" and took < 0.65 + SLACK


def test_the_grace_is_three_seconds_at_most_and_the_deadline_caps_it(finder, monkeypatch):
    monkeypatch.setattr(recommend, "GRACE_MOST", 0.1)
    found, by, took = timed(finder, Service("first", 0.5), Service("better", 0.7, bitrate=256))
    assert by == "first" and took < 0.6 + SLACK
    found, by, took = timed(finder, Service("first2", 0.5), Service("better2", 0.55, bitrate=256), deadline=0.52)
    assert by == "first2" and took < 0.52 + SLACK


def test_a_worse_answer_doesnt_start_the_grace_again_and_youtube_wins_a_tie(finder):
    found, by, took = timed(finder, Service("same", 0.55, bitrate=128), ytm=Ytm("ytm", 0.5))
    assert by == "ytm"
    found, by, took = timed(finder, Service("same2", 0.5, bitrate=128), ytm=Ytm("ytm2", 0.55))
    assert by == "ytm"  # came later, within the grace, and ties win for YouTube Music


def test_a_preview_counts_only_when_no_full_song_comes(finder):
    found, by, took = timed(finder, Service("none", 0.2, has=False), previews=[Clips("clips", 0.05)])
    assert found == ("preview", "clips:7")
    found, by, took = timed(finder, Service("full", 0.55), previews=[Clips("clips2", 0.5)])
    assert by == "full"
    found, by, took = timed(finder, Service("full2", 0.55), previews=[Clips("clips3", 0.05)])
    assert by == "preview"  # a preview's grace is a grace too


def test_answers_are_kept_with_the_time_they_took_misses_too(finder):
    first, better = Service("first", 0.5), Service("better", 0.6, bitrate=256)
    miss = Service("miss", 0.1, has=False)
    assert timed(finder, first, better, miss)[1] == "better"
    # Asked again with a service not asked before, which answers at 0.65 s: the kept answers count as coming at 0.5 and
    # 0.6 s, so the grace runs till 0.78 s and the new best answer is waited for. Kept at no time, there would be none.
    newcomer = Service("newcomer", 0.65, bitrate=320)
    found, by, took = timed(finder, first, better, miss, newcomer)
    assert by == "newcomer" and first.asked == better.asked == miss.asked == 1
    assert took < 0.65 + SLACK


def test_each_answer_is_logged_with_its_time(finder, caplog):
    caplog.set_level(logging.INFO, logger="oasis.recommend")
    timed(finder, Service("quick", 0.3), Service("empty", 0.1, has=False))
    timed(finder, Service("quick", 0.3))
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("Artist - Song: quick answered the song (128 kbit/s mp3) in 0.3") for line in lines)
    assert any(line.startswith("Artist - Song: empty answered nothing in 0.1") for line in lines)
    assert any(line.startswith("Artist - Song: quick answered the song (128 kbit/s mp3) in 0.3") and line.endswith("before") for line in lines)


def test_every_service_failing_is_an_error_and_the_deadline_with_no_answer_a_timeout(finder):
    with pytest.raises(OSError):
        timed(finder, Service("down", 0.05, fails=True), previews=[Clips("clips", 0.1)])
    with pytest.raises(TimeoutError):
        timed(finder, Service("slow", 2), deadline=0.2)
    assert timed(finder, Service("down2", 0.05, fails=True), Service("up", 0.1))[1] == "up"


def test_an_answer_one_worker_kept_serves_another(finder, tmp_path):
    other = Recommender(lambda: connect(tmp_path / "oasis.db"), None, lambda: None, None)  # another process's
    timed(finder, Service("quick", 0.3))
    found, by, took = timed(other, Service("quick", 0.3))
    assert by == "quick" and took < 0.1
