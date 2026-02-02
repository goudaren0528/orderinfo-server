import subprocess
import requests
import json
import os
import sys
import platform
import time

# 默认服务器地址，生产环境请修改为实际地址
DEFAULT_SERVER_URL = "http://localhost:5005"

class AuthManager:
    def __init__(self, server_url=None):
        self.server_url = server_url or DEFAULT_SERVER_URL
        self.license_file = self._get_license_file_path()
        self.machine_id = self._get_machine_id()
        self.current_code = None

    def _get_license_file_path(self):
        if getattr(sys, 'frozen', False):
            # exe 同级目录
            base_dir = os.path.dirname(sys.executable)
        else:
            # 脚本同级目录
            base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, 'license.json')

    def _get_machine_id(self):
        """获取 Windows 机器唯一码 (UUID)"""
        try:
            if platform.system() == "Windows":
                cmd = "wmic csproduct get uuid"
                output = subprocess.check_output(cmd, shell=True).decode().split('\n')[1].strip()
                return output
            else:
                # 非 Windows 环境 fallback (仅用于开发测试)
                return "dev-machine-id-non-windows"
        except Exception as e:
            print(f"获取机器码失败: {e}")
            return "unknown-machine-id"

    def load_license(self):
        """加载本地授权码"""
        if os.path.exists(self.license_file):
            try:
                with open(self.license_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.current_code = data.get('code')
                    return self.current_code
            except:
                pass
        return None

    def save_license(self, code):
        """保存授权码到本地"""
        try:
            with open(self.license_file, 'w', encoding='utf-8') as f:
                json.dump({"code": code}, f)
            self.current_code = code
        except Exception as e:
            print(f"保存授权码失败: {e}")

    def activate(self, code):
        """激活授权"""
        try:
            url = f"{self.server_url}/api/activate"
            payload = {
                "code": code,
                "machine_id": self.machine_id
            }
            response = requests.post(url, json=payload, timeout=10)
            data = response.json()
            
            if response.status_code == 200 and data.get("status") == "success":
                self.save_license(code)
                return True, data
            else:
                return False, data.get("message", "激活失败")
        except Exception as e:
            return False, f"连接验证服务器失败: {e}"

    def heartbeat(self):
        """发送心跳"""
        if not self.current_code:
            return False, "未找到授权码"
            
        try:
            url = f"{self.server_url}/api/heartbeat"
            payload = {
                "code": self.current_code,
                "machine_id": self.machine_id
            }
            response = requests.post(url, json=payload, timeout=10)
            data = response.json()
            
            if response.status_code == 200 and data.get("status") == "success":
                return True, "在线"
            else:
                return False, data.get("message", "心跳失败")
        except Exception as e:
            # 网络问题通常不视为授权失效，暂时忽略
            return True, f"网络连接异常: {e}"

# 全局单例
auth_manager = AuthManager()
