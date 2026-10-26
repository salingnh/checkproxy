# CheckProxy

Công cụ kiểm tra proxy bất đồng bộ, hiển thị tiến trình trên terminal, đo latency qua HTTP(S), lấy IP đầu ra và đo tốc độ tải. Toàn bộ môi trường chạy, phát triển và kiểm thử sử dụng Docker; không cần cài Python hoặc package trên host.

Tài liệu này mô tả mã nguồn hiện tại trong [check_proxy.py](check_proxy.py). CLI và Docker Compose mặc định chạy **20 luồng**, tải tối đa **1.000.000 byte/proxy** để đo tốc độ và giới hạn bước latency ở **5 giây**.

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

- Các scheme được HTTPX hiện tại chấp nhận: `http`, `https`, `socks5`, `socks5h`. **SOCKS4 chưa được hỗ trợ**, dù phần mô tả trong `--help` còn liệt kê `socks4`.
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

[docker-compose.yml](docker-compose.yml) mount thư mục repo vào `/app` và truyền `-i input_proxy`. Hai file `alive.txt` và `dead.txt` được lưu ngay trong repo trên host khi hoàn tất. `--user` giúp file xuất thuộc tài khoản đang chạy lệnh.

Ví dụ tùy chỉnh số luồng, latency và thư mục kết quả:

```bash
docker compose run --rm --user "$(id -u):$(id -g)" checkproxy \
  -i input_proxy -c 20 --latency-timeout 3 --output-dir /app/results
```

Kết quả nằm tại `results/alive.txt` và `results/dead.txt` trên host. Khi truyền tham số sau tên service, cần truyền lại `-i` vì chúng thay thế `command` của Compose.

Ví dụ tải mẫu lớn hơn và sắp xếp bảng tổng kết theo tốc độ:

```bash
docker compose run --rm --user "$(id -u):$(id -g)" checkproxy \
  -i input_proxy --speed-bytes 5000000 --sort speed
```

## 3. Chạy bằng Docker image

### 3.1. Build image từ mã nguồn hiện tại

[Dockerfile](Dockerfile) sử dụng Python 3.12 và cài dependency từ [requirements.txt](requirements.txt).

`Dockerfile` hiện dùng `COPY . .`: khi build hoặc publish, cần bảo đảm build context không chứa file proxy, kết quả có mật khẩu hoặc secret. `.gitignore` không loại chúng khỏi Docker build context.

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

Mỗi proxy được xử lý lần lượt:

1. Gửi GET qua proxy tới `--check-url`, theo chuyển hướng và trích IP đầu ra từ phản hồi.
2. Nếu status cuối cùng là `2xx`, đánh dấu ALIVE và tải dữ liệu từ `--speed-url` để đo tốc độ.
3. Hoàn tất cả bước đo tốc độ mới đưa kết quả vào danh sách đã xong và giải phóng luồng.

Status ngoài `2xx`, lỗi proxy, lỗi kết nối hoặc timeout ở bước kiểm tra IP được ghi là DEAD. Lỗi tải dữ liệu đo tốc độ **vẫn giữ trạng thái ALIVE** nếu kiểm tra IP đã thành công.

Các timeout tính bằng giây:

- `--latency-timeout` mặc định **5**: giới hạn tổng bước kiểm tra IP, gồm kết nối, chuyển hướng và đọc phản hồi. Giới hạn thực tế là giá trị nhỏ hơn giữa `--latency-timeout` và `--timeout`.
- `--connect-timeout` mặc định **3**: timeout kết nối của bước kiểm tra IP.
- `--timeout` mặc định **15**: timeout từng thao tác mạng. Bước đo tốc độ dùng giá trị này cho cả kết nối và đọc dữ liệu, không có giới hạn tổng thời gian riêng. Luồng tải vẫn nhận dữ liệu đều có thể chạy lâu hơn 15 giây.

Latency hiển thị bằng ms, được lấy từ thời gian phản hồi do HTTPX ghi nhận. Dung lượng tải mặc định là **1.000.000 byte**; tốc độ có nhãn `MB/s` trên giao diện và được tính theo byte / 1024² / giây.

### 4.2. Terminal trực tiếp và bảng tổng kết

Trong khi chạy, terminal có ba khu vực:

- **TOP PROXIES HOẠT ĐỘNG (MỚI NHẤT TRƯỚC)**: tối đa 10 proxy ALIVE vừa hoàn tất, mới nhất ở trên cùng.
- **TIẾN TRÌNH CHECK ĐỒNG THỜI**: một dòng cho mỗi luồng thực sự đang chạy, với proxy, Public IP, trạng thái, dung lượng đã tải và tốc độ. `-c 20` hiển thị tối đa 20 dòng, giảm khi còn ít proxy. Proxy chờ lượt không tính vào `Active` hoặc `Đang chạy`; các luồng dùng cùng URL vẫn có dòng riêng.
- **Thanh tổng tiến độ**: số proxy đã hoàn tất/tổng số, ALIVE, DEAD và số luồng đang chạy.

Giao diện không cắt bớt dòng khi vượt chiều cao terminal; cần cuộn hoặc tăng chiều cao cửa sổ để xem hết.

Sau khi hoàn tất, terminal in tổng số và **toàn bộ** kết quả ALIVE/DEAD, không giới hạn số proxy. Bảng DEAD chỉ xuất hiện khi có proxy chết. Bảng ALIVE có các cột `#`, `Proxy`, `Public IP`, `Latency`, `Speed`; bảng DEAD có `Proxy`, `Public IP`, `Lỗi / Status`.

`--sort` chỉ áp dụng cho bảng ALIVE trong tổng kết:

- `none` (mặc định): theo thứ tự hoàn tất.
- `speed`: tốc độ cao trước.
- `latency`: độ trễ thấp trước.

Cột Speed có thể hiện `ReadTimeout`, `ConnectTimeout`, `HTTP 503` hoặc tên lỗi khác. Nếu đã tải được một phần trước khi lỗi, tốc độ có dấu `*`; `NA` xuất hiện khi không có cả tốc độ lẫn lỗi tải.

### 4.3. File kết quả

Sau khi quét xong, chương trình tạo thư mục `--output-dir` nếu cần, rồi tạo hoặc ghi đè:

- `alive.txt`: đầy đủ proxy ALIVE.
- `dead.txt`: đầy đủ proxy DEAD.

Mỗi dòng là URL đầy đủ, ví dụ:

```text
http://192.0.2.10:8080
socks5://myuser:mypassword@192.0.2.12:1080
```

File giữ thứ tự hoàn tất và các dòng trùng; `--sort` không sắp xếp file. Nhóm không có kết quả vẫn được ghi thành file rỗng.

Terminal che mật khẩu mặc định. File xuất **chứa thông tin xác thực đầy đủ**, không chịu ảnh hưởng của `--mask-password`, và có quyền `0600` (chỉ chủ sở hữu đọc/ghi). Với Docker, dùng `--user` như ví dụ để tài khoản trên host sở hữu file.

Đầu vào dùng dấu `|`, còn đầu ra dùng URL. Muốn dùng file kết quả làm đầu vào cho công cụ này, cần chuyển lại định dạng có dấu `|`.

Chương trình từ chối nếu đường dẫn input trùng `alive.txt` hoặc `dead.txt` trong thư mục xuất. File chỉ được lưu khi hoàn tất toàn bộ lượt quét; dừng bằng `Ctrl+C` không xuất kết quả đang chạy.

## 5. Tham số CLI

Các mặc định dưới đây thuộc CLI; xem lưu ý `CMD` của Dockerfile ở mục 3.1. Số luồng, số byte và các timeout nên dùng giá trị dương.

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `-i`, `--input` | Bắt buộc | Đường dẫn file proxy bên trong container. |
| `-c`, `--concurrency` | `20` | Số proxy được kiểm tra đồng thời. |
| `--sort` | `none` | Sắp xếp bảng ALIVE tổng kết: `none`, `speed`, `latency`. |
| `--speed-bytes` | `1000000` | Số byte tối đa dùng để đo tốc độ tải. |
| `--connect-timeout` | `3.0` | Timeout kết nối ở bước kiểm tra IP, tính bằng giây. |
| `--latency-timeout` | `5.0` | Giới hạn tổng bước kiểm tra IP, tính bằng giây. |
| `--timeout` | `15.0` | Timeout từng thao tác mạng; đồng thời giới hạn trên của bước kiểm tra IP. |
| `--check-url` | `https://ifconfig.io/ip` | URL kiểm tra trạng thái và lấy IP đầu ra. |
| `--speed-url` | `https://speed.cloudflare.com/__down?bytes={bytes}` | URL tải mẫu, thay `{bytes}` bằng `--speed-bytes`. |
| `--output-dir` | `.` | Thư mục lưu `alive.txt` và `dead.txt`; mặc định `/app` khi chạy bằng image. |
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

[test_check_proxy.py](test_check_proxy.py) dùng HTTP server/proxy giả lập trong container để kiểm tra timeout, xuất file, che mật khẩu, thứ tự top mới nhất và số dòng luồng chạy (kể cả proxy trùng và terminal thấp). Các bài kiểm thử không cần proxy thật hoặc Internet; bước build image có thể cần mạng để tải base image/dependency.

Dọn container debug sau khi dùng:

```bash
docker compose -f docker-compose.yml -f docker-compose.debug.yml down
```

## 7. Giấy phép

[MIT License](LICENSE).
