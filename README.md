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

## 2. Hướng Dẫn Chạy Bằng Docker Run (Dùng Image từ GitHub Packages / GHCR)

Image đã được tự động build và xuất bản lên GitHub Container Registry (GHCR) thông qua workflow [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml).

### 2.1. Cú pháp cơ bản truyền file input từ bên ngoài

Bạn có thể truyền file proxy từ bất kỳ vị trí nào trên máy chủ (host) vào container thông qua tùy chọn `-v` (volume mount):

```bash
docker run --rm -it \
  -v "/duong/dan/den/file_proxy.txt:/data/input_proxy:ro" \
  ghcr.io/<owner>/checkproxy:latest \
  -i /data/input_proxy
```

> **Lưu ý quan trọng:**
> - Tham số `-it`: Bắt buộc để phân bổ pseudo-TTY, giúp giao diện bảng Live Terminal hiển thị động và mượt mà.
> - Tham số `:ro`: Gắn quyền Read-Only cho file proxy giúp đảm bảo container không thay đổi nội dung file gốc.
> - Thay thế `<owner>` bằng tên người dùng hoặc tổ chức GitHub của bạn (ví dụ: `ghcr.io/sang-nguyen/checkproxy:latest`).

### 2.2. Ví dụ chạy với file proxy tại thư mục hiện tại

Nếu bạn đang đứng ở thư mục chứa file proxy (ví dụ `proxies.txt`):

```bash
docker run --rm -it \
  -v "$(pwd)/proxies.txt:/data/input_proxy:ro" \
  ghcr.io/<owner>/checkproxy:latest \
  -i /data/input_proxy \
  -c 20 \
  --sort speed
```

### 2.3. Ví dụ đo tốc độ sâu (tải file mẫu lớn hơn)

Mặc định công cụ tải ~2MB (`2000000` bytes) để đo tốc độ. Nếu bạn muốn kiểm tra kỹ hơn với lượng dữ liệu tải ~5MB:

```bash
docker run --rm -it \
  -v "$(pwd)/proxies.txt:/data/input_proxy:ro" \
  ghcr.io/<owner>/checkproxy:latest \
  -i /data/input_proxy \
  -c 15 \
  --speed-bytes 5000000 \
  --sort speed
```

---

## 3. Hướng Dẫn Chạy Bằng Docker Compose (Chạy Tại Source Code)

Nếu bạn đã clone mã nguồn về máy:

1. Tạo hoặc đặt file danh sách proxy thành `input_proxy` ở thư mục gốc của repo.
2. Khởi chạy bằng Docker Compose:
   ```bash
   docker compose run --rm checkproxy
   ```
3. Truyền thêm tham số tùy ý:
   ```bash
   docker compose run --rm checkproxy -i input_proxy -c 25 --sort latency
   ```

---

## 4. Tài Liệu Chi Tiết Các Tham Số (Arguments)

Tất cả các tham số có thể được truyền trực tiếp phía sau tên image khi chạy `docker run`:

| Tham số | Viết tắt | Mặc định | Bắt buộc | Chi tiết |
| :--- | :---: | :---: | :---: | :--- |
| `--input` | `-i` | *Không có* | **Có** | Đường dẫn đến file danh sách proxy bên trong container (ví dụ: `/data/input_proxy`). |
| `--concurrency` | `-c` | `10` | Không | Số lượng worker kiểm tra đồng thời (concurrency limit). Tăng lên 20-50 nếu danh sách proxy dài. |
| `--sort` | | `speed` | Không | Tiêu chí sắp xếp bảng kết quả: <br>• `speed`: Ưu tiên tốc độ tải về cao nhất xuống thấp. <br>• `latency`: Ưu tiên ping/độ trễ thấp nhất lên trước. |
| `--speed-bytes` | | `2000000` | Không | Dung lượng byte tối đa tải về để đo tốc độ (mặc định 2MB). Giảm xuống (ví dụ `500000`) để check nhanh hơn hoặc tăng lên để đo chính xác hơn. |
| `--connect-timeout`| | `3.0` | Không | Timeout kết nối TCP ban đầu tới proxy (tính bằng giây). |
| `--timeout` | | `15.0` | Không | Tổng thời gian timeout tối đa cho toàn bộ chu trình kiểm tra một proxy (tính bằng giây). |
| `--check-url` | | `https://ifconfig.io/ip` | Không | URL dùng để kiểm tra proxy có phản hồi và lấy Outbound IP của proxy. |
| `--speed-url` | | `https://speed.cloudflare.com/__down?bytes={bytes}` | Không | URL dùng để tải dữ liệu đo tốc độ. Tự động thay thế placeholder `{bytes}` bằng giá trị của `--speed-bytes`. |
| `--mask-password` | | `True` | Không | Ẩn mật khẩu của proxy khi hiển thị (`user:***@host:port`). Có thể truyền `true`/`false`. |
| `--no-mask-password`| | | Không | Cờ hiển thị rõ ràng thông tin mật khẩu đầy đủ, không che. |

---

## 5. Tự Động Build Docker Image Với GitHub Actions

Repository đã được thiết lập sẵn quy trình CI/CD trong [.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml):

- **Tự động kích hoạt khi**:
  - Có commit mới được đẩy (push) lên nhánh `main`.
  - Có git tag mới theo định dạng phiên bản `v*.*.*` (ví dụ: `v1.0.0`).
  - Kích hoạt thủ công từ tab **Actions** trên GitHub giao diện web (`workflow_dispatch`).
- **Quy trình thực hiện**:
  1. Kiểm tra mã nguồn.
  2. Cài đặt Docker Buildx với bộ nhớ đệm GitHub Actions Cache giúp tăng tốc độ build.
  3. Đăng nhập tự động vào GitHub Container Registry thông qua `GITHUB_TOKEN`.
  4. Đóng gói và gắn tag (`latest`, `sha-...`, `vX.Y.Z`).
  5. Đẩy image lên kho lưu trữ: `ghcr.io/<owner>/checkproxy:latest`.

### Tự build Image thủ công trên máy (Local Build)
Nếu muốn tự build image trên máy mà không kéo từ GHCR:
```bash
docker build -t checkproxy:latest .
```
Sau đó chạy:
```bash
docker run --rm -it -v "$(pwd)/proxies.txt:/data/input_proxy:ro" checkproxy:latest -i /data/input_proxy
```
