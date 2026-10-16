import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "plugins", "PlaylistGenreFilter"))
sys.path.insert(0, HERE)

import fake_navidrome  # noqa: E402
import filtering  # noqa: E402
from filtering import Subsonic, SubsonicError, block_reason, filter_server, name_matcher, song_genres  # noqa: E402

S = fake_navidrome.SONGS


class Matching(unittest.TestCase):
    def test_genre_exact_case_insensitive(self):
        self.assertEqual(block_reason(S["s1"], ["christmas"], []), "genre: christmas")
        self.assertIsNone(block_reason(S["s2"], ["christmas"], []))

    def test_multi_genre_from_list_and_split_string(self):
        self.assertIn("classical", song_genres(S["s4"]))
        self.assertIsNotNone(block_reason(S["s4"], ["Classical"], []))

    def test_slash_genre_kept_whole_and_split(self):
        g = song_genres(S["s5"])
        self.assertTrue({"r&b/soul", "r&b", "soul"} <= g)
        self.assertIsNotNone(block_reason(S["s5"], ["R&B/Soul"], []))

    def test_genre_is_not_substring_match(self):
        # A checked "Rock" must not catch "Jingle Bell Rock"'s Pop tag or the title.
        self.assertIsNone(block_reason(S["s3"], ["Rock"], []))

    def test_album_and_path_patterns(self):
        self.assertIn("album", block_reason(S["s3"], [], ["christmas"]))
        self.assertIn("path", block_reason(S["s7"], [], ["XMAS"]))
        self.assertIsNone(block_reason(S["s6"], [], ["christmas", "xmas"]))

    def test_name_matcher(self):
        m = name_matcher("_automatic")
        self.assertTrue(m.search("Driving Rock_automatic"))
        self.assertTrue(m.search("Late Night Piano_automatic (2)"))
        self.assertFalse(m.search("My Favourites"))
        self.assertFalse(m.search("x_automatic_old"))
        self.assertTrue(name_matcher("").search("Foo_automatic"))


class AgainstFakeServer(unittest.TestCase):
    def setUp(self):
        self.srv, self.state = fake_navidrome.serve()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.client = Subsonic(self.url, user="audiomuse", password="secret")
        self.rock = self.state.add("Driving Rock_automatic", ["s2", "s1", "s6", "s3"])
        self.p1 = self.state.add("Late Night Piano_automatic (1)", ["s4", "s5", "s4"])
        self.p2 = self.state.add("Late Night Piano_automatic (2)", ["s5", "s7"])
        self.mine = self.state.add("My Favourites", ["s1", "s4"], owner="william")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def songs(self, pid):
        return self.state.playlists[pid]["songs"] if pid in self.state.playlists else None

    def run_filter(self, **kw):
        args = dict(excluded_genres=["Christmas", "Classical"], patterns=["christmas", "xmas"],
                    suffix="_automatic", apply=False)
        args.update(kw)
        return filter_server(self.client, **args)

    def test_preview_changes_nothing(self):
        before = {k: list(v["songs"]) for k, v in self.state.playlists.items()}
        summary = self.run_filter()
        self.assertEqual({k: v["songs"] for k, v in self.state.playlists.items()}, before)
        self.assertEqual(summary["checked"], 3)
        self.assertEqual(summary["removed"], 5)  # s1, s3 | s4, s4 | s7
        self.assertFalse(any(c[0] in ("updatePlaylist", "deletePlaylist") for c in self.state.calls))

    def test_apply_removes_only_blocked_and_leaves_other_playlists(self):
        summary = self.run_filter(apply=True)
        self.assertEqual(self.songs(self.rock), ["s2", "s6"])
        self.assertEqual(self.songs(self.p1), ["s5"])
        self.assertEqual(self.songs(self.p2), ["s5"])
        self.assertEqual(self.songs(self.mine), ["s1", "s4"])
        self.assertEqual(summary["removed"], 5)
        # Running again is a no-op.
        again = self.run_filter(apply=True)
        self.assertEqual((again["removed"], again["changed"]), (0, 0))

    def test_other_users_playlists_are_skipped(self):
        theirs = self.state.add("Someone Else_automatic", ["s1", "s2"], owner="jude")
        summary = self.run_filter(apply=True, owner="audiomuse")
        self.assertEqual(self.songs(theirs), ["s1", "s2"])
        self.assertEqual(summary["checked"], 3)
        # Without a known owner (API-key login) every matching playlist is checked.
        self.assertEqual(self.run_filter(apply=False)["checked"], 4)

    def test_min_tracks_deletes_small_playlists(self):
        summary = self.run_filter(apply=True, min_tracks=2)
        self.assertIsNone(self.songs(self.p1))
        self.assertIsNone(self.songs(self.p2))
        self.assertEqual(self.songs(self.rock), ["s2", "s6"])
        self.assertEqual(summary["deleted"], 2)

    def test_large_removal_is_batched_and_correct(self):
        big = ["s1" if i % 3 == 0 else "s2" for i in range(450)]
        pid = self.state.add("Huge_automatic", big)
        self.run_filter(apply=True)
        self.assertEqual(self.songs(pid), ["s2"] * 300)
        batches = [c for c in self.state.calls
                   if c[0] == "updatePlaylist" and c[1]["playlistId"] == [pid]]
        self.assertEqual(len(batches), 2)  # 150 removals in batches of 100

    def test_bad_password_raises_without_leaking_it(self):
        client = Subsonic(self.url, user="audiomuse", password="wrong")
        with self.assertRaises(SubsonicError) as ctx:
            client.playlists()
        self.assertIn("Wrong username", str(ctx.exception))
        self.assertNotIn("wrong".encode().hex(), str(ctx.exception))

    def test_api_key_auth(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.srv, self.state = fake_navidrome.serve(api_key="KEY123")
        url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.assertEqual(Subsonic.from_creds({"url": url, "api_key": "KEY123"}).playlists(), [])

    def test_unreachable_server_message_is_redacted(self):
        client = Subsonic("http://127.0.0.1:1", user="u", password="pw")
        with self.assertRaises(SubsonicError) as ctx:
            client.playlists()
        self.assertNotIn("pw".encode().hex(), str(ctx.exception))

    def test_genres_with_counts(self):
        names = {g["name"]: g["songs"] for g in self.client.genres()}
        self.assertEqual(names["Rock"], 2)
        self.assertEqual(names["Classical"], 1)

    def test_missing_creds(self):
        with self.assertRaises(SubsonicError):
            Subsonic.from_creds({"url": self.url})
        with self.assertRaises(SubsonicError):
            Subsonic.from_creds({"user": "a", "password": "b"})


if __name__ == "__main__":
    unittest.main()
