import unittest
from unittest.mock import patch

from nomi_client import NomiAPIError, NomiClient


class FakeResponse:
    def __init__(self, status=200, content_type="image/webp", body=b"avatar"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def read(self):
        return self.body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.request_url = None
        self.request_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, headers):
        self.request_url = url
        self.request_headers = headers
        return self.response


class NomiAvatarTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_avatar_returns_webp_bytes(self):
        response = FakeResponse(body=b"webp-data")
        session = FakeSession(response)

        with patch("nomi_client.aiohttp.ClientSession", return_value=session):
            client = NomiClient("secret-key")
            avatar, content_type = await client.get_avatar("nomi-123")

        self.assertEqual(avatar, b"webp-data")
        self.assertEqual(content_type, "image/webp")
        self.assertEqual(
            session.request_url,
            "https://api.nomi.ai/v1/nomis/nomi-123/avatar",
        )
        self.assertEqual(session.request_headers["Authorization"], "secret-key")

    async def test_get_avatar_preserves_not_found_status(self):
        session = FakeSession(FakeResponse(status=404))

        with patch("nomi_client.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(NomiAPIError) as raised:
                await NomiClient("secret-key").get_avatar("missing")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_get_avatar_rejects_unexpected_content_type(self):
        session = FakeSession(FakeResponse(content_type="text/html"))

        with patch("nomi_client.aiohttp.ClientSession", return_value=session):
            with self.assertRaises(NomiAPIError) as raised:
                await NomiClient("secret-key").get_avatar("nomi-123")

        self.assertEqual(raised.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
