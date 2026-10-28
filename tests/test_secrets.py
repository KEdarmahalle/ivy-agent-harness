from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.secrets import load_secrets


class SecretStoreTests(unittest.TestCase):
    def test_load_secrets_accepts_legacy_plaintext_value_in_windows_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "secrets.json"
            store.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "backend": "windows-dpapi",
                        "values": {"NVIDIA_NIM_API_KEY": "nvapi-test-plaintext"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                values = load_secrets(path=store)
                self.assertEqual(values["NVIDIA_NIM_API_KEY"], "nvapi-test-plaintext")
                self.assertEqual(os.environ["NVIDIA_NIM_API_KEY"], "nvapi-test-plaintext")


if __name__ == "__main__":
    unittest.main()
