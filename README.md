# CheckProxy

Công cụ kiểm tra proxy bất đồng bộ, hiển thị tiến trình trên terminal, đo latency qua HTTP(S), lấy IP đầu ra và đo tốc độ tải. Toàn bộ môi trường chạy, phát triển và kiểm thử sử dụng Docker; không cần cài Python hoặc package trên host.

Tài liệu này mô tả mã nguồn hiện tại trong [check_proxy.py](check_proxy.py). Mặc định **chỉ kiểm tra sống/chết, latency và IP đầu ra**, dùng **20 luồng** và giới hạn bước latency ở **5 giây**. Thêm `--full` để đo cả tốc độ bằng nhóm **3 luồng riêng**, tải tối đa **1.000.000 byte/proxy**. Kết quả được xuất thành `alive.csv` và `dead.csv`.

## 1. Định dạng file đầu vào

File UTF-8, mỗi dòng dùng dấu `|` để phân tách:

```text
schema|host|port
schema|host|port|username|password
```

Ví dụ file `input_proxy`:

```text
# Proxy không yêu cầu xác thực
http|192.0.2.10|8080
https|192.0.2.11|8443

# Proxy có tài khoản
socks5|192.0.2.12|1080|myuser|mypassword
socks5h|proxy.example.com|1080|myuser|mypassword
```

Các địa chỉ trên chỉ minh họa định dạng; thay bằng proxy của bạn trước khi chạy.

- Các scheme được HTTPX hiện tại chấp nhận: `http`, `https`, `socks5`, `socks5h`. **SOCKS4 chưa được hỗ trợ**.
- Bỏ qua dòng trống, comment bắt đầu bằng `#` và dòng có ít hơn ba trường.
- Để trống cả username/password hoặc dùng định dạng ba trường nếu không có xác thực. Dùng đúng ba hoặc năm trường.
- Proxy trùng dòng vẫn được kiểm tra riêng.
- Nếu không đọc được file hoặc không còn proxy nào sau khi lọc, chương trình dừng trước khi kiểm tra.

## 2. Chạy từ mã nguồn bằng Docker Compose

Yêu cầu Docker và Docker Compose. Chạy các lệnh sau tại thư mục repo; đặt file proxy tên `input_proxy` tại đây.

```bash
docker compose build
docker compose run --rm --user "$(id -u):$(id -g)" checkproxy
```

[docker-compose.yml](docker-compose.yml) mount thư mục repo vào `/app` và truyền `-i input_proxy`. Hai file `alive.csv` và `dead.csv` được lưu ngay trong repo trên host khi hoàn tất. `--user` giúp file xuất thuộc tài khoản đang chạy lệnh.

Ví dụ tùy chỉnh số luồng, latency và thư mục kết quả:

```bash
docker compose run --rm --user "$(id -u):$(id -g)" checkproxy \
  -i input_proxy -c 20 --latency-timeout 3 --output-dir /app/results
```

Kết quả nằm tại `results/alive.csv` và `results/dead.csv` trên host. Khi truyền tham số sau tên service, cần truyền lại `-i` vì chúng thay thế `command` của Compose.

Kiểm tra đầy đủ: 20 luồng kiểm tra sống/chết và 3 luồng đo tốc độ độc lập:

```bash
docker compose run --rm --user "$(id -u):$(id -g)" checkproxy \
  -i input_proxy --full -c 20 --speed-concurrency 3 --sort speed
```

Ví dụ tải mẫu lớn hơn trong chế độ đầy đủ:

```bash
docker compose run --rm --user "$(id -u):$(id -g)" checkproxy \
  -i input_proxy --full --speed-bytes 5000000 --sort speed
```

## 3. Chạy bằng Docker image

### 3.1. Build image từ mã nguồn hiện tại

[Dockerfile](Dockerfile) sử dụng Python 3.12 và cài dependency từ [requirements.txt](requirements.txt).

[.dockerignore](.dockerignore) loại file `input_proxy*`, kết quả `alive*.csv`/`dead*.csv`, file TXT kết quả cũ và `.env*` khỏi build context. Khi dùng tên file nhạy cảm khác, bổ sung quy tắc tương ứng; `.gitignore` chỉ áp dụng cho Git.

```bash
docker build -t checkproxy:local .
docker run --rm -it --user "$(id -u):$(id -g)" \
  -v "$(pwd)/input_proxy:/data/input_proxy:ro" \
  -v "$(pwd):/output" \
  checkproxy:local \
  -i /data/input_proxy -c 20 --output-dir /output
```

Mount input bằng đường dẫn tuyệt đối tới **file có sẵn**. Thư mục `/output` được mount ra host để giữ kết quả sau khi container bị xóa bởi `--rm`. `-it` cấp terminal tương tác để xem giao diện cập nhật trực tiếp; có thể bỏ khi chạy không tương tác.

CLI mặc định `-c 20`. Riêng `CMD` trong Dockerfile vẫn là `-i input_proxy -c 10`, nên image chạy **không truyền tham số** dùng 10 luồng. Truyền `-i ...` thay thế `CMD` và dùng mặc định CLI, hoặc đặt `-c` rõ ràng như ví dụ trên.

### 3.2. Image từ registry

Có thể thay `checkproxy:local` trong lệnh trên bằng `ghcr.io/salingnh/checkproxy:latest`. Image phải được build/publish từ revision có các tính năng được mô tả trong tài liệu; nếu registry chưa cập nhật, hãy build từ mã nguồn.

[Workflow Docker](.github/workflows/docker-publish.yml) build khi có PR vào `main`/`master`; publish khi push vào hai nhánh này, push tag `v*.*.*` hoặc chạy thủ công. Địa chỉ image theo `ghcr.io/<owner>/<repo>`, tag `latest` áp dụng cho `main`/`master`. PR chỉ build, không publish.

## 4. Cách kiểm tra và kết quả

### 4.1. Trạng thái, latency và tốc độ

Mặc định, mỗi luồng kiểm tra gửi GET qua proxy tới `--check-url`, theo chuyển hướng và trích IP đầu ra từ phản hồi. Kết quả được ghi nhận ngay khi bước này hoàn tất, sau đó luồng nhận proxy tiếp theo. Chế độ này không gửi request tải mẫu.

Với `--full`, proxy ALIVE được đưa vào hàng đợi đo tốc độ, xử lý bằng `--speed-concurrency` luồng riêng. Nhóm kiểm tra sống/chết tiếp tục chạy trong lúc nhóm tốc độ tải mẫu. Chương trình đợi cả hai nhóm hoàn tất trước khi xuất CSV và bảng tổng kết; proxy DEAD không được đo tốc độ.

Status ngoài `2xx`, lỗi proxy, lỗi kết nối hoặc timeout ở bước kiểm tra IP được ghi là DEAD. Lỗi tải dữ liệu đo tốc độ **vẫn giữ trạng thái ALIVE** nếu kiểm tra IP đã thành công.

Các timeout tính bằng giây:

- `--latency-timeout` mặc định **5**: giới hạn tổng bước kiểm tra IP, gồm kết nối, chuyển hướng và đọc phản hồi. Giới hạn thực tế là giá trị nhỏ hơn giữa `--latency-timeout` và `--timeout`.
- `--connect-timeout` mặc định **3**: timeout kết nối của bước kiểm tra IP.
- `--timeout` mặc định **15**: timeout từng thao tác mạng và giới hạn tổng thời gian của mỗi lần tải mẫu trong chế độ `--full`. Hết thời gian thì kết thúc phép đo, giữ số liệu tải được một phần và ghi lỗi `Timeout`.

Latency hiển thị bằng ms, được lấy từ thời gian phản hồi do HTTPX ghi nhận. Trong chế độ `--full`, dung lượng tải mặc định là **1.000.000 byte**; tốc độ có nhãn `MB/s` trên giao diện và được tính theo byte / 1024² / giây.

### 4.2. Terminal trực tiếp và bảng tổng kết

Trong khi chạy, terminal có ba khu vực:

- **TOP PROXIES HOẠT ĐỘNG (MỚI NHẤT TRƯỚC)**: tối đa 10 proxy vừa hoàn tất kiểm tra sống/chết và được xác định ALIVE, mới nhất ở trên cùng. Cột Speed chỉ xuất hiện khi dùng `--full`, cập nhật khi phép đo hoàn tất.
- **TIẾN TRÌNH CHECK ĐỒNG THỜI**: một dòng cho mỗi luồng thực sự đang chạy, với proxy, Public IP và trạng thái. Mặc định `-c 20` hiển thị tối đa 20 dòng. Với `--full --speed-concurrency 3`, có thể có thêm 3 dòng đo tốc độ, kèm tiến độ tải; tiêu đề tách số luồng `Check` và `Speed`. Proxy chờ lượt không tính vào `Active` hoặc `Đang chạy`; các luồng dùng cùng URL vẫn có dòng riêng.
- **Thanh tổng tiến độ**: số proxy đã hoàn tất kiểm tra sống/chết/tổng số, ALIVE, DEAD và số luồng đang chạy. Chế độ `--full` hiển thị thêm số phép đo tốc độ đã xong/tổng ALIVE và số proxy `Chờ speed`. Tiến độ kiểm tra đạt 100% vẫn có thể còn tác vụ đo tốc độ đang chạy.

Giao diện không cắt bớt dòng khi vượt chiều cao terminal; cần cuộn hoặc tăng chiều cao cửa sổ để xem hết.

Sau khi hoàn tất, terminal in tổng số và **toàn bộ** kết quả ALIVE/DEAD, không giới hạn số proxy. Bảng DEAD chỉ xuất hiện khi có proxy chết. Bảng ALIVE có các cột `#`, `Proxy`, `Public IP`, `Latency`, thêm `Speed` khi dùng `--full`; bảng DEAD có `Proxy`, `Public IP`, `Lỗi / Status`.

`--sort` chỉ áp dụng cho bảng ALIVE trong tổng kết:

- `none` (mặc định): theo thứ tự hoàn tất.
- `speed`: tốc độ cao trước, yêu cầu `--full`.
- `latency`: độ trễ thấp trước.

Cột Speed có thể hiện `ReadTimeout`, `ConnectTimeout`, `HTTP 503` hoặc tên lỗi khác. Nếu đã tải được một phần trước khi lỗi, tốc độ có dấu `*`; `NA` xuất hiện khi không có cả tốc độ lẫn lỗi tải.

### 4.3. File kết quả

Sau khi quét xong, chương trình tạo thư mục `--output-dir` nếu cần, rồi tạo hoặc ghi đè:

- `alive.csv`: đầy đủ proxy ALIVE.
- `dead.csv`: đầy đủ proxy DEAD.

CSV UTF-8 có dòng tiêu đề và các cột:

```text
status,host,port,user,pass,proxy,out_ip,status_code,latency_ms,download_mb_s,error,download_error,public_ip
```

- `status`: `ALIVE` hoặc `DEAD`; `host`, `port`, `user`, `pass` tách từ URL proxy. Username/password được giải mã về giá trị gốc, dấu phẩy và dấu nháy được escape theo chuẩn CSV.
- `proxy`: URL đầy đủ, giữ thông tin giao thức. `out_ip`, `status_code`, `latency_ms` là kết quả kiểm tra sống/chết.
- `public_ip`: IP public đầu ra của proxy, cùng giá trị với `out_ip`; để trống khi chưa có IP. Cột này được thêm cuối CSV, giữ nguyên các cột hiện có.
- `download_mb_s`, `download_error`: kết quả đo tốc độ trong chế độ `--full`; để trống ở chế độ mặc định hoặc với proxy DEAD.
- `error`: lỗi kiểm tra sống/chết. Proxy có URL/port lỗi vẫn được giữ trong file DEAD; trường không phân tích được để trống.

File giữ thứ tự hoàn tất kiểm tra sống/chết và các dòng trùng; `--sort` không sắp xếp file. Nhóm không có kết quả vẫn có file CSV chứa dòng tiêu đề. File TXT từ phiên bản cũ không được cập nhật hoặc xóa.

Terminal che mật khẩu mặc định. File xuất **chứa thông tin xác thực đầy đủ**, không chịu ảnh hưởng của `--mask-password`, và có quyền `0600` (chỉ chủ sở hữu đọc/ghi). Với Docker, dùng `--user` như ví dụ để tài khoản trên host sở hữu file.

Đầu vào vẫn dùng dấu `|`, đầu ra là CSV. Muốn dùng kết quả làm đầu vào cho công cụ này, cần chuyển lại định dạng có dấu `|`.

Chương trình từ chối nếu đường dẫn input trùng `alive.csv` hoặc `dead.csv` trong thư mục xuất. File chỉ được lưu khi hoàn tất lượt quét và các phép đo đã bật; dừng bằng `Ctrl+C` không xuất kết quả đang chạy.

## 5. Tham số CLI

Các mặc định dưới đây thuộc CLI; xem lưu ý `CMD` của Dockerfile ở mục 3.1. Số luồng, số byte và các timeout nên dùng giá trị dương.

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `-i`, `--input` | Bắt buộc | Đường dẫn file proxy bên trong container. |
| `-c`, `--concurrency` | `20` | Số proxy được kiểm tra đồng thời. |
| `--full` | Không bật | Bật đo tốc độ cho proxy ALIVE bằng nhóm luồng riêng. |
| `--speed-concurrency` | `3` | Số luồng đo tốc độ khi dùng `--full`, độc lập với `-c`. |
| `--sort` | `none` | Sắp xếp bảng ALIVE tổng kết: `none`, `speed`, `latency`; `speed` cần `--full`. |
| `--speed-bytes` | `1000000` | Số byte tối đa tải khi dùng `--full`. |
| `--connect-timeout` | `3.0` | Timeout kết nối ở bước kiểm tra IP, tính bằng giây. |
| `--latency-timeout` | `5.0` | Giới hạn tổng bước kiểm tra IP, tính bằng giây. |
| `--timeout` | `15.0` | Timeout thao tác mạng, giới hạn trên của bước kiểm tra IP và giới hạn tổng bước đo tốc độ. |
| `--check-url` | `https://ifconfig.io/ip` | URL kiểm tra trạng thái và lấy IP đầu ra. |
| `--speed-url` | `https://speed.cloudflare.com/__down?bytes={bytes}` | URL tải mẫu khi dùng `--full`, thay `{bytes}` bằng `--speed-bytes`. |
| `--output-dir` | `.` | Thư mục lưu `alive.csv` và `dead.csv`; mặc định `/app` khi chạy bằng image. |
| `--mask-password`, `--mark-password` | `true` | Che mật khẩu trên terminal. Có thể truyền `true`/`false` hoặc chỉ truyền cờ để bật. |
| `--no-mask-password`, `--no-mark-password` | Không bật | Hiện mật khẩu đầy đủ trên terminal. |
| `-h`, `--help` | — | In trợ giúp CLI. |

Xem trợ giúp từ mã nguồn đang mount:

```bash
docker compose run --rm checkproxy --help
```

## 6. Phát triển và kiểm thử trong Docker

[docker-compose.debug.yml](docker-compose.debug.yml) dùng chung image và source mount với Compose chính, thay entrypoint bằng `sleep infinity` để chạy lệnh kiểm thử trong container.

```bash
docker compose -f docker-compose.yml -f docker-compose.debug.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.debug.yml exec -T checkproxy python -m unittest -v
docker compose -f docker-compose.yml -f docker-compose.debug.yml logs -f --tail=100 checkproxy
```

`logs -f` theo dõi log đến khi nhấn `Ctrl+C`. Container debug chạy `sleep infinity`, nên output kiểm thử nằm ở lệnh `exec` và service có thể không có log. Không cần cài thêm package trên host.

[test_check_proxy.py](test_check_proxy.py) dùng HTTP server/proxy giả lập trong container để kiểm tra chế độ mặc định không tải mẫu, hai nhóm luồng độc lập, timeout, CSV và thông tin xác thực, che mật khẩu, thứ tự top mới nhất và số dòng luồng chạy (kể cả proxy trùng và terminal thấp). Các bài kiểm thử không cần proxy thật hoặc Internet; bước build image có thể cần mạng để tải base image/dependency.

Dọn container debug sau khi dùng:

```bash
docker compose -f docker-compose.yml -f docker-compose.debug.yml down
```

## 7. Giấy phép

[MIT License](LICENSE).
