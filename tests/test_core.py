import json

import pytest

from lirico.clock import PlaybackClock
from lirico.lrc import LyricTimeline, parse_lrc
from lirico.lyrics_source import (NOT_FOUND, SYNCED, LrclibClient, LyricsCache, devanagari_ratio,
                                  pick_best)
from lirico.textnorm import normalize, similarity
from lirico.title_clean import clean_track, clean_artist_channel


# ---------------------------------------------------------------- title cleaning
def titles(ct):
    return [(c.title, c.artist) for c in ct.candidates if not c.free]


@pytest.mark.parametrize("raw, channel, title", [
    ("Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song", "T-Series", "Tum Hi Ho"),
    ("Full Video: Tum Hi Ho | Aashiqui 2 | Aditya Roy Kapur, Shraddha Kapoor | Mithoon", "T-Series", "Tum Hi Ho"),
    ("Coldplay - Yellow (Official Video)", "Coldplay", "Yellow"),
    ("Ed Sheeran - Shape of You [Official Music Video]", "Ed Sheeran", "Shape of You"),
    ("Blinding Lights", "The Weeknd - Topic", "Blinding Lights"),
    ("The Weeknd - Blinding Lights (Official Video)", "TheWeekndVEVO", "Blinding Lights"),
    ("Agar Tum Saath Ho Full Video | Tamasha | Ranbir Kapoor, Deepika Padukone", "T-Series", "Agar Tum Saath Ho"),
    ("Dua Lipa - Levitating Featuring DaBaby (Official Music Video)", "Dua Lipa", "Levitating"),
    ("Raataan Lambiyan – Official Video | Shershaah | Jubin Nautiyal", "Sony Music India", "Raataan Lambiyan"),
    ("Kesariya 🧡 #arijitsingh #brahmastra", "Some Fan", "Kesariya"),
])
def test_browser_titles(raw, channel, title):
    ct = clean_track(raw, channel, "", "chrome.exe")
    assert ct.title == title
    assert not ct.trusted


def test_ambiguous_dash_tries_both_orders_and_free_text():
    ct = clean_track("Arijit Singh: Apna Bana Le (Lyrics) | Bhediya", "Zee Music Company", "", "msedge.exe")
    pairs = titles(ct)
    assert ("Apna Bana Le", "Arijit Singh") in pairs
    assert ct.candidates[0].free  # free-text query first


def test_channel_artist_used_first():
    ct = clean_track("Yellow (Official Video)", "Coldplay", "", "chrome.exe")
    assert ct.candidates[0].title == "Yellow" and ct.candidates[0].artist == "Coldplay"


def test_label_channels_are_not_artists():
    for label in ["T-Series", "Sony Music India", "Zee Music Company", "Tips Official", "Saregama Music", "YRF"]:
        assert clean_artist_channel(label)[1] is False, label
    assert clean_artist_channel("Arijit Singh - Topic") == ("Arijit Singh", True)
    assert clean_artist_channel("Arijit Singh Official") == ("Arijit Singh", True)


def test_spotify_metadata_is_trusted():
    ct = clean_track("Here Comes The Sun - Remastered 2009", "The Beatles", "Abbey Road", "Spotify.exe")
    assert ct.trusted and ct.title == "Here Comes The Sun" and ct.artist == "The Beatles"
    ct = clean_track('Kal Ho Naa Ho (From "Kal Ho Naa Ho")', "Sonu Nigam, Shankar-Ehsaan-Loy", "", "Spotify.exe")
    assert ct.title == "Kal Ho Naa Ho" and ct.artist == "Sonu Nigam"


def test_devanagari_title_survives():
    ct = clean_track("तुम ही हो | Tum Hi Ho", "T-Series", "", "chrome.exe")
    assert ct.title == "तुम ही हो"
    assert normalize("तुम ही हो!") == "तुम ही हो"  # matras kept, punctuation dropped


def test_similarity_handles_romanization_variants():
    assert similarity("Kal Ho Naa Ho", "kal ho na ho") > 0.9
    assert similarity("Tum Hi Ho", "Tum Hi Ho (From Aashiqui 2)") > 0.7  # >= accept threshold 0.6
    assert similarity("Yellow", "Shape of You") < 0.4


# ---------------------------------------------------------------- LRC
LRC = """[ar:Someone]
[offset:+500]
[00:01.00]First line
[00:05.50][00:20.00]Chorus <00:05.60>word
[00:10]
[01:02.345]Later line
"""


def test_parse_lrc():
    lines = parse_lrc(LRC)
    starts = [round(l.start, 3) for l in lines]
    # offset +500ms moves lyrics earlier; repeated timestamps expand; word tags removed
    assert starts == [0.5, 5.0, 9.5, 19.5, 61.845]
    assert lines[1].text == "Chorus word"
    assert lines[2].text == ""


def test_timeline_lookup():
    tl = LyricTimeline(parse_lrc("[00:01.00]a\n[00:03.00]\n[00:05.00]b"))
    assert tl.index_at(0.5) == -1
    assert tl.index_at(1.0) == 0
    assert tl.index_at(4.0) == 1
    assert tl.next_text(0) == "b"
    assert tl.progress(0, 2.0) == pytest.approx(0.5)


# ---------------------------------------------------------------- clock
def test_clock_runs_pauses_and_seeks():
    c = PlaybackClock()
    assert c.update("k", 10.0, True, 1.0, captured_at=100.0, duration=200) == "track"
    assert c.position(102.0) == pytest.approx(12.0)
    # consistent report: nothing / tiny nudge
    assert c.update("k", 12.02, True, 1.0, captured_at=102.0, duration=200) in ("none", "nudge")
    # pause freezes
    assert c.update("k", 12.5, False, 1.0, captured_at=102.5, duration=200) == "state"
    assert c.position(110.0) == pytest.approx(12.5)
    # seek while paused
    assert c.update("k", 50.0, False, 1.0, captured_at=111.0, duration=200) == "seek"
    # resume then seek while playing
    assert c.update("k", 50.0, True, 1.0, captured_at=112.0, duration=200) == "state"
    assert c.update("k", 90.0, True, 1.0, captured_at=113.0, duration=200) == "seek"
    assert c.position(114.0) == pytest.approx(91.0)
    # track change
    assert c.update("k2", 0.0, True, 1.0, captured_at=115.0, duration=180) == "track"


def test_clock_free_runs_without_timeline():
    c = PlaybackClock()
    c.update("k", 0.0, True, 1.0, captured_at=0.0, duration=0)
    assert c.update("k", 0.0, True, 1.0, captured_at=5.0, duration=0) == "none"
    assert c.position(5.0) == pytest.approx(5.0)


def test_clock_small_backward_error_never_jumps_back_much():
    c = PlaybackClock()
    c.update("k", 10.0, True, 1.0, captured_at=0.0, duration=100)
    c.update("k", 10.7, True, 1.0, captured_at=1.0, duration=100)  # -0.3s error
    assert c.position(1.0) >= 10.95


# ---------------------------------------------------------------- matching & cache
def item(i, title, artist, dur, synced=True, plain=True, lyrics="[00:01.00]hello"):
    return {"id": i, "trackName": title, "artistName": artist, "duration": dur, "instrumental": False,
            "syncedLyrics": lyrics if synced else None, "plainLyrics": "hello" if plain else None}


def test_pick_best_prefers_matching_artist_duration_and_synced():
    ct = clean_track("Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song", "T-Series", "", "chrome.exe")
    items = [
        item(1, "Tum Hi Ho", "Via Vallen", 260),
        item(2, "Tum Hi Ho", "Arijit Singh", 262, synced=False),
        item(3, "Tum Hi Ho", "Arijit Singh, Mithoon", 262),
        item(4, "Tum Hi Ho (Remix)", "Arijit Singh", 262),
        item(5, "Something Else", "Arijit Singh", 262),
    ]
    best, _ = pick_best(ct, items, 262)
    assert best["id"] == 3


def test_pick_best_rejects_wrong_titles():
    ct = clean_track("Yellow", "Coldplay - Topic", "", "chrome.exe")
    assert pick_best(ct, [item(1, "Fix You", "Coldplay", 295)], 266) is None


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
        return self.routes(url.rsplit("/", 1)[-1], params or {})


def test_video_style_titles_from_non_browser_players_are_parsed():
    ct = clean_track("Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song", "T-Series", "", "vlc.exe")
    assert not ct.trusted and ct.title == "Tum Hi Ho"


def test_client_fetches_caches_and_remembers_not_found(tmp_path):
    def routes(path, params):
        if params.get("track_name") == "Tum Hi Ho" or "Tum Hi Ho" in params.get("q", ""):
            return FakeResp(200, [item(7, "Tum Hi Ho", "Arijit Singh", 262,
                                       lyrics="[00:11.20]Hum tere bin ab reh nahi sakte")])
        return FakeResp(200, [])

    sess = FakeSession(routes)
    client = LrclibClient(LyricsCache(tmp_path), session=sess)
    ct = clean_track("Tum Hi Ho | Aashiqui 2 | Arijit Singh | Full Video Song", "T-Series", "", "chrome.exe")
    r = client.find(ct, 262)
    assert r.status == SYNCED and r.lines[0].text.startswith("Hum tere") and r.duration_delta == 0
    n = len(sess.calls)
    r2 = client.find(ct, 262)
    assert r2.from_cache and r2.status == SYNCED and len(sess.calls) == n  # no new request

    missing = clean_track("Nope Nope | Nothing", "Somebody", "", "chrome.exe")
    assert client.find(missing, 100).status == NOT_FOUND
    n = len(sess.calls)
    assert client.find(missing, 100).from_cache and len(sess.calls) == n


# ---------------------------------------------------------------- sync-first matching
def test_duration_match_beats_album_version():
    """The official video (300 s) must get the lyrics timed to the video, not the album audio."""
    ct = clean_track("Aashiqui 2: Tum Hi Ho 8K Full Song | Aditya Roy Kapur | Arijit Singh", "T-Series", "", "chrome.exe")
    album = item(1, "Tum Hi Ho", "Arijit Singh", 262)
    video = item(2, "Tum Hi Ho", "Arijit Singh", 300)
    mashup = item(3, "Aashiqui 2 Mashup", "Arijit Singh", 299)
    assert pick_best(ct, [album, mashup, video], 300)[0]["id"] == 2


def test_far_off_unconfirmed_artist_is_rejected():
    ct = clean_track("Coke Studio | Season 14 | Pasoori | Ali Sethi x Shae Gill", "Coke Studio Pakistan", "", "chrome.exe")
    wrong = item(1, "Coke Studio | Season 14 | Sajan Das Na", "Atif Aslam, Momina Mustehsan", 247)
    right = item(2, "Pasoori", "Ali Sethi, Shae Gill", 277)
    assert pick_best(ct, [wrong, right], 277)[0]["id"] == 2
    assert ct.title == "Pasoori"  # show-name prefix dropped


def test_actor_names_later_in_title_do_not_match_songs():
    ct = clean_track("Heeriye Nasha Tera Karke Ranjha Ye Tabah Ho Gaya | Salman Khan | Jacqueline", "Music Beats", "", "chrome.exe")
    assert pick_best(ct, [item(1, "Salman Khan", "Eligarf", 153)], 306) is None


def test_artist_part_of_title_is_not_a_title():
    ct = clean_track("EMIWAY - FIRSE MACHAYENGE (OFFICIAL MUSIC VIDEO)", "Emiway Bantai", "", "chrome.exe")
    assert ct.title == "FIRSE MACHAYENGE"
    wrong = item(1, "Fuck Emiway", "Emiway Bantai", 243)
    right = item(2, "Firse Machayenge", "Emiway Bantai", 243)
    assert pick_best(ct, [wrong, right], 243)[0]["id"] == 2


def test_devanagari_and_fancy_titles_get_searchable_candidates():
    ct = clean_track("आशिकी 2 का गाना तुम ही हो | मिथुन का संगीत", "T-Series", "", "chrome.exe")
    assert any(c.title.lower() == "tum hi ho" for c in ct.candidates)
    ct = clean_track("अगर तुम साथ हो 'पूरा वीडियो गाना | तमाशा", "T-Series", "", "chrome.exe")
    assert any(c.title.lower() == "agar tum saath ho" for c in ct.candidates)
    ct = clean_track("𝐏𝐡𝐢𝐫 𝐁𝐡𝐢 𝐓𝐮𝐦𝐤𝐨 𝐂𝐡𝐚𝐚𝐡𝐮𝐧𝐠𝐚💖 – 𝐅𝐮𝐥𝐥 𝐒𝐨𝐧𝐠 | Arijit Singh", "Zee Music Company", "", "chrome.exe")
    assert ct.title == "Phir Bhi Tumko Chaahunga"


def test_english_letters_by_default_keeping_devanagari_timing(tmp_path):
    """Default is English letters. If the version timed to this video is in
    Devanagari, use a romanized version's text on those timings."""
    from lirico.lyrics_source import _result_from_item
    deva = item(1, "Tum Hi Ho", "Arijit Singh", 300, lyrics="[00:20.00]हम तेरे बिन\n[00:25.00]\n[00:26.00]तेरे बिना")
    latin = item(2, "Tum Hi Ho", "Arijit Singh", 262, lyrics="[00:11.00]Hum tere bin\n[00:15.00]Tere bina")
    r = _result_from_item(deva, 300, "latin", [deva, latin])
    assert [(l.start, l.text) for l in r.lines] == [(20.0, "Hum tere bin"), (25.0, ""), (26.0, "Tere bina")]
    # no matching romanized version: transliterate
    r = _result_from_item(deva, 300, "latin", [deva])
    assert r.lines[0].text == "Ham tere bin" and r.romanized
    # Devanagari wanted: untouched
    assert _result_from_item(deva, 300, "devanagari", [deva, latin]).lines[0].text == "हम तेरे बिन"


def test_transliteration():
    from lirico.translit import to_hinglish
    assert to_hinglish("हम तेरे बिन अब रह नहीं सकते") == "Ham tere bin ab rah nahin sakte"
    assert to_hinglish("कैसे तुझसे दिल ना लगाए कोई") == "Kaise tujhse dil na lagaae koi"
    assert to_hinglish("तेरी फ़िकर में") == "Teri fikar mein"
    assert to_hinglish("Already latin") == "Already latin"


# ---------------------------------------------------------------- timeline guard & underline
def test_guard_rejects_foreign_timeline_and_adopts_real_change():
    from lirico.clock import TimelineGuard
    g = TimelineGuard()
    assert g.accept(272, True, now=0.0)
    assert g.accept(272.2, True, now=5.0)
    assert not g.accept(43.4, False, now=6.0)       # another player's timeline while paused
    assert not g.accept(43.4, True, now=7.0)        # ...and briefly on resume
    assert g.accept(272, True, now=8.0)
    # stale previous timeline right after a track change is replaced at once
    g.reset()
    assert g.accept(219.8, True, now=10.0)
    assert g.accept(300, True, now=10.4) and g.duration == 300


def test_sung_chars_follow_words():
    tl = LyricTimeline(parse_lrc("[00:10.00]one two three four\n[00:14.00]next"))
    assert tl.sung_chars(0, 9.0) == 0
    a, b, c = tl.sung_chars(0, 10.3), tl.sung_chars(0, 11.0), tl.sung_chars(0, 13.9)
    assert 0 < a < b <= c == len("one two three four")
    tl = LyricTimeline(parse_lrc("[00:20.00]<00:20.00>Word <00:20.50>timed <00:21.00>line\n[00:25.00]x"))
    assert tl.sung_chars(0, 20.25) == pytest.approx(2.0)
    assert 5 <= tl.sung_chars(0, 20.6) < 11


def test_settings_defaults_and_theme_overrides():
    from lirico.settings import Settings
    from lirico.theme import THEME
    s = Settings()
    assert s.script_pref == "latin" and s.hotkey("toggle") == "Ctrl+Alt+Space"
    t = THEME.with_overrides({"font_size": "18", "panel_opacity": 0.2, "bogus": 1})
    assert t.font_size == 18.0 and t.panel_opacity == 0.2
    assert t.diff(THEME) == {"font_size": 18.0, "panel_opacity": 0.2}


# ---------------------------------------------------------------- auto-sync (listen to the singer)
def _lyrics_lines():
    from lirico.lrc import LyricLine
    return [LyricLine(10.96, "Poori bottle down"), LyricLine(12.26, "Par woh hili bhi nahi"),
            LyricLine(13.63, "Aake saath mere dance kare, chiggy wiggy"),
            LyricLine(16.35, "Boli karna chahti grind, deti kissi bhi nahi"),
            LyricLine(18.74, "Bom-bom lage, karti woh diggy diggy"), LyricLine(21.0, "Chalo ghar chalein")]


def test_skeleton_ignores_hinglish_spelling():
    from lirico.autosync import skeleton
    assert skeleton("bhookh") == skeleton("buki") == "bk"
    assert skeleton("sharam") == skeleton("siram") == "srm"
    assert skeleton("wajood") == skeleton("vajud")


def test_guess_language():
    from lirico.autosync import guess_language
    from lirico.lrc import LyricLine
    assert guess_language(_lyrics_lines()) == "hi"
    assert guess_language([LyricLine(1, "Look at the stars, look how they shine for you")]) == "en"


def test_estimate_offset_finds_intro_shift_despite_noise():
    """Recognised words (garbled, as Whisper hears singing) 21 s later than the
    lyric timing -> offset -21 (lyrics must be delayed by 21 s)."""
    from lirico.autosync import estimate_offset, lyric_words
    heard = [(33.4, "parvohi"), (35.7, "dance"), (37.2, "Bulli"), (37.8, "kana"), (38.3, "grind"),
             (39.1, "kesevi"), (40.1, "Bomb"), (41.2, "karatini"), (30.0, "random"), (44.0, "noise")]
    est = estimate_offset(heard, lyric_words(_lyrics_lines()))
    assert est is not None and est.confident
    assert abs(est.offset - (-21.0)) < 1.0


def test_estimate_offset_not_fooled_by_repetition_loop_or_garbage():
    from lirico.autosync import estimate_offset, lyric_words
    loop = [(16.5 + 0.1 * i, "Par") for i in range(20)]          # whisper stuck repeating
    est = estimate_offset(loop, lyric_words(_lyrics_lines()))
    assert est is None or not est.confident
    garbage = [(5.0 + i, w) for i, w in enumerate(["xylo", "quartz", "fjord", "nymph", "glyph"])]
    est = estimate_offset(garbage, lyric_words(_lyrics_lines()))
    assert est is None or not est.confident


def test_settings_keep_zero_offsets_and_migrate_hotkeys(tmp_path, monkeypatch):
    import json
    from lirico import settings as S
    monkeypatch.setattr(S, "config_dir", lambda: tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"hotkeys": {"toggle": ""}, "script_pref": "auto"}))
    st = S.Settings.load()
    assert st.hotkey("toggle") == "Ctrl+Alt+Space" and st.script_pref == "latin"
    st.set_offset("t", "a", 0.0)
    assert st.has_offset("t", "a") and st.offset_for("t", "a") == 0.0
    st.set_offset("t", "a", None)
    assert not st.has_offset("t", "a")


# ---------------------------------------------------------------- auto-update
def test_update_versions():
    from lirico.updater import is_newer, parse_version
    assert parse_version("v1.2.0") == (1, 2, 0) and parse_version("1.10") == (1, 10, 0)
    assert is_newer("v1.3.0", "1.2.0") and is_newer("1.10.0", "1.9.9")
    assert not is_newer("v1.2.0", "1.2.0") and not is_newer("1.1.9", "1.2.0")


# ---------------------------------------------------------------- other scripts -> English letters
def test_to_latin_many_scripts():
    from lirico.translit import latin_ratio, to_latin
    assert to_latin("ایسا دیکھا نہیں خوبصورت کوئی") == "Aisa dekha nahin khoobsurat koi"   # Urdu
    assert to_latin("جسم جیسے اجنتا") == "Jism jaise ajanta"
    assert to_latin("함께 노래해요") == "Hamkke noraehaeyo"                                   # Korean
    assert to_latin("Я тебя люблю") == "Ya tebya lyublyu"                                     # Russian
    assert to_latin("ਸੋਹਣਾ ਮੁੱਖੜਾ ਤੇਰਾ") == "Sohna mukhra tera"                                 # Punjabi
    assert to_latin("तेरे बिना") == "Tere bina"                                                 # Hindi
    assert to_latin("一緒に歌おう") == "一緒に歌おう"        # kanji: can't be read without a dictionary
    assert to_latin("Canción de amor") == "Canción de amor"
    assert latin_ratio("Canción") == 1.0 and latin_ratio("نہیں") == 0.0


def test_prefers_english_letters_for_any_script():
    from lirico.lyrics_source import rank
    from lirico.title_clean import clean_track
    tr = clean_track("Afreen Afreen", "Rahat Fateh Ali Khan", "", "Spotify")
    urdu = {"id": 1, "trackName": "Afreen Afreen", "artistName": "Rahat Fateh Ali Khan", "duration": 400.0,
            "syncedLyrics": "[00:50.14] ایسا دیکھا نہیں خوبصورت کوئی"}
    latin = dict(urdu, id=2, syncedLyrics="[00:50.26] Aisa dekha nahi khoobsurat koi")
    assert rank(tr, [urdu, latin], 400.0, "latin")[0][2]["id"] == 2
    assert rank(tr, [urdu, latin], 400.0, "original")[0][2]["id"] == 1
