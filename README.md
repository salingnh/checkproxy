# CheckProxy - Công Cụ Kiểm Tra Proxy Đa Năng

Công cụ kiểm tra danh sách proxy (HTTP, HTTPS, SOCKS4, SOCKS5) bất đồng bộ với hiệu năng cao, giao diện dòng lệnh (CLI) trực quan cập nhật theo thời gian thực (Live Terminal) và đo lường chi tiết:
- **Trạng thái**: Alive / Dead.
- **Độ trễ (Latency)**: Tính bằng mili-giây (ms).
- **Tốc độ tải về (Download Speed)**: Đo đạc tốc độ thực tế (MB/s) qua luồng stream.
- **Outbound IP**: Nhận diện IP thực tế mà proxy sử dụng khi ra Internet.
- **Bảo mật**: Mặc định tự động che mật khẩu proxy (`user:***@host:port`) khi hiển thị trên màn hình.

---

## 1. Định Dạng File Proxy Đầu Vào

File danh sách proxy sử dụng định dạng phân tách bởi dấu gạch đứng `|`:

```text
schema|host|port|username|password
schema|host|port
```

- **schema**: `http`, `https`, `socks4`, hoặc `socks5`.
- **username/password**: Để trống hoặc bỏ qua nếu proxy không yêu cầu xác thực.
- Hỗ trợ comment bằng ký tự `#` ở đầu dòng và tự động bỏ qua các dòng trống.

**Ví dụ:**
```text
# Proxy có tài khoản mật khẩu
http|103.152.118.23|8080|myuser|mypassword
socks5|45.76.182.90|1080|proxyuser|secret123

# Proxy không có xác thực (không có user/pass)
http|116.202.12.34|3128
socks4|194.67.210.15|1080
```

---

## 2. Cách 1: Sử Dụng Docker Image Có Sẵn (Khuyên Dùng)

Bạn không cần clone mã nguồn hay build image, chỉ cần máy có Docker là có thể chạy ngay bằng image công khai:

```bash
ghcr.io/salingnh/checkproxy:latest
```

### 2.1. Cú pháp cơ bản (Truyền file proxy từ bên ngoài)

Sử dụng tùy chọn `-v` (volume mount) để gắn file proxy từ máy chủ (host) vào container:

```bash
docker run --rm -it \
  -v "/duong/dan/den/file_proxy.txt:/data/input_proxy:ro" \
  ghcr.io/salingnh/checkproxy:latest \
  -i /data/input_proxy
```

> **Lưu ý:**
> - `-it`: Bắt buộc để giao diện Live Terminal hiển thị bảng cập nhật động.
> - `:ro`: Quyền Read-Only đảm bảo file gốc trên máy của bạn không bị thay đổi.

### 2.2. Ví dụ chạy với file proxy tại thư mục hiện tại

Nếu bạn đang đứng ở thư mục có chứa file proxy (ví dụ `proxies.txt`):

```bash
docker run --rm -it \
  -v "$(pwd)/proxies.txt:/data/input_proxy:ro" \
  ghcr.io/salingnh/checkproxy:latest \
  -i /data/input_proxy \
  -c 20 \
  --sort speed
```

### 2.3. Ví dụ đo tốc độ sâu (tải file mẫu lớn hơn)

Mặc định công cụ tải ~2MB (`2000000` bytes) để đo tốc độ. Nếu bạn muốn kiểm tra kỹ hơn với lượng dữ liệu tải ~5MB:

```bash
docker run --rm -it \
  -v "$(pwd)/proxies.txt:/data/input_proxy:ro" \
  ghcr.io/salingnh/checkproxy:latest \
  -i /data/input_proxy \
  -c 15 \
  --speed-bytes 5000000 \
  --sort speed
```

---

## 3. Cách 2: Tự Build Từ Mã Nguồn (Source Code)

Nếu bạn đã tải mã nguồn về máy:

### 3.1. Chạy nhanh bằng Docker Compose
Đặt file danh sách proxy thành `input_proxy` ở thư mục gốc của repo, sau đó chạy:

```bash
docker compose run --rm checkproxy
```

Hoặc truyền các tham số tùy ý:
```bash
docker compose run --rm checkproxy -i input_proxy -c 25 --sort latency
```

### 3.2. Tự build Docker Image thủ công
```bash
# 1. Build image từ Dockerfile
docker build -t checkproxy:latest .

# 2. Chạy container truyền file input ngoài
docker run --rm -it \
  -v "$(pwd)/proxies.txt:/data/input_proxy:ro" \
  checkproxy:latest \
  -i /data/input_proxy \
  -c 10 \
  --sort speed
```

---

## 4. Bảng Tham Số Chi Tiết (CLI Arguments)

Các tham số có thể truyền trực tiếp vào lệnh chạy để tùy biến hành vi:

| Tham số | Viết tắt | Mặc định | Bắt buộc | Chi tiết |
| :--- | :---: | :---: | :---: | :--- |
| `--input` | `-i` | *Không có* | **Có** | Đường dẫn đến file danh sách proxy bên trong container (ví dụ: `/data/input_proxy`). |
| `--concurrency` | `-c` | `10` | Không | Số lượng worker kiểm tra đồng thời. Tăng lên 20-50 nếu danh sách proxy nhiều. |
| `--sort` | | `speed` | Không | Tiêu chí sắp xếp bảng kết quả: <br>• `speed`: Ưu tiên tốc độ tải về cao nhất xuống thấp. <br>• `latency`: Ưu tiên ping/độ trễ thấp nhất lên trước. |
| `--speed-bytes` | | `2000000` | Không | Dung lượng byte tải về để đo tốc độ (mặc định 2MB). Giảm xuống để check nhanh hơn hoặc tăng lên để đo kỹ hơn. |
| `--connect-timeout`| | `3.0` | Không | Timeout kết nối TCP ban đầu tới proxy (tính bằng giây). |
| `--timeout` | | `15.0` | Không | Tổng thời gian timeout tối đa cho toàn bộ chu trình kiểm tra một proxy (tính bằng giây). |
| `--check-url` | | `https://ifconfig.io/ip` | Không | URL dùng để kiểm tra proxy còn sống (alive) và lấy Outbound IP của proxy. |
| `--speed-url` | | `https://speed.cloudflare.com/__down?bytes={bytes}` | Không | URL dùng để tải dữ liệu đo tốc độ. Tự động thay thế placeholder `{bytes}`. |
| `--mask-password` | | `True` | Không | Ẩn mật khẩu proxy khi hiển thị (`user:***@host:port`). Có thể truyền `true`/`false`. |
| `--no-mask-password`| | | Không | Hiển thị mật khẩu proxy đầy đủ trên màn hình, không che. |

---

## 5. Giấy Phép (License)

Dự án được phân phối dưới giấy phép mã nguồn mở [LICENSE](LICENSE) (MIT License).
