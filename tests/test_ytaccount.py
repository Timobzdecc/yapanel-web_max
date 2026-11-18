import io
import json
import urllib.error
import pytest

from extmusic.store import ExtMusicStore
from extmusic.ytaccount import YTAccount, YTAccountError, _tracks, _two_column
from extmusic.ytmusic import YTMusicError


def test_account_with_oauth(tmp_path):
    store = ExtMusicStore(tmp_path / "config.json")
    store.save_oauth({"email": "test@example.com", "access_token": "ya29.test", "refresh_token": "1//test"})
    account = YTAccount(store=store)
    assert account.is_configured() is True
    assert account.account() == {"configured": True, "type": "oauth", "account": "test@example.com"}


def test_account_with_cookies(tmp_path):
    store = ExtMusicStore(tmp_path / "config.json")
    store.save_cookies("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\txyz123\n")
    account = YTAccount(store=store)
    assert account.is_configured() is True
    assert account.account() == {"configured": True, "type": "cookies", "account": "Google-аккаунт (Cookies)"}


def test_account_empty(tmp_path):
    store = ExtMusicStore(tmp_path / "config.json")
    account = YTAccount(store=store)
    assert account.is_configured() is False
    assert account.account() == {"configured": False, "type": "", "account": ""}


def test_account_logout(tmp_path):
    store = ExtMusicStore(tmp_path / "config.json")
    store.save_oauth({"email": "test@example.com", "access_token": "ya29.test"})
    account = YTAccount(store=store)
    assert account.is_configured() is True
    res = account.logout()
    assert res == {"ok": True, "account": "test@example.com"}
    assert account.is_configured() is False
    assert account.account() == {"configured": False, "type": "", "account": ""}


def test_two_column_track_and_playlist():
    track = _two_column({
        "title": {"runs": [{"text": "Song"}]},
        "subtitle": {"runs": [{"text": "Artist • 3:59"}]},
        "thumbnail": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [{"url": "https://i.test/a.jpg"}]}}},
        "navigationEndpoint": {"watchEndpoint": {"videoId": "abc123", "playlistId": "LM"}},
    })
    assert track["title"] == "Song"
    assert track["artist"] == "Artist"
    assert track["duration"] == 239
    assert track["video_id"] == "abc123"
    assert track["playlist_id"] == "LM"
    playlist = _two_column({
        "title": {"simpleText": "My list"},
        "subtitle": {"simpleText": "Playlist"},
        "navigationEndpoint": {"browseEndpoint": {
            "browseId": "VLPL123",
            "browseEndpointContextSupportedConfigs": {
                "browseEndpointContextMusicConfig": {
                    "pageType": "MUSIC_PAGE_TYPE_PLAYLIST",
                    "playlistIdForFallback": "PL123",
                }
            },
        }},
    })
    assert playlist["kind"] == "playlist"
    assert playlist["browse_id"] == "VLPL123"
    assert playlist["playlist_id"] == "PL123"


def test_tracks_dedupes_and_reads_responsive():
    payload = {
        "a": {"musicTwoColumnItemRenderer": {
            "title": {"simpleText": "One"},
            "subtitle": {"simpleText": "A • 1:00"},
            "navigationEndpoint": {"watchEndpoint": {"videoId": "v1"}},
        }},
        "b": {"musicTwoColumnItemRenderer": {
            "title": {"simpleText": "One duplicate"},
            "subtitle": {"simpleText": "A • 1:00"},
            "navigationEndpoint": {"watchEndpoint": {"videoId": "v1"}},
        }},
        "c": {"musicResponsiveListItemRenderer": {
            "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Two"}]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [
                    {"text": "B"}, {"text": " • "}, {"text": "2:00"},
                ]}}},
            ],
            "playlistItemData": {"videoId": "v2"},
        }},
    }
    tracks = _tracks(payload)
    assert [track["video_id"] for track in tracks] == ["v1", "v2"]
    assert tracks[1]["artist"] == "B"
    assert tracks[1]["duration"] == 120


def test_tile_playlist_handles_artist_and_playlist():
    from extmusic.ytaccount import _tile_playlist

    pl_tile = {
        "onSelectCommand": {"browseEndpoint": {"browseId": "VLPL123"}},
        "metadata": {"tileMetadataRenderer": {"title": {"simpleText": "My Hits"}, "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": "Playlist • 5 tracks"}}}]}}]}},
    }
    pl = _tile_playlist(pl_tile)
    assert pl["kind"] == "playlist"
    assert pl["title"] == "My Hits"
    assert pl["browse_id"] == "VLPL123"

    artist_tile = {
        "onSelectCommand": {"browseEndpoint": {
            "browseId": "UCYp3rk70ACGXQ4gFAiMr1SQ",
            "browseEndpointContextSupportedConfigs": {"browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}},
        }},
        "metadata": {"tileMetadataRenderer": {"title": {"runs": [{"text": "Rammstein"}]}, "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": "@RammsteinOfficial"}}}]}}]}},
    }
    art = _tile_playlist(artist_tile)
    assert art["kind"] == "artist"
    assert art["title"] == "Rammstein"
    assert art["browse_id"] == "UCYp3rk70ACGXQ4gFAiMr1SQ"


def test_home_oauth_fallback(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "config.json")
    store.save_oauth({"email": "test@example.com", "access_token": "ya29.test"})
    account = YTAccount(store=store)

    def mock_browse(browse_id="", continuation=""):
        if browse_id == "FEmusic_home":
            raise YTAccountError("YouTube Music ответил HTTP 400")
        if browse_id == "FEwhat_to_watch":
            return {
                "contents": {
                    "item": {
                        "tileRenderer": {
                            "contentId": "v123",
                            "header": {"tileHeaderRenderer": {"thumbnail": {"thumbnails": [{"url": "http://img"}]}}},
                            "metadata": {"tileMetadataRenderer": {"title": {"simpleText": "Cool Track"}, "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": "Band"}}}]}}]}},
                        }
                    }
                }
            }
        return {}

    monkeypatch.setattr(account, "_browse", mock_browse)
    res = account.home(limit=10)
    assert len(res["tracks"]) == 1
    assert res["tracks"][0]["title"] == "Cool Track"
    assert res["tracks"][0]["artist"] == "Band"


def test_post_uses_endpoint_for_oauth_and_cookies(tmp_path, monkeypatch):
    seen_urls = []

    class FakeResp:
        def __init__(self, body):
            self._b = body

        def read(self, n=None):
            return json.dumps(self._b).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=25):
        seen_urls.append((req.full_url, json.loads(req.data.decode()), dict(req.header_items())))
        return FakeResp({"ok": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)
    res_like = acc.like("vid1", True)
    assert res_like == {"ok": True, "liked": True, "video_id": "vid1"}
    assert "www.youtube.com/youtubei/v1/like/like" in seen_urls[-1][0]
    assert seen_urls[-1][1]["target"] == {"videoId": "vid1"}
    assert seen_urls[-1][1]["context"]["client"]["clientName"] == "TVHTML5"
    assert "vid1" in acc.liked_ids()
    assert acc.is_liked("vid1") is True

    res_unlike = acc.like("vid1", False)
    assert res_unlike == {"ok": True, "liked": False, "video_id": "vid1"}
    assert "www.youtube.com/youtubei/v1/like/removelike" in seen_urls[-1][0]
    assert "vid1" not in acc.liked_ids()

    store.clear_oauth()
    store.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ts123\n")
    res_dislike = acc.dislike("vid2")
    assert res_dislike == {"ok": True, "disliked": True, "video_id": "vid2"}
    assert "music.youtube.com/youtubei/v1/like/dislike" in seen_urls[-1][0]
    assert seen_urls[-1][1]["context"]["client"]["clientName"] == "WEB_REMIX"
    headers = {k.lower(): v for k, v in seen_urls[-1][2].items()}
    assert headers["authorization"].startswith("SAPISIDHASH ")


def test_post_validates_endpoint_and_handles_auth_errors(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    store.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ts123\n")
    acc = YTAccount(store=store)

    for bad_ep in ("", "../etc", "browse?x=1", "BROWSE"):
        with pytest.raises(YTAccountError):
            acc._post({}, endpoint=bad_ep)

    def fail_401(req, timeout=25):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", fail_401)
    with pytest.raises(YTAccountError) as exc:
        acc.like("vid1", True)
    msg = str(exc.value).lower()
    assert "cookies" in msg and "qr" in msg

    # OAuth 403 refreshes token once and retries with the SAME endpoint
    store.clear_cookies()
    store.save_oauth({"access_token": "old_tok", "refresh_token": "ref", "expires_at": 9999999999})
    calls = []

    class FakeResp:
        def read(self, n=None):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fail_then_ok(req, timeout=25):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fail_then_ok)
    monkeypatch.setattr(acc.oauth_handler, "refresh_access_token", lambda tokens: "new_tok")
    assert acc.like("vid9", True) == {"ok": True, "liked": True, "video_id": "vid9"}
    assert len(calls) == 2
    assert all("like/like" in url for url in calls)


def test_liked_populates_cache_and_is_liked_queries_next(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    acc = YTAccount(store=store)
    assert acc.liked_ids() == set()
    assert acc.is_liked("v1") is None

    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    seen_posts = []

    def fake_post(body, retry=True, endpoint="browse"):
        seen_posts.append((endpoint, body))
        if endpoint == "browse" and body.get("browseId") in ("FEmusic_liked_videos", "VLLM"):
            return {
                "contents": {
                    "a": {
                        "musicTwoColumnItemRenderer": {
                            "title": {"simpleText": "Liked Song"},
                            "subtitle": {"simpleText": "Artist • 3:00"},
                            "navigationEndpoint": {"watchEndpoint": {"videoId": "v_liked"}},
                        }
                    }
                }
            }
        if endpoint == "next":
            vid = body.get("videoId")
            if vid == "v_extra":
                return {"likeButtonRenderer": {"target": {"videoId": "v_extra"}, "likeStatus": "LIKE"}}
            return {"likeButtonRenderer": {"target": {"videoId": vid}, "likeStatus": "INDIFFERENT"}}
        return {}

    monkeypatch.setattr(acc, "_post", fake_post)
    tracks = acc.liked(limit=50)
    assert [t["id"] for t in tracks] == ["v_liked"]
    assert acc.liked_ids() == {"v_liked"}
    # Cached hit must not call next
    assert acc.is_liked("v_liked") is True
    assert all(ep != "next" for ep, _ in seen_posts)

    # Unknown track queries next
    assert acc.is_liked("v_extra") is True
    assert any(ep == "next" and b.get("videoId") == "v_extra" for ep, b in seen_posts)
    assert "v_extra" in acc.liked_ids()

    assert acc.is_liked("v_nope") is False
    assert "v_nope" not in acc.liked_ids()


def test_optimistic_like_dislike_does_not_poison_full_liked_cache(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)

    seen_calls = []
    next_should_fail = True

    def fake_post(body, retry=True, endpoint="browse"):
        seen_calls.append((endpoint, body))
        if endpoint in ("like/like", "like/removelike", "like/dislike"):
            return {"ok": True}
        if endpoint == "next":
            if next_should_fail:
                raise YTAccountError("next failed")
            return {"likeButtonRenderer": {"target": {"videoId": body.get("videoId")}, "likeStatus": "INDIFFERENT"}}
        if endpoint == "browse" and body.get("browseId") in ("FEmusic_liked_videos", "VLLM"):
            return {
                "contents": {
                    "a": {
                        "musicTwoColumnItemRenderer": {
                            "title": {"simpleText": "From Server 1"},
                            "subtitle": {"simpleText": "Artist • 3:00"},
                            "navigationEndpoint": {"watchEndpoint": {"videoId": "srv_1"}},
                        }
                    },
                    "b": {
                        "musicTwoColumnItemRenderer": {
                            "title": {"simpleText": "From Server 2"},
                            "subtitle": {"simpleText": "Artist • 3:10"},
                            "navigationEndpoint": {"watchEndpoint": {"videoId": "srv_2"}},
                        }
                    },
                }
            }
        return {}

    monkeypatch.setattr(acc, "_post", fake_post)

    # Single-track like/dislike before full library fetch
    acc.like("single_vid", True)
    acc.dislike("srv_2")

    # Immediate optimistic status checks work without touching next
    assert acc.is_liked("single_vid") is True
    assert acc.is_liked("srv_2") is False

    # Unknown track queries `next` and returns None (not False) when `next` errors
    assert acc.is_liked("unknown_vid") is None
    assert any(ep == "next" and b.get("videoId") == "unknown_vid" for ep, b in seen_calls)

    # First liked_ids() MUST still fetch the full library and merge optimistic toggles
    ids = acc.liked_ids()
    assert any(ep == "browse" and b.get("browseId") in ("FEmusic_liked_videos", "VLLM") for ep, b in seen_calls)
    assert ids == {"srv_1", "single_vid"}

    # Subsequent liked_ids() hits cache without another browse
    browse_count = sum(1 for ep, _ in seen_calls if ep == "browse")
    assert acc.liked_ids() == {"srv_1", "single_vid"}
    assert sum(1 for ep, _ in seen_calls if ep == "browse") == browse_count

    # refresh=True forces a fresh library fetch and clears stale optimistic overrides
    refreshed = acc.liked_ids(refresh=True)
    assert sum(1 for ep, _ in seen_calls if ep == "browse") > browse_count
    assert refreshed == {"srv_1", "srv_2"}


def test_home_combines_public_music_and_tv_feed_resiliently(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)
    monkeypatch.setattr(
        "extmusic.ytmusic.home",
        lambda limit=30: {
            "tracks": [{"id": "qp1", "title": "Quick", "artist": "Art"}],
            "shelves": [],
            "moods": [{"title": "Workout", "browse_id": "FEmusic_mood", "params": "p1"}],
        },
    )

    def mock_browse(browse_id="", continuation=""):
        if browse_id == "FEwhat_to_watch":
            return {
                "contents": {
                    "a": {
                        "tileRenderer": {
                            "contentId": "tv1",
                            "metadata": {"tileMetadataRenderer": {"title": {"simpleText": "YouTube Video"}, "lines": []}},
                        }
                    }
                }
            }
        if browse_id == "FEmusic_library_landing":
            return {
                "contents": {
                    "a": {
                        "musicTwoColumnItemRenderer": {
                            "title": {"simpleText": "Linkin Park"},
                            "subtitle": {"simpleText": "Artist"},
                            "navigationEndpoint": {
                                "browseEndpoint": {
                                    "browseId": "UCZU9T1ceaOgwfLRq7OKFU4Q",
                                    "browseEndpointContextSupportedConfigs": {
                                        "browseEndpointContextMusicConfig": {"pageType": "MUSIC_PAGE_TYPE_ARTIST"}
                                    },
                                }
                            },
                        }
                    }
                }
            }
        return {}

    monkeypatch.setattr(acc, "_browse", mock_browse)
    res = acc.home(limit=20)
    assert any(t["id"] == "qp1" for t in res["quick_picks"])
    assert any(t["video_id"] == "tv1" for t in res["feed"])
    assert any(m["title"] == "Workout" for m in res["moods"])
    assert res["mixes"]
    assert res["tracks"]

    # Partial failure in ytmusic.home() must not drop feed
    monkeypatch.setattr("extmusic.ytmusic.home", lambda limit=30: (_ for _ in ()).throw(YTMusicError("down")))
    res2 = acc.home(limit=20)
    assert res2["quick_picks"] == []
    assert any(t["video_id"] == "tv1" for t in res2["feed"])
    assert res2["tracks"]

    # Partial failure in _browse must not drop quick_picks
    monkeypatch.setattr("extmusic.ytmusic.home", lambda limit=30: {"tracks": [{"id": "qp2", "title": "Public", "artist": "A"}], "shelves": [], "moods": []})
    monkeypatch.setattr(acc, "_browse", lambda browse_id="", continuation="": (_ for _ in ()).throw(YTAccountError("blip")))
    res3 = acc.home(limit=20)
    assert any(t["id"] == "qp2" for t in res3["quick_picks"])
    assert res3["tracks"][0]["id"] == "qp2"

    # Cookies mode queries FEmusic_home for feed
    store.clear_oauth()
    store.save_cookies(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\ts123\n")
    monkeypatch.setattr(
        acc,
        "_browse",
        lambda browse_id="", continuation="": {
            "contents": {
                "a": {
                    "musicTwoColumnItemRenderer": {
                        "title": {"simpleText": "Cookie Feed Song"},
                        "subtitle": {"simpleText": "Singer • 2:40"},
                        "navigationEndpoint": {"watchEndpoint": {"videoId": "ck1"}},
                    }
                }
            }
        }
        if browse_id == "FEmusic_home"
        else {},
    )
    res4 = acc.home(limit=20)
    assert any(t["video_id"] == "ck1" for t in res4["feed"])
    assert any(t["id"] == "qp2" for t in res4["quick_picks"])


def test_collection_and_playlist_public_fallback(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    acc = YTAccount(store=store)

    monkeypatch.setattr(
        "extmusic.ytmusic.browse_collection",
        lambda bid, params="", limit=100: {
            "kind": "album" if bid.startswith("MPREb_") else "playlist",
            "title": "Hybrid Theory",
            "subtitle": "Альбом • 2000",
            "description": "",
            "thumb": "https://i.ytimg.com/vi/a/hqdefault.jpg",
            "browse_id": bid,
            "playlist_id": "OLAK5uy_ht",
            "artist": "Linkin Park",
            "artist_id": "UClp1",
            "tracks": [{"source": "yt", "id": "p1", "title": "Papercut", "artist": "Linkin Park"}],
            "albums": [],
            "similar": [],
        },
    )

    # Unauthenticated album/playlist uses ytmusic.browse_collection
    col = acc.collection("MPREb_ht")
    assert col["kind"] == "album"
    assert col["title"] == "Hybrid Theory"
    assert col["tracks"][0]["video_id"] == "p1"
    pl_tracks = acc.playlist("MPREb_ht")
    assert isinstance(pl_tracks, list) and pl_tracks[0]["id"] == "p1"

    # Unauthenticated VLLM must fail clearly
    with pytest.raises(YTAccountError):
        acc.collection("VLLM")

    # Authenticated private playlist uses _browse first, falls back if _browse fails
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    monkeypatch.setattr(
        acc,
        "_browse",
        lambda browse_id="", continuation="": {
            "header": {"musicDetailHeaderRenderer": {"title": {"simpleText": "Private Mix"}}},
            "contents": {
                "a": {
                    "musicTwoColumnItemRenderer": {
                        "title": {"simpleText": "Priv Song"},
                        "subtitle": {"simpleText": "Art • 2:10"},
                        "navigationEndpoint": {"watchEndpoint": {"videoId": "priv1"}},
                    }
                }
            },
        }
        if browse_id == "VLPLpriv1"
        else (_ for _ in ()).throw(YTAccountError("HTTP 400")),
    )
    priv = acc.collection("PLpriv1")
    assert priv["title"] == "Private Mix"
    assert priv["tracks"][0]["video_id"] == "priv1"

    pub_pl = acc.collection("PLpublic1")
    assert pub_pl["title"] == "Hybrid Theory"
    assert pub_pl["tracks"][0]["id"] == "p1"


def test_tvhtml5_private_collection_parses_overlay_header_and_falls_back_to_track_thumb(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)

    def tv_tile(vid, title, artist, thumb):
        return {
            "tileRenderer": {
                "contentId": vid,
                "header": {"tileHeaderRenderer": {"thumbnail": {"thumbnails": [{"url": thumb}]}}},
                "metadata": {
                    "tileMetadataRenderer": {
                        "title": {"simpleText": title},
                        "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": artist}}}]}}],
                    }
                },
            }
        }

    playlist_payload = {
        "header": [],
        "contents": {
            "tvBrowseRenderer": {
                "content": {
                    "tvSurfaceContentRenderer": {
                        "content": {
                            "twoColumnRenderer": {
                                "leftColumn": {
                                    "entityMetadataRenderer": {
                                        "title": {"simpleText": "TikTok Songs"},
                                        "overlayPanelHeaderRenderer": {
                                            "title": {"simpleText": "TikTok Songs"},
                                            "metadataLines": [
                                                {
                                                    "lineRenderer": {
                                                        "items": [
                                                            {"lineItemRenderer": {"text": {"simpleText": "Cheburek"}}},
                                                            {"lineItemRenderer": {"text": {"simpleText": "•"}}},
                                                            {
                                                                "lineItemRenderer": {
                                                                    "text": {"runs": [{"text": "2"}, {"text": " видео"}]}
                                                                }
                                                            },
                                                        ]
                                                    }
                                                }
                                            ],
                                        },
                                    }
                                },
                                "rightColumn": {
                                    "playlistVideoListRenderer": {
                                        "contents": [
                                            tv_tile("tt1", "Trend One", "DJ One", "https://i.ytimg.com/vi/tt1/hq.jpg"),
                                            tv_tile("tt2", "Trend Two", "DJ Two", "https://i.ytimg.com/vi/tt2/hq.jpg"),
                                        ]
                                    }
                                },
                            }
                        }
                    }
                }
            }
        },
    }

    vllm_payload = {
        "header": [],
        "contents": {
            "tvBrowseRenderer": {
                "content": {
                    "tvSurfaceContentRenderer": {
                        "content": {
                            "twoColumnRenderer": {
                                "leftColumn": {
                                    "entityMetadataRenderer": {
                                        "title": {"simpleText": "Понравившаяся музыка"},
                                        "overlayPanelHeaderRenderer": {
                                            "title": {"simpleText": "Понравившаяся музыка"},
                                            "subtitle": {
                                                "runs": [
                                                    {"text": "Создан автоматически • "},
                                                    {"text": "Cheburek"},
                                                ]
                                            },
                                            "additionalSubtitles": [
                                                {"simpleText": "57\xa0треков • 54 минуты"}
                                            ],
                                        },
                                    }
                                },
                                "rightColumn": {
                                    "playlistVideoListRenderer": {
                                        "contents": [
                                            tv_tile("lk1", "Liked One", "Art One", "https://i.ytimg.com/vi/lk1/hq.jpg"),
                                        ]
                                    }
                                },
                            }
                        }
                    }
                }
            }
        },
    }

    monkeypatch.setattr(
        acc,
        "_browse",
        lambda browse_id="", continuation="": (
            playlist_payload if browse_id == "VLPLTLC123"
            else (vllm_payload if browse_id == "VLLM" else {})
        ),
    )

    col = acc.collection("VLPLTLC123")
    assert col["kind"] == "playlist"
    assert col["title"] == "TikTok Songs"
    assert col["subtitle"] == "Cheburek • 2 видео"
    assert col["thumb"] == "https://i.ytimg.com/vi/tt1/hq.jpg"
    assert [t["video_id"] for t in col["tracks"]] == ["tt1", "tt2"]

    vllm_direct = acc._browse_collection_private("VLLM")
    assert vllm_direct["kind"] == "liked"
    assert vllm_direct["title"] == "Понравившаяся музыка"
    assert "Создан автоматически" in vllm_direct["subtitle"]
    assert "Cheburek" in vllm_direct["subtitle"]
    assert "57\xa0треков • 54 минуты" in vllm_direct["subtitle"]
    assert vllm_direct["thumb"] == "https://i.ytimg.com/vi/lk1/hq.jpg"


def test_collection_vllm_and_liked_videos_return_full_liked_tracks(tmp_path, monkeypatch):
    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)

    def make_tiles(start, count):
        return [
            {
                "tileRenderer": {
                    "contentId": f"vid_{i}",
                    "header": {
                        "tileHeaderRenderer": {
                            "thumbnail": {"thumbnails": [{"url": f"https://i.ytimg.com/vi/vid_{i}/hq.jpg"}]}
                        }
                    },
                    "metadata": {
                        "tileMetadataRenderer": {
                            "title": {"simpleText": f"Song {i}"},
                            "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": f"Artist {i}"}}}]}}],
                        }
                    },
                }
            }
            for i in range(start, start + count)
        ]

    def mock_browse(browse_id="", continuation=""):
        if browse_id == "VLLM":
            # TVHTML5 VLLM only returns a truncated 15-track preview
            return {"header": [], "contents": {"preview": make_tiles(1, 15)}}
        if browse_id == "FEmusic_liked_videos":
            return {
                "contents": {
                    "sectionListRenderer": {
                        "contents": make_tiles(1, 15),
                        "continuations": [{"nextContinuationData": {"continuation": "liked_p2"}}],
                    }
                }
            }
        if continuation == "liked_p2":
            return {"contents": {"items": make_tiles(16, 10)}}
        return {}

    monkeypatch.setattr(acc, "_browse", mock_browse)

    for bid in ("VLLM", "FEmusic_liked_videos"):
        col = acc.collection(bid, limit=100)
        assert col["kind"] == "liked"
        assert col["title"] == "Понравившаяся музыка"
        assert col["subtitle"]
        assert col["playlist_id"] == "LM"
        assert col["thumb"].startswith("https://")
        assert len(col["tracks"]) == 25
        assert col["tracks"][0]["source"] == "yt"
        assert col["tracks"][0]["video_id"] == "vid_1"
        assert col["tracks"][0]["playlist_id"] == "LM"
        assert col["tracks"][-1]["id"] == "vid_25"


def test_liked_and_collection_follow_multipage_continuations_and_break_cycles(tmp_path, monkeypatch):
    from extmusic.ytaccount import LIKED_THUMB_URL, _parse_tv_collection_header

    store = ExtMusicStore(tmp_path / "c.json")
    store.save_oauth({"access_token": "tok", "expires_at": 9999999999})
    acc = YTAccount(store=store)

    def make_tiles(start, count):
        return [
            {
                "tileRenderer": {
                    "contentId": f"t_{i}",
                    "header": {
                        "tileHeaderRenderer": {
                            "thumbnail": {"thumbnails": [{"url": f"https://i.ytimg.com/vi/t_{i}/hq.jpg"}]}
                        }
                    },
                    "metadata": {
                        "tileMetadataRenderer": {
                            "title": {"simpleText": f"Track {i}"},
                            "lines": [{"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": f"Art {i}"}}}]}}],
                        }
                    },
                }
            }
            for i in range(start, start + count)
        ]

    seen_conts = []

    def mock_browse(browse_id="", continuation=""):
        if continuation:
            seen_conts.append(continuation)
        if browse_id == "FEmusic_liked_videos":
            return {
                "contents": {
                    "playlistVideoListRenderer": {
                        "contents": make_tiles(1, 5),
                        "continuations": [{"nextContinuationData": {"continuation": "p2_tok"}}],
                    }
                }
            }
        if continuation == "p2_tok":
            return {
                "continuationContents": {
                    "playlistVideoListContinuation": {
                        "contents": make_tiles(6, 5),
                        "continuations": [{"nextContinuationData": {"continuation": "p3_tok"}}],
                    }
                }
            }
        if continuation == "p3_tok":
            return {
                "continuationContents": {
                    "musicPlaylistShelfContinuation": {
                        "contents": make_tiles(11, 5),
                    }
                }
            }
        if browse_id == "VLPLmulti":
            return {
                "header": [],
                "contents": {
                    "entityMetadataRenderer": {
                        "title": {"simpleText": "Multi Playlist"},
                        "overlayPanelHeaderRenderer": {
                            "title": {},
                            "metadataLines": [
                                {"lineRenderer": {"items": [{"lineItemRenderer": {"text": {"simpleText": "15 треков"}}}]}}
                            ],
                        },
                    },
                    "playlistVideoListRenderer": {
                        "contents": make_tiles(1, 5),
                        "continuations": [{"nextContinuationData": {"continuation": "p2_tok"}}],
                    },
                },
            }
        if browse_id == "VLPLcycle":
            return {
                "header": [],
                "contents": {
                    "gridRenderer": {
                        "items": make_tiles(1, 2),
                        "continuations": [{"nextContinuationData": {"continuation": "loop_tok"}}],
                    }
                },
            }
        if continuation == "loop_tok":
            return {
                "continuationContents": {
                    "gridContinuation": {
                        "items": make_tiles(3, 2),
                        "continuations": [{"nextContinuationData": {"continuation": "loop_tok"}}],
                    }
                }
            }
        if browse_id == "FEmusic_library_landing":
            return {
                "contents": {
                    "a": {
                        "tileRenderer": {
                            "onSelectCommand": {"browseEndpoint": {"browseId": "VLLM"}},
                            "metadata": {"tileMetadataRenderer": {"title": {"simpleText": "Понравившаяся музыка"}, "lines": []}},
                        }
                    }
                }
            }
        return {}

    monkeypatch.setattr(acc, "_browse", mock_browse)

    # 1. 3-page liked() & collection("VLLM")
    col_liked = acc.collection("VLLM", limit=50)
    assert [t["id"] for t in col_liked["tracks"]] == [f"t_{i}" for i in range(1, 16)]
    assert col_liked["thumb"] == LIKED_THUMB_URL

    # 2. 3-page private playlist via collection("VLPLmulti") + empty oph title falls back to em title
    col_pl = acc.collection("VLPLmulti", limit=50)
    assert col_pl["title"] == "Multi Playlist"
    assert col_pl["subtitle"] == "15 треков"
    assert [t["id"] for t in col_pl["tracks"]] == [f"t_{i}" for i in range(1, 16)]

    # 3. Cycle guard stops after seeing loop_tok once
    seen_conts.clear()
    col_cycle = acc.collection("VLPLcycle", limit=50)
    assert seen_conts == ["loop_tok"]
    assert [t["id"] for t in col_cycle["tracks"]] == ["t_1", "t_2", "t_3", "t_4"]

    # 4. library() backfills empty liked thumb with LIKED_THUMB_URL
    lib = acc.library()
    assert lib["liked"]["thumb"] == LIKED_THUMB_URL

    # 5. _parse_tv_collection_header handles empty oph.title dict gracefully
    hdr = _parse_tv_collection_header(
        {"entityMetadataRenderer": {"title": {"simpleText": "From EM"}, "overlayPanelHeaderRenderer": {"title": {}}}},
        "VLPLx",
    )
    assert hdr["title"] == "From EM"

