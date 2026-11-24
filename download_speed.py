#!/usr/bin/env python3
"""Замер скорости скачивания: N последовательных запросов к одному URL.

Использование:
    python3 download_speed.py https://example.com/big-image.jpg
"""

import argparse
import http.client
import sys
import time
import urllib.error
import urllib.request

CHUNK_SIZE = 64 * 1024
USER_AGENT = "download-speed-check/1.0"


def fetch(url: str, timeout: float) -> tuple[int, int, float]:
    """Скачивает URL целиком. Возвращает (HTTP-статус, байт получено, секунд)."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Просим не отдавать закешированную копию с промежуточных прокси.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    received = 0
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        expected = _content_length(response)
        while chunk := response.read(CHUNK_SIZE):
            received += len(chunk)
        status = response.status
    elapsed = time.perf_counter() - started
    # http.client при чтении кусками не сообщает об обрыве соединения,
    # а просто отдаёт пустой кусок, поэтому полноту ответа проверяем сами.
    if expected is not None and received < expected:
        raise http.client.IncompleteRead(b"", expected - received)
    return status, received, elapsed


def _content_length(response) -> int | None:
    value = response.headers.get("Content-Length")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def mbit_per_s(num_bytes: int, seconds: float) -> float:
    return num_bytes * 8 / 1_000_000 / seconds if seconds > 0 else 0.0


def mb_per_s(num_bytes: int, seconds: float) -> float:
    return num_bytes / 1_000_000 / seconds if seconds > 0 else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Делает N последовательных запросов к URL и считает скорость скачивания."
    )
    parser.add_argument("url", help="адрес, который скачиваем (http:// или https://)")
    parser.add_argument("-n", "--count", type=int, default=10, help="число запросов (по умолчанию 10)")
    parser.add_argument("-t", "--timeout", type=float, default=60.0, help="таймаут одного запроса, сек (по умолчанию 60)")
    args = parser.parse_args()

    if not args.url.startswith(("http://", "https://")):
        parser.error("адрес должен начинаться с http:// или https://")
    if args.count < 1:
        parser.error("--count должен быть >= 1")

    print(f"URL: {args.url}")
    print(f"Запросов: {args.count}\n")

    total_bytes = 0
    total_time = 0.0

    for i in range(1, args.count + 1):
        try:
            status, received, elapsed = fetch(args.url, args.timeout)
        except urllib.error.HTTPError as e:
            print(f"Запрос {i}: сервер вернул HTTP {e.code} {e.reason}", file=sys.stderr)
            return 1
        except http.client.IncompleteRead as e:
            print(f"Запрос {i}: ответ оборвался, не получено {e.expected} байт", file=sys.stderr)
            return 1
        except http.client.HTTPException as e:
            print(f"Запрос {i}: некорректный HTTP-ответ: {e!r}", file=sys.stderr)
            return 1
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            print(f"Запрос {i}: ошибка соединения: {reason}", file=sys.stderr)
            return 1

        total_bytes += received
        total_time += elapsed
        print(
            f"#{i:>2}  HTTP {status}  {received / 1_000_000:9.3f} МБ  "
            f"{elapsed:8.3f} с  {mbit_per_s(received, elapsed):9.2f} Мбит/с"
        )

    avg_time = total_time / args.count
    print()
    print(f"Среднее время запроса: {avg_time:.3f} с")
    print(f"Всего скачано:         {total_bytes / 1_000_000:.3f} МБ ({total_bytes} байт)")
    print(
        f"Скорость скачивания:   {mbit_per_s(total_bytes, total_time):.2f} Мбит/с "
        f"({mb_per_s(total_bytes, total_time):.2f} МБ/с)"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        sys.exit(130)
