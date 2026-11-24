"""Тесты для download_speed.py.

Запуск из корня проекта:
    python3 -m unittest -v
"""

import contextlib
import io
import re
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import download_speed

PAYLOAD = bytes(range(256)) * 4096  # 1 048 576 байт, больше одного CHUNK_SIZE


class _Handler(BaseHTTPRequestHandler):
    """Тестовый сервер. Поведение задаётся путём запроса."""

    server: "_TestServer"

    def log_message(self, *args):  # не засоряем вывод тестов
        pass

    def do_GET(self):
        srv = self.server
        with srv.lock:
            srv.requests.append({"path": self.path, "headers": dict(self.headers)})
            srv.active += 1
            srv.max_active = max(srv.max_active, srv.active)
        try:
            if self.path == "/file":
                time.sleep(0.01)  # чтобы параллельные запросы точно пересеклись
                self._send(200, PAYLOAD)
            elif self.path == "/empty":
                self._send(200, b"")
            elif self.path == "/missing":
                self._send(404, b"not found")
            elif self.path == "/server-error":
                self._send(500, b"boom")
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/truncated":
                # Обещаем 1000 байт, отдаём 10 и рвём соединение.
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b"x" * 10)
                self.wfile.flush()
                self.close_connection = True
            elif self.path == "/slow":
                time.sleep(1.0)
                self._send(200, b"late")
            else:
                self._send(404, b"")
        finally:
            with srv.lock:
                srv.active -= 1

    def _send(self, code: int, body: bytes):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _TestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.active = 0
        self.max_active = 0


def _free_closed_port() -> int:
    """Порт, на котором точно никто не слушает."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_main(*argv: str) -> tuple[int, str, str]:
    """Запускает main() с аргументами, возвращает (код выхода, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["download_speed.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = download_speed.main()
        except SystemExit as e:  # argparse выходит через SystemExit
            code = e.code
    return code, out.getvalue(), err.getvalue()


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _TestServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        with self.server.lock:
            self.server.requests.clear()
            self.server.max_active = 0


class FetchTest(ServerTestCase):
    def test_downloads_whole_body(self):
        status, received, elapsed = download_speed.fetch(f"{self.base}/file", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(received, len(PAYLOAD))
        self.assertGreater(elapsed, 0)

    def test_empty_body(self):
        status, received, _ = download_speed.fetch(f"{self.base}/empty", timeout=5)
        self.assertEqual((status, received), (200, 0))

    def test_follows_redirect(self):
        status, received, _ = download_speed.fetch(f"{self.base}/redirect", timeout=5)
        self.assertEqual((status, received), (200, len(PAYLOAD)))

    def test_sends_no_cache_and_user_agent(self):
        download_speed.fetch(f"{self.base}/empty", timeout=5)
        headers = self.server.requests[-1]["headers"]
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        self.assertEqual(headers.get("Pragma"), "no-cache")
        self.assertEqual(headers.get("User-Agent"), download_speed.USER_AGENT)

    def test_time_covers_server_delay(self):
        _, _, elapsed = download_speed.fetch(f"{self.base}/slow", timeout=5)
        self.assertGreaterEqual(elapsed, 1.0)


class MainAgainstServerTest(ServerTestCase):
    def test_ten_sequential_requests_by_default(self):
        code, out, err = run_main(f"{self.base}/file")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.server.requests), 10)
        self.assertEqual(self.server.max_active, 1, "запросы должны идти последовательно")
        self.assertEqual(len(re.findall(r"^#\s*\d+\s+HTTP 200", out, re.M)), 10)

    def test_total_bytes_reported(self):
        code, out, _ = run_main("-n", "3", f"{self.base}/file")
        self.assertEqual(code, 0)
        self.assertIn(f"({3 * len(PAYLOAD)} байт)", out)
        self.assertRegex(out, r"Среднее время запроса: \d+\.\d{3} с")
        self.assertRegex(out, r"Скорость скачивания:\s+\d+\.\d{2} Мбит/с \(\d+\.\d{2} МБ/с\)")

    def test_count_option(self):
        code, _, _ = run_main("--count", "4", f"{self.base}/empty")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.server.requests), 4)

    def test_http_404_stops_with_error(self):
        code, out, err = run_main(f"{self.base}/missing")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", err)
        self.assertEqual(len(self.server.requests), 1, "после ошибки запросы не продолжаются")
        self.assertNotIn("Скорость скачивания", out)

    def test_http_500_stops_with_error(self):
        code, _, err = run_main(f"{self.base}/server-error")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", err)

    def test_truncated_response_is_an_error(self):
        code, out, err = run_main("-n", "1", f"{self.base}/truncated")
        self.assertEqual(code, 1)
        self.assertIn("Запрос 1", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Скорость скачивания", out)

    def test_timeout_is_an_error(self):
        code, _, err = run_main("-n", "1", "-t", "0.2", f"{self.base}/slow")
        self.assertEqual(code, 1)
        self.assertIn("Запрос 1", err)


class MainWithoutServerTest(unittest.TestCase):
    def test_connection_refused(self):
        code, _, err = run_main("-n", "1", f"http://127.0.0.1:{_free_closed_port()}/x")
        self.assertEqual(code, 1)
        self.assertIn("ошибка соединения", err)

    def test_rejects_non_http_url(self):
        code, _, err = run_main("ftp://example.com/file")
        self.assertEqual(code, 2)
        self.assertIn("http://", err)

    def test_rejects_zero_count(self):
        code, _, _ = run_main("-n", "0", "http://example.com/")
        self.assertEqual(code, 2)

    def test_requires_url(self):
        code, _, _ = run_main()
        self.assertEqual(code, 2)


class CalculationTest(unittest.TestCase):
    """Арифметика итогов на подменённом fetch() с известными цифрами."""

    def test_summary_numbers(self):
        # Каждый запрос: 1 000 000 байт за 0.5 с -> 16 Мбит/с, 2 МБ/с.
        with mock.patch.object(download_speed, "fetch", return_value=(200, 1_000_000, 0.5)) as fetch:
            code, out, _ = run_main("http://example.com/")
        self.assertEqual(code, 0)
        self.assertEqual(fetch.call_count, 10)
        self.assertIn("Среднее время запроса: 0.500 с", out)
        self.assertIn("Всего скачано:         10.000 МБ (10000000 байт)", out)
        self.assertIn("Скорость скачивания:   16.00 Мбит/с (2.00 МБ/с)", out)

    def test_speed_is_total_bytes_over_total_time(self):
        # 3 МБ за 1 с и 1 МБ за 3 с: 4 МБ / 4 с = 1 МБ/с = 8 Мбит/с
        # (а не среднее скоростей (24 + 2.67) / 2).
        results = [(200, 3_000_000, 1.0), (200, 1_000_000, 3.0)]
        with mock.patch.object(download_speed, "fetch", side_effect=results):
            code, out, _ = run_main("-n", "2", "http://example.com/")
        self.assertEqual(code, 0)
        self.assertIn("Среднее время запроса: 2.000 с", out)
        self.assertIn("Скорость скачивания:   8.00 Мбит/с (1.00 МБ/с)", out)

    def test_unit_helpers(self):
        self.assertAlmostEqual(download_speed.mbit_per_s(1_000_000, 1.0), 8.0)
        self.assertAlmostEqual(download_speed.mb_per_s(1_000_000, 1.0), 1.0)
        self.assertAlmostEqual(download_speed.mbit_per_s(125_000, 0.5), 2.0)
        self.assertEqual(download_speed.mbit_per_s(100, 0), 0.0)
        self.assertEqual(download_speed.mb_per_s(100, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
