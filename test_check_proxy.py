import asyncio
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import httpx
from rich.console import Console

import check_proxy


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = set()
        self.server = await asyncio.start_server(self.respond, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.proxy = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        connections = list(self.connections)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)

    async def respond(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            target = request.split(b" ")[1]
            if target.endswith(b"/slow"):
                await asyncio.sleep(0.2)
            if target.endswith(b"/drip"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n")
                for _ in range(20):
                    writer.write(b"x")
                    await writer.drain()
                    await asyncio.sleep(0.02)
            else:
                status = b"503 Service Unavailable" if target.endswith(b"/failure") else b"200 OK"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 12\r\nConnection: close\r\n\r\n198.51.100.1")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.connections.discard(task)

    async def test_latency_preserves_client_read_timeout(self):
        async with httpx.AsyncClient(timeout=0.03, trust_env=False) as client:
            with self.assertRaises(httpx.ReadTimeout):
                await check_proxy.measure_latency(client, self.proxy + "/slow", 0.5)

    async def test_latency_deadline_stops_a_trickling_response(self):
        async with httpx.AsyncClient(timeout=0.1, trust_env=False) as client:
            with self.assertRaises(TimeoutError):
                await check_proxy.measure_latency(client, self.proxy + "/drip", 0.06)

    async def test_latency_returns_status_time_and_outbound_ip(self):
        async with httpx.AsyncClient(trust_env=False) as client:
            status, latency, out_ip = await check_proxy.measure_latency(
                client, self.proxy + "/ip", 0.5
            )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(latency, 0)
        self.assertEqual(out_ip, "198.51.100.1")

    async def test_proxy_latency_deadline_reports_dead(self):
        result = await check_proxy.test_one_proxy(
            self.proxy, check_url="http://check.invalid/slow", latency_timeout_s=0.03
        )
        self.assertFalse(result.alive)
        self.assertIn("Timeout", result.error)
        self.assertIsNone(result.download_mb_s)

    async def test_speed_failure_keeps_proxy_alive(self):
        result = await check_proxy.test_one_proxy(
            self.proxy,
            check_url="http://check.invalid/ip",
            speed_url="http://check.invalid/failure",
            speed_max_bytes=12,
        )
        self.assertTrue(result.alive)
        self.assertEqual(result.out_ip, "198.51.100.1")
        self.assertEqual(result.download_error, "HTTP 503")

    async def test_cli_prints_and_exports_both_proxy_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "input_proxy"
            port = self.server.sockets[0].getsockname()[1]
            input_file.write_text(f"http|127.0.0.1|{port}|user|secret\nhttp|127.0.0.1|0\n")
            output = Path(directory) / "results"
            process = await asyncio.create_subprocess_exec(
                sys.executable, check_proxy.__file__, "-i", str(input_file),
                "--check-url", "http://check.invalid/ip",
                "--speed-url", "http://check.invalid/failure",
                "--latency-timeout", "0.1", "--output-dir", str(output),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertIn("ALIVE: 1", stdout.decode())
            self.assertIn("DEAD: 1", stdout.decode())
            self.assertNotIn("secret", stdout.decode())
            self.assertEqual((output / "alive.txt").read_text(), f"http://user:secret@127.0.0.1:{port}\n")
            self.assertEqual((output / "dead.txt").read_text(), "http://127.0.0.1:0\n")

    async def test_cli_refuses_to_overwrite_the_input_file(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "alive.txt"
            port = self.server.sockets[0].getsockname()[1]
            original = f"http|127.0.0.1|{port}\n"
            input_file.write_text(original)
            process = await asyncio.create_subprocess_exec(
                sys.executable, check_proxy.__file__, "-i", str(input_file),
                "--output-dir", directory,
                "--check-url", "http://check.invalid/ip",
                "--speed-url", "http://check.invalid/failure",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(input_file.read_text(), original)
            self.assertNotEqual(process.returncode, 0)

    async def test_live_active_count_matches_workers_including_duplicate_proxies(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as directory:
                port = self.server.sockets[0].getsockname()[1]
                input_file = Path(directory) / "input_proxy"
                input_file.write_text("".join(
                    f"http|127.0.0.1|{port}|user{0 if duplicate else n}|secret\n"
                    for n in range(60)
                ))
                process = await asyncio.create_subprocess_exec(
                    sys.executable, check_proxy.__file__, "-i", str(input_file), "-c", "20",
                    "--output-dir", directory,
                    "--check-url", "http://check.invalid/ip",
                    "--speed-url", "http://check.invalid/slow",
                    env={**os.environ, "FORCE_COLOR": "1", "COLUMNS": "240", "LINES": "24"},
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
                self.assertEqual(process.returncode, 0, stderr.decode())
                text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stdout.decode())
                active_counts = [int(count) for count in re.findall(r"Active: (\d+)", text)]
                self.assertTrue(active_counts, "Live output must report running workers")
                self.assertEqual(max(active_counts), 20)
                active_rows = [
                    frame.count("Downloading") + frame.count("Ping...")
                    for frame in text.split("TIẾN TRÌNH CHECK ĐỒNG THỜI")[1:]
                ]
                self.assertEqual(max(active_rows), 20, "Live output must retain all worker rows")


class OutputTests(unittest.TestCase):
    def test_live_lists_ten_latest_alive_results_regardless_of_final_sort(self):
        for sort_by in ("none", "speed", "latency"):
            with self.subTest(sort_by=sort_by):
                state = check_proxy.CheckerState(total=13, sort_by=sort_by)
                state.completed = [
                    check_proxy.ProxyResult(
                        f"http://proxy-{n:02}.example:80", True,
                        latency_ms=n + 1, download_mb_s=100 - n,
                    ) for n in range(12)
                ] + [check_proxy.ProxyResult("http://dead.example:80", False)]
                output = io.StringIO()
                Console(file=output, width=240, color_system=None).print(check_proxy.make_render_group(state))
                text = output.getvalue()
                self.assertNotIn("proxy-00.example", text)
                self.assertNotIn("proxy-01.example", text)
                self.assertNotIn("dead.example", text)
                positions = [text.index(f"proxy-{n:02}.example") for n in range(11, 1, -1)]
                self.assertEqual(positions, sorted(positions))

    def test_live_displays_all_twenty_running_workers(self):
        state = check_proxy.CheckerState(total=60)
        state.active_tasks = {
            n: check_proxy.ActiveTaskInfo(f"http://active-{n:02}.example:80", phase="Kiểm tra Ping")
            for n in range(20)
        }
        output = io.StringIO()
        Console(file=output, width=240, color_system=None).print(check_proxy.make_render_group(state))
        text = output.getvalue()
        for n in range(20):
            self.assertIn(f"active-{n:02}.example", text, f"Missing running worker {n}")
        self.assertIn("Active: 20", text)

    def test_terminal_lists_every_proxy_and_masks_passwords(self):
        results = [
            check_proxy.ProxyResult(f"http://user:secret@alive-{n:03}.example:80", True)
            for n in range(60)
        ] + [
            check_proxy.ProxyResult(f"http://user:secret@dead-{n:03}.example:80", False, error="Timeout")
            for n in range(40)
        ]
        output = io.StringIO()
        console = Console(file=output, width=240, color_system=None)
        with patch.object(check_proxy, "Console", return_value=console):
            check_proxy.print_results(results)
        text = output.getvalue()
        for result in results:
            self.assertIn(result.proxy.split("@", 1)[1], text)
        self.assertNotIn("secret", text)

    def test_files_contain_complete_unmasked_urls_and_replace_old_results(self):
        results = [
            check_proxy.ProxyResult("http://user:secret@alive.example:80", True),
            check_proxy.ProxyResult("socks5://dead.example:1080", False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            check_proxy.save_results(results, output)
            self.assertEqual((output / "alive.txt").read_text(), "http://user:secret@alive.example:80\n")
            self.assertEqual((output / "dead.txt").read_text(), "socks5://dead.example:1080\n")
            self.assertEqual((output / "alive.txt").stat().st_mode & 0o777, 0o600)
            check_proxy.save_results([], output)
            self.assertEqual((output / "alive.txt").read_text(), "")
            self.assertEqual((output / "dead.txt").read_text(), "")


if __name__ == "__main__":
    unittest.main()
