import io
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from extmusic import fast_yt, ytdlp, ytmusic
from extmusic.service import ExtMusicService
from extmusic.store import ExtMusicStore
from extmusic.ytdlp import YTDLPError
from extmusic.ytmusic import YTMusicError, parse_duration, track_from_item


def _text_column(text):
    return {
        "musicResponsiveListItemFlexColumnRenderer": {
            "text": {"runs": [{"text": text}]}
        }
    }


def _song(video_id, title, parts):
    runs = []
    for index, part in enumerate(parts):
        if index:
            runs.append({"text": " • "})
        runs.append({"text": part})
    return {
        "musicResponsiveListItemRenderer": {
            "playlistItemData": {"videoId": video_id},
            "flexColumns": [
                _text_column(title),
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": runs}}},
            ],
            "thumbnail": {
                "musicThumbnailRenderer": {
                    "thumbnail": {"thumbnails": [{"url": f"https://i.ytimg.com/vi/{video_id}/hq.jpg"}]}
                }
            },
        }
    }


def test_parse_duration():
    assert parse_duration("3:08") == 188
    assert parse_duration("1:02:03") == 3723
    assert parse_duration("bad") == 0
    assert parse_duration("") == 0


def test_track_from_item_maps_yt_music_fields():
    track = track_from_item(_song("abc123", "Numb", ["Linkin Park", "Meteora", "3:08"])["musicResponsiveListItemRenderer"])
    assert track == {
        "source": "yt",
        "id": "abc123",
        "title": "Numb",
        "artist": "Linkin Park",
        "album": "Meteora",
        "duration": 188,
        "thumb": "https://i.ytimg.com/vi/abc123/hq.jpg",
        "url": "",
    }


def test_track_from_item_without_album():
    track = track_from_item(_song("abc123", "Numb", ["Linkin Park", "3:08"])["musicResponsiveListItemRenderer"])
    assert track["album"] == ""
    assert track["duration"] == 188


def test_search_deduplicates_and_follows_continuation(monkeypatch):
    calls = []
    first = {
        "contents": {
            "musicShelfRenderer": {
                "contents": [_song("a", "One", ["Artist", "Album", "1:00"])],
                "continuations": [{"nextContinuationData": {"continuation": "next-token"}}],
            }
        }
    }
    second = {
        "contents": {
            "musicShelfRenderer": {
                "contents": [
                    _song("a", "One", ["Artist", "Album", "1:00"]),
                    _song("b", "Two", ["Artist", "Album", "2:00"]),
                ]
            }
        }
    }

    def fake_post(body):
        calls.append(body)
        return first if len(calls) == 1 else second

    monkeypatch.setattr(ytmusic, "_post", fake_post)
    tracks = ytmusic.search("test", count=10)
    assert [track["id"] for track in tracks] == ["a", "b"]
    assert calls[0]["params"] == ytmusic.SONGS_PARAMS
    assert calls[1]["continuation"] == "next-token"


def test_search_requires_query():
    with pytest.raises(YTMusicError):
        ytmusic.search("x")


def test_ytdlp_resolve_uses_bundled_zipapp(monkeypatch):
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(ytdlp, "path", lambda: type("P", (), {"__str__": lambda self: "device/ytmusic/yt-dlp"})())
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        payload = {
            "url": "https://rr.example/audio.m4a",
            "ext": "m4a",
            "acodec": "mp4a.40.2",
            "abr": 129.6,
            "duration": 188,
            "title": "Numb",
            "uploader": "Linkin Park",
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    info = ytdlp.resolve("abc123")
    assert info["url"] == "https://rr.example/audio.m4a"
    assert info["ext"] == "m4a"
    command = seen["command"]
    assert "--dump-single-json" in command
    assert command[-1] == "https://music.youtube.com/watch?v=abc123"
    assert "bestaudio[ext=m4a]" in command[command.index("-f") + 1]


def test_ytdlp_fast_resolve_uses_get_url(monkeypatch):
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(ytdlp, "path", lambda: Path("yt-dlp"))
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "https://rr.example/fast.m4a\n", "")

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    info = ytdlp.resolve("abc123", fast=True)
    assert info["url"] == "https://rr.example/fast.m4a"
    assert "--get-url" in seen["command"]
    assert "--dump-single-json" not in seen["command"]


def test_ytdlp_friendly_bot_error(monkeypatch):
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(ytdlp, "path", lambda: type("P", (), {"__str__": lambda self: "yt-dlp"})())

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "ERROR: [youtube] abc: Sign in to confirm you're not a bot"
        )

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    with pytest.raises(YTDLPError) as info:
        ytdlp.resolve("abc123")
    assert "подтверждение" in str(info.value)


def test_store_source_roundtrip(tmp_path):
    store = ExtMusicStore(tmp_path / "extmusic.json")
    assert store.source() == "yt"
    store.save_source("vk")
    assert store.source() == "vk"
    store.save_source("yt")
    assert store.source() == "yt"
    with pytest.raises(ValueError):
        store.save_source("unknown")


def test_ytdlp_library_uses_cookies(tmp_path, monkeypatch):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n", encoding="utf-8")
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(ytdlp, "path", lambda: Path("yt-dlp"))
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        payload = {
            "entries": [{
                "id": "abc",
                "title": "Song",
                "uploader": "Artist",
                "duration": 10,
                "thumbnails": [{"url": "https://i.ytimg.com/vi/abc/hq.jpg"}],
            }]
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(ytdlp.subprocess, "run", fake_run)
    before = cookie.read_text(encoding="utf-8")
    tracks = ytdlp.library(str(cookie), limit=5)
    assert tracks[0]["id"] == "abc"
    assert tracks[0]["artist"] == "Artist"
    assert "--cookies" in seen["command"]
    assert seen["command"][-1].endswith("list=LM")
    cookie_path = seen["command"][seen["command"].index("--cookies") + 1]
    assert cookie_path != str(cookie)
    assert cookie.read_text(encoding="utf-8") == before


def test_store_cookies_are_private_and_validated(tmp_path):
    import stat

    store = ExtMusicStore(tmp_path / "extmusic.json")
    with pytest.raises(ValueError):
        store.save_cookies("not a cookie file")
    assert not store.has_cookies()
    with pytest.raises(ValueError):
        store.save_cookies(
            "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tpublic"
        )
    assert not store.has_auth_cookies()
    store.save_cookies("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret")
    assert store.has_cookies()
    assert store.has_auth_cookies()
    import os
    if os.name != "nt":
        assert stat.S_IMODE(store.cookies_path().stat().st_mode) == 0o600
    store.clear_cookies()
    assert not store.has_cookies()
    assert not store.has_auth_cookies()


class FakePlayer:
    def __init__(self):
        self.played = ""

    def play(self, url):
        self.played = url
        return {"state": "playing"}

    def status(self):
        return {"state": "playing", "position": 1, "duration": 10, "playing": True}

    def stop(self):
        return {"state": "idle"}


class FakePanel:
    glagol = None

    @staticmethod
    def music_cmd(data):
        return {"ok": True}


def test_service_searches_youtube_and_plays_it(tmp_path, monkeypatch):
    service = ExtMusicService(FakePanel())
    service.store = ExtMusicStore(tmp_path / "extmusic.json")
    service.player = FakePlayer()
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(
        ytmusic,
        "search",
        lambda query, count=30: [{
            "source": "yt", "id": "abc", "title": "Song", "artist": "Artist",
            "album": "Album", "duration": 10, "thumb": "", "url": "",
        }],
    )
    monkeypatch.setattr(
        ytdlp,
        "resolve",
        lambda video_id, **kwargs: {"url": "https://rr.example/audio.m4a", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 128, "duration": 10},
    )
    found = service.search("query", 10, "yt")
    assert found["tracks"][0]["source"] == "yt"
    result = service.play(found["tracks"][0], found["tracks"])
    assert result["source"] == "yt"
    assert result["current"]["url"] == "https://rr.example/audio.m4a"
    assert service.player.played == "https://rr.example/audio.m4a"
    assert len(result["queue"]) == 1


def test_service_prefetches_next_track(tmp_path, monkeypatch):
    service = ExtMusicService(FakePanel())
    service.store = ExtMusicStore(tmp_path / "extmusic.json")
    service.player = FakePlayer()
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    monkeypatch.setattr(fast_yt, "fast_resolve", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")))
    one = {"source": "yt", "id": "one", "title": "One", "artist": "A", "duration": 10, "thumb": "", "url": ""}
    two = {"source": "yt", "id": "two", "title": "Two", "artist": "B", "duration": 10, "thumb": "", "url": ""}
    calls = []

    def fake_resolve(video_id, timeout=90, fast=False):
        calls.append(video_id)
        return {"url": f"https://rr.example/{video_id}.m4a", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 128, "duration": 10}

    monkeypatch.setattr(ytdlp, "resolve", fake_resolve)
    service.play(one, [one, two])
    for _ in range(20):
        if service._prefetch_get("two"):
            break
        __import__("time").sleep(0.02)
    cached = service._prefetch_get("two")
    assert cached and cached["url"] == "https://rr.example/two.m4a"
    before = len(calls)
    service.next()
    assert len(calls) == before


def test_service_next_skips_unavailable_track(tmp_path, monkeypatch):
    service = ExtMusicService(FakePanel())
    service.store = ExtMusicStore(tmp_path / "extmusic.json")
    service.player = FakePlayer()
    monkeypatch.setattr(ytdlp, "available", lambda: True)
    good1 = {"source": "yt", "id": "good1", "title": "One", "artist": "A", "duration": 10, "thumb": "", "url": ""}
    bad = {"source": "yt", "id": "bad", "title": "Bad", "artist": "B", "duration": 10, "thumb": "", "url": ""}
    good2 = {"source": "yt", "id": "good2", "title": "Two", "artist": "C", "duration": 10, "thumb": "", "url": ""}
    service.current = good1
    service.queue = [good1, bad, good2]
    service.index = 0
    service._prefetch_after = lambda *args: None
    calls = []

    def fake_resolve(video_id, **kwargs):
        calls.append(video_id)
        if video_id == "bad":
            raise YTDLPError("Трек недоступен в YouTube")
        return {"url": f"https://rr.example/{video_id}.m4a", "ext": "m4a", "acodec": "mp4a.40.2", "abr": 128, "duration": 10}

    monkeypatch.setattr(ytdlp, "resolve", fake_resolve)
    result = service.next()
    assert result["current"]["id"] == "good2"
    assert result["index"] == 2
    assert result["skipped"][0]["id"] == "bad"
    assert calls == ["bad", "good2"]


def test_store_recent_yt_dedupes_and_caps(tmp_path):
    store = ExtMusicStore(tmp_path / "ext.json")
    assert store.recent_yt() == []
    for i in range(35):
        store.push_recent_yt(
            {
                "source": "yt",
                "id": f"v{i}",
                "title": f"S{i}",
                "artist": "A",
                "thumb": "https://i.ytimg.com/vi/x/hq.jpg",
                "url": "https://rr.example/ephemeral.m4a",
            },
            limit=30,
        )
    store.push_recent_yt({"source": "yt", "id": "v5", "title": "S5", "artist": "A", "artist_id": "UC123"}, limit=30)
    rec = store.recent_yt()
    assert len(rec) == 30
    assert rec[0]["id"] == "v5"
    assert rec[0]["artist_id"] == "UC123"
    assert "url" not in rec[0]
    assert sum(1 for x in rec if x["id"] == "v5") == 1


def test_track_from_item_extracts_browse_ids_only_when_present():
    item = {
        "playlistItemData": {"videoId": "v1"},
        "flexColumns": [
            _text_column("Numb"),
            {
                "musicResponsiveListItemFlexColumnRenderer": {
                    "text": {
                        "runs": [
                            {
                                "text": "Linkin Park",
                                "navigationEndpoint": {
                                    "browseEndpoint": {
                                        "browseId": "UCxgN32UVVztKAQd2HkXzBtw",
                                        "browseEndpointContextSupportedConfigs": {
                                            "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}
                                        },
                                    }
                                },
                            },
                            {"text": " • "},
                            {
                                "text": "Meteora",
                                "navigationEndpoint": {
                                    "browseEndpoint": {
                                        "browseId": "MPREb_123",
                                        "browseEndpointContextSupportedConfigs": {
                                            "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}
                                        },
                                    }
                                },
                            },
                            {"text": " • "},
                            {"text": "3:08"},
                        ]
                    }
                }
            },
        ],
    }
    track = track_from_item(item)
    assert track["artist_id"] == "UCxgN32UVVztKAQd2HkXzBtw"
    assert track["album_id"] == "MPREb_123"
    assert track["duration"] == 188


def test_search_with_kind_filters_and_card_types(monkeypatch):
    seen = []

    def fake_post(body, endpoint="search"):
        seen.append((endpoint, body))
        p = body.get("params")
        if p == ytmusic.SEARCH_PARAMS["albums"]:
            return {
                "contents": {
                    "musicShelfRenderer": {
                        "contents": [
                            {
                                "musicResponsiveListItemRenderer": {
                                    "flexColumns": [
                                        _text_column("Meteora"),
                                        _text_column("Альбом • Linkin Park • 2003"),
                                    ],
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseId": "MPREb_123",
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}
                                            },
                                        }
                                    },
                                    "thumbnail": {
                                        "musicThumbnailRenderer": {
                                            "thumbnail": {"thumbnails": [{"url": "https://lh3.googleusercontent.com/alb"}]}
                                        }
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        if p == ytmusic.SEARCH_PARAMS["artists"]:
            return {
                "contents": {
                    "musicShelfRenderer": {
                        "contents": [
                            {
                                "musicResponsiveListItemRenderer": {
                                    "flexColumns": [
                                        _text_column("Linkin Park"),
                                        _text_column("Исполнитель • 179 млн слушателей"),
                                    ],
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseId": "UCxgN32UVVztKAQd2HkXzBtw",
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}
                                            },
                                        }
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        if p == ytmusic.SEARCH_PARAMS["playlists"]:
            return {
                "contents": {
                    "musicShelfRenderer": {
                        "contents": [
                            {
                                "musicResponsiveListItemRenderer": {
                                    "flexColumns": [
                                        _text_column("Rock Hits"),
                                        _text_column("Плейлист • User • 50 треков"),
                                    ],
                                    "navigationEndpoint": {
                                        "browseEndpoint": {
                                            "browseId": "VLPLrock1",
                                            "browseEndpointContextSupportedConfigs": {
                                                "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_PLAYLIST"}
                                            },
                                        }
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        return {"contents": {"musicShelfRenderer": {"contents": [_song("vid99", "Clip", ["Band", "1M views", "4:05"])]}}}

    monkeypatch.setattr(ytmusic, "_post", fake_post)
    res_alb = ytmusic.search("Meteora", count=10, kind="albums")
    assert seen[-1][1]["params"] == ytmusic.SEARCH_PARAMS["albums"]
    assert res_alb[0]["kind"] == "album"
    assert res_alb[0]["browse_id"] == "MPREb_123"
    assert res_alb[0]["artist"] == "Linkin Park"
    assert res_alb[0]["year"] == "2003"

    res_art = ytmusic.search("Linkin Park", count=10, kind="artists")
    assert seen[-1][1]["params"] == ytmusic.SEARCH_PARAMS["artists"]
    assert res_art[0]["kind"] == "artist"
    assert res_art[0]["browse_id"] == "UCxgN32UVVztKAQd2HkXzBtw"

    res_pl = ytmusic.search("Rock", count=10, kind="playlists")
    assert seen[-1][1]["params"] == ytmusic.SEARCH_PARAMS["playlists"]
    assert res_pl[0]["kind"] == "playlist"
    assert res_pl[0]["playlist_id"] == "PLrock1"

    res_vid = ytmusic.search("Clip", count=10, kind="videos")
    assert seen[-1][1]["params"] == ytmusic.SEARCH_PARAMS["videos"]
    assert res_vid[0]["id"] == "vid99" and res_vid[0]["duration"] == 245


def test_home_explore_and_charts(monkeypatch):
    def fake_post(body, endpoint="browse"):
        assert endpoint == "browse"
        bid = body.get("browseId")
        if bid == "FEmusic_home":
            return {
                "contents": {
                    "sectionListRenderer": {
                        "contents": [
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Быстрый выбор"}]}}},
                                    "contents": [
                                        _song("h1", "Home Song", ["Art", "Alb", "2:30"]),
                                        {
                                            "musicTwoRowItemRenderer": {
                                                "title": {"runs": [{"text": "Top Mix"}]},
                                                "subtitle": {"runs": [{"text": "Playlist"}]},
                                                "thumbnailRenderer": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://lh3.googleusercontent.com/mix"}]}}},
                                                "navigationEndpoint": {"browseEndpoint": {"browseId": "VLRD1"}},
                                            }
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                },
                "header": {
                    "chipCloudRenderer": {
                        "chips": [
                            {
                                "chipCloudChipRenderer": {
                                    "text": {"runs": [{"text": "Энергия"}]},
                                    "navigationEndpoint": {"browseEndpoint": {"browseId": "FEmusic_home", "params": "p_energy"}},
                                }
                            }
                        ]
                    }
                },
            }
        if bid == "FEmusic_explore":
            return {
                "contents": {
                    "sectionListRenderer": {
                        "contents": [
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Новые альбомы"}]}}},
                                    "contents": [
                                        {
                                            "musicTwoRowItemRenderer": {
                                                "title": {"runs": [{"text": "New LP"}]},
                                                "subtitle": {"runs": [{"text": "Band • 2026"}]},
                                                "thumbnailRenderer": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://lh3.googleusercontent.com/rel"}]}}},
                                                "navigationEndpoint": {
                                                    "browseEndpoint": {
                                                        "browseId": "MPREb_new1",
                                                        "browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ALBUM"}},
                                                    }
                                                },
                                            }
                                        }
                                    ],
                                }
                            },
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Настроения"}]}}},
                                    "contents": [
                                        {
                                            "musicNavigationButtonRenderer": {
                                                "buttonText": {"runs": [{"text": "В дороге"}]},
                                                "clickCommand": {"browseEndpoint": {"browseId": "FEmusic_moods_and_genres_category", "params": "ggMood1"}},
                                            }
                                        }
                                    ],
                                }
                            },
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "В тренде"}]}}},
                                    "contents": [_song("tr1", "Trend", ["Singer", "3:00"])],
                                }
                            },
                        ]
                    }
                }
            }
        if bid == "FEmusic_charts":
            assert body["context"]["client"]["gl"] == "US"
            return {
                "contents": {
                    "sectionListRenderer": {
                        "contents": [
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Топ треков"}]}}},
                                    "contents": [_song("c1", "Chart Song", ["Star", "2:15"])],
                                }
                            },
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Топ исполнителей"}]}}},
                                    "contents": [
                                        {
                                            "musicResponsiveListItemRenderer": {
                                                "flexColumns": [_text_column("Star Artist"), _text_column("10M subscribers")],
                                                "navigationEndpoint": {"browseEndpoint": {"browseId": "UCstar1"}},
                                            }
                                        }
                                    ],
                                }
                            },
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Хит-парады видео"}]}}},
                                    "contents": [
                                        {
                                            "musicTwoRowItemRenderer": {
                                                "title": {"runs": [{"text": "Top Video"}]},
                                                "subtitle": {"runs": [{"text": "Star • 3:20"}]},
                                                "navigationEndpoint": {"watchEndpoint": {"videoId": "cv1"}},
                                            }
                                        }
                                    ],
                                }
                            },
                        ]
                    }
                }
            }
        raise AssertionError(f"unexpected browseId: {bid}")

    monkeypatch.setattr(ytmusic, "_post", fake_post)
    hm = ytmusic.home(limit=10)
    assert hm["tracks"][0]["id"] == "h1"
    assert len(hm["shelves"]) == 1
    assert hm["moods"][0]["title"] == "Энергия"

    ex = ytmusic.explore()
    assert ex["releases"][0]["browse_id"] == "MPREb_new1"
    assert ex["moods"][0]["params"] == "ggMood1"
    assert ex["tracks"][0]["id"] == "tr1"

    ch = ytmusic.charts(country="US")
    assert ch["tracks"][0]["id"] == "c1"
    assert ch["artists"][0]["browse_id"] == "UCstar1"
    assert ch["videos"][0]["id"] == "cv1"


def test_browse_mood_and_browse_collection(monkeypatch):
    def fake_post(body, endpoint="browse"):
        assert endpoint == "browse"
        bid = body.get("browseId")
        if bid == "FEmusic_moods_and_genres_category":
            return {
                "header": {"musicHeaderRenderer": {"title": {"runs": [{"text": "В дороге"}]}}},
                "contents": {
                    "musicCarouselShelfRenderer": {
                        "contents": [
                            {
                                "musicTwoRowItemRenderer": {
                                    "title": {"runs": [{"text": "Drive Mix"}]},
                                    "subtitle": {"runs": [{"text": "Playlist"}]},
                                    "navigationEndpoint": {"browseEndpoint": {"browseId": "VLPLdrive"}},
                                }
                            },
                            _song("m1", "Road Track", ["Driver", "3:12"]),
                        ]
                    }
                },
            }
        if bid == "MPREb_alb1":
            return {
                "header": {
                    "musicResponsiveHeaderRenderer": {
                        "title": {"runs": [{"text": "Hybrid Theory"}]},
                        "subtitle": {"runs": [{"text": "Альбом • 2000"}]},
                        "straplineTextOne": {
                            "runs": [{"text": "Linkin Park", "navigationEndpoint": {"browseEndpoint": {"browseId": "UClp1"}}}]
                        },
                        "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://lh3.googleusercontent.com/ht544"}]}}},
                        "buttons": [
                            {"musicPlayButtonRenderer": {"playNavigationEndpoint": {"watchPlaylistEndpoint": {"playlistId": "OLAK5uy_ht"}}}}
                        ],
                    }
                },
                "contents": {
                    "musicShelfRenderer": {
                        "contents": [
                            {
                                "musicResponsiveListItemRenderer": {
                                    "playlistItemData": {"videoId": "papercut1"},
                                    "flexColumns": [_text_column("Papercut"), _text_column("")],
                                    "fixedColumns": [{"musicResponsiveListItemFixedColumnRenderer": {"text": {"runs": [{"text": "3:05"}]}}}],
                                }
                            }
                        ]
                    }
                },
            }
        if bid == "UClp1":
            return {
                "header": {
                    "musicImmersiveHeaderRenderer": {
                        "title": {"runs": [{"text": "Linkin Park"}]},
                        "description": {"runs": [{"text": "Rock band"}]},
                        "monthlyListenerCount": {"runs": [{"text": "179 млн слушателей"}]},
                        "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://lh3.googleusercontent.com/lp_img"}]}}},
                    }
                },
                "contents": {
                    "sectionListRenderer": {
                        "contents": [
                            {"musicShelfRenderer": {"title": {"runs": [{"text": "Топ треков"}]}, "contents": [_song("t1", "In the End", ["Linkin Park", "3:36"])]}},
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Альбомы"}]}}},
                                    "contents": [
                                        {
                                            "musicTwoRowItemRenderer": {
                                                "title": {"runs": [{"text": "Meteora"}]},
                                                "subtitle": {"runs": [{"text": "Альбом • 2003"}]},
                                                "navigationEndpoint": {"browseEndpoint": {"browseId": "MPREb_met"}},
                                            }
                                        }
                                    ],
                                }
                            },
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Deep Cuts"}], "moreContentButton": None}}},
                                    "contents": [
                                        {
                                            "musicTwoRowItemRenderer": {
                                                "title": {"runs": [{"text": "Deep Mix"}]},
                                                "subtitle": {"runs": [{"text": "Плейлист • Joseph"}]},
                                                "navigationEndpoint": {"browseEndpoint": {"browseId": "VLPLdeep1"}},
                                            }
                                        }
                                    ],
                                }
                            },
                            {
                                "musicCarouselShelfRenderer": {
                                    "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Похожие исполнители"}]}}},
                                    "contents": [
                                        {
                                            "musicTwoRowItemRenderer": {
                                                "title": {"runs": [{"text": "Papa Roach"}]},
                                                "subtitle": {"runs": [{"text": "10M"}]},
                                                "navigationEndpoint": {"browseEndpoint": {"browseId": "UCpr1"}},
                                            }
                                        }
                                    ],
                                }
                            },
                        ]
                    }
                },
            }
        if bid == "VLPLlist1":
            return {
                "header": {"musicDetailHeaderRenderer": {"title": {"runs": [{"text": "My Playlist"}]}, "subtitle": {"runs": [{"text": "Playlist"}]}}},
                "contents": {"musicPlaylistShelfRenderer": {"contents": [_song("p1", "PL Track", ["Art", "2:00"])]}},
            }
        if bid == "VLPLjoseph":
            return {
                "header": {
                    "musicResponsiveHeaderRenderer": {
                        "title": {"runs": [{"text": "Joseph Playlist"}]},
                        "subtitle": {"runs": [{"text": "Плейлист • Joseph"}]},
                        "straplineTextOne": {"runs": [{"text": "Joseph"}]},
                    }
                },
                "contents": {
                    "musicPlaylistShelfRenderer": {
                        "contents": [_song("jp1", "Song 1", ["Original Artist", "Original Album", "2:30"])]
                    }
                },
            }
        if bid == "VLPLdeep":
            return {
                "header": {
                    "musicResponsiveHeaderRenderer": {
                        "title": {"runs": [{"text": "Deep Cuts"}]},
                        "subtitle": {"runs": [{"text": "Playlist • Deep Cuts"}]},
                    }
                },
                "contents": {
                    "musicPlaylistShelfRenderer": {
                        "contents": [_song("dc1", "Cut 1", ["Band", "1:45"])]
                    }
                },
            }
        raise AssertionError(f"unexpected bid: {bid}")

    monkeypatch.setattr(ytmusic, "_post", fake_post)

    mood = ytmusic.browse_mood("FEmusic_moods_and_genres_category", params="ggMood1")
    assert mood["title"] == "В дороге"
    assert mood["collections"][0]["browse_id"] == "VLPLdrive"
    assert mood["tracks"][0]["id"] == "m1"

    alb = ytmusic.browse_collection("MPREb_alb1")
    assert alb["kind"] == "album"
    assert alb["playlist_id"] == "OLAK5uy_ht"
    assert alb["tracks"][0]["album"] == "Hybrid Theory"
    assert alb["tracks"][0]["thumb"] == "https://lh3.googleusercontent.com/ht544"
    assert alb["tracks"][0]["artist"] == "Linkin Park"
    assert alb["tracks"][0]["artist_id"] == "UClp1"
    assert alb["tracks"][0]["duration"] == 185

    art = ytmusic.browse_collection("UClp1")
    assert art["kind"] == "artist"
    assert art["tracks"][0]["id"] == "t1"
    assert [a["browse_id"] for a in art["albums"]] == ["MPREb_met"]
    assert art["similar"][0]["browse_id"] == "UCpr1"

    pl = ytmusic.browse_collection("PLlist1")
    assert pl["kind"] == "playlist"
    assert pl["browse_id"] == "VLPLlist1"
    assert pl["playlist_id"] == "PLlist1"
    assert pl["tracks"][0]["id"] == "p1"

    pl_joseph = ytmusic.browse_collection("VLPLjoseph")
    assert pl_joseph["kind"] == "playlist"
    assert pl_joseph["tracks"][0]["artist"] == "Original Artist"
    assert pl_joseph["tracks"][0]["album"] == "Original Album"

    pl_deep = ytmusic.browse_collection("VLPLdeep")
    assert pl_deep["kind"] == "playlist"
    assert pl_deep["tracks"][0]["album"] == ""


def test_clean_track_meta_and_thumb_allowlist():
    t, a = ytmusic.clean_track_meta("Noize MC — Спящая красавица (Official Video) [4K]", "Noize MC")
    assert t == "Спящая красавица" and a == "Noize MC"
    t2, a2 = ytmusic.clean_track_meta("Linkin Park - Numb [Official Audio] (Lyrics) ft. Jay-Z", "YouTube Music")
    assert t2 == "Numb" and a2 == "Linkin Park"
    t3, a3 = ytmusic.clean_track_meta("In the End (Live at Texas)", "Linkin Park - Topic")
    assert t3 == "In the End" and a3 == "Linkin Park"

    assert ytmusic.is_allowed_thumb_url("https://i.ytimg.com/vi/abc/hq720.jpg") is True
    assert ytmusic.is_allowed_thumb_url("https://lh3.googleusercontent.com/xyz=w544") is True
    assert ytmusic.is_allowed_thumb_url("https://yt3.ggpht.com/abc=s200") is True
    assert ytmusic.is_allowed_thumb_url("https://music.youtube.com/img.png") is True
    assert ytmusic.is_allowed_thumb_url("https://img.youtube.com/vi/abc/0.jpg") is True
    assert (
        ytmusic.is_allowed_thumb_url(
            "https://www.gstatic.com/youtube/media/ytm/images/pbg/liked-songs-delhi-576.png"
        )
        is True
    )
    assert ytmusic.is_allowed_thumb_url("https://evil-gstatic.com/a.png") is False
    assert ytmusic.is_allowed_thumb_url("http://i.ytimg.com/vi/abc/hq.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://127.0.0.1/evil.png") is False
    assert ytmusic.is_allowed_thumb_url("https://evil.example.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://evil-ytimg.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://evil-youtube.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://тест.ytimg.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://user:pass@i.ytimg.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://i.ytimg.com@evil.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://evil.com\\@i.ytimg.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://i.ytimg.com:8443/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("https://.ytimg.com/a.jpg") is False
    assert ytmusic.is_allowed_thumb_url("") is False


def test_fetch_thumb_guards_url_redirects_and_size(monkeypatch):
    called = []

    def fail_if_called(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("network must not be called for disallowed URL")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)
    with pytest.raises(YTMusicError):
        ytmusic.fetch_thumb("http://i.ytimg.com/vi/abc/hq.jpg")
    with pytest.raises(YTMusicError):
        ytmusic.fetch_thumb("https://evil-ytimg.com/a.jpg")
    with pytest.raises(YTMusicError):
        ytmusic.fetch_thumb("https://169.254.169.254/latest/meta-data/")
    assert called == []

    class FakeResp:
        def __init__(self, data, ctype="image/jpeg", url="https://i.ytimg.com/vi/x/hq.jpg", status=200, loc=None):
            self._data = data
            self.headers = {"Content-Type": ctype}
            if loc:
                self.headers["Location"] = loc
            self._url = url
            self.status = status

        def geturl(self):
            return self._url

        def read(self, n=-1):
            return self._data if n is None or n < 0 else self._data[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    # Valid fetch
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6: FakeResp(b"IMG", "image/webp; charset=binary"))
    body, ctype = ytmusic.fetch_thumb("https://i.ytimg.com/vi/x/hq.jpg")
    assert body == b"IMG" and ctype == "image/webp"

    # Oversized fetch
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6: FakeResp(b"x" * 20, "image/png"))
    with pytest.raises(YTMusicError):
        ytmusic.fetch_thumb("https://i.ytimg.com/vi/x/hq.jpg", max_bytes=10)

    # SVG image must be rejected
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6: FakeResp(b"<svg/>", "image/svg+xml"))
    with pytest.raises(YTMusicError):
        ytmusic.fetch_thumb("https://i.ytimg.com/vi/x/hq.jpg")

    # Unsafe redirect blocked via SafeThumbRedirectHandler
    handler = ytmusic._SafeThumbRedirectHandler()
    req = urllib.request.Request("https://i.ytimg.com/vi/x/hq.jpg")
    with pytest.raises(YTMusicError):
        handler.redirect_request(req, io.BytesIO(), 302, "Found", {}, "http://127.0.0.1/secret")
    with pytest.raises(YTMusicError):
        handler.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://evil-ytimg.com/x.jpg")

    # Unsafe redirect via mocked urlopen
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=6: FakeResp(b"secret", "image/jpeg", url="http://127.0.0.1/secret"),
    )
    with pytest.raises(YTMusicError):
        ytmusic.fetch_thumb("https://i.ytimg.com/vi/x/hq.jpg")


def test_radio_and_lyrics_from_next(monkeypatch):
    def fake_post(body, endpoint="browse"):
        if endpoint == "next":
            return {
                "contents": {
                    "singleColumnMusicWatchNextResultsRenderer": {
                        "tabbedRenderer": {
                            "watchNextTabbedResultsRenderer": {
                                "tabs": [
                                    {
                                        "tabRenderer": {
                                            "title": "Далее",
                                            "content": {
                                                "musicQueueRenderer": {
                                                    "content": {
                                                        "playlistPanelRenderer": {
                                                            "contents": [
                                                                {
                                                                    "playlistPanelVideoRenderer": {
                                                                        "videoId": "r1",
                                                                        "title": {"runs": [{"text": "R1"}]},
                                                                        "shortBylineText": {"runs": [{"text": "Art1"}]},
                                                                        "lengthText": {"runs": [{"text": "3:10"}]},
                                                                        "thumbnail": {
                                                                            "thumbnails": [
                                                                                {"url": "https://i.ytimg.com/vi/r1/hq.jpg"}
                                                                            ]
                                                                        },
                                                                    }
                                                                }
                                                            ]
                                                        }
                                                    }
                                                }
                                            },
                                        }
                                    },
                                    {"tabRenderer": {"title": "Текст", "endpoint": {"browseEndpoint": {"browseId": "MPLYt_1"}}}},
                                    {"tabRenderer": {"title": "Похожие", "endpoint": {"browseEndpoint": {"browseId": "MPTRt_1"}}}},
                                ]
                            }
                        }
                    }
                }
            }
        if body.get("browseId") == "MPLYt_1":
            return {
                "contents": {
                    "sectionListRenderer": {
                        "contents": [{"musicDescriptionShelfRenderer": {"description": {"runs": [{"text": "Line 1\nLine 2"}]}}}]
                    }
                }
            }
        raise AssertionError(f"unexpected browseId: {body.get('browseId')}")

    monkeypatch.setattr(ytmusic, "_post", fake_post)
    rad = ytmusic.radio("v0", limit=10)
    assert rad[0]["id"] == "r1" and rad[0]["duration"] == 190 and rad[0]["thumb"] == "https://i.ytimg.com/vi/r1/hq.jpg"
    ly = ytmusic.lyrics("v0")
    assert "Line 1" in ly["text"] and ly["source"] == "YouTube Music" and ly["sync"] is None

    # When no MPLYt tab exists, lyrics must not scrape MPTRt_1 description and must return empty result
    monkeypatch.setattr(
        ytmusic,
        "_post",
        lambda body, endpoint="browse": {
            "contents": {
                "singleColumnMusicWatchNextResultsRenderer": {
                    "tabbedRenderer": {
                        "watchNextTabbedResultsRenderer": {
                            "tabs": [{"tabRenderer": {"title": "Похожие", "endpoint": {"browseEndpoint": {"browseId": "MPTRt_1"}}}}]
                        }
                    }
                }
            }
        }
        if endpoint == "next"
        else (_ for _ in ()).throw(AssertionError("should not browse MPTRt_1")),
    )
    assert ytmusic.lyrics("v0") == {"sync": None, "text": None, "source": None}
    with pytest.raises(YTMusicError):
        ytmusic.browse_collection("../etc/passwd")


def test_iter_shelves_handles_null_header_fields():
    payload = {
        "musicCarouselShelfRenderer": {
            "header": {
                "musicCarouselShelfBasicHeaderRenderer": {
                    "title": {"runs": None},
                    "moreContentButton": None,
                }
            },
            "contents": [],
        }
    }
    shelves = list(ytmusic._iter_shelves(payload))
    assert shelves == [("", "", "", [])]


def test_store_concurrent_writes_preserve_all_keys(tmp_path):
    import threading

    store = ExtMusicStore(tmp_path / "ext.json")
    errors = []

    def push_recent():
        try:
            for i in range(25):
                store.push_recent_yt({"source": "yt", "id": f"v{i}", "title": f"T{i}", "artist": "A"}, limit=30)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def write_meta():
        try:
            for i in range(25):
                store.save_oauth({"access_token": f"tok{i}", "refresh_token": "ref"})
                store.save_source("yt")
                store.save_yt_cookie_account(f"acc{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=push_recent) for _ in range(2)] + [
        threading.Thread(target=write_meta) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = store.load()
    assert data.get("source") == "yt"
    assert isinstance(data.get("yt_oauth"), dict) and data["yt_oauth"].get("refresh_token") == "ref"
    assert len(store.recent_yt()) == 25
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.new")) == []


def test_charts_global_zz_uses_us_context_and_zz_selected_value(monkeypatch):
    seen_bodies = []

    def fake_post(body, endpoint="browse"):
        seen_bodies.append((endpoint, body))
        return {
            "contents": {
                "sectionListRenderer": {
                    "contents": [
                        {
                            "musicCarouselShelfRenderer": {
                                "header": {"musicCarouselShelfBasicHeaderRenderer": {"title": {"runs": [{"text": "Топ треков"}]}}},
                                "contents": [_song("z1", "Global Hit", ["Artist", "3:00"])],
                            }
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(ytmusic, "_post", fake_post)
    res = ytmusic.charts("ZZ")
    assert len(seen_bodies) == 1
    endpoint, body = seen_bodies[0]
    assert endpoint == "browse"
    assert body["browseId"] == "FEmusic_charts"
    assert body["context"]["client"]["gl"] == "US"
    assert body["context"]["client"]["hl"] == "ru"
    assert body["formData"] == {"selectedValues": ["ZZ"]}
    assert res["tracks"][0]["id"] == "z1"

    seen_bodies.clear()
    ytmusic.charts("ru")
    assert seen_bodies[0][1]["context"]["client"]["gl"] == "RU"
    assert seen_bodies[0][1]["formData"] == {"selectedValues": ["RU"]}


def test_post_retries_once_on_transient_gaierror(monkeypatch):
    attempts = []
    sleeps = []

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self, n=-1):
            return self._body if n is None or n < 0 else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=20):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise urllib.error.URLError(socket.gaierror(-2, "Name does not resolve"))
        return FakeResp(json.dumps({"ok": True, "contents": {}}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda secs: sleeps.append(secs))

    payload = ytmusic._post({"query": "test"}, endpoint="search")
    assert payload == {"ok": True, "contents": {}}
    assert len(attempts) == 2
    assert sleeps == [0.15]

    # Exhausting both attempts on direct socket.gaierror raises YTMusicError after 1 retry
    attempts.clear()
    sleeps.clear()

    def always_gaierror(req, timeout=20):
        attempts.append(req.full_url)
        raise socket.gaierror(-2, "Name does not resolve")

    monkeypatch.setattr(urllib.request, "urlopen", always_gaierror)
    with pytest.raises(YTMusicError):
        ytmusic._post({"query": "test"}, endpoint="search")
    assert len(attempts) == 2
    assert sleeps == [0.15]


def test_fetch_thumb_retries_once_on_transient_gaierror(monkeypatch):
    attempts = []
    sleeps = []

    class FakeResp:
        headers = {"Content-Type": "image/jpeg"}
        status = 200

        def geturl(self):
            return "https://i.ytimg.com/vi/abc/hq.jpg"

        def read(self, n=-1):
            return b"IMG"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=6):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise urllib.error.URLError(socket.gaierror(-2, "Name does not resolve"))
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda secs: sleeps.append(secs))

    body, ctype = ytmusic.fetch_thumb("https://i.ytimg.com/vi/abc/hq.jpg")
    assert body == b"IMG" and ctype == "image/jpeg"
    assert len(attempts) == 2
    assert sleeps == [0.15]
