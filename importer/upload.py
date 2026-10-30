"""Verified, framed USB transfer into the device's persistent song store."""
from __future__ import annotations

from pathlib import Path
import os
import re
import time
import zlib

from .package import read_package
from .themes import LEGACY_THEME_COUNT

def find_device_port() -> str:
    from serial.tools import list_ports
    candidates = [p.device for p in list_ports.comports() if p.vid == 0x303A and p.pid == 0x4001]
    if not candidates:
        raise RuntimeError("没有检测到 Mosaico 运行串口。请接上 USB，并让设备正常启动。下载模式不能导入歌曲。")
    if len(candidates) > 1:
        raise RuntimeError("检测到多个 Espressif 设备，请用 --port 指定 Mosaico 串口。")
    return candidates[0]


def parse_fields(line: str) -> dict:
    return dict(re.findall(r"([a-zA-Z_][a-zA-Z_0-9]*)=([^\s]+)", line))


class StoreConnection:
    def __init__(self, port: str | None = None, serial_object=None, *, expected_mac=None):
        configured = expected_mac if expected_mac is not None else os.environ.get("MOSAICO_EXPECTED_DEVICE_MAC")
        if not isinstance(configured, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", configured):
            raise ValueError("请先设置目标设备身份：MOSAICO_EXPECTED_DEVICE_MAC=设备MAC，或在命令行传入 --expected-mac；尚未连接或写入设备。")
        self.expected_mac = configured.lower()
        if serial_object is not None:
            self.serial = serial_object
        else:
            import serial
            self.serial = serial.Serial(port=None, baudrate=115200, timeout=.2, write_timeout=8)
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.port = port or find_device_port()
            self.serial.open()

    def close(self):
        self.serial.close()

    def send(self, command: str):
        self.serial.write((command + "\n").encode("ascii"))
        self.serial.flush()

    def receive(self, prefix: str, timeout=12.) -> str:
        deadline, recent = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            line = self.serial.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            recent.append(line)
            recent = recent[-8:]
            if line.startswith("STORE ERROR"):
                raise RuntimeError("设备导入失败：" + line)
            if line.startswith(prefix):
                return line
        raise TimeoutError("设备没有及时响应 " + prefix + "。" + "\n".join(recent))

    def info(self) -> dict:
        self.send("STORE INFO")
        fields = parse_fields(self.receive("STORE INFO"))
        if fields.get("mac", "").lower() != self.expected_mac:
            raise RuntimeError("连接设备的身份与设置的目标 Mosaico 不一致，已停止传输。")
        if fields.get("protocol") != "2":
            raise RuntimeError("设备歌曲导入协议不匹配，请先更新支持 VSK2 的固件。")
        if fields.get("mounted") != "1":
            raise RuntimeError("Mosaico 歌曲存储未挂载。请查看设备日志。")
        return fields

    def upload(self, path: Path, progress=lambda message: None) -> dict:
        package = read_package(path)
        data = Path(path).read_bytes()
        info = self.info()
        if package.get("required_theme_count", 0) > LEGACY_THEME_COUNT:
            minimum = package["minimum_firmware"]
            try:
                theme_count = int(info.get("themes", "0"))
                theme_ids = {int(value) for value in info.get("theme_ids", "").split(",") if value}
                version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", info.get("version", ""))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("设备返回了无效的场景主题能力，请检查固件。") from exc
            if (theme_count < package["required_theme_count"]
                    or not set(range(1, package["required_theme_count"]+1)).issubset(theme_ids)
                    or version is None or tuple(map(int, version.groups())) < tuple(map(int, minimum.split(".")))):
                raise RuntimeError(f"这份编排使用雪山、森林或风暴背景，请先更新 Mosaico 到 {minimum} 或更新固件；电脑预览和保存仍可使用。")
        if package.get("required_effect_revision"):
            try:
                revision = int(info.get("effect_revision", "0"))
                effects = int(info.get("effects", "0"))
                scene_revision = int(info.get("scene_revision", "0"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("设备返回了无效的脚边特效能力，请检查固件。") from exc
            if (revision < package["required_effect_revision"]
                    or effects < package["required_effect_count"]
                    or scene_revision < package["required_scene_revision"]):
                raise RuntimeError("这份编排包含脚边特效设置，请先更新 Mosaico 到 2.9.0 或更新固件；电脑预览和保存仍可使用。")
        if package.get('required_slalom_revision'):
            try:
                revision = int(info.get('slalom_revision', '0'))
            except (TypeError, ValueError) as exc:
                raise RuntimeError('设备返回了无效的绕桩动作能力，请检查固件。') from exc
            if revision < package['required_slalom_revision']:
                raise RuntimeError('这份编排使用新增绕桩动作，请先更新 Mosaico 到 2.7.0 或更新固件；电脑预览和保存仍可使用。')
        if package.get("required_record_color_count"):
            try:
                colors = int(info.get("record_colors", "1"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("设备返回了无效的唱片配色能力，请检查固件。") from exc
            if colors < package["required_record_color_count"]:
                raise RuntimeError("这份编排使用新版唱片配色，请先更新 Mosaico 到 2.6.0 或更新固件；电脑预览和保存仍可使用。")
        if package.get("required_theme_count"):
            try:
                theme_count = int(info.get("themes", "1"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("设备返回了无效的场景主题能力，请检查固件。") from exc
            if theme_count < package["required_theme_count"]:
                raise RuntimeError(f"这份编排使用新的场景主题，请先更新 Mosaico 到 {package['minimum_firmware']} 或更新固件；电脑预览和保存仍可使用。")
        if package.get("version", 2) >= 3 and (info.get("reactivity") != "1" or "3" not in info.get("formats", "2").split(",")):
            raise RuntimeError(f"这份编排包含鼓点与音量控制轨，请先更新 Mosaico 到 {package['minimum_firmware']} 或更新固件；电脑预览和保存仍可使用。")
        # Check before starting a NAND transaction. ID 14 kept its number
        # but gained a full airborne turn in 2.2.0, so its semantics require
        # the new bank even though older firmware can decode that ID.
        try:
            supported = int(info.get("clips", "10"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("设备返回了无效的动作库能力，请检查固件。") from exc
        clip_ids = {item["clip_id"] for item in package["plan"]["segments"]}
        new_bank = 14 in clip_ids or any(clip >= 16 for clip in clip_ids)
        required = max(package["required_clip_count"], 19 if new_bank else 1 + max(clip_ids))
        if supported < required:
            minimum = package["minimum_firmware"]
            raise RuntimeError(f"这份编排包含新增花式动作，当前设备固件暂不支持。请先更新到 {minimum} 或更新版本；电脑预览和保存仍可使用。")
        if package.get("required_motion_revision"):
            version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", info.get("version", ""))
            minimum = package["minimum_firmware"]
            required_version = tuple(map(int, minimum.split(".")))
            if version is None or tuple(map(int, version.groups())) < required_version:
                raise RuntimeError(f"这份编排使用新版花式动作，请先更新 Mosaico 到 {minimum} 或更新固件，才能与电脑预览一致。")
        if int(info.get("free", "0")) < len(data):
            raise RuntimeError("设备歌曲存储空间不足。")
        begun = False
        try:
            self.send(f"STORE BEGIN {len(data)} {package['sha256']}")
            ready = parse_fields(self.receive("STORE READY", 30.))
            offset = int(ready.get("offset", "-1"))
            if offset != 0:
                raise RuntimeError("设备未从歌曲包起点开始接收。")
            maximum = min(4096, int(ready.get("max_chunk", "4096")))
            if maximum <= 0:
                raise RuntimeError("设备返回了无效传输块大小。")
            begun = True
            while offset < len(data):
                chunk = data[offset:offset + maximum]
                crc = f"{zlib.crc32(chunk):08x}"
                self.send(f"STORE CHUNK {offset} {len(chunk)} {crc}")
                response = parse_fields(self.receive("STORE DATA"))
                if int(response.get("offset", "-1")) != offset or int(response.get("bytes", "-1")) != len(chunk):
                    raise RuntimeError("设备传输块索引不一致。")
                self.serial.write(chunk)
                self.serial.flush()
                acknowledgement = parse_fields(self.receive("STORE ACK", 20.))
                next_offset = offset + len(chunk)
                if int(acknowledgement.get("offset", "-1")) != next_offset:
                    raise RuntimeError("设备未确认完整的歌曲数据块。")
                if acknowledgement.get("crc", "").lower() != crc:
                    raise RuntimeError("设备确认的传输块 CRC 不一致。")
                offset = next_offset
                progress(f"正在导入 Mosaico：{offset / len(data):.0%}")
            self.send("STORE COMMIT")
            result = parse_fields(self.receive("STORE COMMIT", 60.))
            if result.get("sha256", "").lower() != package["sha256"]:
                raise RuntimeError("设备保存的歌曲 SHA256 不一致。")
            if int(result.get("samples", "-1")) != package["plan"]["sample_count"]:
                raise RuntimeError("设备保存的音频样本数不一致。")
            begun = False
            return {"port": getattr(self.serial, "port", "test"), "device": info,
                    "package": {k: v for k, v in package.items() if k not in ("pcm_bytes", "plan")},
                    "stored": result}
        finally:
            if begun:
                try:
                    self.send("STORE ABORT")
                    self.receive("STORE ABORT", 5.)
                except Exception:
                    pass


def upload_package(path: Path, port=None, progress=lambda message: None, *, expected_mac=None) -> dict:
    connection = StoreConnection(port, expected_mac=expected_mac)
    try:
        return connection.upload(path, progress)
    finally:
        connection.close()
