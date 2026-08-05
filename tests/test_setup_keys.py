import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_bridge_collector.config import CollectorConfig
from wechat_bridge_collector.setup_keys import (
    _format_extract_error,
    extract_wechat_keys,
    setup_collector,
)


class SetupKeysTest(unittest.TestCase):
    def test_extract_error_redacts_scanner_secrets(self):
        detail = _format_extract_error(
            "\n".join(
                [
                    "image_aes_key = 28843e68111936eb",
                    "UIN=678542980",
                    "wxid=private_account",
                    "MISSING: message/message_0.db (salt=16db2726561609e3601798513e6a13b2)",
                    "[ERROR] 未能从微信进程提取到密钥",
                ]
            ),
            "",
        )

        self.assertIn("key extraction failed", detail)
        self.assertIn("MISSING: message/message_0.db", detail)
        self.assertIn("[ERROR] 未能从微信进程提取到密钥", detail)
        self.assertNotIn("28843e68111936eb", detail)
        self.assertNotIn("678542980", detail)
        self.assertNotIn("private_account", detail)
        self.assertNotIn("16db2726561609e3601798513e6a13b2", detail)

    def test_setup_writes_collector_owned_config_without_extracting(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd_dir = Path(tmp) / "wechat-decrypt"
            wd_dir.mkdir()
            (wd_dir / "key_utils.py").write_text("", encoding="utf-8")
            db_dir = Path(tmp) / "db_storage"
            db_dir.mkdir()
            state_dir = Path(tmp) / "state"

            cfg = CollectorConfig(
                state_dir=str(state_dir),
                wechat_decrypt_dir=str(wd_dir),
                db_dir=str(db_dir),
            )
            result = setup_collector(cfg, extract_keys=False)

            saved = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "config_written")
        self.assertEqual(saved["keys_file"], str(state_dir / "all_keys.json"))
        self.assertEqual(saved["decrypted_dir"], str(state_dir / "decrypted"))
        self.assertEqual(saved["db_dir"], str(db_dir))

    def test_windows_extraction_writes_keys_to_connector_owned_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wd_dir = root / "wechat-decrypt"
            wd_dir.mkdir()
            (wd_dir / "key_utils.py").write_text("", encoding="utf-8")
            (wd_dir / "find_all_keys.py").write_text("", encoding="utf-8")
            db_dir = root / "db_storage"
            db_dir.mkdir()
            output_path = root / "connector-data" / "all_keys.json"
            observed_runtime_dir = None

            cfg = CollectorConfig(
                wechat_decrypt_dir=str(wd_dir),
                db_dir=str(db_dir),
                decrypted_dir=str(root / "decrypted"),
            )

            def fake_run(_args, *, cwd, env, **_kwargs):
                nonlocal observed_runtime_dir
                observed_runtime_dir = Path(env["WECHAT_DECRYPT_APP_DIR"])
                runtime_config = json.loads(
                    (observed_runtime_dir / "config.json").read_text(encoding="utf-8")
                )
                self.assertEqual(runtime_config["db_dir"], str(db_dir))
                self.assertEqual(runtime_config["keys_file"], str(output_path.resolve()))
                self.assertEqual(cwd, str(output_path.parent))
                output_path.write_text("{}\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch(
                "wechat_bridge_collector.setup_keys.os.uname",
                return_value=SimpleNamespace(sysname="Windows"),
            ), patch(
                "wechat_bridge_collector.setup_keys.subprocess.run",
                side_effect=fake_run,
            ):
                extract_wechat_keys(cfg, output_path)

            self.assertTrue(output_path.is_file())
            self.assertIsNotNone(observed_runtime_dir)
            self.assertFalse(observed_runtime_dir.exists())


if __name__ == "__main__":
    unittest.main()
