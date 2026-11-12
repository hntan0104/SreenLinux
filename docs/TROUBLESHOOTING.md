# Cẩm Nang Khắc Phục Sự Cố (Troubleshooting Guide) - SreenLinux

Tài liệu này tổng hợp các vấn đề thường gặp trong quá trình cài đặt, kết nối và sử dụng **SreenLinux**, cùng các bước chẩn đoán và giải pháp khắc phục chi tiết.

---

## Mục Lục
1. [Sự Cố Kết Nối Cáp USB & ADB](#1-sự-cố-kết-nối-cáp-usb--adb)
2. [Sự Cố Máy Chủ Linux & Màn Hình X11 (Wayland vs X11)](#2-sự-cố-máy-chủ-linux--màn-hình-x11-wayland-vs-x11)
3. [Sự Cố Màn Hình Ảo (Virtual Display)](#3-sự-cố-màn-hình-ảo-virtual-display)
4. [Sự Cố Điều Khiển Cảm Ứng (Touch Input & XTest)](#4-sự-cố-điều-khiển-cảm-ứng-touch-input--xtest)
5. [Hiệu Năng: Giật Lag, Tụt FPS, Hoặc Độ Trễ Cao](#5-hiệu-năng-giật-lag-tụt-fps-hoặc-độ-trễ-cao)
6. [Sự Cố Biên Dịch & Cài Đặt Ứng Dụng Android](#6-sự-cố-biên-dịch--cài-đặt-ứng-dụng-android)
7. [Chạy Không Có Màn Hình Thật (Headless)](#7-chạy-không-có-màn-hình-thật-headless)

---

## 1. Sự Cố Kết Nối Cáp USB & ADB

### Hiện tượng 1: Lệnh `adb devices` không hiển thị thiết bị hoặc báo `unauthorized`

#### Nguyên nhân:
- Chưa kích hoạt **Gỡ lỗi USB (USB Debugging)** trên thiết bị Android.
- Cáp USB chỉ có chức năng sạc (không truyền dữ liệu) hoặc cổng USB chập chờn.
- Chưa xác nhận vân tay khóa RSA trên màn hình điện thoại.
- Linux thiếu quyền truy cập thiết bị qua udev.

#### Cách khắc phục:
1. **Kiểm tra cáp USB**: Thử đổi cáp truyền dữ liệu chất lượng cao và cắm trực tiếp vào cổng USB trên mainboard (tránh dùng hub USB không có nguồn riêng).
2. **Kích hoạt USB Debugging trên Android**:
   - Vào `Cài đặt` -> `Thông tin điện thoại` (Settings -> About phone).
   - Nhấn 7 lần liên tiếp vào dòng `Số phiên bản` (Build number) cho đến khi hiện thông báo "Bạn đã là nhà phát triển".
   - Quay lại `Cài đặt` -> `Tùy chọn nhà phát triển` (Developer options) -> Bật `Gỡ lỗi USB` (USB Debugging).
3. **Xác nhận khóa RSA**:
   - Khi cắm cáp vào máy tính, trên điện thoại sẽ xuất hiện hộp thoại: *"Cho phép gỡ lỗi USB từ máy tính này?"*.
   - Tích chọn **"Luôn cho phép từ máy tính này"** rồi nhấn **OK**.
4. **Khởi động lại ADB Daemon**:
   ```bash
   adb kill-server
   adb start-server
   adb devices
   ```
   Kết quả đúng sẽ có dạng:
   ```
   List of devices attached
   9A123BCDEF456    device
   ```
5. **Cấu hình udev rules trên Linux (nếu báo `no permissions`)**:
   ```bash
   sudo usermod -aG plugdev $USER
   sudo apt-get install android-sdk-platform-tools-common
   sudo udevadm control --reload-rules
   sudo udevadm trigger
   ```

---

### Hiện tượng 2: Lỗi `adb reverse: cannot bind to socket` hoặc cổng 6100 bị chiếm dụng

#### Nguyên nhân:
Cổng `6100` trên máy tính hoặc trên điện thoại đang bị tiến trình khác chiếm dụng.

#### Cách khắc phục:
1. Kiểm tra tiến trình đang chiếm cổng `6100`:
   ```bash
   sudo lsof -i :6100
   # hoặc
   ss -tulpn | grep 6100
   ```
2. Tắt tiến trình cũ:
   ```bash
   kill -9 <PID>
   ```
3. Khởi tạo lại reverse port forwarding:
   ```bash
   adb reverse --remove-all
   adb reverse tcp:6100 tcp:6100
   ```
4. Hoặc khởi động server với cổng khác:
   ```bash
   python3 server/screen_server.py --port 6105
   adb reverse tcp:6105 tcp:6105
   ```
   *(Trong ứng dụng Android, nhấn biểu tượng Settings và đổi cổng thành 6105).*

---

## 2. Sự Cố Máy Chủ Linux & Màn Hình X11 (Wayland vs X11)

### Hiện tượng 1: Server báo lỗi chụp màn hình hoặc màn hình Android chỉ có màu đen

#### Nguyên nhân chính:
SreenLinux yêu cầu máy tính chạy trên nền tảng **X11 (Xorg)** để truy cập trực tiếp bộ nhớ khung hình (XShm) và tạo màn hình ảo qua `xrandr`. Môi trường **Wayland** (mặc định trên Ubuntu 22.04+) chặn các ứng dụng ngoài đọc khung hình màn hình desktop trực tiếp vì lý do bảo mật.

#### Cách kiểm tra:
Chạy lệnh trong terminal:
```bash
echo $XDG_SESSION_TYPE
```
- Nếu hiển thị: `x11` -> Hệ thống đạt yêu cầu.
- Nếu hiển thị: `wayland` -> Cần chuyển sang phiên bản X11.

#### Cách khắc phục:
1. Đăng xuất khỏi phiên làm việc hiện tại (Log out).
2. Tại màn hình đăng nhập (GDM), nhấp vào tên người dùng của bạn.
3. Nhấp vào **biểu tượng bánh răng cưa (⚙)** ở góc dưới bên phải màn hình.
4. Chọn **"Ubuntu on Xorg"** (hoặc "GNOME on Xorg").
5. Đăng nhập và kiểm tra lại `echo $XDG_SESSION_TYPE` (sẽ ra `x11`).

---

### Hiện tượng 2: Lỗi thiếu thư viện chụp ảnh hoặc `xwd: command not found`

#### Cách khắc phục:
Cài đặt bổ sung gói tiện ích X11 và Pillow:
```bash
sudo apt-get update
sudo apt-get install -y x11-utils x11-apps x11-xserver-utils xdotool
pip3 install -r server/requirements.txt
```

---

## 3. Sự Cố Màn Hình Ảo (Virtual Display)

### Hiện tượng: Báo lỗi `No disconnected output found` khi chạy với cờ `--virtual`

#### Nguyên nhân:
Trình điều khiển đồ họa (card Intel, AMD hoặc Nvidia) không tự động khai báo các cổng xuất ảo `VIRTUAL1` ở trạng thái ngắt kết nối.

#### Cách khắc phục:
1. **Kiểm tra danh sách cổng hiển thị hiện có**:
   ```bash
   xrandr --query
   ```
   Tìm xem có cổng nào đang ở trạng thái `disconnected` (ví dụ `HDMI-1`, `DP-1`, `VIRTUAL1`...).
2. **Chạy server chỉ định vùng cắt màn hình (Region) thay vì Virtual Display**:
   Nếu không tạo được màn hình ảo độc lập, bạn có thể chạy chế độ chia vùng hoặc mirror màn hình chính:
   ```bash
   # Mirror toàn bộ màn hình chính
   python3 server/screen_server.py

   # Hoặc chỉ stream một góc màn hình (X,Y,Width,Height)
   python3 server/screen_server.py --region 0,0,1920,1080
   ```
3. **Kích hoạt Virtual Output cho driver Intel**:
   Tạo file cấu hình Xorg:
   ```bash
   sudo nano /etc/X11/xorg.conf.d/20-intel.conf
   ```
   Thêm nội dung:
   ```xorg
   Section "Device"
       Identifier "intelgpu0"
       Driver "intel"
       Option "VirtualHeads" "1"
   EndSection
   ```
   Sau đó khởi động lại máy để xuất hiện `VIRTUAL1`.

---

### Dọn Dẹp Màn Hình Ảo Bị "Treo" Sau Khi Thoát Đột Ngột:
Nếu bạn tắt server bằng `kill -9` mà không qua SIGINT (Ctrl+C), màn hình ảo có thể vẫn còn xuất hiện trong Display Settings. Để xóa nó thủ công:
```bash
xrandr --output VIRTUAL1 --off
```

---

## 4. Sự Cố Điều Khiển Cảm Ứng (Touch Input & XTest)

### Hiện tượng 1: Chạm trên màn hình điện thoại nhưng chuột Linux không di chuyển

#### Cách kiểm tra:
1. Xem dòng log khi server khởi động:
   - `Input backend: XTest` — bình thường.
   - `Input backend: xdotool` — dùng phương án dự phòng (chậm hơn nhưng vẫn chạy).
   - `Neither XTest nor xdotool available` — cài thêm: `sudo apt-get install -y libxtst6 xdotool`
   - `Input injection disabled (view-only mode)` — bạn đang chạy với `--no-input`.
2. Lưu ý: chạm **1 ngón rồi nhấc** là click; con trỏ chỉ di chuyển liên tục khi **rê ngón** (kéo thả).
3. Thử nghiệm nhanh từ máy tính: `xdotool mousemove 100 100` — nếu lệnh này không chạy, phiên làm việc không phải X11.

### Hiện tượng 2: Chuột click lệch vị trí so với ngón tay chạm

#### Nguyên nhân:
- Màn hình ảo được đặt ở vị trí lệch (offset) so với màn hình chính nhưng tham số offset tính sai.
- Tỉ lệ khung hình (Aspect Ratio) của điện thoại khác với độ phân giải stream.

#### Giải pháp:
- SreenLinux tự động đọc offset của màn hình ảo (`_output_geometry()` trong `virtual_display.py`). Hãy đảm bảo không thay đổi layout màn hình trong `Settings -> Displays` khi server đang chạy (nếu đã đổi, khởi động lại server).
- Nếu sử dụng chế độ cắt vùng, hãy khai báo chính xác tọa độ `--region X,Y,W,H`.

---

## 5. Hiệu Năng: Giật Lag, Tụt FPS, Hoặc Độ Trễ Cao

| Nguyên nhân | Giải pháp tối ưu |
|---|---|
| **Độ phân giải quá cao (2K/4K) làm nghẽn CPU** | Giảm độ phân giải stream xuống 1080p hoặc 720p:<br>`python3 server/screen_server.py --width 1280 --height 720` |
| **Chất lượng nén JPEG quá cao** | Giảm chất lượng nén xuống 35-50 (vẫn đảm bảo rõ nét văn bản mà dung lượng giảm một nửa):<br>`python3 server/screen_server.py --quality 40` |
| **Cáp USB chất lượng kém hoặc chạy ở chế độ USB 2.0 chậm** | Cắm vào cổng USB 3.0 (cổng màu xanh dương trên PC) và dùng cáp USB 3.0/3.1 zin theo máy. |
| **Không dùng được MIT-SHM** (log: `MIT-SHM capture unavailable`) | Cài `libxext6`; chỉ hoạt động khi server chạy trên chính máy có X server (không qua SSH X-forwarding). Kiểm tra dòng `Capture backend: shm` khi khởi động. |
| **Máy yếu (ARM, Jetson, Raspberry Pi)** | Nén JPEG chiếm nhiều CPU nhất. Giảm `--fps 20` và `--quality 40`; tránh `--width/--height` vì resize cũng tốn CPU — thay vào đó dùng màn hình ảo độ phân giải thấp (`--virtual-res 720p`). |

---

## 6. Sự Cố Biên Dịch & Cài Đặt Ứng Dụng Android

### Hiện tượng 1: Lỗi `SDK location not found` khi chạy `./gradlew assembleDebug`

#### Khắc phục:
Tạo file `android/local.properties` và trỏ đường dẫn tới Android SDK của bạn:
```properties
sdk.dir=/home/<username>/Android/Sdk
```
*(Trên hầu hết máy Ubuntu, đường dẫn mặc định là `/home/$USER/Android/Sdk`).*

### Hiện tượng 2: `AAPT2 ... Daemon startup failed` / `aapt2: cannot execute binary file` trên máy ARM64

Google chỉ phát hành công cụ build Android (`aapt2`, `build-tools`) cho Linux **x86_64**. Trên Jetson/Raspberry Pi/máy ARM64 khác:
- Build APK trên một máy x86_64 (hoặc Android Studio trên Windows/macOS), rồi copy file `app-debug.apk` sang và cài bằng `adb install -r app-debug.apk`.

### Hiện tượng 3: Không hiển thị menu trên điện thoại

Ứng dụng SreenLinux mặc định chạy ở chế độ **Immersive Full-Screen** (toàn màn hình, ẩn thanh điều hướng và thanh trạng thái) để tối đa diện tích hiển thị:
- **Để bật/tắt thanh thông tin**: chạm **3 ngón tay** cùng lúc, hoặc bấm nút **Back**.
- Thanh thông tin hiển thị FPS thực tế, độ phân giải, nút **Hide** và **Disconnect**. Sau khi Disconnect, bạn có thể đổi Host/Port ở màn hình chính.

### Hiện tượng 4: App liên tục báo "Server not reachable"

- Server chưa chạy → chạy `./start.sh`.
- Server tự chạy lại `adb reverse` mỗi khi cắm cáp; xem log server có dòng `Device … connected — adb reverse tcp:6100 active`. Nếu thấy `unauthorized`: mở khóa điện thoại và chấp nhận hộp thoại gỡ lỗi USB.
- Kiểm tra: `adb reverse --list` phải có dòng `tcp:6100 tcp:6100`.

---

## 7. Chạy Không Có Màn Hình Thật (Headless)

- **Đăng nhập**: dùng `sudo ./install-service.sh` để thấy màn hình đăng nhập trên điện thoại và gõ mật khẩu bằng nút ⌨.
- **Màn hình đăng nhập đen hoặc chỉ 640×480 khi rút màn hình** (log Xorg: `Setting mode "NULL"`): khi không có màn hình nào, driver NVIDIA không bật đầu ra và GNOME không vẽ gì. Chạy `sudo ./setup-headless.sh` (mặc định 1920×1080) rồi khởi động lại: script tạo một màn hình ảo (`ConnectedMonitor` + `CustomEDID` do `edid_gen.py` sinh ra) trên cổng DisplayPort. Sao lưu ở `/etc/X11/xorg.conf.sreenlinux.bak`; khôi phục bằng `sudo ./setup-headless.sh --undo`. Cách chắc chắn nhất vẫn là **dummy plug DisplayPort**.
- **Điện thoại/máy tính bảng liên tục ngắt kết nối, cứ hỏi "Cho phép gỡ lỗi USB" dù đã tick "Luôn cho phép"**: GNOME tự gắn thiết bị qua MTP/PTP (`gvfsd-mtp`) và tranh chấp với `adb`, làm USB reset liên tục. Trên thiết bị: *Tùy chọn nhà phát triển → Cấu hình USB mặc định → Không truyền dữ liệu* (adb vẫn hoạt động). Kiểm tra: `journalctl -k | grep "USB disconnect"` không còn xuất hiện liên tục.
- **Không thấy UEFI / log khởi động**: phần mềm chỉ chạy được khi Linux đã khởi động tới màn hình đăng nhập. Để xem từ lúc bật nguồn: dùng cổng **micro-USB debug** của Jetson (console dạng chữ) với app terminal serial trên điện thoại, hoặc một **USB HDMI capture card**.
- **Dịch vụ báo `active session … is 'wayland'`**: đặt `WaylandEnable=false` trong `/etc/gdm3/custom.conf` rồi khởi động lại.
- **Lối thoát khi có sự cố**: SSH từ máy khác (`ssh <user>@<IP>`), rồi `journalctl -u sreenlinux -n 50`.

---

## Cần Trợ Giúp Thêm?
Nếu bạn gặp vấn đề không nằm trong danh mục trên:
1. Chạy server với chế độ chi tiết: `./start.sh -v`
2. Kiểm tra logcat trên Android: `adb logcat -s ScreenClient`
3. Mở issue trên GitHub repository của dự án kèm theo 2 đoạn log trên.
