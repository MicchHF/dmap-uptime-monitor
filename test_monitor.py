import json
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
from wsgiref.util import setup_testing_defaults

import monitor
from redis.exceptions import LockError


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.data = {}
        self.redis = Mock()
        self.redis.lock.return_value = nullcontext()
        self.redis.get.side_effect = self.data.get
        self.redis.set.side_effect = self.data.__setitem__
        self.notify = Mock()
        self.chat_ids = ('123', '456')
        self.errors = Mock(return_value={"/map/": "тайм-аут"})

    def check_at(self, when):
        with patch.object(monitor.time, "time", return_value=when):
            return monitor.run_check(self.redis, self.notify, self.chat_ids, self.errors)

    def test_one_alert_per_incident_and_one_recovery(self):
        self.check_at(120)
        self.notify.assert_not_called()
        self.check_at(240)
        self.assertIn("/map/ — тайм-аут", self.notify.call_args.args[1])
        self.check_at(360)
        self.assertEqual(self.notify.call_count, 2)
        self.errors.return_value = {}
        self.check_at(480)
        self.assertIn("снова отвечают", self.notify.call_args.args[1])
        self.check_at(600)
        self.assertEqual(self.notify.call_count, 4)

    def test_retry_does_not_count_as_second_failed_check(self):
        self.check_at(120)
        self.assertEqual(self.check_at(125)["status"], "recently_checked")
        self.errors.assert_called_once()
        self.notify.assert_not_called()

    def test_partial_delivery_retries_only_the_missing_chat(self):
        def reject_first_chat(chat_id, _message):
            if chat_id == "123":
                raise monitor.TelegramError("test")

        self.check_at(120)
        self.notify.side_effect = reject_first_chat
        with self.assertRaises(monitor.TelegramError):
            self.check_at(240)
        self.assertEqual(json.loads(self.data[monitor.STATE_KEY])["alerted_chats"], ["456"])
        self.assertEqual([call.args[0] for call in self.notify.call_args_list], ["123", "456"])
        self.notify.reset_mock(side_effect=True)
        self.check_at(360)
        self.assertEqual(self.notify.call_args.args[0], "123")
        self.notify.assert_called_once()

        self.errors.return_value = {}
        self.notify.side_effect = reject_first_chat
        with self.assertRaises(monitor.TelegramError):
            self.check_at(480)
        self.assertEqual(json.loads(self.data[monitor.STATE_KEY])["alerted_chats"], ["123"])
        self.notify.reset_mock(side_effect=True)
        self.check_at(600)
        self.assertEqual(self.notify.call_args.args[0], "123")
        self.notify.assert_called_once()
        self.assertEqual(json.loads(self.data[monitor.STATE_KEY])["alerted_chats"], [])

    def test_report_records_short_and_continuing_incidents(self):
        self.check_at(120)
        self.assertEqual(self.check_at(125)["status"], "recently_checked")
        self.errors.return_value = {}
        self.check_at(240)
        self.notify.assert_not_called()
        self.errors.return_value = {"/map/": "тайм-аут"}
        self.check_at(360)
        self.errors.return_value = {"/map/cabinet/": "HTTP 503"}
        self.check_at(480)

        state = json.loads(self.data[monitor.STATE_KEY])
        self.assertEqual(state["report_days"]["1970-01-01"], {"total": 4, "failed": 3})
        self.assertEqual(state["incidents"], [
            {"started_at": 120, "recovered_at": 240, "targets": ["/map/"]},
            {"started_at": 360, "recovered_at": None, "targets": ["/map/", "/map/cabinet/"]},
        ])
        with patch.object(monitor.time, "time", return_value=480):
            page = monitor.render_report(state)
        self.assertIn('href="/report" aria-current="page"', page)
        self.assertIn("25,0%", page)
        self.assertIn("Восстановлено", page)
        self.assertIn("Продолжается", page)

    def test_report_expires_completed_incidents_but_keeps_an_ongoing_one(self):
        self.check_at(120)
        self.errors.return_value = {}
        self.check_at(240)
        self.errors.return_value = {"/map/": "тайм-аут"}
        self.check_at(360)

        now = 31 * 24 * 60 * 60
        self.check_at(now)
        state = json.loads(self.data[monitor.STATE_KEY])
        self.assertEqual(len(state["report_days"]), 1)
        self.assertEqual(state["incidents"], [
            {"started_at": 360, "recovered_at": None, "targets": ["/map/"]},
        ])


    def test_endpoints_require_auth_and_overlapping_check_is_skipped(self):
        config = {
            "CHECK_TOKEN": "x" * 32,
            "TELEGRAM_BOT_TOKEN": "1:dummy",
            "TELEGRAM_CHAT_IDS": "123, 456, 123",
            "REDIS_URL": "redis://localhost:6379",
        }
        with patch.dict(monitor.os.environ, config), patch.object(
            monitor.Redis, "from_url", return_value=self.redis
        ), patch.object(monitor, "telegram") as telegram:
            app = monitor.create_app()

            def request(path, method="POST", authorization=""):
                environ = {}
                setup_testing_defaults(environ)
                environ.update(PATH_INFO=path, REQUEST_METHOD=method, HTTP_AUTHORIZATION=authorization)
                start = Mock()
                response = b"".join(app(environ, start))
                return start.call_args.args[0], json.loads(response)

            self.assertEqual(request("/healthz", "GET"), ("200 OK", {"ok": True}))
            self.assertEqual(request("/check")[0], "401 Unauthorized")
            self.assertEqual(request("/test-alert", authorization="Bearer иной".encode().decode("latin-1"))[0],
                             "401 Unauthorized")
            self.redis.lock.assert_not_called()
            telegram.assert_not_called()
            auth = "Bearer " + config["CHECK_TOKEN"]
            self.redis.lock.side_effect = LockError()
            self.assertEqual(request("/check", authorization=auth),
                             ("202 Accepted", {"status": "check_in_progress"}))
            self.assertEqual(request("/test-alert", authorization=auth),
                             ("200 OK", {"status": "test_sent"}))
            self.assertEqual([call.args[2]["chat_id"] for call in telegram.call_args_list], [123, 456])


if __name__ == "__main__":
    unittest.main()
