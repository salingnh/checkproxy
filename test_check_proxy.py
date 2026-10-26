import asyncio
import csv
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


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = set()
        self.processes = []
        self.requests = []
        self.all_checks = asyncio.Event()
        self.speed_started = asyncio.Event()
        self.release_speed = asyncio.Event()
        self.server = await asyncio.start_server(self.respond, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.proxy = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
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
            self.requests.append(target)
            if sum(item.endswith(b"/ip") for item in self.requests) >= 3:
                self.all_checks.set()
            if target.endswith(b"/gated-speed"):
                self.speed_started.set()
                await self.release_speed.wait()
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

    async def start_cli(self, input_file, output_dir, *options):
        process = await asyncio.create_subprocess_exec(
            sys.executable, check_proxy.__file__, "-i", str(input_file),
            "--output-dir", str(output_dir),
            "--check-url", "http://check.invalid/ip",
            "--speed-url", "http://check.invalid/failure", *options,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        return process

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
            self.proxy, check_url="http://check.invalid/ip",
        )
        await check_proxy.test_proxy_speed(
            result, speed_url="http://check.invalid/failure", speed_max_bytes=12,
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
            self.assertEqual(self.requests, [b"http://check.invalid/ip"])
            alive = read_csv(output / "alive.csv")
            dead = read_csv(output / "dead.csv")
            self.assertEqual(len(alive), 1)
            self.assertEqual(len(dead), 1)
            self.assertEqual(
                [alive[0][key] for key in ("status", "host", "port", "user", "pass")],
                ["ALIVE", "127.0.0.1", str(port), "user", "secret"],
            )
            self.assertEqual(dead[0]["status"], "DEAD")
            self.assertEqual(dead[0]["port"], "0")
            self.assertEqual(alive[0]["public_ip"], "198.51.100.1")
            self.assertEqual(dead[0]["public_ip"], "")
            self.assertEqual(alive[0]["download_mb_s"], "")
            self.assertNotIn("Speed", stdout.decode())

    async def test_cli_refuses_to_overwrite_the_input_file(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "alive.csv"
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
                    "--check-url", "http://check.invalid/slow",
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

    async def test_full_checks_continue_while_speed_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "input_proxy"
            port = self.server.sockets[0].getsockname()[1]
            input_file.write_text(f"http|127.0.0.1|{port}\n" * 3)
            process = await self.start_cli(
                input_file, directory, "--full", "-c", "1", "--speed-concurrency", "1",
                "--speed-url", "http://check.invalid/gated-speed",
            )
            try:
                await asyncio.wait_for(self.all_checks.wait(), 3)
                await asyncio.wait_for(self.speed_started.wait(), 3)
                self.assertIsNone(process.returncode)
                self.assertEqual(sum(item.endswith(b"/gated-speed") for item in self.requests), 1)
            finally:
                self.release_speed.set()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            rows = read_csv(Path(directory) / "alive.csv")
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(float(row["download_mb_s"]) > 0 for row in rows))
            self.assertEqual(sum(item.endswith(b"/gated-speed") for item in self.requests), 3)
            self.assertIn("Speed", stdout.decode())

    async def test_full_exports_speed_errors_without_marking_alive_proxy_dead(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "input_proxy"
            port = self.server.sockets[0].getsockname()[1]
            input_file.write_text(f"http|127.0.0.1|{port}\nhttp|127.0.0.1|0\n")
            process = await self.start_cli(input_file, directory, "--full")
            _, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            row = read_csv(Path(directory) / "alive.csv")[0]
            self.assertEqual(row["status"], "ALIVE")
            self.assertEqual(row["public_ip"], "198.51.100.1")
            self.assertEqual(row["download_error"], "HTTP 503")
            self.assertEqual(len(read_csv(Path(directory) / "dead.csv")), 1)
            self.assertEqual(self.requests.count(b"http://check.invalid/failure"), 1)

    async def test_speed_deadline_keeps_partial_measurement(self):
        result = check_proxy.ProxyResult(self.proxy, True)
        await check_proxy.test_proxy_speed(
            result, speed_url="http://check.invalid/drip", total_timeout_s=0.06, speed_max_bytes=20,
        )
        self.assertTrue(result.alive)
        self.assertEqual(result.download_error, "Timeout")
        self.assertGreater(result.download_mb_s, 0)

    async def test_speed_sort_requires_full_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            process = await self.start_cli(Path(directory) / "unused", directory, "--sort", "speed")
            _, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 2)
            self.assertIn("--full", stderr.decode())

    async def test_full_mode_finishes_when_all_proxies_are_dead(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "input_proxy"
            input_file.write_text("http|127.0.0.1|0\n")
            process = await self.start_cli(input_file, directory, "--full")
            _, stderr = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(read_csv(Path(directory) / "alive.csv"), [])
            self.assertEqual(len(read_csv(Path(directory) / "dead.csv")), 1)
            self.assertEqual(self.requests, [])

    async def test_bad_speed_url_does_not_strand_queued_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            input_file = Path(directory) / "input_proxy"
            port = self.server.sockets[0].getsockname()[1]
            input_file.write_text(f"http|127.0.0.1|{port}\n" * 3)
            process = await self.start_cli(
                input_file, directory, "--full", "--speed-concurrency", "1",
                "--speed-url", "http://check.invalid/{bytes}/{bytes.foo}",
            )
            _, stderr = await asyncio.wait_for(process.communicate(), 3)
            self.assertEqual(process.returncode, 0, stderr.decode())
            rows = read_csv(Path(directory) / "alive.csv")
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["download_error"].startswith("AttributeError:") for row in rows))

    async def test_zero_workers_are_rejected_before_checking(self):
        for option in ("--concurrency", "--speed-concurrency"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                process = await self.start_cli(Path(directory) / "unused", directory, "--full", option, "0")
                _, stderr = await asyncio.wait_for(process.communicate(), 10)
                self.assertEqual(process.returncode, 2)
                self.assertIn("phải lớn hơn 0", stderr.decode())


class OutputTests(unittest.TestCase):
    def test_full_terminal_reports_separate_stages_and_pending_speed(self):
        state = check_proxy.CheckerState(total=4, full=True)
        state.alive_count = 3
        state.speed_completed = 1
        state.active_tasks = {
            0: check_proxy.ActiveTaskInfo("http://speed.example:80", phase="Đang tải speed"),
            3: check_proxy.ActiveTaskInfo("http://check.example:80", phase="Kiểm tra Ping"),
        }
        output = io.StringIO()
        Console(file=output, width=240, color_system=None).print(check_proxy.make_render_group(state))
        text = output.getvalue()
        self.assertIn("Active: 2 | Check: 1 | Speed: 1", text)
        self.assertIn("Speed: 1/3 | Chờ speed: 1", text)
        self.assertIn("speed.example", text)
        self.assertIn("check.example", text)

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

    def test_csv_contains_credentials_and_stats_and_replaces_old_results(self):
        results = [
            check_proxy.ProxyResult(
                "http://user:secret@alive.example:80", True,
                status_code=200, latency_ms=12.5, download_mb_s=1.25, out_ip="198.51.100.1",
            ),
            check_proxy.ProxyResult("socks5://dead.example:1080", False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            check_proxy.save_results(results, output)
            self.assertEqual(read_csv(output / "alive.csv"), [{
                "status": "ALIVE", "host": "alive.example", "port": "80", "user": "user", "pass": "secret",
                "proxy": "http://user:secret@alive.example:80", "out_ip": "198.51.100.1", "status_code": "200",
                "latency_ms": "12.5", "download_mb_s": "1.25", "error": "", "download_error": "",
                "public_ip": "198.51.100.1",
            }])
            dead = read_csv(output / "dead.csv")[0]
            self.assertEqual([dead[key] for key in ("status", "host", "port", "user", "pass")],
                             ["DEAD", "dead.example", "1080", "", ""])
            self.assertEqual(dead["public_ip"], "")
            self.assertEqual((output / "alive.csv").stat().st_mode & 0o777, 0o600)
            check_proxy.save_results([], output)
            for name in ("alive.csv", "dead.csv"):
                self.assertEqual(read_csv(output / name), [])
                self.assertTrue((output / name).read_text().startswith("status,host,port,user,pass,"))

    def test_csv_preserves_special_characters_in_credentials(self):
        proxy = check_proxy.normalize_proxy('http|example.com|8080|a/b+user|p/ass,"+%é')
        self.assertEqual(proxy, "http://a%2Fb%2Buser:p%2Fass%2C%22%2B%25%C3%A9@example.com:8080")
        with tempfile.TemporaryDirectory() as directory:
            check_proxy.save_results([check_proxy.ProxyResult(proxy, True)], directory)
            row = read_csv(Path(directory) / "alive.csv")[0]
            self.assertEqual(row["user"], "a/b+user")
            self.assertEqual(row["pass"], 'p/ass,"+%é')

    def test_csv_keeps_dead_proxies_with_invalid_ports_or_urls(self):
        results = [
            check_proxy.ProxyResult("http://host.example:invalid", False, error="Invalid port"),
            check_proxy.ProxyResult("http://[invalid", False, error="Invalid URL"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            check_proxy.save_results(results, directory)
            rows = read_csv(Path(directory) / "dead.csv")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["host"], "host.example")
            self.assertEqual(rows[0]["port"], "invalid")
            self.assertEqual(rows[1]["proxy"], "http://[invalid")


if __name__ == "__main__":
    unittest.main()
