"""Tests for braveduck_service (stdlib unittest): python -m unittest test_braveduck_service"""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import braveduck_service as bds


class Visibility(unittest.TestCase):
    def test_labels(self):
        for text, want in [
            ("Tuyển Dụng AI\nPublic group · 12K members\nAbout", "public"),
            ("Tuyển Dụng AI\nNhóm Công khai · 12K thành viên", "public"),
            ("Secret Club\nPrivate group · 300 members", "private"),
            ("Secret Club\nNhóm Riêng tư · 300 thành viên", "private"),
            ("Just some text", None),
            ("Public group\n…Private group", None),  # both: can't tell
            ("Republic groupie", None),              # not the label
            ("x" * 2000 + "Public group", None),     # far down: a suggestion, not this group
            ("", None),
        ]:
            self.assertEqual(bds.parse_visibility(text), want, text[:40])

    def test_name_and_id(self):
        self.assertEqual(bds.parse_name("Tuyển Dụng AI Engineer Việt Nam | Facebook"), "Tuyển Dụng AI Engineer Việt Nam")
        self.assertEqual(bds.parse_group_id("", "3989249071208661"), "3989249071208661")
        self.assertEqual(bds.parse_group_id('<meta content="fb://group/?id=1234567890">', "ai.jobs"), "1234567890")
        self.assertEqual(bds.parse_group_id('{"groupID":"98765432101"}', "ai.jobs"), "98765432101")
        self.assertIsNone(bds.parse_group_id("<html>no id</html>", "ai.jobs"))


class Schedule(unittest.TestCase):
    def test_due_for_check(self):
        now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        recent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(bds.due_for_check({"visibility": "checking"}, now))
        self.assertTrue(bds.due_for_check({"visibility": "public", "checked_at": ""}, now))
        self.assertFalse(bds.due_for_check({"visibility": "public", "checked_at": recent}, now))
        self.assertTrue(bds.due_for_check({"visibility": "private", "checked_at": old}, now))

    def test_new_posts_oldest_first(self):
        posts = [{"key": "g:3", "url": "u3", "text": "c"}, {"key": "g:2", "url": "u2", "text": "b"},
                 {"key": "g:1", "url": "u1", "text": "a"}, {"key": "g:0", "url": "", "text": "no link"}]
        got = bds.new_posts(posts, lambda k: k == "g:2")
        self.assertEqual([p["key"] for p in got], ["g:1", "g:3"])


class API(unittest.TestCase):
    def test_calls_with_key(self):
        seen = []

        class Fake(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body):
                seen.append((self.command, self.path, self.headers.get("Authorization"),
                             json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")))
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply([{"key": "ai.jobs", "url": "https://www.facebook.com/groups/ai.jobs/", "visibility": "checking"}])

            def do_POST(self):
                self._reply({"added": 1, "boards": 2})

        srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            api = bds.ScannerAPI(f"http://127.0.0.1:{srv.server_port}/", "k" * 40)
            self.assertEqual(api.groups()[0]["key"], "ai.jobs")
            api.status("ai.jobs", "public", "AI Jobs", "")
            api.posts("ai.jobs", [{"url": "https://www.facebook.com/groups/1/posts/2/", "text": "Tuyển n8n", "key": "x"}])
        finally:
            srv.shutdown()
        self.assertEqual([(m, p) for m, p, _, _ in seen],
                         [("GET", "/groups"), ("POST", "/groups/ai.jobs/status"), ("POST", "/groups/ai.jobs/posts")])
        self.assertTrue(all(auth == "Bearer " + "k" * 40 for _, _, auth, _ in seen))
        self.assertEqual(seen[2][3], {"posts": [{"url": "https://www.facebook.com/groups/1/posts/2/", "text": "Tuyển n8n"}]})


if __name__ == "__main__":
    unittest.main()
