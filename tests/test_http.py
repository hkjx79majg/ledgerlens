import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens.server import Handler

VALIDATE_PATH = "/v1/journal-entries/validate"


class HttpTestCase(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: threading.Thread

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, content_type=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            if not raw and not isinstance(body, bytes):
                body = json.dumps(body).encode("utf-8")
                content_type = content_type or "application/json"
            headers["Content-Type"] = content_type or ""
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data.decode("utf-8"))

    def balanced_body(self):
        return {
            "voucher_id": "V-1",
            "posting_date": "2026-03-31",
            "currency": "CNY",
            "lines": [
                {"line_id": "L1", "account_code": "1001", "debit": "10.00", "credit": "0"},
                {"line_id": "L2", "account_code": "2001", "debit": "0", "credit": "10.00"},
            ],
        }


class HealthAndRoutingTest(HttpTestCase):
    def test_healthz_unchanged(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/healthz")
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "ledgerlens")
        self.assertIn("version", data)

    def test_unknown_get_path_still_404_json(self):
        status, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_unknown_post_path_404_json(self):
        status, body = self.request("POST", "/nope", body={}, content_type="application/json")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


class ValidateTransportTest(HttpTestCase):
    def test_balanced_request(self):
        status, body = self.request("POST", VALIDATE_PATH, body=self.balanced_body())
        self.assertEqual(status, 200)
        self.assertEqual(
            body, {"valid": True, "debit_total": "10.00", "credit_total": "10.00"}
        )

    def test_application_json_with_charset_accepted(self):
        status, body = self.request(
            "POST", VALIDATE_PATH, body=self.balanced_body(),
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])

    def test_unbalanced_is_422_error_list(self):
        payload = self.balanced_body()
        payload["lines"][1]["credit"] = "9.00"
        status, body = self.request("POST", VALIDATE_PATH, body=payload)
        self.assertEqual(status, 422)
        self.assertFalse(body["valid"])
        self.assertEqual(body["errors"][0]["code"], "unbalanced_entry")
        self.assertNotIn("error", body)
        self.assertNotIn("debit_total", body)

    def test_unsupported_media_type(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
            with self.subTest(content_type=content_type):
                status, body = self.request(
                    "POST", VALIDATE_PATH, body=b"{}", content_type=content_type, raw=True
                )
                self.assertEqual(status, 415)
                self.assertEqual(body["error"]["code"], "unsupported_media_type")
                self.assertTrue(body["error"]["message"])

    def test_invalid_json(self):
        status, body = self.request(
            "POST", VALIDATE_PATH, body=b"{not json", content_type="application/json", raw=True
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_top_level_must_be_object(self):
        for payload in (b"[]", b'"x"', b"42", b"true", b"null"):
            with self.subTest(payload=payload):
                status, body = self.request(
                    "POST", VALIDATE_PATH, body=payload,
                    content_type="application/json", raw=True,
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "request_not_object")

    def test_validation_errors_are_not_error_object(self):
        status, body = self.request("POST", VALIDATE_PATH, body={})
        self.assertEqual(status, 422)
        self.assertNotIn("error", body)
        self.assertFalse(body["valid"])
        self.assertIsInstance(body["errors"], list)

    def test_requests_are_independent(self):
        first_status, first = self.request("POST", VALIDATE_PATH, body=self.balanced_body())
        bad = self.balanced_body()
        bad["lines"][0]["debit"] = "1.000"
        self.request("POST", VALIDATE_PATH, body=bad)
        second_status, second = self.request("POST", VALIDATE_PATH, body=self.balanced_body())
        self.assertEqual((first_status, first), (second_status, second))


if __name__ == "__main__":
    unittest.main()
