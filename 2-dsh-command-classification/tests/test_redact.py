from __future__ import annotations

import unittest

from jevguard.redact import redact


class RedactTests(unittest.TestCase):
    def test_bearer_is_replaced(self) -> None:
        text, hits = redact('curl -H "Authorization: Bearer abcdefghijklmnop" https://x')
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertIn("bearer", hits)

    def test_assignment_is_replaced(self) -> None:
        text, hits = redact("$env:API_KEY = sk-1234567890abcdef")
        self.assertNotIn("sk-1234567890abcdef", text)
        self.assertIn("secret-assignment", hits)

    def test_url_password_is_replaced(self) -> None:
        text, hits = redact("git clone https://user:hunter2@example.com/repo")
        self.assertNotIn("hunter2", text)
        self.assertIn("url-credentials", hits)

    def test_typesafe_key_is_replaced(self) -> None:
        text, hits = redact("TYPESAFE_API_KEY=apik_1234567890abcdefgh")
        self.assertNotIn("apik_1234567890abcdefgh", text)
        self.assertIn("typesafe-key", hits)

    def test_plain_command_is_untouched(self) -> None:
        text, hits = redact("Remove-Item -Recurse -Force .\\build")
        self.assertEqual(text, "Remove-Item -Recurse -Force .\\build")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
