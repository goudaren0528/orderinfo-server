import subprocess
import requests
import json
import os
import sys
import platform
import time
import uuid
import base64
import ctypes
import hashlib
import logging
from ctypes import wintypes
from urllib.parse import urlparse
from datetime import datetime
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv


# 配置日志
def setup_client_logging():
    log_filename = "client_debug.log"
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    log_path = os.path.join(base_dir, log_filename)

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8', mode='a'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger("AuthManager")


logger = setup_client_logging()

# 加载环境变量
# 1. 尝试从 PyInstaller 临时目录加载 (打包进 EXE 的 .env)
if getattr(sys, 'frozen', False):
    # 优先加载 EXE 同级目录下的 .env (最高优先级)
    exe_dir = os.path.dirname(sys.executable)
    exe_env_path = os.path.join(exe_dir, '.env')
    if os.path.exists(exe_env_path):
        load_dotenv(exe_env_path, override=True)
        logger.info(f"Loaded .env from EXE directory: {exe_env_path}")

    # 其次尝试 _internal 目录 (PyInstaller onedir 默认资源目录)
    internal_env_path = os.path.join(exe_dir, '_internal', '.env')
    if os.path.exists(internal_env_path):
        load_dotenv(internal_env_path, override=True)
        logger.info(f"Loaded .env from _internal directory: {internal_env_path}")

    # 最后尝试 sys._MEIPASS (onefile 模式)
    bundle_dir = getattr(sys, '_MEIPASS', '')
    if bundle_dir:
        meipass_env_path = os.path.join(bundle_dir, '.env')
        if os.path.exists(meipass_env_path):
            load_dotenv(meipass_env_path)
            logger.info(f"Loaded .env from MEIPASS: {meipass_env_path}")

# 2. 尝试从当前工作目录加载 (开发环境 或 用户放置在 EXE 旁的 .env)
# 注意：这会覆盖打包在 EXE 内部的 .env 配置 (如果上面没有 override=True 的话，但上面用了 override=True，这里主要用于开发环境)
load_dotenv(override=True)

# 默认服务器地址
DEFAULT_SERVER_URL = os.environ.get("LICENSE_SERVER_URL", "http://localhost:5005")
logger.info(f"Loaded Configuration: SERVER_URL={DEFAULT_SERVER_URL}")
if getattr(sys, 'frozen', False):
    logger.info(f"Running in Frozen mode. Executable: {sys.executable}")
    if 'localhost' in DEFAULT_SERVER_URL and 'speedstarsunblocked' not in DEFAULT_SERVER_URL:
        logger.warning("WARNING: Using localhost in potentially Production environment?")
DPAPI_PURPOSE = b"zubaobao-license"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte))
    ]


def _dpapi_available():
    return platform.system() == "Windows"


def _bytes_to_blob(raw):
    if not raw:
        return _DataBlob()
    buf = ctypes.create_string_buffer(raw)
    return _DataBlob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _blob_to_bytes(blob):
    if not blob.pbData or not blob.cbData:
        return b""
    data = ctypes.string_at(blob.pbData, blob.cbData)
    return data


def _crypt_protect(data):
    if not _dpapi_available():
        return data
    in_blob = _bytes_to_blob(data)
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(_bytes_to_blob(DPAPI_PURPOSE)),
        None,
        None,
        0,
        ctypes.byref(out_blob)
    ):
        return data
    try:
        return _blob_to_bytes(out_blob)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _crypt_unprotect(data):
    if not _dpapi_available():
        return data
    in_blob = _bytes_to_blob(data)
    out_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(_bytes_to_blob(DPAPI_PURPOSE)),
        None,
        None,
        0,
        ctypes.byref(out_blob)
    ):
        return b""
    try:
        return _blob_to_bytes(out_blob)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


class AuthManager:
    def __init__(self, server_url=None, license_file=None):
        self.server_url = (server_url or DEFAULT_SERVER_URL).strip().rstrip('/')
        self.license_file = license_file or self._get_license_file_path()
        self.machine_id = self._get_machine_id()
        self.current_code = None
        self.state = {}
        self.device_private_key = None
        self.device_public_key_pem = None
        self.server_public_key_pem = None
        self.grace_seconds = 86400

    def _get_license_file_path(self):
        if getattr(sys, 'frozen', False):
            # exe 同级目录
            base_dir = os.path.dirname(sys.executable)
            # 兼容后端 onedir 模式：如果在 backend 子目录下，优先读取上级目录的 license.json
            if os.path.basename(base_dir).lower() == 'backend':
                return os.path.join(os.path.dirname(base_dir), 'license.json')
        else:
            # 脚本同级目录
            base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, 'license.json')

    def _wmic_values(self, cmd):
        output = subprocess.check_output(cmd, shell=True).decode(errors='ignore').splitlines()
        return [v.strip() for v in output[1:] if v.strip()]

    def _filter_wmic_values(self, values):
        invalids = {"to be filled by o.e.m.", "default string", "none", "unknown", "na", "n/a"}
        cleaned = []
        for v in values:
            lv = v.lower()
            if lv in invalids:
                continue
            cleaned.append(v)
        return cleaned

    def _get_machine_parts(self):
        parts = []
        cmds = (
            "wmic csproduct get uuid",
            "wmic baseboard get serialnumber",
            "wmic bios get serialnumber",
            "wmic diskdrive get serialnumber",
        )
        for cmd in cmds:
            parts.extend(self._filter_wmic_values(self._wmic_values(cmd)))
        return parts

    def _get_machine_id(self):
        try:
            state = self._load_state()
            cached = state.get('machine_id')
            if cached:
                return cached
            if platform.system() == "Windows":
                parts = self._get_machine_parts()
                if parts:
                    raw = "|".join(parts).encode('utf-8')
                    machine_id = hashlib.sha256(raw).hexdigest()
                    state['machine_id'] = machine_id
                    self._save_state()
                    return machine_id
                values = self._wmic_values("wmic csproduct get uuid")
                state['machine_id'] = values[0] if values else "unknown-machine-id"
                self._save_state()
                return state['machine_id']
            state['machine_id'] = "dev-machine-id-non-windows"
            self._save_state()
            return state['machine_id']
        except Exception as e:
            print(f"获取机器码失败: {e}")
            return "unknown-machine-id"

    def _load_state(self):
        if os.path.exists(self.license_file):
            try:
                with open(self.license_file, 'rb') as f:
                    raw = f.read()
                if raw.startswith(b"ENC1:"):
                    payload = raw[5:]
                    data_raw = _crypt_unprotect(base64.b64decode(payload))
                else:
                    data_raw = raw
                if data_raw:
                    data = json.loads(data_raw.decode('utf-8'))
                    if isinstance(data, dict):
                        self.state = data
                        return self.state
            except Exception:
                pass
        self.state = {}
        return self.state

    def _save_state(self):
        try:
            raw = json.dumps(self.state, ensure_ascii=False).encode('utf-8')
            protected = _crypt_protect(raw)
            if protected != raw:
                payload = b"ENC1:" + base64.b64encode(protected)
                with open(self.license_file, 'wb') as f:
                    f.write(payload)
            else:
                with open(self.license_file, 'w', encoding='utf-8') as f:
                    json.dump(self.state, f)
        except Exception as e:
            logger.error(f"保存授权信息失败: {e}")

    def _canonical_json(self, data):
        return json.dumps(data, separators=(',', ':'), sort_keys=True)

    def _is_secure_server_url(self):
        try:
            parsed = urlparse(self.server_url)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        return parsed.scheme == "https"

    def _get_or_create_device_keypair(self):
        state = self._load_state()
        priv_b64 = state.get('device_private_key')
        pub_pem = state.get('device_public_key')
        if priv_b64 and pub_pem:
            try:
                private_bytes = base64.b64decode(priv_b64)
                private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
                self.device_private_key = private_key
                self.device_public_key_pem = pub_pem
                return
            except Exception:
                pass
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        self.device_private_key = private_key
        self.device_public_key_pem = public_pem
        state['device_private_key'] = base64.b64encode(private_bytes).decode()
        state['device_public_key'] = public_pem
        self._save_state()

    def _sign_body(self, body_bytes):
        if not self.device_private_key:
            self._get_or_create_device_keypair()
        signature = self.device_private_key.sign(body_bytes)
        return base64.b64encode(signature).decode()

    def _post_signed(self, path, payload):
        body = self._canonical_json(payload)
        headers = {
            "Content-Type": "application/json",
            "X-Device-Signature": self._sign_body(body.encode())
        }
        url = f"{self.server_url}{path}"
        return requests.post(url, data=body, headers=headers, timeout=10)

    def _load_server_public_key(self):
        if self.server_public_key_pem:
            return
        if not self._is_secure_server_url():
            return
        state = self._load_state()
        stored_pem = state.get('server_public_key')
        env_pem = os.environ.get('LICENSE_PUBLIC_KEY')

        # 即使有本地存储的公钥，也尝试从服务器更新，因为服务器可能重置了密钥
        # 但我们不会在每次调用时都请求，只在初始化或验证失败时

        # 策略：如果 stored_pem 存在，先用它。但如果后续验证失败，会强制刷新。
        # 这里我们先尝试获取最新的，如果获取失败则回退到 stored_pem

        pem = stored_pem or env_pem

        # 强制尝试从服务器获取最新公钥（如果 URL 可达）
        try:
            url = f"{self.server_url}/api/public-key"
            response = requests.get(url, timeout=5)  # 短超时
            if response.status_code == 200:
                data = response.json()
                server_pem = data.get('public_key')
                if server_pem:
                    pem = server_pem
                    # 更新本地缓存
                    if server_pem != stored_pem:
                        state['server_public_key'] = server_pem
                        self._save_state()
        except Exception:
            pass

        if pem:
            self.server_public_key_pem = pem
            # 确保保存到内存和状态
            if pem != stored_pem:
                state['server_public_key'] = pem
                self._save_state()

    def _verify_license_signature(self, license_payload, signature):
        if not license_payload or not signature:
            return False
        self._load_server_public_key()
        if not self.server_public_key_pem:
            return False
        try:
            public_key = serialization.load_pem_public_key(self.server_public_key_pem.encode())
            body = self._canonical_json(license_payload).encode()
            public_key.verify(base64.b64decode(signature), body)
            return True
        except Exception:
            return False

    def _verify_config_signature(self, config_payload, signature):
        if not config_payload or not signature:
            return False
        self._load_server_public_key()
        if not self.server_public_key_pem:
            return False
        try:
            public_key = serialization.load_pem_public_key(self.server_public_key_pem.encode())
            body = self._canonical_json(config_payload).encode()
            public_key.verify(base64.b64decode(signature), body)
            return True
        except Exception:
            return False

    def load_license(self):
        state = self._load_state()
        license_payload = state.get('license')
        signature = state.get('license_signature')
        if not license_payload or not signature:
            return None
        if not self._verify_license_signature(license_payload, signature):
            return None
        if license_payload.get('machine_id') != self.machine_id:
            return None
        expire_date = license_payload.get('expire_date')
        if expire_date:
            try:
                exp = datetime.strptime(expire_date, '%Y-%m-%d')
                if datetime.now() > exp:
                    # 即使过期，也返回 code，由上层处理过期逻辑
                    self.current_code = license_payload.get('code')
                    return self.current_code
            except Exception:
                return None
        self.current_code = license_payload.get('code')
        return self.current_code

    def is_license_expired(self):
        state = self._load_state()
        license_payload = state.get('license')
        if not license_payload:
            return False
        expire_date = license_payload.get('expire_date')
        if expire_date:
            try:
                exp = datetime.strptime(expire_date, '%Y-%m-%d')
                return datetime.now() > exp
            except Exception:
                return False
        return False

    def _save_license_payload(self, license_payload, license_signature):
        state = self._load_state()
        state['license'] = license_payload
        state['license_signature'] = license_signature
        self.current_code = license_payload.get('code')
        self._save_state()

    def activate(self, code):
        try:
            if not self._is_secure_server_url():
                return False, "授权服务器地址不安全，请使用 https"
            self._get_or_create_device_keypair()
            url = f"{self.server_url}/api/activate"
            payload = {
                "code": code,
                "machine_id": self.machine_id,
                "device_public_key": self.device_public_key_pem,
                "ts": int(time.time()),
                "nonce": uuid.uuid4().hex
            }
            body = self._canonical_json(payload)
            headers = {
                "Content-Type": "application/json",
                "X-Device-Signature": self._sign_body(body.encode())
            }
            response = requests.post(url, data=body, headers=headers, timeout=10)
            try:
                data = response.json()
            except json.decoder.JSONDecodeError:
                return False, f"服务器响应异常 (Status: {response.status_code}): {response.text[:100]}"

            if response.status_code == 200 and data.get("status") == "success":
                license_payload = data.get('license')
                license_signature = data.get('license_signature')
                server_public_key = data.get('public_key')
                if server_public_key:
                    state = self._load_state()
                    stored_pem = state.get('server_public_key')
                    if stored_pem and server_public_key != stored_pem:
                        # 服务器公钥变更（可能是服务器重置），允许更新
                        pass
                    self.server_public_key_pem = server_public_key
                    state['server_public_key'] = server_public_key
                if not self._verify_license_signature(license_payload, license_signature):
                    return False, "授权签名无效（本地验证失败）"
                self._save_license_payload(license_payload, license_signature)
                state = self._load_state()
                state['last_ok_ts'] = int(time.time())
                self._save_state()
                return True, data
            return False, data.get("message", "激活失败")
        except Exception as e:
            return False, f"连接验证服务器失败: {e}"

    def heartbeat(self):
        if not self.load_license():
            return False, "未找到授权码"
        try:
            if not self._is_secure_server_url():
                return False, "授权服务器地址不安全，请使用 https"
            url = f"{self.server_url}/api/heartbeat"
            payload = {
                "code": self.current_code,
                "machine_id": self.machine_id,
                "ts": int(time.time()),
                "nonce": uuid.uuid4().hex
            }
            body = self._canonical_json(payload)
            headers = {
                "Content-Type": "application/json",
                "X-Device-Signature": self._sign_body(body.encode())
            }
            response = requests.post(url, data=body, headers=headers, timeout=10)
            data = response.json()
            if response.status_code == 200 and data.get("status") == "success":
                self.state['last_ok_ts'] = int(time.time())
                self._save_state()
                return True, "在线"
            return False, data.get("message", "心跳失败")
        except Exception as e:
            logger.error(f"Heartbeat exception: {e}")
            state = self._load_state()
            last_ok = state.get('last_ok_ts')
            if last_ok and int(time.time()) - int(last_ok) <= self.grace_seconds:
                return True, f"网络连接异常，已进入宽限期: {e}"
            return False, f"网络连接异常: {e}"

    def fetch_config(self):
        if not self.load_license():
            return False, "未找到授权码"
        try:
            if not self._is_secure_server_url():
                return False, "授权服务器地址不安全，请使用 https"
            payload = {
                "code": self.current_code,
                "machine_id": self.machine_id,
                "ts": int(time.time()),
                "nonce": uuid.uuid4().hex
            }
            response = self._post_signed("/api/config/fetch", payload)
            data = response.json()
            if response.status_code == 200 and data.get("status") == "success":
                config_ts = data.get("config_ts")
                config_signature = data.get("config_signature")
                common_config = data.get("common_config") or {}
                user_config = data.get("user_config") or {}
                config_payload = {
                    "code": self.current_code,
                    "machine_id": self.machine_id,
                    "ts": config_ts,
                    "common_config": common_config,
                    "user_config": user_config
                }
                if not self._verify_config_signature(config_payload, config_signature):
                    return False, "配置签名无效"
                self.state['last_ok_ts'] = int(time.time())
                if data.get("config_token"):
                    self.state['config_token'] = data.get("config_token")
                if data.get("config_token_expire"):
                    self.state['config_token_expire'] = data.get("config_token_expire")
                self._save_state()
                return True, data
            return False, data.get("message", "获取配置失败")
        except Exception as e:
            return False, f"连接验证服务器失败: {e}"

    def _ensure_config_token(self, refresh=False):
        state = self._load_state()
        token = state.get('config_token')
        expire_ts = state.get('config_token_expire')
        if not refresh and token and expire_ts:
            try:
                if int(time.time()) < int(expire_ts):
                    return token
            except Exception:
                pass
        success, data = self.fetch_config()
        if success:
            state = self._load_state()
            return state.get('config_token')
        return None

    def _save_user_config_with_token(self, config, token):
        payload = {
            "code": self.current_code,
            "machine_id": self.machine_id,
            "config": config,
            "config_token": token,
            "ts": int(time.time()),
            "nonce": uuid.uuid4().hex
        }
        response = self._post_signed("/api/config/save", payload)
        data = response.json()
        if response.status_code == 200 and data.get("status") == "success":
            if data.get("config_token"):
                self.state['config_token'] = data.get("config_token")
            if data.get("config_token_expire"):
                self.state['config_token_expire'] = data.get("config_token_expire")
            self.state['last_ok_ts'] = int(time.time())
            self._save_state()
            return True, "保存成功"
        return False, data.get("message", "保存配置失败")

    def _filter_sensitive_data(self, data):
        """递归过滤敏感字段 (如密码)"""
        if isinstance(data, dict):
            new_data = {}
            for k, v in data.items():
                # 过滤常见密码字段
                if k.lower() in ("password", "pwd", "secret", "passwd"):
                    continue
                new_data[k] = self._filter_sensitive_data(v)
            return new_data
        elif isinstance(data, list):
            return [self._filter_sensitive_data(item) for item in data]
        else:
            return data

    def save_user_config(self, config):
        if not self.load_license():
            return False, "未找到授权码"
        try:
            if not self._is_secure_server_url():
                return False, "授权服务器地址不安全，请使用 https"

            # 过滤敏感信息
            safe_config = self._filter_sensitive_data(config)

            token = self._ensure_config_token()
            if not token:
                return False, "配置令牌获取失败"
            success, message = self._save_user_config_with_token(safe_config, token)
            if success:
                return True, message
            if "令牌无效" in message:
                token = self._ensure_config_token(refresh=True)
                if token:
                    return self._save_user_config_with_token(safe_config, token)
            return False, message
        except Exception as e:
            return False, f"连接验证服务器失败: {e}"

    def get_license_info(self):
        state = self._load_state()
        return state.get('license', {})


# 全局单例
auth_manager = AuthManager()
