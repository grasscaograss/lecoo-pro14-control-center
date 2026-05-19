import sys
import os
import subprocess
import re
import platform
import json
import winreg
import threading
import struct
import time
import logging
import ctypes
import shutil
from logging.handlers import RotatingFileHandler
from enum import IntEnum
from typing import Any, Tuple, Union

# 尝试导入Windows特定的模块
try:
    import win32pipe
    import win32file
    HAS_WIN32 = True
except ImportError:
    print("警告: 未找到win32file模块，请安装pywin32库")
    HAS_WIN32 = False

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QComboBox, QGroupBox, QStatusBar,
    QMessageBox, QSlider, QCheckBox, QSystemTrayIcon, QMenu, QAction,
    QDesktopWidget
)
from PyQt5.QtCore import QPointF, QThread, pyqtSignal, Qt, QMutex, QMutexLocker, QTimer
from PyQt5.QtGui import QFont, QIcon, QPainter, QPen, QColor, QPolygonF
from PyQt5.QtNetwork import QLocalServer, QLocalSocket

# IPC协议实现
# 枚举类型定义（与Rust代码对应）
class PowerProfile(IntEnum):
    Silent = 0
    Default = 1
    Performance = 2

class FanIndex(IntEnum):
    CPU = 0
    GPU = 1
    Both = 2

class FanMode(IntEnum):
    Auto = 0
    Full = 1
    Custom = 2

class KeyboardBacklightLevel(IntEnum):
    Off = 0
    Low = 1
    Medium = 2
    High = 3

class ChargeLimit(IntEnum):
    Full = 0
    High = 1
    Balanced = 2
    Lifespan = 3
    Desk = 4

class PowerLedMode(IntEnum):
    Auto = 0
    Custom = 1

# 请求类型（与Rust代码中的IpcRequest对应）
class IpcRequestType(IntEnum):
    GetSystemState = 0
    GetFansRPM = 1
    GetTemperatures = 2
    GetChargeLimit = 3
    GetPowerProfile = 4
    GetKeyboardBacklight = 5
    SetPowerProfile = 6
    SetFanMode = 7
    SetKeyboardBacklight = 8
    SetChargeLimit = 9
    SetLedMode = 10
    DaemonCommand = 11

# 响应类型（与Rust代码中的IpcResponse对应）
class IpcResponseType(IntEnum):
    Success = 0
    Message = 1
    FanRPM = 2
    Temp = 3
    ChargeLimit = 4
    KeyboardBacklight = 5
    PowerLimit = 6
    DaemonResponse = 7
    Error = 8

# IPC协议常量
IPC_PROTOCOL_VERSION = (0, 3, 3)
MAGIC_BYTES = b'LCC'
HANDSHAKE_LEN = 5
PIPE_NAME = r"\\.\pipe\lecoo_ctl_daemon"
IPC_LOCK = threading.Lock()
IPC_LOCK_TIMEOUT_SEC = 1.5


def encode_varint(v: int) -> bytes:
    """编码变长整数"""
    buf = []
    while True:
        byte = v & 0x7F
        v >>= 7
        if v:
            buf.append(byte | 0x80)
        else:
            buf.append(byte)
            break
    return bytes(buf)

def encode_u8(v: int) -> bytes:
    """编码u8"""
    return struct.pack('<B', v)

def encode_u16(v: int) -> bytes:
    """编码u16"""
    return struct.pack('<H', v)


class BincodeDecoder:
    """bincode解码器"""
    
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_varint(self) -> int:
        """读取变长整数"""
        result = 0
        shift = 0
        while True:
            if self.pos >= len(self.data):
                raise EOFError("Unexpected end of data")
            byte = self.data[self.pos]
            self.pos += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if byte & 0x80 == 0:
                break
        return result

    def read_u8(self) -> int:
        """读取u8"""
        if self.pos + 1 > len(self.data):
            raise EOFError
        val = self.data[self.pos]
        self.pos += 1
        return val

    def read_u16(self) -> int:
        """读取u16"""
        if self.pos + 2 > len(self.data):
            raise EOFError
        val = struct.unpack('<H', self.data[self.pos:self.pos+2])[0]
        self.pos += 2
        return val

    def read_string(self) -> str:
        """读取字符串"""
        length = self.read_varint()
        if self.pos + length > len(self.data):
            raise EOFError
        s = self.data[self.pos:self.pos+length].decode('utf-8')
        self.pos += length
        return s


# 构建请求函数
def build_request_get_temperatures() -> bytes:
    """构建获取温度请求"""
    return encode_varint(IpcRequestType.GetTemperatures)

def build_request_get_fans_rpm() -> bytes:
    """构建获取风扇转速请求"""
    return encode_varint(IpcRequestType.GetFansRPM)

def build_request_set_power_profile(profile: PowerProfile) -> bytes:
    """构建设置电源模式请求"""
    return encode_varint(IpcRequestType.SetPowerProfile) + encode_varint(profile.value)

def build_request_set_fan_mode(fan: FanIndex, mode: FanMode, custom_pwm: int = 0) -> bytes:
    """构建设置风扇模式请求"""
    data = encode_varint(IpcRequestType.SetFanMode) + encode_varint(fan.value) + encode_varint(mode.value)
    if mode == FanMode.Custom:
        data += encode_u16(custom_pwm)
    return data

def build_request_set_keyboard_backlight(level: KeyboardBacklightLevel) -> bytes:
    """构建设置键盘背光请求"""
    return encode_varint(IpcRequestType.SetKeyboardBacklight) + encode_varint(level.value)

def build_request_set_charge_limit(limit: ChargeLimit) -> bytes:
    """构建设置充电限制请求"""
    return encode_varint(IpcRequestType.SetChargeLimit) + encode_varint(limit.value)

def build_request_set_led_mode(mode: PowerLedMode, brightness: int = 0) -> bytes:
    """构建设置LED模式请求"""
    data = encode_varint(IpcRequestType.SetLedMode) + encode_varint(mode.value)
    if mode == PowerLedMode.Custom:
        data += encode_u8(brightness)
    return data


# 解码响应函数
def decode_response(data: bytes) -> Tuple[str, Any]:
    """解码响应数据"""
    if not data:
        return "Error", "Empty Response"
    
    decoder = BincodeDecoder(data)
    try:
        tag = decoder.read_varint()
    except EOFError:
        return "Error", "Empty Response"

    if tag == IpcResponseType.Success:
        return "Success", None
    elif tag == IpcResponseType.Message:
        try:
            msg = decoder.read_string()
            return "Message", msg
        except:
            return "Error", "Invalid message"
    elif tag == IpcResponseType.FanRPM:
        # FanRPM响应格式：u8 + u16 + u8 + u16
        try:
            decoder.read_u8()  # 跳过第一个u8
            cpu = decoder.read_u16()
            decoder.read_u8()  # 跳过第二个u8
            gpu = decoder.read_u16()
            return "FanRPM", (cpu, gpu)
        except:
            return "Error", "Invalid FanRPM response"
    elif tag == IpcResponseType.Temp:
        # Temp(u8, u8)
        try:
            cpu = decoder.read_u8()
            sys_temp = decoder.read_u8()
            return "Temp", (cpu, sys_temp)
        except:
            return "Error", "Invalid Temp response"
    elif tag == IpcResponseType.ChargeLimit:
        # ChargeLimit(u8, u8, u8)
        try:
            min_p = decoder.read_u8()
            max_p = decoder.read_u8()
            current = decoder.read_u8()
            return "ChargeLimit", (min_p, max_p, current)
        except:
            return "Error", "Invalid ChargeLimit response"
    elif tag == IpcResponseType.KeyboardBacklight:
        try:
            level = decoder.read_varint()
            return "KeyboardBacklight", level
        except:
            return "Error", "Invalid KeyboardBacklight response"
    elif tag == IpcResponseType.PowerLimit:
        try:
            profile = decoder.read_varint()
            return "PowerLimit", profile
        except:
            return "Error", "Invalid PowerLimit response"
    elif tag == IpcResponseType.Error:
        try:
            msg = decoder.read_string()
            return "Error", msg
        except:
            return "Error", "Invalid error message"
    else:
        return "Error", f"Unknown response type: {tag}"


def request_name(request_data: bytes) -> str:
    if not request_data:
        return "EmptyRequest"
    try:
        return IpcRequestType(request_data[0] & 0x7F).name
    except Exception:
        return f"UnknownRequest({request_data[0]})"


class IpcClient:
    """IPC客户端 - 与Lecoo Control Center守护进程通信"""
    
    def __init__(self):
        self.handle = None

    def connect(self):
        """连接到守护进程"""
        if not HAS_WIN32:
            logger.error("IPC 连接失败：win32file 模块未安装")
            raise ConnectionError("win32file模块未安装")
        
        try:
            self.handle = win32file.CreateFile(
                PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None
            )
            # 发送握手
            handshake = MAGIC_BYTES + bytes([IPC_PROTOCOL_VERSION[0], IPC_PROTOCOL_VERSION[1]])
            win32file.WriteFile(self.handle, handshake)

            # 接收握手响应
            err, resp = win32file.ReadFile(self.handle, HANDSHAKE_LEN)
            if len(resp) != HANDSHAKE_LEN or resp[:3] != b'OKK':
                raise ConnectionError(f"握手失败，响应: {resp.hex() if resp else 'empty'}")
            return True
        except Exception as e:
            self.close()
            logger.warning("IPC 连接或握手失败: %s", e)
            if isinstance(e, ConnectionError):
                raise
            raise ConnectionError(f"无法连接到守护进程: {e}")

    def send_request(self, request_data: bytes) -> bytes:
        """发送请求并接收响应"""
        if self.handle is None:
            raise RuntimeError("未连接")

        req_name = request_name(request_data)
        started_at = time.monotonic()
        # 构建完整消息：长度(4字节) + 版本(3字节) + 数据
        msg_len = len(request_data)
        msg = struct.pack('<I', msg_len) + bytes(IPC_PROTOCOL_VERSION[:3]) + request_data
        win32file.WriteFile(self.handle, msg)

        # 读取响应长度
        err, len_bytes = win32file.ReadFile(self.handle, 4)
        if len(len_bytes) != 4:
            raise IOError("读取响应长度失败")
        resp_len = struct.unpack('<I', len_bytes)[0]

        # 读取版本和数据
        err, ver_bytes = win32file.ReadFile(self.handle, 3)
        err, data_bytes = win32file.ReadFile(self.handle, resp_len)
        if len(data_bytes) != resp_len:
            raise IOError("读取响应数据不完整")
        elapsed = time.monotonic() - started_at
        if elapsed >= 0.8:
            logger.warning("IPC 请求响应较慢: request=%s elapsed=%.3fs response_len=%d", req_name, elapsed, resp_len)
        return data_bytes

    def close(self):
        """关闭连接"""
        if self.handle and HAS_WIN32:
            try:
                win32file.CloseHandle(self.handle)
            except:
                pass
            self.handle = None

    def __enter__(self):
        if not IPC_LOCK.acquire(timeout=IPC_LOCK_TIMEOUT_SEC):
            raise TimeoutError("IPC 正忙，请稍后重试")
        try:
            self.connect()
            return self
        except Exception:
            IPC_LOCK.release()
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.close()
        finally:
            IPC_LOCK.release()

    # 便捷方法
    def get_temperatures(self):
        """获取温度信息"""
        try:
            request = build_request_get_temperatures()
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            if resp_type == "Temp":
                return data
            logger.debug("读取温度返回异常响应: type=%s data=%s", resp_type, data)
        except Exception:
            logger.debug("读取温度异常", exc_info=True)
        return None

    def get_fans_rpm(self):
        """获取风扇转速"""
        try:
            request = build_request_get_fans_rpm()
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            if resp_type == "FanRPM":
                return data
            logger.debug("读取风扇转速返回异常响应: type=%s data=%s", resp_type, data)
        except Exception:
            logger.debug("读取风扇转速异常", exc_info=True)
        return None

    def set_power_profile(self, profile: PowerProfile):
        """设置电源模式"""
        try:
            request = build_request_set_power_profile(profile)
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            success = resp_type == "Success"
            if not success:
                logger.warning("设置电源模式失败响应: profile=%s type=%s data=%s", profile, resp_type, data)
            return success
        except Exception:
            logger.warning("设置电源模式异常: profile=%s", profile, exc_info=True)
        return False

    def set_fan_mode(self, fan: FanIndex, mode: FanMode, custom_pwm: int = 0):
        """设置风扇模式"""
        try:
            request = build_request_set_fan_mode(fan, mode, custom_pwm)
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            success = resp_type == "Success"
            if not success:
                logger.warning("设置风扇模式失败响应: fan=%s mode=%s pwm=%s type=%s data=%s", fan, mode, custom_pwm, resp_type, data)
            return success
        except Exception:
            logger.warning("设置风扇模式异常: fan=%s mode=%s pwm=%s", fan, mode, custom_pwm, exc_info=True)
        return False

    def set_keyboard_backlight(self, level: KeyboardBacklightLevel):
        """设置键盘背光"""
        try:
            request = build_request_set_keyboard_backlight(level)
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            success = resp_type == "Success"
            if not success:
                logger.warning("设置键盘背光失败响应: level=%s type=%s data=%s", level, resp_type, data)
            return success
        except Exception:
            logger.warning("设置键盘背光异常: level=%s", level, exc_info=True)
        return False

    def set_charge_limit(self, limit: ChargeLimit):
        """设置充电限制"""
        try:
            request = build_request_set_charge_limit(limit)
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            success = resp_type == "Success"
            if not success:
                logger.warning("设置充电限制失败响应: limit=%s type=%s data=%s", limit, resp_type, data)
            return success
        except Exception:
            logger.warning("设置充电限制异常: limit=%s", limit, exc_info=True)
        return False

    def set_led_mode(self, mode: PowerLedMode, brightness: int = 0):
        """设置LED模式"""
        try:
            request = build_request_set_led_mode(mode, brightness)
            response_data = self.send_request(request)
            resp_type, data = decode_response(response_data)
            success = resp_type == "Success"
            if not success:
                logger.warning("设置电源灯失败响应: mode=%s brightness=%s type=%s data=%s", mode, brightness, resp_type, data)
            return success
        except Exception:
            logger.warning("设置电源灯异常: mode=%s brightness=%s", mode, brightness, exc_info=True)
        return False

QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

APP_DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", CURRENT_DIR), "LecooControlCenter")
LEGACY_CONFIG_PATH = os.path.join(CURRENT_DIR, "config.json")
CONFIG_PATH = os.path.join(APP_DATA_DIR, "config.json")
AUTOSTART_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_APP_NAME = "LecooControl"

def app_resource_path(filename):
    external_path = os.path.join(CURRENT_DIR, filename)
    if os.path.exists(external_path):
        return external_path
    if getattr(sys, 'frozen', False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, filename)
    return external_path

LECOO_CTRL_PATH = app_resource_path("lecoo-ctrl.exe")
INSTALLER_PATH = app_resource_path("install.bat")
REPAIR_SERVICE_RECOVERY_PATH = app_resource_path("repair-service-recovery.bat")
INSTALL_SOURCE_DIR = os.path.dirname(app_resource_path("lecoo-ec-daemon.exe"))
SERVICE_NAME = "LecooControlDaemon"
SERVICE_WATCHDOG_INTERVAL_MS = 120000
CPU_FAN_MAX_RPM = 6500
GPU_FAN_MAX_RPM = 6400
AUTOSTART_DELAY_SEC = 10
DEFAULT_CURVE_POINTS = [[40, 30], [60, 60], [75, 90], [90, 100]]
DEFAULT_CURVE_RISE_DELAY_SEC = 0
DEFAULT_CURVE_FALL_DELAY_SEC = 10
CURVE_TEMP_HYSTERESIS = 3
CURVE_PWM_HYSTERESIS = 5
UDO_CMD = []

def setup_logging():
    logger = logging.getLogger("LecooControlGUI")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        candidate_dirs = [
            os.path.join(os.environ.get("LOCALAPPDATA", CURRENT_DIR), "LecooControlCenter", "logs"),
            os.path.join(CURRENT_DIR, "logs"),
            os.path.join(os.environ.get("TEMP", CURRENT_DIR), "LecooControlCenter", "logs")
        ]
        log_path = ""
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s")

        for log_base_dir in candidate_dirs:
            try:
                os.makedirs(log_base_dir, exist_ok=True)
                log_path = os.path.join(log_base_dir, "gui.log")
                handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
                handler.setFormatter(formatter)
                logger.addHandler(handler)
                break
            except Exception:
                log_path = ""

        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
            log_path = "disabled"
    else:
        log_path = getattr(logger.handlers[0], "baseFilename", "unknown")

    return logger, log_path

logger, LOG_PATH = setup_logging()

def subprocess_creationflags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)

def is_admin_user():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        logger.exception("检查管理员权限失败")
        return False

def cmd_quote(value):
    return '"' + str(value).replace('"', '""') + '"'

def installer_log_path():
    if LOG_PATH not in ("disabled", "unknown"):
        return os.path.join(os.path.dirname(LOG_PATH), "installer.log")
    return os.path.join(os.environ.get("TEMP", CURRENT_DIR), "LecooControlCenter", "logs", "installer.log")

def installer_launcher_path():
    return os.path.join(os.path.dirname(installer_log_path()), "installer-run.cmd")

def service_recovery_log_path():
    if LOG_PATH not in ("disabled", "unknown"):
        return os.path.join(os.path.dirname(LOG_PATH), "service-recovery-repair.log")
    return os.path.join(os.environ.get("TEMP", CURRENT_DIR), "LecooControlCenter", "logs", "service-recovery-repair.log")

def service_recovery_launcher_path():
    return os.path.join(os.path.dirname(service_recovery_log_path()), "service-recovery-repair-run.cmd")

def ensure_app_data_dir():
    os.makedirs(APP_DATA_DIR, exist_ok=True)

def build_autostart_command():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" --hidden --startup-delay {AUTOSTART_DELAY_SEC}'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}" --hidden --startup-delay {AUTOSTART_DELAY_SEC}'

def parse_startup_delay(argv):
    for idx, arg in enumerate(argv):
        value = None
        if arg == "--startup-delay" and idx + 1 < len(argv):
            value = argv[idx + 1]
        elif arg.startswith("--startup-delay="):
            value = arg.split("=", 1)[1]

        if value is not None:
            try:
                return max(0, min(60, int(value)))
            except (TypeError, ValueError):
                return 0
    return 0

def apply_startup_delay():
    delay_sec = parse_startup_delay(sys.argv)
    if delay_sec <= 0:
        return
    logger.info("延迟启动以等待桌面 DPI 状态稳定: seconds=%s", delay_sec)
    time.sleep(delay_sec)

def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        logger.exception("读取开机自启状态失败")
        return False

def migrate_config_if_needed():
    if os.path.exists(CONFIG_PATH):
        return
    for source in (LEGACY_CONFIG_PATH, app_resource_path("config.json")):
        if not source or not os.path.exists(source):
            continue
        try:
            ensure_app_data_dir()
            shutil.copy2(source, CONFIG_PATH)
            logger.info("已迁移配置文件: %s -> %s", source, CONFIG_PATH)
            return
        except Exception:
            logger.exception("迁移配置失败: %s -> %s", source, CONFIG_PATH)

migrate_config_if_needed()

def read_tail(path, max_chars=6000):
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return data[-max_chars:]
    except Exception:
        logger.exception("读取日志失败: %s", path)
        return ""

def query_daemon_service_state():
    try:
        result = subprocess.run(
            ["sc", "query", SERVICE_NAME],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=8,
            creationflags=subprocess_creationflags()
        )
    except Exception:
        logger.exception("查询服务状态失败: service=%s", SERVICE_NAME)
        return "QUERY_FAILED"

    output = f"{result.stdout}\n{result.stderr}".upper()
    if result.returncode != 0:
        logger.warning("服务不存在或查询失败: service=%s returncode=%s output=%s", SERVICE_NAME, result.returncode, output.strip())
        return "MISSING"
    if "RUNNING" in output:
        return "RUNNING"
    if "STOPPED" in output:
        return "STOPPED"
    if "START_PENDING" in output:
        return "START_PENDING"
    if "STOP_PENDING" in output:
        return "STOP_PENDING"
    return "UNKNOWN"

def is_service_recovery_configured():
    try:
        service_key = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, service_key, 0, winreg.KEY_READ) as key:
            actions, actions_type = winreg.QueryValueEx(key, "FailureActions")
            flag = 0
            try:
                flag, _ = winreg.QueryValueEx(key, "FailureActionsOnNonCrashFailures")
            except FileNotFoundError:
                pass
            return actions_type == winreg.REG_BINARY and bool(actions) and flag == 1
    except FileNotFoundError:
        return False
    except Exception:
        logger.exception("读取服务恢复注册表配置失败")

    try:
        result = subprocess.run(
            ["sc", "qfailure", SERVICE_NAME],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=8,
            creationflags=subprocess_creationflags()
        )
    except Exception:
        logger.exception("查询服务恢复策略失败")
        return False

    output = f"{result.stdout}\n{result.stderr}".upper()
    return result.returncode == 0 and "RESTART" in output

def run_service_recovery_repair_headless():
    if not os.path.exists(REPAIR_SERVICE_RECOVERY_PATH):
        logger.warning("找不到服务恢复修复脚本: %s", REPAIR_SERVICE_RECOVERY_PATH)
        return False

    repair_log = service_recovery_log_path()
    os.makedirs(os.path.dirname(repair_log), exist_ok=True)
    try:
        with open(repair_log, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        logger.exception("清空服务恢复修复日志失败: %s", repair_log)

    if is_admin_user():
        try:
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "call", REPAIR_SERVICE_RECOVERY_PATH, "--headless"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
                creationflags=subprocess_creationflags()
            )
        except Exception:
            logger.exception("服务恢复修复脚本执行失败")
            return False
        if result.stdout.strip():
            logger.info("服务恢复修复输出: %s", result.stdout.strip())
        if result.stderr.strip():
            logger.warning("服务恢复修复错误输出: %s", result.stderr.strip())
        return result.returncode == 0

    launcher_path = service_recovery_launcher_path()
    launcher_content = "\n".join([
        "@echo off",
        f"call {cmd_quote(REPAIR_SERVICE_RECOVERY_PATH)} --headless > {cmd_quote(repair_log)} 2>&1",
        "exit /b %errorlevel%",
        ""
    ])
    try:
        with open(launcher_path, "w", encoding="gbk", errors="replace") as f:
            f.write(launcher_content)
    except Exception:
        logger.exception("写入服务恢复修复启动器失败: %s", launcher_path)
        return False

    params = f"/d /c {cmd_quote(launcher_path)}"
    logger.info("服务恢复策略缺失，准备通过 UAC 运行修复脚本: script=%s log=%s", REPAIR_SERVICE_RECOVERY_PATH, repair_log)
    try:
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", params, CURRENT_DIR, 0)
    except Exception:
        logger.exception("通过 UAC 运行服务恢复修复脚本失败")
        return False
    if result <= 32:
        logger.warning("UAC 运行服务恢复修复脚本失败或被取消: shell_execute_result=%s", result)
        return False
    return True

def ensure_service_recovery_configured():
    state = query_daemon_service_state()
    if state == "MISSING":
        logger.info("后台服务不存在，跳过服务恢复策略修复")
        return False
    recovery_configured = is_service_recovery_configured()
    if state == "RUNNING" and recovery_configured:
        logger.info("服务正在运行，恢复策略已配置")
        return True
    logger.warning("后台服务需要管理员修复: state=%s recovery_configured=%s", state, recovery_configured)
    return run_service_recovery_repair_headless()

def write_installer_repair_launcher(launcher_path, installer_log):
    launcher_content = "\n".join([
        "@echo off",
        "setlocal",
        f"call {cmd_quote(INSTALLER_PATH)} < nul > {cmd_quote(installer_log)} 2>&1",
        "set \"INSTALL_EXIT=%errorlevel%\"",
        f"if exist {cmd_quote(REPAIR_SERVICE_RECOVERY_PATH)} call {cmd_quote(REPAIR_SERVICE_RECOVERY_PATH)} --headless >> {cmd_quote(installer_log)} 2>&1",
        "exit /b %INSTALL_EXIT%",
        ""
    ])
    with open(launcher_path, "w", encoding="gbk", errors="replace") as f:
        f.write(launcher_content)

def run_installer_with_recovery_repair():
    if not os.path.exists(INSTALLER_PATH):
        logger.error("找不到安装脚本，无法自动修复后台服务: %s", INSTALLER_PATH)
        return False

    logger.warning("后台服务未运行，准备通过上游安装脚本修复后台服务: installer=%s source=%s", INSTALLER_PATH, INSTALL_SOURCE_DIR)
    installer_log = installer_log_path()
    os.makedirs(os.path.dirname(installer_log), exist_ok=True)
    try:
        with open(installer_log, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        logger.exception("清空安装日志失败: %s", installer_log)

    launcher_path = installer_launcher_path()
    try:
        write_installer_repair_launcher(launcher_path, installer_log)
    except Exception:
        logger.exception("写入安装修复启动器失败: %s", launcher_path)
        return False

    if not is_admin_user():
        params = f"/d /c {cmd_quote(launcher_path)}"
        logger.warning("当前 GUI 不是管理员，准备通过 UAC 拉起安装脚本: launcher=%s log=%s", launcher_path, installer_log)
        try:
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", params, CURRENT_DIR, 0)
        except Exception:
            logger.exception("通过 UAC 拉起安装脚本失败")
            return False

        if result <= 32:
            logger.warning("UAC 拉起安装脚本失败或被取消: shell_execute_result=%s", result)
            return False

        for _ in range(90):
            state = query_daemon_service_state()
            if state == "RUNNING":
                installer_output = read_tail(installer_log)
                if installer_output.strip():
                    logger.info("安装脚本输出: %s", installer_output.strip())
                logger.info("管理员安装脚本执行后服务已运行")
                return True
            installer_output = read_tail(installer_log)
            if "INSTALLATION FAILED" in installer_output:
                state = query_daemon_service_state()
                if state == "RUNNING":
                    logger.info("安装日志包含失败字样，但服务已运行，按成功处理")
                    return True
                logger.warning("管理员安装脚本失败: %s", installer_output.strip())
                return False
            time.sleep(1)

        installer_output = read_tail(installer_log)
        if installer_output.strip():
            logger.warning("管理员安装脚本输出: %s", installer_output.strip())
        logger.warning("等待管理员安装脚本启动服务超时")
        return False

    try:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "call", launcher_path],
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=120,
            creationflags=subprocess_creationflags()
        )
    except subprocess.TimeoutExpired:
        logger.exception("安装脚本执行超时")
        return False
    except Exception:
        logger.exception("运行安装脚本失败")
        return False

    installer_output = read_tail(installer_log)
    if installer_output.strip():
        logger.info("安装脚本输出: %s", installer_output.strip())
    elif result.stdout.strip():
        logger.info("安装启动器输出: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("安装启动器错误输出: %s", result.stderr.strip())
    if result.returncode != 0:
        logger.warning("安装启动器返回失败: returncode=%s", result.returncode)
        return False
    for _ in range(10):
        if query_daemon_service_state() == "RUNNING":
            return True
        time.sleep(1)
    logger.warning("安装启动器已结束，但后台服务未进入运行状态")
    return False

def try_start_daemon_service():
    try:
        result = subprocess.run(
            ["sc", "start", SERVICE_NAME],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            creationflags=subprocess_creationflags()
        )
    except Exception:
        logger.exception("尝试启动后台服务失败")
        return False

    output = f"{result.stdout}\n{result.stderr}".strip()
    if output:
        logger.info("尝试启动后台服务输出: %s", output)
    return result.returncode == 0

def ensure_daemon_service():
    state = query_daemon_service_state()
    logger.info("后台服务状态: service=%s state=%s", SERVICE_NAME, state)
    if state == "RUNNING":
        return True

    if state == "STOPPED":
        logger.warning("后台服务已停止，尝试直接启动: service=%s", SERVICE_NAME)
        if try_start_daemon_service():
            for _ in range(10):
                state = query_daemon_service_state()
                if state == "RUNNING":
                    logger.info("后台服务启动成功: service=%s", SERVICE_NAME)
                    return True
                time.sleep(1)

    logger.warning("后台服务未运行，请先用安装包完成安装或手动修复: service=%s state=%s", SERVICE_NAME, state)
    return False

def log_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("未处理异常", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = log_uncaught_exception

CMD_LOCK = QMutex()

class SysInfoThread(QThread):
    temps_signal = pyqtSignal(str, str)
    fans_signal = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self._running = True
        self._last_cpu_temp_str = "--"
        self._last_sys_temp_str = "--"
        self._last_cpu_fan_str = "--"
        self._last_gpu_fan_str = "--"
        self._poll_fail_count = 0

    def run(self):
        while self._running:
            cpu_temp_str = self._last_cpu_temp_str
            sys_temp_str = self._last_sys_temp_str
            cpu_fan_str = self._last_cpu_fan_str
            gpu_fan_str = self._last_gpu_fan_str
            poll_ok = False

            try:
                # 同一个连接内完成一次轮询，避免状态读取和设置请求并发抢管道。
                with IpcClient() as client:
                    temps = client.get_temperatures()
                    if temps:
                        cpu_temp_str = str(temps[0])
                        sys_temp_str = str(temps[1])
                        self._last_cpu_temp_str = cpu_temp_str
                        self._last_sys_temp_str = sys_temp_str
                        poll_ok = True

                    fans = client.get_fans_rpm()
                    if fans:
                        cpu_fan_str = str(fans[0])
                        gpu_fan_str = str(fans[1])
                        self._last_cpu_fan_str = cpu_fan_str
                        self._last_gpu_fan_str = gpu_fan_str
                        poll_ok = True
            except Exception as e:
                # 保留上一次有效值，避免短暂 IPC 失败时系统信息整块跳成 --。
                self._poll_fail_count += 1
                if self._poll_fail_count in (1, 3, 10) or self._poll_fail_count % 30 == 0:
                    logger.warning("系统信息轮询失败，保留上次有效值: count=%s error=%s", self._poll_fail_count, e)
            else:
                if poll_ok:
                    if self._poll_fail_count:
                        logger.info("系统信息轮询恢复: previous_failures=%s", self._poll_fail_count)
                    self._poll_fail_count = 0
                else:
                    self._poll_fail_count += 1
                    if self._poll_fail_count in (1, 3, 10) or self._poll_fail_count % 30 == 0:
                        logger.warning("系统信息轮询未取得有效数据，保留上次有效值: count=%s", self._poll_fail_count)

            self.temps_signal.emit(cpu_temp_str, sys_temp_str)
            self.fans_signal.emit(cpu_fan_str, gpu_fan_str)

            self.msleep(1000)

    def stop(self):
        self._running = False
        if not self.wait(1500):
            self.terminate()
            self.wait(500)

class IpcCommandThread(QThread):
    result_signal = pyqtSignal(bool, str)

    def __init__(self, action, params, success_message="", failure_message=""):
        super().__init__()
        self.action = action
        self.params = params
        self.success_message = success_message
        self.failure_message = failure_message

    def run(self):
        success = False
        error_message = ""
        started_at = time.monotonic()
        logger.info("IPC 命令开始: action=%s params=%r", self.action, self.params)
        try:
            with IpcClient() as client:
                def execute(action, params):
                    if action == "set_fan":
                        return client.set_fan_mode(
                            params["fan"],
                            params["mode"],
                            params.get("custom_value", 0)
                        )
                    if action == "set_fans":
                        for fan, mode, custom_value in params.get("requests", []):
                            if not client.set_fan_mode(fan, mode, custom_value):
                                return False
                        return True
                    if action == "set_power_profile":
                        return client.set_power_profile(params["profile"])
                    if action == "set_charge_limit":
                        return client.set_charge_limit(params["limit"])
                    if action == "set_keyboard_backlight":
                        return client.set_keyboard_backlight(params["level"])
                    if action == "set_led":
                        return client.set_led_mode(
                            params["mode"],
                            params.get("brightness", 0)
                        )
                    raise ValueError(f"未知 IPC 动作: {action}")

                if self.action == "apply_all":
                    success = True
                    for action, params in self.params.get("commands", []):
                        if not execute(action, params):
                            success = False
                            break
                else:
                    success = execute(self.action, self.params)
        except Exception as e:
            error_message = str(e)

        if success:
            message = self.success_message
        elif error_message and self.failure_message:
            message = f"{self.failure_message}: {error_message}"
        else:
            message = self.failure_message or error_message or "IPC 操作失败"

        elapsed = time.monotonic() - started_at
        if success:
            logger.info("IPC 命令成功: action=%s elapsed=%.3fs", self.action, elapsed)
        else:
            logger.warning("IPC 命令失败: action=%s elapsed=%.3fs message=%s", self.action, elapsed, message)

        self.result_signal.emit(success, message)

class ServiceWatchdogThread(QThread):
    result_signal = pyqtSignal(str, bool)

    def run(self):
        state = "UNKNOWN"
        restarted = False
        try:
            state = query_daemon_service_state()
            if state == "STOPPED":
                logger.warning("服务监控发现后台服务停止，尝试启动: service=%s", SERVICE_NAME)
                restarted = try_start_daemon_service()
                for _ in range(10):
                    state = query_daemon_service_state()
                    if state == "RUNNING":
                        break
                    self.msleep(1000)
        except Exception:
            logger.exception("服务监控检查失败")
            state = "QUERY_FAILED"

        self.result_signal.emit(state, restarted)

class FanCurveWidget(QWidget):
    points_changed = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = [[30, 20], [50, 40], [70, 70], [90, 100]]
        self.setMinimumHeight(200)
        self.dragging_index = -1
        self.margin = 30
        self.setMouseTracking(True)

    def set_points(self, points):
        self.points = sorted(points, key=lambda x: x[0])
        self.update()

    def to_screen(self, temp, pwm):
        width, height = self.width(), self.height()
        margin = self.margin
        draw_w = width - 2 * margin
        draw_h = height - 2 * margin
        x = margin + (temp / 100) * draw_w
        y = margin + draw_h - (pwm / 100) * draw_h
        return QPointF(x, y)

    def from_screen(self, pos):
        width, height = self.width(), self.height()
        margin = self.margin
        draw_w = width - 2 * margin
        draw_h = height - 2 * margin
        temp = (pos.x() - margin) / draw_w * 100
        pwm = 100 - (pos.y() - margin) / draw_h * 100
        return temp, pwm

    def mousePressEvent(self, event):
        self.dragging_index = -1
        for i, p in enumerate(self.points):
            pos = self.to_screen(p[0], p[1])
            if abs(pos.x() - event.pos().x()) < 15 and abs(pos.y() - event.pos().y()) < 15:
                self.dragging_index = i
                self.update()
                break
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging_index >= 0:
            temp, pwm = self.from_screen(event.pos())
            temp = max(0, min(100, temp))
            pwm = max(0, min(100, pwm))
            self.points[self.dragging_index] = [int(temp), int(pwm)]
            self.points = sorted(self.points, key=lambda x: x[0])
            self.dragging_index = self.points.index([int(temp), int(pwm)])
            self.update()
            self.points_changed.emit(self.points)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.dragging_index = -1
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width, height = self.width(), self.height()
        margin = self.margin
        draw_w = width - 2 * margin
        draw_h = height - 2 * margin

        painter.setPen(QPen(QColor("#cbd5e0"), 1))
        painter.drawRect(margin, margin, draw_w, draw_h)

        for i in range(0, 101, 20):
            x = margin + (i / 100) * draw_w
            painter.setPen(QPen(QColor("#cbd5e0"), 1))
            painter.drawLine(int(x), margin, int(x), margin + draw_h)
            painter.setPen(QPen(QColor("#7f8c8d"), 9))
            painter.drawText(int(x) - 10, margin + draw_h + 15, f"{i}")

        for i in range(0, 101, 20):
            y = margin + draw_h - (i / 100) * draw_h
            painter.setPen(QPen(QColor("#cbd5e0"), 1))
            painter.drawLine(margin, int(y), margin + draw_w, int(y))
            painter.setPen(QPen(QColor("#7f8c8d"), 9))
            painter.drawText(margin - 5, int(y) + 4, f"{i}%")

        path = QPolygonF()
        path.append(self.to_screen(0, self.points[0][1]))
        for p in self.points:
            path.append(self.to_screen(p[0], p[1]))
        path.append(self.to_screen(100, self.points[-1][1]))

        painter.setPen(QPen(QColor("#3498db"), 2))
        painter.drawPolyline(path)

        painter.setBrush(QColor("#e74c3c"))
        for i, p in enumerate(self.points):
            pos = self.to_screen(p[0], p[1])
            if i == self.dragging_index:
                painter.drawEllipse(pos, 8, 8)
            else:
                painter.drawEllipse(pos, 5, 5)

def calculate_curve_percent(current_temp, curve_points):
    points = sorted(curve_points, key=lambda x: x[0])

    if current_temp <= points[0][0]:
        return int(points[0][1])
    if current_temp >= points[-1][0]:
        return int(points[-1][1])

    for i in range(len(points) - 1):
        t1, p1 = points[i]
        t2, p2 = points[i + 1]
        if t1 <= current_temp <= t2:
            if t2 == t1:
                return int(p2)
            ratio = (current_temp - t1) / (t2 - t1)
            return int(p1 + ratio * (p2 - p1))
    return 0


def calculate_pwm(current_temp, curve_points):
    return int(calculate_curve_percent(current_temp, curve_points) * 1.5)


def estimate_fan_rpm(percent, fan_type):
    max_rpm = CPU_FAN_MAX_RPM if fan_type == "cpu" else GPU_FAN_MAX_RPM
    return int(round(max_rpm * max(0, min(100, percent)) / 100.0))

class FanCurveDialog(QMainWindow):
    curve_enabled_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("风扇曲线设置")
        self.setMinimumSize(500, 400)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.curve_enabled = QCheckBox("启用自定义曲线控制")
        self.curve_enabled.stateChanged.connect(self.on_curve_enabled_changed)
        self.curve_plot = FanCurveWidget()
        self.curve_plot.setMinimumHeight(200)
        self.curve_plot.points_changed.connect(self.sync_from_curve)
        self.point_editors = []
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._do_save_config)
        points_layout = QHBoxLayout()

        for i in range(4):
            vbox = QVBoxLayout()
            t_spin = QSpinBox()
            t_spin.setRange(0, 100)
            t_spin.setSuffix(" °C")
            p_spin = QSpinBox()
            p_spin.setRange(0, 100)
            p_spin.setSuffix(" %")
            t_spin.setValue(DEFAULT_CURVE_POINTS[i][0])
            p_spin.setValue(DEFAULT_CURVE_POINTS[i][1])
            t_spin.valueChanged.connect(self.sync_curve_data)
            p_spin.valueChanged.connect(self.sync_curve_data)
            vbox.addWidget(t_spin)
            vbox.addWidget(p_spin)
            points_layout.addLayout(vbox)
            self.point_editors.append((t_spin, p_spin))

        delay_group = QGroupBox("响应延迟")
        delay_layout = QHBoxLayout()
        self.rise_delay_spin = QSpinBox()
        self.rise_delay_spin.setRange(0, 120)
        self.rise_delay_spin.setSuffix(" 秒")
        self.rise_delay_spin.setValue(DEFAULT_CURVE_RISE_DELAY_SEC)
        self.rise_delay_spin.valueChanged.connect(self.on_delay_changed)
        self.fall_delay_spin = QSpinBox()
        self.fall_delay_spin.setRange(0, 300)
        self.fall_delay_spin.setSuffix(" 秒")
        self.fall_delay_spin.setValue(DEFAULT_CURVE_FALL_DELAY_SEC)
        self.fall_delay_spin.valueChanged.connect(self.on_delay_changed)
        delay_layout.addWidget(QLabel("升速延迟："))
        delay_layout.addWidget(self.rise_delay_spin)
        delay_layout.addWidget(QLabel("降速延迟："))
        delay_layout.addWidget(self.fall_delay_spin)
        delay_layout.addStretch()
        delay_group.setLayout(delay_layout)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(12)
        main_layout.addWidget(self.curve_enabled)
        main_layout.addWidget(self.curve_plot)
        main_layout.addWidget(delay_group)
        main_layout.addLayout(points_layout)

    def _do_save_config(self):
        if self.parent():
            self.parent().save_config()

    def _schedule_save(self):
        if not self._save_timer.isActive():
            self._save_timer.start(500)

    def on_curve_enabled_changed(self, state):
        self.curve_enabled_changed.emit(state == Qt.Checked)
        self._schedule_save()
        logger.info("风扇曲线开关变化: enabled=%s", state == Qt.Checked)
        if state == Qt.Unchecked and self.parent():
            self.parent()._curve_apply_request_id += 1
            self.parent()._curve_apply_inflight = False
            self.parent().reset_curve_pending()
            # 使用IPC客户端设置风扇模式为自动
            self.parent()._exec_cmd_silent(
                "set_fans",
                requests=[
                    (FanIndex.CPU, FanMode.Auto, 0),
                    (FanIndex.GPU, FanMode.Auto, 0)
                ]
            )

    def set_curve_enabled(self, enabled):
        self.curve_enabled.setChecked(enabled)

    def set_points(self, points):
        for i, pt in enumerate(points):
            if i < len(self.point_editors):
                t_spin, p_spin = self.point_editors[i]
                t_spin.setValue(pt[0])
                p_spin.setValue(pt[1])

    def set_response_delays(self, rise_delay, fall_delay):
        self.rise_delay_spin.blockSignals(True)
        self.fall_delay_spin.blockSignals(True)
        self.rise_delay_spin.setValue(int(rise_delay))
        self.fall_delay_spin.setValue(int(fall_delay))
        self.rise_delay_spin.blockSignals(False)
        self.fall_delay_spin.blockSignals(False)

    def get_curve_enabled(self):
        return self.curve_enabled.isChecked()

    def get_points(self):
        pts = []
        for t_spin, p_spin in self.point_editors:
            pts.append([t_spin.value(), p_spin.value()])
        return pts

    def get_response_delays(self):
        return self.rise_delay_spin.value(), self.fall_delay_spin.value()

    def on_delay_changed(self):
        if self.parent():
            self.parent()._saved_curve_rise_delay = self.rise_delay_spin.value()
            self.parent()._saved_curve_fall_delay = self.fall_delay_spin.value()
            self.parent().reset_curve_pending()
        self._schedule_save()

    def sync_curve_data(self):
        pts = self.get_points()
        self.curve_plot.set_points(pts)
        self._schedule_save()

    def sync_from_curve(self, pts):
        for i, pt in enumerate(pts):
            if i < len(self.point_editors):
                t_spin, p_spin = self.point_editors[i]
                t_spin.blockSignals(True)
                p_spin.blockSignals(True)
                t_spin.setValue(pt[0])
                p_spin.setValue(pt[1])
                t_spin.blockSignals(False)
                p_spin.blockSignals(False)
        self._schedule_save()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

class LecooControlGUI(QMainWindow):
    def __init__(self, hidden=False):
        super().__init__()
        self.hidden = hidden
        logger.info("GUI 启动: hidden=%s current_dir=%s config=%s log=%s has_win32=%s", hidden, CURRENT_DIR, CONFIG_PATH, LOG_PATH, HAS_WIN32)
        self.setWindowTitle("来酷Pro14 控制中心")
        logo_path = app_resource_path("logo.png")
        if os.path.exists(logo_path):
            self.setWindowIcon(QIcon(logo_path))

        self.setMinimumSize(620, 780)

        self.active_threads = []
        self._auto_applying = False
        self._auto_pending_count = 0
        self._curve_apply_inflight = False
        self._curve_apply_request_id = 0
        self.last_curve_pwm = -1
        self.last_curve_percent = -1
        self.last_curve_temp = -1
        self._curve_pending_direction = None
        self._curve_pending_since = 0.0
        self._curve_pending_pwm = -1
        self._curve_pending_percent = -1
        self._curve_pending_temp = -1
        self._saved_curve_rise_delay = DEFAULT_CURVE_RISE_DELAY_SEC
        self._saved_curve_fall_delay = DEFAULT_CURVE_FALL_DELAY_SEC
        self._service_watchdog_inflight = False
        self._service_repair_attempted = False

        self.init_status_bar()
        self.init_ui()
        self.setup_stylesheet()
        self.init_tray()

        self.status_bar.showMessage("正在检查后台服务...", 3000)
        self._daemon_ready = ensure_daemon_service()
        if not self._daemon_ready:
            self.status_bar.showMessage("后台服务未运行，准备检查修复状态", 3000)
        if not self.hidden:
            self.ensure_service_repair_on_user_launch()
        self.init_service_watchdog()

        self.sysinfo_thread = SysInfoThread()
        self.sysinfo_thread.temps_signal.connect(self.update_temps)
        self.sysinfo_thread.fans_signal.connect(self.update_fans)
        self.sysinfo_thread.start()

        if os.path.exists(CONFIG_PATH):
            self.load_config()
            self.apply_all_settings()
        else:
            logger.warning("未找到配置文件，使用默认设置: %s", CONFIG_PATH)
            self.startup_checkbox.blockSignals(True)
            self.startup_checkbox.setChecked(is_autostart_enabled())
            self.startup_checkbox.blockSignals(False)
            self.status_bar.showMessage("未找到配置文件，使用默认设置", 5000)

        if not self.hidden:
            self.setGeometry(QDesktopWidget().availableGeometry().center().x() - self.width() // 2,
                           QDesktopWidget().availableGeometry().center().y() - self.height() // 2,
                           self.width(), self.height())
            self.show()
        else:
            self.hide()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(25, 20, 25, 20)

        title_label = QLabel("来酷Pro14 控制中心")
        title_label.setFont(QFont("Microsoft YaHei", 18, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("color: #2c3e50; margin-bottom: 0;")
        main_layout.addWidget(title_label)

        info_group = QGroupBox("系统信息")
        info_layout = QVBoxLayout()
        row_layout = QHBoxLayout()
        row_layout.setSpacing(20)

        self.cpu_temp_value = QLabel("-- °C")
        self.sys_temp_value = QLabel("-- °C")
        self.cpu_fan_value = QLabel("-- RPM")
        self.gpu_fan_value = QLabel("-- RPM")
        for label, text in [("CPU温度:", self.cpu_temp_value),
                            ("系统温度:", self.sys_temp_value),
                            ("CPU风扇:", self.cpu_fan_value),
                            ("GPU风扇:", self.gpu_fan_value)]:
            hbox = QHBoxLayout()
            hbox.addWidget(QLabel(label))
            hbox.addWidget(text)
            row_layout.addLayout(hbox)
        row_layout.addStretch()
        info_layout.addLayout(row_layout)
        info_group.setLayout(info_layout)
        main_layout.addWidget(info_group)

        power_charge_group = QGroupBox("电源设置")
        power_charge_layout = QHBoxLayout()

        power_mode_layout = QHBoxLayout()
        power_mode_layout.addWidget(QLabel("性能模式："))
        self.power_mode = QComboBox()
        self.power_mode.addItems(["静音 (silent)", "均衡 (default)", "高性能 (perf)"])
        self.power_btn = QPushButton("应用")
        self.power_btn.clicked.connect(self.set_power_mode)
        power_mode_layout.addWidget(self.power_mode)
        power_mode_layout.addWidget(self.power_btn)
        power_mode_layout.addStretch()

        charge_mode_layout = QHBoxLayout()
        charge_mode_layout.addWidget(QLabel("充电模式："))
        self.charge_mode = QComboBox()
        self.charge_mode.addItems(["满充 (100%)", "高 (95%)", "平衡 (80%)", "保养 (60%)", "低 (50%)"])
        self.charge_btn = QPushButton("应用")
        self.charge_btn.clicked.connect(self.set_charge)
        charge_mode_layout.addWidget(self.charge_mode)
        charge_mode_layout.addWidget(self.charge_btn)
        charge_mode_layout.addStretch()

        power_charge_layout.addLayout(power_mode_layout)
        power_charge_layout.addLayout(charge_mode_layout)
        power_charge_group.setLayout(power_charge_layout)
        main_layout.addWidget(power_charge_group)

        fan_group = QGroupBox("风扇控制")
        fan_layout = QVBoxLayout()
        fan_layout.setSpacing(12)

        cpu_fan_layout = QHBoxLayout()
        cpu_fan_layout.addWidget(QLabel("CPU 风扇："))
        self.cpu_fan_mode = QComboBox()
        self.cpu_fan_mode.addItems(["自动", "满速", "自定义"])
        self.cpu_fan_mode.currentTextChanged.connect(self.on_fan_mode_change)
        self.cpu_fan_slider = QSlider(Qt.Horizontal)
        self.cpu_fan_slider.setRange(0, 100)
        self.cpu_fan_slider.setValue(60)
        self.cpu_fan_slider.setEnabled(False)
        self.cpu_fan_spin = QSpinBox()
        self.cpu_fan_spin.setRange(0, 100)
        self.cpu_fan_spin.setSuffix(" %")
        self.cpu_fan_spin.setValue(60)
        self.cpu_fan_spin.setEnabled(False)
        self.cpu_fan_slider.valueChanged.connect(self.cpu_fan_spin.setValue)
        self.cpu_fan_spin.valueChanged.connect(self.cpu_fan_slider.setValue)
        self.cpu_fan_btn = QPushButton("应用")
        self.cpu_fan_btn.clicked.connect(lambda: self.set_fan("cpu"))
        cpu_fan_layout.addWidget(self.cpu_fan_mode)
        cpu_fan_layout.addWidget(self.cpu_fan_slider)
        cpu_fan_layout.addWidget(self.cpu_fan_spin)
        cpu_fan_layout.addWidget(self.cpu_fan_btn)

        gpu_fan_layout = QHBoxLayout()
        gpu_fan_layout.addWidget(QLabel("GPU 风扇："))
        self.gpu_fan_mode = QComboBox()
        self.gpu_fan_mode.addItems(["自动", "满速", "自定义"])
        self.gpu_fan_mode.currentTextChanged.connect(self.on_fan_mode_change)
        self.gpu_fan_slider = QSlider(Qt.Horizontal)
        self.gpu_fan_slider.setRange(0, 100)
        self.gpu_fan_slider.setValue(60)
        self.gpu_fan_slider.setEnabled(False)
        self.gpu_fan_spin = QSpinBox()
        self.gpu_fan_spin.setRange(0, 100)
        self.gpu_fan_spin.setSuffix(" %")
        self.gpu_fan_spin.setValue(60)
        self.gpu_fan_spin.setEnabled(False)
        self.gpu_fan_slider.valueChanged.connect(self.gpu_fan_spin.setValue)
        self.gpu_fan_spin.valueChanged.connect(self.gpu_fan_slider.setValue)
        self.gpu_fan_btn = QPushButton("应用")
        self.gpu_fan_btn.clicked.connect(lambda: self.set_fan("gpu"))
        gpu_fan_layout.addWidget(self.gpu_fan_mode)
        gpu_fan_layout.addWidget(self.gpu_fan_slider)
        gpu_fan_layout.addWidget(self.gpu_fan_spin)
        gpu_fan_layout.addWidget(self.gpu_fan_btn)

        fan_layout.addLayout(cpu_fan_layout)
        fan_layout.addLayout(gpu_fan_layout)
        fan_group.setLayout(fan_layout)
        main_layout.addWidget(fan_group)

        curve_group = QGroupBox("风扇曲线")
        curve_layout = QVBoxLayout()
        self.curve_enabled = QCheckBox("启用自定义曲线控制")
        self.curve_open_btn = QPushButton("打开风扇曲线设置")
        self.curve_open_btn.clicked.connect(self.open_fan_curve_dialog)
        curve_layout.addWidget(self.curve_enabled)
        curve_layout.addWidget(self.curve_open_btn)
        curve_group.setLayout(curve_layout)
        main_layout.addWidget(curve_group)

        led_group = QGroupBox("LED设置")
        led_layout = QVBoxLayout()

        kbd_layout = QHBoxLayout()
        kbd_layout.addWidget(QLabel("键盘背光："))
        self.kbd_level = QComboBox()
        self.kbd_level.addItems(["0 (关)", "1 (低)", "2 (中)", "3 (高)"])
        self.kbd_btn = QPushButton("应用")
        self.kbd_btn.clicked.connect(self.set_kbd_backlight)
        kbd_layout.addWidget(self.kbd_level)
        kbd_layout.addWidget(self.kbd_btn)
        kbd_layout.addStretch()
        led_layout.addLayout(kbd_layout)

        led_controls = QHBoxLayout()
        led_controls.addWidget(QLabel("电源灯亮度："))
        self.led_slider = QSlider(Qt.Horizontal)
        self.led_slider.setRange(0, 255)
        self.led_slider.setValue(127)
        self.led_spin = QSpinBox()
        self.led_spin.setRange(0, 255)
        self.led_spin.setValue(127)
        self.led_btn = QPushButton("应用")
        self.led_btn.clicked.connect(self.set_led_brightness)

        self.led_slider.valueChanged.connect(self.led_spin.setValue)
        self.led_spin.valueChanged.connect(self.led_slider.setValue)

        led_controls.addWidget(self.led_slider)
        led_controls.addWidget(self.led_spin)
        led_controls.addWidget(self.led_btn)
        led_controls.addStretch()
        led_layout.addLayout(led_controls)

        led_group.setLayout(led_layout)
        main_layout.addWidget(led_group)

        startup_group = QGroupBox("开机自启")
        startup_layout = QHBoxLayout()
        self.startup_checkbox = QCheckBox("开机自动启动")
        self.startup_checkbox.stateChanged.connect(self.toggle_autostart)
        startup_layout.addWidget(self.startup_checkbox)
        startup_layout.addStretch()
        startup_group.setLayout(startup_layout)
        main_layout.addWidget(startup_group)

    def on_fan_mode_change(self):
        self.cpu_fan_slider.setEnabled(self.cpu_fan_mode.currentText() == "自定义")
        self.cpu_fan_spin.setEnabled(self.cpu_fan_mode.currentText() == "自定义")
        self.gpu_fan_slider.setEnabled(self.gpu_fan_mode.currentText() == "自定义")
        self.gpu_fan_spin.setEnabled(self.gpu_fan_mode.currentText() == "自定义")

    def open_fan_curve_dialog(self):
        if not hasattr(self, 'fan_curve_dialog') or self.fan_curve_dialog is None:
            self.fan_curve_dialog = FanCurveDialog(self)
            self.fan_curve_dialog.set_curve_enabled(self.curve_enabled.isChecked())
            self.fan_curve_dialog.curve_enabled_changed.connect(self.curve_enabled.setChecked)
            self.curve_enabled.stateChanged.connect(self.fan_curve_dialog.set_curve_enabled)
            pts = getattr(self, '_saved_curve_points', DEFAULT_CURVE_POINTS)
            self.fan_curve_dialog.set_points(pts)
            self.fan_curve_dialog.set_response_delays(
                getattr(self, '_saved_curve_rise_delay', DEFAULT_CURVE_RISE_DELAY_SEC),
                getattr(self, '_saved_curve_fall_delay', DEFAULT_CURVE_FALL_DELAY_SEC)
            )
        self.fan_curve_dialog.show()
        self.fan_curve_dialog.raise_()
        self.fan_curve_dialog.activateWindow()

    def sync_curve_data(self):
        if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog is not None:
            pts = self.fan_curve_dialog.get_points()
            self.fan_curve_dialog.curve_plot.set_points(pts)
            self._saved_curve_points = pts

    def reset_curve_pending(self):
        self._curve_pending_direction = None
        self._curve_pending_since = 0.0
        self._curve_pending_pwm = -1
        self._curve_pending_percent = -1
        self._curve_pending_temp = -1

    def get_curve_response_delays(self):
        if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog is not None:
            rise_delay, fall_delay = self.fan_curve_dialog.get_response_delays()
            return int(rise_delay), int(fall_delay)
        return int(getattr(self, '_saved_curve_rise_delay', DEFAULT_CURVE_RISE_DELAY_SEC)), int(getattr(self, '_saved_curve_fall_delay', DEFAULT_CURVE_FALL_DELAY_SEC))

    def _normalize_curve_delay(self, value, default_value, maximum_value):
        try:
            return max(0, min(maximum_value, int(value)))
        except (TypeError, ValueError):
            return default_value

    def init_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        # 尝试多个可能的logo路径
        logo_paths = [
            app_resource_path("logo.png")
        ]
        
        icon_set = False
        for logo_path in logo_paths:
            if os.path.exists(logo_path):
                self.tray_icon.setIcon(QIcon(logo_path))
                icon_set = True
                break
        
        if not icon_set:
            # 使用默认图标
            self.tray_icon.setIcon(QIcon())

        tray_menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show_window)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def init_service_watchdog(self):
        self.service_watchdog_timer = QTimer(self)
        self.service_watchdog_timer.setInterval(SERVICE_WATCHDOG_INTERVAL_MS)
        self.service_watchdog_timer.timeout.connect(self.check_daemon_service)
        self.service_watchdog_timer.start()

    def check_daemon_service(self):
        if self._service_watchdog_inflight:
            return
        self._service_watchdog_inflight = True
        thread = ServiceWatchdogThread()

        def handle_result(state, restarted):
            self._service_watchdog_inflight = False
            self._daemon_ready = state == "RUNNING"
            if restarted and state == "RUNNING":
                logger.info("服务监控已恢复后台服务: service=%s", SERVICE_NAME)
                self.status_bar.showMessage("后台服务已自动重启", 3000)
            elif state == "STOPPED":
                logger.warning("服务监控未能启动后台服务: service=%s", SERVICE_NAME)
                self.status_bar.showMessage("后台服务已停止，请检查服务状态", 5000)

        thread.result_signal.connect(handle_result)
        self.add_thread(thread)

    def ensure_service_repair_on_user_launch(self):
        if self._service_repair_attempted or self.hidden:
            return
        self._service_repair_attempted = True

        state = query_daemon_service_state()
        recovery_ok = is_service_recovery_configured()
        if state == "RUNNING" and recovery_ok:
            self.status_bar.showMessage("后台服务状态正常", 2000)
            return

        if state != "RUNNING":
            logger.warning("用户启动时发现后台服务未运行，准备运行安装脚本: state=%s", state)
            if run_installer_with_recovery_repair():
                self._daemon_ready = True
                self.status_bar.showMessage("后台服务已修复并启动", 4000)
                return

        if not recovery_ok and ensure_service_recovery_configured():
            self.status_bar.showMessage("已修复后台服务自动恢复策略", 4000)
        else:
            self.status_bar.showMessage("后台服务修复未能启动，请查看日志", 5000)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_window()

    def show_window(self):
        self.hidden = False
        self.ensure_service_repair_on_user_launch()
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        logger.info("准备退出程序")
        self.tray_icon.hide()
        if hasattr(self, "sysinfo_thread"):
            self.sysinfo_thread.stop()
        for thread in list(self.active_threads):
            thread.wait(300)
            if thread.isRunning():
                thread.terminate()
                thread.wait(300)
        QApplication.quit()

    def closeEvent(self, event):
        logger.info("收到窗口关闭事件")
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("关闭确认")
        msg_box.setText("请选择关闭时的操作：")
        minimize_btn = msg_box.addButton("最小化到托盘", QMessageBox.AcceptRole)
        quit_btn = msg_box.addButton("退出程序", QMessageBox.DestructiveRole)
        msg_box.setDefaultButton(minimize_btn)
        msg_box.exec_()

        if msg_box.clickedButton() == minimize_btn:
            event.ignore()
            self.hide()
        else:
            event.accept()
            self.quit_app()

    def toggle_autostart(self, state):
        cmd = build_autostart_command()
        if state == Qt.Checked:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY_PATH, 0, winreg.KEY_WRITE) as key:
                    winreg.SetValueEx(key, AUTOSTART_APP_NAME, 0, winreg.REG_SZ, cmd)
                self.status_bar.showMessage("已添加开机自启", 3000)
                logger.info("已设置开机自启: %s", cmd)
            except Exception as e:
                logger.exception("添加开机自启失败")
                QMessageBox.warning(self, "错误", f"添加开机自启失败：{e}")
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY_PATH, 0, winreg.KEY_WRITE) as key:
                    winreg.DeleteValue(key, AUTOSTART_APP_NAME)
                self.status_bar.showMessage("已取消开机自启", 3000)
                logger.info("已取消开机自启")
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.exception("取消开机自启失败")
                QMessageBox.warning(self, "错误", f"取消开机自启失败：{e}")

    def _run_ipc_command(self, action, params=None, success_message="", failure_message="", show_success=True, show_failure=True, on_result=None):
        thread = IpcCommandThread(action, params or {}, success_message, failure_message)

        def handle_result(success, message):
            if on_result:
                try:
                    on_result(success, message)
                except Exception:
                    logger.exception("IPC 结果回调异常: action=%s", action)
            if message and ((success and show_success) or (not success and show_failure)):
                self.status_bar.showMessage(message, 3000 if success else 5000)

        thread.result_signal.connect(handle_result)
        self.add_thread(thread)
        return thread

    def _set_led_brightness_silent(self, brightness):
        self._run_ipc_command(
            "set_led",
            params={"mode": PowerLedMode.Custom, "brightness": brightness},
            failure_message="设置电源灯失败",
            show_success=False,
            show_failure=False
        )

    def _exec_cmd_silent(self, action, **params):
        if action == "set_fan":
            self._run_ipc_command(
                "set_fan",
                params=params,
                failure_message="设置风扇模式失败",
                show_success=False,
                show_failure=False
            )
        elif action == "set_fans":
            self._run_ipc_command(
                "set_fans",
                params=params,
                failure_message="设置风扇模式失败",
                show_success=False,
                show_failure=False
            )

    def set_led_brightness(self):
        brightness = self.led_slider.value()
        self._set_led_brightness_silent(brightness)
        if not self._auto_applying:
            self.save_config()

    def add_thread(self, thread):
        self.active_threads.append(thread)
        thread.finished.connect(lambda: self.remove_thread(thread))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def remove_thread(self, thread):
        if thread in self.active_threads:
            self.active_threads.remove(thread)

    def update_temps(self, cpu_temp_str, sys_temp_str):
        self.cpu_temp_value.setText(f"{cpu_temp_str} °C" if cpu_temp_str not in ("获取失败", "--") else cpu_temp_str)
        self.sys_temp_value.setText(f"{sys_temp_str} °C" if sys_temp_str not in ("获取失败", "--") else sys_temp_str)

        if not self.curve_enabled.isChecked() or not cpu_temp_str.isdigit():
            self.reset_curve_pending()
            return

        temp = int(cpu_temp_str)
        pts = self.fan_curve_dialog.get_points() if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog else getattr(self, '_saved_curve_points', DEFAULT_CURVE_POINTS)
        target_percent = calculate_curve_percent(temp, pts)
        target_pwm = int(target_percent * 1.5)

        temp_delta = abs(temp - self.last_curve_temp)
        pwm_delta = abs(target_pwm - self.last_curve_pwm)
        should_apply_initial = self.last_curve_pwm < 0
        should_consider_change = (
            (temp_delta >= CURVE_TEMP_HYSTERESIS or pwm_delta >= CURVE_PWM_HYSTERESIS)
            and self.last_curve_pwm != target_pwm
        )

        if not should_apply_initial and not should_consider_change:
            self.reset_curve_pending()
            return

        rise_delay, fall_delay = self.get_curve_response_delays()
        if should_apply_initial or target_pwm > self.last_curve_pwm:
            direction = "rise"
            delay_seconds = 0 if should_apply_initial else rise_delay
        else:
            direction = "fall"
            delay_seconds = fall_delay

        now = time.monotonic()
        if self._curve_pending_direction != direction:
            self._curve_pending_direction = direction
            self._curve_pending_since = now

        self._curve_pending_pwm = target_pwm
        self._curve_pending_percent = target_percent
        self._curve_pending_temp = temp

        if now - self._curve_pending_since < delay_seconds or self._curve_apply_inflight:
            return

        self._curve_apply_inflight = True
        self._curve_apply_request_id += 1
        request_id = self._curve_apply_request_id

        def on_curve_result(success, message, scheduled_temp=temp, scheduled_percent=target_percent, scheduled_pwm=target_pwm, scheduled_request_id=request_id):
            self._curve_apply_inflight = False
            if scheduled_request_id != self._curve_apply_request_id or not self.curve_enabled.isChecked():
                return
            if success:
                self.last_curve_pwm = scheduled_pwm
                self.last_curve_percent = scheduled_percent
                self.last_curve_temp = scheduled_temp
                self.reset_curve_pending()
                cpu_target_rpm = estimate_fan_rpm(scheduled_percent, "cpu")
                gpu_target_rpm = estimate_fan_rpm(scheduled_percent, "gpu")
                logger.info(
                    "自动调速目标下发: basis_temp=%s target_percent=%s%% ec_pwm=%s cpu_target_rpm=%s gpu_target_rpm=%s",
                    scheduled_temp,
                    scheduled_percent,
                    scheduled_pwm,
                    cpu_target_rpm,
                    gpu_target_rpm
                )
                self.status_bar.showMessage(
                    f"自动调速目标已下发：约 CPU {cpu_target_rpm} RPM / GPU {gpu_target_rpm} RPM（曲线 {scheduled_percent}%，按 {scheduled_temp}°C 计算）",
                    3000
                )
            elif message:
                self._curve_pending_since = time.monotonic()
                logger.warning("自动调速失败: basis_temp=%s target_percent=%s%% ec_pwm=%s message=%s", scheduled_temp, scheduled_percent, scheduled_pwm, message)
                self.status_bar.showMessage(message, 5000)

        self._run_ipc_command(
            "set_fans",
            params={
                "requests": [
                    (FanIndex.CPU, FanMode.Custom, target_pwm),
                    (FanIndex.GPU, FanMode.Custom, target_pwm)
                ]
            },
            failure_message="自动调速失败",
            show_success=False,
            show_failure=False,
            on_result=on_curve_result
        )

    def update_fans(self, cpu_fan_str, gpu_fan_str):
        self.cpu_fan_value.setText(f"{cpu_fan_str} RPM" if cpu_fan_str not in ("获取失败", "--") else cpu_fan_str)
        self.gpu_fan_value.setText(f"{gpu_fan_str} RPM" if gpu_fan_str not in ("获取失败", "--") else gpu_fan_str)

    def save_config(self):
        curve_points = self.fan_curve_dialog.get_points() if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog else getattr(self, '_saved_curve_points', DEFAULT_CURVE_POINTS)
        rise_delay, fall_delay = self.get_curve_response_delays()
        config = {
            "power_mode": self.power_mode.currentText(),
            "cpu_fan_mode": self.cpu_fan_mode.currentText(),
            "cpu_fan_pwm": self.cpu_fan_slider.value(),
            "gpu_fan_mode": self.gpu_fan_mode.currentText(),
            "gpu_fan_pwm": self.gpu_fan_slider.value(),
            "charge_mode": self.charge_mode.currentText(),
            "kbd_level": self.kbd_level.currentText(),
            "led_brightness": self.led_slider.value(),
            "curve_enabled": self.curve_enabled.isChecked(),
            "curve_points": curve_points,
            "curve_rise_delay": rise_delay,
            "curve_fall_delay": fall_delay,
            "startup_enabled": self.startup_checkbox.isChecked()
        }
        try:
            ensure_app_data_dir()
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.exception("保存配置失败: %s", CONFIG_PATH)

    def load_config(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            logger.exception("加载配置失败: %s", CONFIG_PATH)
            return

        if "power_mode" in config:
            idx = self.power_mode.findText(config["power_mode"])
            if idx >= 0: self.power_mode.setCurrentIndex(idx)
        if "cpu_fan_mode" in config:
            idx = self.cpu_fan_mode.findText(config["cpu_fan_mode"])
            if idx >= 0: self.cpu_fan_mode.setCurrentIndex(idx)
        if "cpu_fan_pwm" in config:
            val = config["cpu_fan_pwm"]
            self.cpu_fan_slider.setValue(val)
            self.cpu_fan_spin.setValue(val)
        if "gpu_fan_mode" in config:
            idx = self.gpu_fan_mode.findText(config["gpu_fan_mode"])
            if idx >= 0: self.gpu_fan_mode.setCurrentIndex(idx)
        if "gpu_fan_pwm" in config:
            val = config["gpu_fan_pwm"]
            self.gpu_fan_slider.setValue(val)
            self.gpu_fan_spin.setValue(val)
        if "charge_mode" in config:
            idx = self.charge_mode.findText(config["charge_mode"])
            if idx >= 0: self.charge_mode.setCurrentIndex(idx)
        if "kbd_level" in config:
            idx = self.kbd_level.findText(config["kbd_level"])
            if idx >= 0: self.kbd_level.setCurrentIndex(idx)
        if "led_brightness" in config:
            val = config["led_brightness"]
            if val <= 100:
                val = int(val * 2.55)
            self.led_slider.setValue(val)
            self.led_spin.setValue(val)
        if "curve_enabled" in config:
            self.curve_enabled.setChecked(config["curve_enabled"])
        if "curve_points" in config:
            pts = config["curve_points"]
            self._saved_curve_points = pts
            if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog:
                self.fan_curve_dialog.set_points(pts)
        if "curve_rise_delay" in config:
            self._saved_curve_rise_delay = self._normalize_curve_delay(
                config["curve_rise_delay"],
                DEFAULT_CURVE_RISE_DELAY_SEC,
                120
            )
            if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog:
                self.fan_curve_dialog.rise_delay_spin.setValue(self._saved_curve_rise_delay)
        if "curve_fall_delay" in config:
            self._saved_curve_fall_delay = self._normalize_curve_delay(
                config["curve_fall_delay"],
                DEFAULT_CURVE_FALL_DELAY_SEC,
                300
            )
            if hasattr(self, 'fan_curve_dialog') and self.fan_curve_dialog:
                self.fan_curve_dialog.fall_delay_spin.setValue(self._saved_curve_fall_delay)
        if "startup_enabled" in config:
            self.startup_checkbox.setChecked(config["startup_enabled"])

        self.on_fan_mode_change()

    def apply_all_settings(self):
        self._auto_applying = True
        self._auto_pending_count = 0

        power_map = {"静音 (silent)": PowerProfile.Silent, "均衡 (default)": PowerProfile.Default, "高性能 (perf)": PowerProfile.Performance}
        fan_mode_map = {"自动": FanMode.Auto, "满速": FanMode.Full, "自定义": FanMode.Custom}
        charge_map = {
            "满充 (100%)": ChargeLimit.Full, "高 (95%)": ChargeLimit.High, "平衡 (80%)": ChargeLimit.Balanced,
            "保养 (60%)": ChargeLimit.Lifespan, "低 (50%)": ChargeLimit.Desk
        }
        kbd_map = {"0 (关)": KeyboardBacklightLevel.Off, "1 (低)": KeyboardBacklightLevel.Low, "2 (中)": KeyboardBacklightLevel.Medium, "3 (高)": KeyboardBacklightLevel.High}

        cpu_mode_text = self.cpu_fan_mode.currentText()
        gpu_mode_text = self.gpu_fan_mode.currentText()
        cpu_mode = fan_mode_map.get(cpu_mode_text, FanMode.Auto)
        gpu_mode = fan_mode_map.get(gpu_mode_text, FanMode.Auto)

        commands = [
            ("set_power_profile", {"profile": power_map.get(self.power_mode.currentText(), PowerProfile.Default)}),
            ("set_fan", {
                "fan": FanIndex.CPU,
                "mode": cpu_mode,
                "custom_value": int(self.cpu_fan_slider.value() * 1.5) if cpu_mode_text == "自定义" else 0
            }),
            ("set_fan", {
                "fan": FanIndex.GPU,
                "mode": gpu_mode,
                "custom_value": int(self.gpu_fan_slider.value() * 1.5) if gpu_mode_text == "自定义" else 0
            }),
            ("set_charge_limit", {"limit": charge_map.get(self.charge_mode.currentText(), ChargeLimit.Full)}),
            ("set_keyboard_backlight", {"level": kbd_map.get(self.kbd_level.currentText(), KeyboardBacklightLevel.Off)}),
            ("set_led", {"mode": PowerLedMode.Custom, "brightness": self.led_slider.value()})
        ]

        self._run_ipc_command(
            "apply_all",
            params={"commands": commands},
            failure_message="应用配置失败",
            show_success=False
        )
        self._auto_applying = False

    def set_power_mode(self):
        mode_map = {"静音 (silent)": PowerProfile.Silent, "均衡 (default)": PowerProfile.Default, "高性能 (perf)": PowerProfile.Performance}
        profile = mode_map.get(self.power_mode.currentText(), PowerProfile.Default)
        if not self._auto_applying:
            self.status_bar.showMessage("正在设置电源模式...", 2000)
        self._run_ipc_command(
            "set_power_profile",
            params={"profile": profile},
            success_message=f"电源模式已设置为 {self.power_mode.currentText()}",
            failure_message="设置电源模式失败",
            show_success=not self._auto_applying
        )
        if not self._auto_applying:
            self.save_config()

    def set_fan(self, fan_type):
        if fan_type == "cpu":
            mode_text = self.cpu_fan_mode.currentText()
            pwm = self.cpu_fan_slider.value()
            fan = FanIndex.CPU
        else:
            mode_text = self.gpu_fan_mode.currentText()
            pwm = self.gpu_fan_slider.value()
            fan = FanIndex.GPU
        
        mode_map = {"自动": FanMode.Auto, "满速": FanMode.Full, "自定义": FanMode.Custom}
        mode = mode_map.get(mode_text, FanMode.Auto)
        custom_value = int(pwm * 1.5) if mode_text == "自定义" else 0

        if not self._auto_applying:
            self.status_bar.showMessage(f"正在设置{fan_type.upper()}风扇模式...", 2000)
        self._run_ipc_command(
            "set_fan",
            params={"fan": fan, "mode": mode, "custom_value": custom_value},
            success_message=f"{fan_type.upper()}风扇模式已设置",
            failure_message=f"设置{fan_type.upper()}风扇模式失败",
            show_success=not self._auto_applying
        )
        if not self._auto_applying:
            self.save_config()

    def set_charge(self):
        charge_map = {
            "满充 (100%)": ChargeLimit.Full, "高 (95%)": ChargeLimit.High, "平衡 (80%)": ChargeLimit.Balanced,
            "保养 (60%)": ChargeLimit.Lifespan, "低 (50%)": ChargeLimit.Desk
        }
        limit = charge_map.get(self.charge_mode.currentText(), ChargeLimit.Full)
        if not self._auto_applying:
            self.status_bar.showMessage("正在设置充电模式...", 2000)
        self._run_ipc_command(
            "set_charge_limit",
            params={"limit": limit},
            success_message=f"充电模式已设置为 {self.charge_mode.currentText()}",
            failure_message="设置充电模式失败",
            show_success=not self._auto_applying
        )
        if not self._auto_applying:
            self.save_config()

    def set_kbd_backlight(self):
        level_map = {"0 (关)": KeyboardBacklightLevel.Off, "1 (低)": KeyboardBacklightLevel.Low, "2 (中)": KeyboardBacklightLevel.Medium, "3 (高)": KeyboardBacklightLevel.High}
        level = level_map.get(self.kbd_level.currentText(), KeyboardBacklightLevel.Off)
        if not self._auto_applying:
            self.status_bar.showMessage("正在设置键盘背光...", 2000)
        self._run_ipc_command(
            "set_keyboard_backlight",
            params={"level": level},
            success_message=f"键盘背光已设置为 {self.kbd_level.currentText()}",
            failure_message="设置键盘背光失败",
            show_success=not self._auto_applying
        )
        if not self._auto_applying:
            self.save_config()

    def setup_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f5f7fa; }
            QGroupBox {
                font-weight: bold; border: 1px solid #cbd5e0;
                border-radius: 8px; margin-top: 10px;
                padding-top: 10px; background-color: white;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left: 10px;
                padding: 0 5px 0 5px; color: #2c3e50;
            }
            QPushButton {
                background-color: #3498db; color: white;
                border: none; border-radius: 4px;
                padding: 6px 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #2980b9; }
            QPushButton:pressed { background-color: #1f618d; }
            QComboBox, QSpinBox {
                border: 1px solid #cbd5e0; border-radius: 4px;
                padding: 4px 8px; background-color: white;
            }
            QComboBox:hover, QSpinBox:hover { border-color: #3498db; }
            QSlider::groove:horizontal {
                height: 6px; background: #e2e8f0; border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #3498db; width: 14px; height: 14px;
                margin: -4px 0; border-radius: 7px;
            }
            QSlider::handle:horizontal:hover { background: #2980b9; }
            QLabel { color: #2c3e50; }
            QLabel.info-value { font-weight: bold; color: #2980b9; font-family: monospace; }
            QStatusBar { background-color: #ecf0f1; color: #2c3e50; font-size: 12px; }
        """)

    def init_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 - 请确保程序有足够权限", 3000)

if __name__ == "__main__":
    apply_startup_delay()

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 9))

    server_name = "LecooControl_Server"

    # 1. 优先尝试作为客户端连接
    socket = QLocalSocket()
    socket.connectToServer(server_name)

    if socket.waitForConnected(500):
        # 连接成功，说明已经有实例在运行
        socket.write(b"show")
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        sys.exit(0)  # 第二个实例发送完指令后正常退出

    # 2. 如果连接失败，说明当前没有运行的实例，安全启动 Server
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    
    if server.listen(server_name):
        hidden = "--hidden" in sys.argv
        window = LecooControlGUI(hidden=hidden)

        def handle_connection():
            conn_socket = server.nextPendingConnection()
            if conn_socket:
                # 尝试读取一下客户端发来的数据（比如 b"show"），清理缓冲区
                if conn_socket.waitForReadyRead(200):
                    _ = conn_socket.readAll()
                
                # 直接调用即可，因为 handle_connection 已经在主 GUI 线程中运行
                window.show_window()
                
                # 安全断开并清理 socket
                conn_socket.disconnectFromServer()
                conn_socket.deleteLater()

        server.newConnection.connect(handle_connection)

        sys.exit(app.exec_())
    else:
        QMessageBox.critical(None, "启动失败", f"无法启动应用程序服务：\n{server.errorString()}")
        sys.exit(1)
