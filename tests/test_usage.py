import io
import json
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, mock_open, patch
from urllib.error import HTTPError, URLError

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)
sys.modules["decky"] = types.SimpleNamespace(DECKY_PLUGIN_DIR=ROOT, DECKY_USER_HOME="/unused",
                                          DECKY_PLUGIN_SETTINGS_DIR="/unused", logger=logging.getLogger("usage"))
import main


class UsageTests(unittest.TestCase):
    def test_parse_merge_and_expiry(self):
        week = {"utilization": .4, "resetsAt": 2000000000}
        old = {"status": "rejected", "other": True, "unifiedWindows": {"seven_day": week}}
        data = {"five_hour": {"utilization": 25, "resets_at": "2033-05-18T03:33:20Z"}}
        with patch.object(main.time, "time", return_value=1900000000):
            merged = main._merge_usage(old, data)
            self.assertEqual(merged["unifiedWindows"], {"seven_day": week,
                             "five_hour": {"utilization": .25, "resetsAt": 2000000000}})
            self.assertTrue(merged["other"])
            self.assertNotIn("status", merged)
            for percent in (True, "25", float("nan"), float("inf")):
                data["five_hour"]["utilization"] = percent
                self.assertEqual(main._merge_usage(old, data), old)
            data["five_hour"] = {"utilization": 150, "resets_at": "2033-05-18T03:33:20+00:00"}
            self.assertEqual(main._merge_usage(old, data)["unifiedWindows"]["five_hour"]["utilization"], 1)
            for reset in (None, "invalid", "2033-05-18T03:33:20"):
                data["five_hour"]["resets_at"] = reset
                self.assertEqual(main._merge_usage(old, data), old)
            self.assertNotIn("unifiedWindows", main._merge_usage(old, {"seven_day": None}))
            partial = {"status": "rejected", "unifiedWindows": {"five_hour": {"resetsAt": 1900000000}, "seven_day": week}}
            self.assertEqual(main._current_usage(partial), {"unifiedWindows": {"seven_day": week}})
        self.assertEqual(main._current_usage(old, 2000000000), {"other": True})
        self.assertEqual(old["status"], "rejected")
        self.assertIsNone(main._current_usage(None))

    def test_fetch_and_errors_never_log_token(self):
        token = "FAKE_USAGE_TOKEN_MUST_NOT_APPEAR"
        credentials = json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": 3000000000000}})
        opener = Mock()
        payload = {"five_hour": {"utilization": 25, "resets_at": "2033-05-18T03:33:20Z"}}
        with patch("builtins.open", mock_open(read_data=credentials)), patch.object(main, "_ssl"), \
                patch.object(main.urllib.request, "build_opener", return_value=opener), \
                self.assertLogs("usage", level="INFO") as logs:
            main.decky.logger.info("capture active")
            opener.open.return_value = io.StringIO(json.dumps(payload))
            self.assertEqual(main._fetch_usage(), payload)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer " + token)
            self.assertEqual(request.get_header("Anthropic-beta"), "oauth-2025-04-20")
            for error in (OSError(token), ValueError(token), URLError(token),
                          HTTPError("https://api.anthropic.com", 401, token, {}, None),
                          HTTPError("https://api.anthropic.com", 429, token, {}, None)):
                opener.open.side_effect = error
                self.assertIsNone(main._fetch_usage())
                if isinstance(error, HTTPError):
                    self.assertTrue(error.closed)
            self.assertNotIn(token, repr(main._merge_usage(None, {"accessToken": token})))
        self.assertNotIn(token, "\n".join(logs.output))
        self.assertIsNone(main._NoUsageRedirect().redirect_request(None, None, 302, "", {}, "https://other"))
        with patch("builtins.open", side_effect=FileNotFoundError), \
                patch.object(main.urllib.request, "build_opener") as build:
            self.assertIsNone(main._fetch_usage())
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
