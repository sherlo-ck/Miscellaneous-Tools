"""
适用：iOS 17 及以上（含 iOS 27.x），Windows / macOS / Linux。
准备：
  1. python -m pip install -U pymobiledevice3
  2. iPhone 用数据线连电脑，解锁并点“信任”
  3. 打开开发者模式：设置 → 隐私与安全性 → 开发者模式
     （找不到这一项，先运行：python ios_gps.py reveal）

用法：
  python ios_gps.py check                                   # 环境与设备自检
  python ios_gps.py reveal                                  # 让“开发者模式”菜单显示出来
  python ios_gps.py set 31.2304 121.4737                    # 设置固定坐标（纬度 经度，WGS-84）
  python ios_gps.py set 39.9042 116.4074 --gcj02            # 坐标来自高德/腾讯地图时加 --gcj02
  python ios_gps.py make-gpx --points="31.23,121.47;31.24,121.48" --kmh 5 -o route.gpx
  python ios_gps.py play route.gpx                          # 按轨迹移动
  python ios_gps.py clear                                   # 恢复真实定位

重要：set / play 运行期间，终端窗口必须保持打开。按 Ctrl+C（或回车）结束，
      设备会随会话结束恢复真实定位。
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 用当前 Python 解释器调用，避免 PATH 找不到 pymobiledevice3 命令
PY_CLI = [sys.executable, "-m", "pymobiledevice3"]
SIM = PY_CLI + ["developer", "dvt", "simulate-location"]

HINTS = """\
可能的原因：
  1. 手机未解锁 / 未点“信任” / 数据线只能充电 —— 运行 check 确认设备能被识别
  2. 未开启开发者模式 —— 先运行 reveal，再到 设置 → 隐私与安全性 → 开发者模式 打开，
     重启后在弹窗里确认
  3. pymobiledevice3 版本过旧 —— python -m pip install -U pymobiledevice3
  4. userspace 方式不可用时，改用 tunnel：
       终端 1：sudo python -m pymobiledevice3 lockdown start-tunnel   （Windows 用管理员终端，保持运行）
       终端 2：python ios_gps.py set 纬度 经度 --rsd <终端1输出的地址> <端口>
"""


# --------------------------------------------------------------------------
# 坐标工具
# --------------------------------------------------------------------------
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """GCJ-02（高德/腾讯）→ WGS-84（系统定位使用）。中国境外原样返回。"""
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat * 2 - (lat + dlat), lon * 2 - (lon + dlon)


def check_range(lat: float, lon: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise SystemExit(f"坐标超出范围：lat={lat}, lon={lon}（注意：纬度在前，经度在后）")


def parse_point(text: str) -> tuple[float, float]:
    try:
        lat_s, lon_s = text.replace("，", ",").split(",")
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        raise SystemExit(f"坐标格式错误：{text!r}，应为 纬度,经度")
    check_range(lat, lon)
    return lat, lon


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# 子进程管理
# --------------------------------------------------------------------------
def ensure_lib() -> None:
    if importlib.util.find_spec("pymobiledevice3") is None:
        raise SystemExit("未安装 pymobiledevice3，请先运行：\n  python -m pip install -U pymobiledevice3")


def conn_args(args: argparse.Namespace) -> list[str]:
    """默认走 userspace tunnel（免 root、免 tunneld）；手动指定 --rsd 时走传统 tunnel。"""
    rsd = getattr(args, "rsd", None)
    if rsd:
        return ["--rsd", rsd[0], rsd[1]]
    return ["--userspace"]


def stop(proc: subprocess.Popen) -> None:
    """等子进程自行退出（终端 Ctrl+C 已同时发给它），超时则强制结束。"""
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()


def run_session(cmd: list[str], *, ok_msg: str, hold: bool) -> int:
    """
    运行一个需要“保持会话”的命令。
    - 15 秒内退出：视为失败/异常（play 播完很短的轨迹除外）
    - 超过 15 秒仍在运行：认为已生效，提示用户，等待 Ctrl+C
    """
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            code = proc.poll()
            if code is not None:
                if code != 0:
                    print(f"\n命令异常退出（退出码 {code}）。\n{HINTS}")
                elif hold:
                    print("\n命令很快就结束了，定位不会保持。"
                          "请在可交互的终端里直接运行本脚本（不要后台运行或重定向输入）。")
                else:
                    print("播放结束。")
                return code
            time.sleep(0.3)

        print(ok_msg, flush=True)
        code = proc.wait()
        if code != 0:
            print(f"\n命令异常退出（退出码 {code}）。\n{HINTS}")
        else:
            print("已结束，设备会恢复真实定位。")
        return code
    except KeyboardInterrupt:
        print("\n正在停止，设备将恢复真实定位……")
        stop(proc)
        return 0


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_check(_: argparse.Namespace) -> int:
    print(f"Python {sys.version.split()[0]}  ({sys.platform})")
    if importlib.util.find_spec("pymobiledevice3") is None:
        print("✗ 未安装 pymobiledevice3：python -m pip install -U pymobiledevice3")
        return 1
    print(f"✓ pymobiledevice3 {importlib.metadata.version('pymobiledevice3')}")
    print("\n已连接的设备（usbmux list）：")
    code = subprocess.call(PY_CLI + ["usbmux", "list"])
    if code != 0:
        print("✗ 无法列出设备。Windows 请确认已安装 iTunes 或“Apple 设备”应用；Linux 请安装 usbmuxd。")
        return code
    print("\n若上面是空列表：检查数据线（需支持数据传输）、手机是否解锁、是否点了“信任”。")
    print("若设置里没有“开发者模式”：运行 python ios_gps.py reveal")
    return 0


def cmd_reveal(_: argparse.Namespace) -> int:
    ensure_lib()
    code = subprocess.call(PY_CLI + ["amfi", "reveal-developer-mode"])
    if code != 0:
        print("\n命令失败。可用 `python -m pymobiledevice3 amfi --help` 查看当前版本的子命令名。")
        return code
    print(
        "\n已发送。接下来在 iPhone 上：\n"
        "  1. 从多任务界面强制关闭“设置”，再重新打开\n"
        "  2. 设置 → 隐私与安全性 → 滑到最底部 → 开发者模式 → 打开\n"
        "  3. 手机重启后解锁，在弹窗里点“打开”并输入锁屏密码"
    )
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    ensure_lib()
    lat, lon = args.lat, args.lon
    check_range(lat, lon)
    if args.gcj02:
        lat, lon = gcj02_to_wgs84(lat, lon)
        print(f"GCJ-02 → WGS-84：{lat:.6f}, {lon:.6f}")
    cmd = SIM + ["set"] + conn_args(args) + ["--", f"{lat:.7f}", f"{lon:.7f}"]
    return run_session(
        cmd,
        ok_msg=f"✓ 虚拟定位已生效：{lat:.6f}, {lon:.6f}\n"
               "  保持本窗口打开；按 Ctrl+C（或回车）结束并恢复真实定位。",
        hold=True,
    )


def cmd_play(args: argparse.Namespace) -> int:
    ensure_lib()
    path = Path(args.gpx)
    if not path.is_file():
        raise SystemExit(f"找不到 GPX 文件：{path}")
    cmd = SIM + ["play"] + conn_args(args) + [str(path)]
    if args.noise:
        cmd.append(str(args.noise))
    return run_session(
        cmd,
        ok_msg="✓ 正在按轨迹移动。保持本窗口打开；按 Ctrl+C 结束并恢复真实定位。",
        hold=False,
    )


def cmd_clear(args: argparse.Namespace) -> int:
    ensure_lib()
    return subprocess.call(SIM + ["clear"] + conn_args(args))


def cmd_make_gpx(args: argparse.Namespace) -> int:
    raw: list[str] = []
    if args.points:
        raw += re.split(r"[;\n]+", args.points)
    if args.points_file:
        raw += Path(args.points_file).read_text(encoding="utf-8").splitlines()
    pts = [parse_point(s.strip()) for s in raw if s.strip() and not s.strip().startswith("#")]
    if len(pts) < 2:
        raise SystemExit("至少需要 2 个坐标点。示例：--points=\"31.23,121.47;31.24,121.48\"")
    if args.gcj02:
        pts = [gcj02_to_wgs84(*p) for p in pts]
    if args.loop:
        pts.append(pts[0])

    speed = args.kmh / 3.6  # m/s
    if speed <= 0 or args.interval <= 0:
        raise SystemExit("--kmh 与 --interval 必须大于 0")

    t = datetime.now(timezone.utc).replace(microsecond=0)
    track = [(pts[0], t)]
    total_m = 0.0
    for a, b in zip(pts, pts[1:]):
        dist = haversine_m(a, b)
        total_m += dist
        steps = max(1, math.ceil(dist / (speed * args.interval)))
        for i in range(1, steps + 1):
            f = i / steps
            t += timedelta(seconds=args.interval)
            track.append(((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), t))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="ios_gps.py" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <trk><name>route</name><trkseg>",
    ]
    for (lat, lon), ts in track:
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><time>{ts:%Y-%m-%dT%H:%M:%SZ}</time></trkpt>')
    lines += ["  </trkseg></trk>", "</gpx>"]

    out = Path(args.output)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    duration = (track[-1][1] - track[0][1]).total_seconds()
    print(f"已生成 {out}：{len(track)} 个轨迹点，约 {total_m / 1000:.2f} km，"
          f"{args.kmh:g} km/h 下耗时约 {duration / 60:.1f} 分钟")
    print(f"播放：python {Path(sys.argv[0]).name} play {out}")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ios_gps.py",
        description="通过 USB 给 iPhone 设置虚拟定位（基于 pymobiledevice3，iOS 17+）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    conn = argparse.ArgumentParser(add_help=False)
    conn.add_argument("--rsd", nargs=2, metavar=("HOST", "PORT"),
                      help="使用 lockdown start-tunnel 输出的 RSD 地址和端口（默认走 --userspace，无需此项）")

    sp = sub.add_parser("check", help="检查 Python、pymobiledevice3 和已连接设备")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("reveal", help="让“设置→隐私与安全性→开发者模式”菜单显示出来")
    sp.set_defaults(func=cmd_reveal)

    sp = sub.add_parser("set", parents=[conn], help="设置固定虚拟坐标（会保持运行）")
    sp.add_argument("lat", type=float, help="纬度")
    sp.add_argument("lon", type=float, help="经度")
    sp.add_argument("--gcj02", action="store_true", help="输入坐标为 GCJ-02（高德/腾讯），自动转为 WGS-84")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("play", parents=[conn], help="播放 GPX 轨迹（会保持运行）")
    sp.add_argument("gpx", help="GPX 文件路径（轨迹点需带 <time>，可用 make-gpx 生成）")
    sp.add_argument("--noise", type=int, default=0, metavar="MS",
                    help="相邻点时间间隔加入 ±MS 毫秒随机抖动（可选）")
    sp.set_defaults(func=cmd_play)

    sp = sub.add_parser("clear", parents=[conn], help="清除虚拟定位，恢复真实定位")
    sp.set_defaults(func=cmd_clear)

    sp = sub.add_parser("make-gpx", help="根据若干途经点生成带时间戳的 GPX 轨迹")
    sp.add_argument("--points", help='途经点，"纬度,经度;纬度,经度;..."（请用 --points="..." 写法，兼容负数坐标）')
    sp.add_argument("--points-file", help="途经点文件，每行一个“纬度,经度”，# 开头为注释")
    sp.add_argument("--kmh", type=float, default=5.0, help="移动速度 km/h，默认 5（步行）")
    sp.add_argument("--interval", type=float, default=1.0, help="轨迹点时间间隔（秒），默认 1")
    sp.add_argument("--gcj02", action="store_true", help="输入坐标为 GCJ-02，自动转为 WGS-84")
    sp.add_argument("--loop", action="store_true", help="走完后回到起点")
    sp.add_argument("-o", "--output", default="route.gpx", help="输出文件，默认 route.gpx")
    sp.set_defaults(func=cmd_make_gpx)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())