import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jose import jwt

from backend.core import security
from backend.core.security import _load_or_create_secret, _token_expire_minutes, create_access_token


class SecretLoadingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.secret_file = Path(self._tmp.name) / ".jwt_secret"

    def _patched_project_path(self):
        return patch(
            "backend.core.security.project_path",
            return_value=self.secret_file,
        )

    def test_env_var_takes_priority_and_skips_file(self):
        with patch.dict(os.environ, {"AUTODROID_SECRET_KEY": "env-secret"}), self._patched_project_path():
            self.assertEqual(_load_or_create_secret(), "env-secret")
        self.assertFalse(self.secret_file.exists())

    def test_generates_and_persists_secret_when_missing(self):
        with patch.dict(os.environ, {}, clear=False), self._patched_project_path():
            os.environ.pop("AUTODROID_SECRET_KEY", None)
            first = _load_or_create_secret()
            self.assertTrue(self.secret_file.exists())
            second = _load_or_create_secret()

        self.assertEqual(first, second)
        self.assertEqual(self.secret_file.read_text(encoding="utf-8").strip(), first)
        self.assertGreaterEqual(len(first), 64)

    def test_reads_existing_secret_file(self):
        self.secret_file.write_text("stored-secret\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=False), self._patched_project_path():
            os.environ.pop("AUTODROID_SECRET_KEY", None)
            self.assertEqual(_load_or_create_secret(), "stored-secret")


class TokenExpireMinutesTests(unittest.TestCase):
    def test_default_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTODROID_TOKEN_EXPIRE_MINUTES", None)
            self.assertEqual(_token_expire_minutes(), 60 * 24 * 30)

    def test_env_override(self):
        with patch.dict(os.environ, {"AUTODROID_TOKEN_EXPIRE_MINUTES": "120"}):
            self.assertEqual(_token_expire_minutes(), 120)

    def test_invalid_values_fall_back_to_default(self):
        for raw in ("abc", "-5", "0"):
            with patch.dict(os.environ, {"AUTODROID_TOKEN_EXPIRE_MINUTES": raw}):
                self.assertEqual(_token_expire_minutes(), 60 * 24 * 30)


class CreateAccessTokenTests(unittest.TestCase):
    def test_token_is_decodable_and_expires_in_future(self):
        token = create_access_token({"sub": "alice"})
        payload = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])

        self.assertEqual(payload["sub"], "alice")
        expire = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        self.assertGreater(expire, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
