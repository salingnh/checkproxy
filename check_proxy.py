import argparse
import asyncio
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    raise SystemExit(
        "Lỗi: Thư viện 'rich' chưa được cài đặt.\n"
        "Vui lòng cài đặt bằng: pip install rich 'httpx[socks]'"
    )


# Chạy bằng Docker Compose (Khuyên dùng - không bao giờ lo pip timeout):
#   cd checkproxy && docker compose run --rm checkproxy
#
# Hoặc chạy bằng Docker run:
#   docker run --rm -it \
#     -v "$PWD:/work" \
#     -w /work \
#     python:3.12-slim \
#     bash -lc "pip install --no-cache-dir --default-timeout=100 -q 'httpx[socks]' rich && python /work/check_proxy.py -i /work/input_proxy -c 5 --sort speed --speed-bytes 2000000"


DEFAULT_CHECK_URL = "https://ifconfig.io/ip"
# Nguồn để đo tốc độ: trả về bytes ngẫu nhiên theo size.
# Có thể dùng placeholder {bytes} để tự động build URL theo số byte cần tải.
DEFAULT_SPEED_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"


@dataclass
class ProxyResult:
    proxy: str
    alive: bool
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    download_mb_s: Optional[float] = None
    out_ip: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ActiveTaskInfo:
    proxy: str
    phase: str = "Đang chờ"  # "Đang chờ" | "Kiểm tra Ping" | "Đang tải speed"
    downloaded: int = 0
    total_bytes: int = 0
    speed_mb_s: float = 0.0
    latency_ms: Optional[float] = None


class CheckerState:
    def __init__(self, total: int, sort_by: str = "latency", mask_password: bool = True):
        self.total = total
        self.sort_by = sort_by
        self.mask_password = mask_password
        self.active_tasks: Dict[str, ActiveTaskInfo] = {}
        self.completed: List[ProxyResult] = []
        self.alive_count: int = 0
        self.dead_count: int = 0


def mask_proxy(p: str, mask: bool = True) -> str:
    """
    Che giấu mật khẩu trong URL proxy khi in:
    schema://user:pass@host:port -> schema://user:***@host:port
    """
    if not mask or not p:
        return p
    return re.sub(r"(://[^:/@]+):[^@]*(@)", r"\1:***\2", p)


def normalize_proxy(p: str) -> str:
    """
    Accepts:
      - schema|host|port|user|password
    Returns:
      - schema://user:password@host:port
    """
    p = p.strip()
    if not p or p.startswith("#"):
        return ""
    p_split = p.split("|")
    if len(p_split) < 3:
        return ""
    schema = p_split[0]
    host = p_split[1]
    port = p_split[2]
    if len(p_split) == 5:
        user = p_split[3]
        password = p_split[4]
        if user or password:
            user = quote(user)
            password = quote(password)
            p = f"{schema}://{user}:{password}@{host}:{port}"
        else:
            p = f"{schema}://{host}:{port}"
    else:
        p = f"{schema}://{host}:{port}"
    return p.strip()


def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    else:
        return f"{b / (1024 * 1024):.2f} MB"


def format_progress_bar(completed: int, total: int, width: int = 15) -> str:
    if total <= 0:
        return "[dim]--/--[/dim]"
    ratio = min(max(completed / total, 0.0), 1.0)
    filled = int(ratio * width)
    unfilled = width - filled
    pct = ratio * 100
    return f"[cyan]{'━' * filled}[/cyan][dim]{'━' * unfilled}[/dim] {pct:5.1f}%"


async def measure_latency(
    client: httpx.AsyncClient, url: str, timeout_s: float
) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    """
    Returns: (status_code, latency_ms, out_ip)
    """
    r = await client.get(url, timeout=timeout_s)
    latency_ms = r.elapsed.total_seconds() * 1000.0

    out_ip = None
    try:
        out_ip = r.content.strip().decode("utf-8")
    except Exception:
        pass

    return r.status_code, latency_ms, out_ip


async def measure_download_speed(
    client: httpx.AsyncClient,
    url: str,
    timeout_s: float,
    max_bytes: int,
    active_info: Optional[ActiveTaskInfo] = None,
) -> Optional[float]:
    """
    Stream download up to max_bytes, live update % and MB/s.
    """
    downloaded = 0
    t0 = time.perf_counter()

    if active_info:
        active_info.phase = "Đang tải speed"
        active_info.total_bytes = max_bytes
        active_info.downloaded = 0
        active_info.speed_mb_s = 0.0

    try:
        async with client.stream("GET", url, timeout=timeout_s) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                if not chunk:
                    continue
                remaining = max_bytes - downloaded
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                downloaded += len(chunk)

                dt = time.perf_counter() - t0
                if dt > 0 and active_info:
                    active_info.downloaded = downloaded
                    active_info.speed_mb_s = (downloaded / (1024 * 1024)) / dt

                if downloaded >= max_bytes:
                    break
    except Exception:
        # Nếu bị timeout hoặc ngắt kết nối giữa chừng nhưng đã tải được dữ liệu,
        # vẫn tính tốc độ dựa trên lượng byte thực tế đã tải trước khi timeout
        dt = time.perf_counter() - t0
        if dt > 0 and downloaded > 0:
            return (downloaded / (1024 * 1024)) / dt
        return None

    dt = time.perf_counter() - t0
    if dt <= 0 or downloaded <= 0:
        return None

    mb = downloaded / (1024 * 1024)
    return mb / dt  # MB/s


async def test_one_proxy(
    proxy_url: str,
    check_url: str = DEFAULT_CHECK_URL,
    speed_url: str = DEFAULT_SPEED_URL,
    connect_timeout_s: float = 8.0,
    total_timeout_s: float = 15.0,
    speed_max_bytes: int = 500_000,
    active_info: Optional[ActiveTaskInfo] = None,
) -> ProxyResult:
    res = ProxyResult(proxy=proxy_url, alive=False)

    if active_info:
        active_info.phase = "Kiểm tra Ping"

    limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)
    timeout = httpx.Timeout(total_timeout_s, connect=connect_timeout_s)

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            headers={"User-Agent": "proxy-checker/1.0"},
        ) as client:
            status, latency_ms, out_ip = await measure_latency(
                client, check_url, total_timeout_s
            )
            res.status_code = status
            res.latency_ms = latency_ms
            res.out_ip = out_ip

            if active_info:
                active_info.latency_ms = latency_ms

            if 200 <= status < 300:
                res.alive = True
                url_for_speed = speed_url
                if "{bytes}" in speed_url:
                    try:
                        url_for_speed = speed_url.format(bytes=speed_max_bytes)
                    except (KeyError, ValueError):
                        url_for_speed = speed_url

                sp = await measure_download_speed(
                    client,
                    url_for_speed,
                    total_timeout_s,
                    speed_max_bytes,
                    active_info=active_info,
                )
                res.download_mb_s = sp
            else:
                res.alive = False
    except httpx.ProxyError as e:
        res.error = f"ProxyError: {e}"
    except httpx.ConnectTimeout:
        res.error = "ConnectTimeout"
    except httpx.ReadTimeout:
        res.error = "ReadTimeout"
    except httpx.HTTPStatusError as e:
        res.error = f"HTTPStatusError: {e.response.status_code}"
        res.status_code = getattr(e.response, "status_code", None)
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"

    return res


def load_proxies(path: str) -> List[str]:
    import os

    if not os.path.exists(path):
        raise FileNotFoundError(f"Proxy file not found: {path}")

    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            p = normalize_proxy(line)
            if p:
                out.append(p)
    return out


def sort_alive_proxies(proxies: List[ProxyResult], sort_by: str = "latency") -> List[ProxyResult]:
    """Sort danh sách proxy theo điều kiện"""
    if sort_by == "speed":
        return sorted(
            proxies,
            key=lambda x: (
                x.download_mb_s is None,
                -(x.download_mb_s or 0.0),
                x.latency_ms or 1e18,
            ),
        )
    else:
        return sorted(
            proxies,
            key=lambda x: (
                x.latency_ms is None,
                x.latency_ms or 1e18,
                -(x.download_mb_s or 0.0),
            ),
        )


def make_render_group(state: CheckerState) -> Group:
    """Tạo layout hiển thị Terminal Live cập nhật liên tục"""
    # 1. BẢNG KẾT QUẢ ĐÃ XONG (Sắp xếp theo sort_by)
    alive_completed = [r for r in state.completed if r.alive]
    sorted_alive = sort_alive_proxies(alive_completed, sort_by=state.sort_by)

    top_table = Table(
        title=f"🏆 [bold green]TOP PROXIES HOẠT ĐỘNG (Sắp xếp theo {state.sort_by.upper()})[/bold green]",
        expand=True,
    )
    top_table.add_column("#", justify="right", style="dim", width=4)
    top_table.add_column("Proxy", style="cyan", no_wrap=True)
    top_table.add_column("Latency", justify="right", style="magenta", width=12)
    top_table.add_column("Speed", justify="right", style="bold green", width=14)
    top_table.add_column("Out IP", style="yellow", width=18)

    if sorted_alive:
        for idx, r in enumerate(sorted_alive[:10], 1):
            lat = f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "NA"
            spd = f"{r.download_mb_s:.2f} MB/s" if r.download_mb_s is not None else "NA"
            proxy_display = mask_proxy(r.proxy, state.mask_password)
            top_table.add_row(str(idx), proxy_display, lat, spd, r.out_ip or "NA")
    else:
        top_table.add_row("-", "[dim]Đang kiểm tra, chưa có proxy ALIVE...[/dim]", "-", "-", "-")

    # 2. BẢNG CÁC PROXY ĐANG CHẠY CONCURRENT (Hiển thị % và MB/s)
    active_table = Table(
        title=f"⚡ [bold cyan]TIẾN TRÌNH CHECK ĐỒNG THỜI (Active: {len(state.active_tasks)})[/bold cyan]",
        expand=True,
    )
    active_table.add_column("Proxy Đang Check", style="blue", no_wrap=True)
    active_table.add_column("Trạng thái", width=16)
    active_table.add_column("Tiến độ Download", width=36)
    active_table.add_column("Tốc độ", justify="right", style="bold green", width=14)

    active_items = list(state.active_tasks.values())
    if active_items:
        for info in active_items[:8]:
            if info.phase == "Đang tải speed":
                status = "[bold cyan]Downloading[/bold cyan]"
                prog_bar = format_progress_bar(info.downloaded, info.total_bytes, width=12)
                prog_text = f"{prog_bar} ({format_bytes(info.downloaded)}/{format_bytes(info.total_bytes)})"
                speed_text = f"{info.speed_mb_s:.2f} MB/s"
            elif info.phase == "Kiểm tra Ping":
                status = "[yellow]Ping...[/yellow]"
                prog_text = "[dim]đang đo latency[/dim]"
                speed_text = "[dim]-[/dim]"
            else:
                status = "[dim]Đang chờ[/dim]"
                prog_text = "[dim]--[/dim]"
                speed_text = "[dim]-[/dim]"

            proxy_display = mask_proxy(info.proxy, state.mask_password)
            active_table.add_row(proxy_display, status, prog_text, speed_text)

        if len(active_items) > 8:
            active_table.add_row(
                f"[dim]+ {len(active_items) - 8} proxy khác...[/dim]", "", "", ""
            )
    else:
        active_table.add_row("[dim]Không có worker đang chạy[/dim]", "-", "-", "-")

    # 3. THANH TRẠNG THÁI TIẾN ĐỘ TỔNG
    bar_overall = format_progress_bar(len(state.completed), state.total, width=20)
    summary_text = (
        f"Tiến độ: {bar_overall} [bold]{len(state.completed)}/{state.total}[/bold] | "
        f"[bold green]Alive: {state.alive_count}[/bold green] | "
        f"[bold red]Dead: {state.dead_count}[/bold red] | "
        f"[bold yellow]Đang chạy: {len(state.active_tasks)}[/bold yellow]"
    )
    summary_panel = Panel(summary_text, expand=True)

    return Group(top_table, active_table, summary_panel)


def print_results(results: List[ProxyResult], sort_by: str = "latency", mask_password: bool = True):
    """In kết quả tổng kết cuối cùng ra terminal"""
    console = Console()
    alive = [r for r in results if r.alive]
    dead = [r for r in results if not r.alive]
    sorted_alive = sort_alive_proxies(alive, sort_by=sort_by)

    console.print()
    console.print(
        f"[bold]Tổng kết:[/bold] [bold green]ALIVE: {len(alive)}[/bold green] | "
        f"[bold red]DEAD: {len(dead)}[/bold red] | "
        f"[bold]TOTAL: {len(results)}[/bold]"
    )
    console.print()

    # In bảng Top Alive đầy đủ
    alive_table = Table(
        title=f"=== TOP ALIVE PROXIES (Sắp xếp theo {sort_by.upper()}) ===",
        expand=True,
    )
    alive_table.add_column("#", justify="right", style="dim", width=4)
    alive_table.add_column("Proxy", style="cyan")
    alive_table.add_column("Latency", justify="right", style="magenta")
    alive_table.add_column("Speed", justify="right", style="bold green")
    alive_table.add_column("Out IP", style="yellow")

    for idx, r in enumerate(sorted_alive[:50], 1):
        lat = f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "NA"
        spd = f"{r.download_mb_s:.2f} MB/s" if r.download_mb_s is not None else "NA"
        proxy_display = mask_proxy(r.proxy, mask_password)
        alive_table.add_row(str(idx), proxy_display, lat, spd, r.out_ip or "NA")

    console.print(alive_table)

    # In danh sách Dead mẫu
    if dead:
        dead_table = Table(title="=== DEAD PROXIES (Mẫu 30 cái) ===", expand=True)
        dead_table.add_column("Proxy", style="dim")
        dead_table.add_column("Lỗi / Status", style="red")

        for r in dead[:30]:
            err = r.error or f"status={r.status_code}"
            proxy_display = mask_proxy(r.proxy, mask_password)
            err_display = mask_proxy(err, mask_password)
            dead_table.add_row(proxy_display, err_display)

        console.print()
        console.print(dead_table)


def str2bool(v):
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif str(v).lower() in ("no", "false", "f", "n", "0"):
        return False
    raise argparse.ArgumentTypeError("Cần truyền giá trị boolean (true/false).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Proxy checker (http/https/socks4/socks5) - alive + latency + speed với giao diện Live Terminal"
    )
    ap.add_argument("-i", "--input", required=True, help="Đường dẫn tới file proxy")
    ap.add_argument(
        "-c", "--concurrency", type=int, default=10, help="Số lượng proxy check đồng thời (mặc định 10)"
    )
    ap.add_argument(
        "--sort",
        choices=["latency", "speed"],
        default="speed",
        help="Sắp xếp kết quả theo speed hoặc latency (mặc định speed)",
    )
    ap.add_argument(
        "--check-url", default=DEFAULT_CHECK_URL, help="URL kiểm tra alive và IP"
    )
    ap.add_argument(
        "--speed-url",
        default=DEFAULT_SPEED_URL,
        help="URL kiểm tra download speed (hỗ trợ placeholder {bytes})",
    )
    ap.add_argument(
        "--speed-bytes",
        type=int,
        default=2_000_000,
        help="Số bytes download để đo tốc độ (mặc định 2,000,000 = ~2MB)",
    )
    ap.add_argument(
        "--connect-timeout", type=float, default=3.0, help="Timeout kết nối (giây)"
    )
    ap.add_argument(
        "--timeout", type=float, default=15.0, help="Timeout toàn bộ request (giây)"
    )
    ap.add_argument(
        "--mask-password",
        "--mark-password",
        dest="mask_password",
        nargs="?",
        const=True,
        default=True,
        type=str2bool,
        help="Che giấu mật khẩu proxy khi in kết quả (mặc định: True). Có thể truyền true/false hoặc dùng --no-mask-password",
    )
    ap.add_argument(
        "--no-mask-password",
        "--no-mark-password",
        dest="mask_password",
        action="store_false",
        help="Hiển thị mật khẩu proxy đầy đủ (không che)",
    )
    args = ap.parse_args()

    proxies = load_proxies(args.input)
    if not proxies:
        raise SystemExit("Không tìm thấy proxy nào trong file đầu vào.")

    async def main():
        sem = asyncio.Semaphore(args.concurrency)
        state = CheckerState(
            total=len(proxies),
            sort_by=args.sort,
            mask_password=args.mask_password,
        )

        async def runner(p: str):
            info = ActiveTaskInfo(proxy=p, phase="Đang chờ")
            state.active_tasks[p] = info
            try:
                async with sem:
                    res = await test_one_proxy(
                        p,
                        check_url=args.check_url,
                        speed_url=args.speed_url,
                        connect_timeout_s=args.connect_timeout,
                        total_timeout_s=args.timeout,
                        speed_max_bytes=args.speed_bytes,
                        active_info=info,
                    )
                    state.completed.append(res)
                    if res.alive:
                        state.alive_count += 1
                    else:
                        state.dead_count += 1
                    return res
            finally:
                state.active_tasks.pop(p, None)

        tasks = [asyncio.create_task(runner(p)) for p in proxies]

        async def refresh_display(live: Live):
            while True:
                live.update(make_render_group(state))
                await asyncio.sleep(0.1)

        # Khởi chạy giao diện Live cập nhật liên tục
        with Live(
            make_render_group(state),
            refresh_per_second=10,
            transient=True,
        ) as live:
            updater = asyncio.create_task(refresh_display(live))
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                updater.cancel()
                try:
                    await updater
                except asyncio.CancelledError:
                    pass
            # Cập nhật lần cuối trước khi tắt Live
            live.update(make_render_group(state))

        # In kết quả tổng kết lưu lại lịch sử terminal
        print_results(state.completed, sort_by=args.sort, mask_password=args.mask_password)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Đã dừng bởi người dùng.")
