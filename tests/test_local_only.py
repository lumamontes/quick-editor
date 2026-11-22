"""The studio's API refuses other websites (CSRF) and other host names (DNS rebinding)."""
import asyncio
import unittest

from quick_editor.server import LocalOnly


def call(method="POST", host="localhost:4400", origin=None, ctype="application/json"):
    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    if ctype:
        headers.append((b"content-type", ctype.encode()))
    seen = {}

    async def inner(scope, receive, send):
        seen["passed"] = True

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": method, "headers": headers, "path": "/api/save", "query_string": b""}
    asyncio.run(LocalOnly(inner)(scope, receive, send))
    if seen.get("passed"):
        return 200
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


class LocalOnlyTest(unittest.TestCase):
    def test_own_page_passes(self):
        self.assertEqual(call(origin="http://localhost:4400"), 200)
        self.assertEqual(call(host="127.0.0.1:4400", origin="http://127.0.0.1:4400"), 200)
        self.assertEqual(call(method="GET", ctype=None), 200)

    def test_other_sites_are_refused(self):
        self.assertEqual(call(origin="https://evil.example"), 403)
        self.assertEqual(call(origin="http://localhost:4400", ctype="text/plain"), 415)
        self.assertEqual(call(ctype="multipart/form-data"), 415)

    def test_other_host_names_are_refused(self):
        self.assertEqual(call(method="GET", host="evil.example:4400", ctype=None), 403)
        self.assertEqual(call(host="evil.example", origin="http://evil.example"), 403)


if __name__ == "__main__":
    unittest.main()
