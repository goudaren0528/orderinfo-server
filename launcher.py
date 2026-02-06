import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog
import json
import subprocess
import threading
import os
import sys
import time
from datetime import datetime
import requests
import importlib
from typing import Any
from PIL import Image, ImageDraw
import webbrowser
import re
from auth import auth_manager

pystray: Any = importlib.import_module("pystray")

CONFIG_FILE = 'config.json'
if getattr(sys, 'frozen', False):
    # 如果是打包后的 exe，配置文件在 exe 同级目录
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), 'config.json')
else:
    # 如果是源码运行，配置文件在脚本同级目录
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

APP_TITLE = "租帮宝 - 多后台订单监控助手"
APP_VERSION = "v1.0.2"


def _normalize_config(data):
    if isinstance(data, list):
        data = {"sites": data}
    if not isinstance(data, dict):
        data = {"sites": []}
    if "sites" not in data or not isinstance(data.get("sites"), list):
        data["sites"] = []
    if "webhook_urls" not in data:
        data["webhook_urls"] = []
    if "feishu_webhook_urls" not in data:
        data["feishu_webhook_urls"] = []
    if "alert_webhook_urls" not in data:
        data["alert_webhook_urls"] = []
    if "interval" not in data:
        data["interval"] = 60
    if "desktop_notify" not in data:
        data["desktop_notify"] = True
    return data


def _merge_configs(common_config, user_config):
    common = _normalize_config(common_config)
    user = _normalize_config(user_config)
    merged = dict(common)
    for key, value in user.items():
        if key != "sites":
            merged[key] = value
    merged_sites = []
    index = {}
    common_sites = common.get("sites", [])
    if isinstance(common_sites, list):
        for site in common_sites:
            if isinstance(site, dict) and site.get("name"):
                item = dict(site)
                merged_sites.append(item)
                index[item.get("name")] = item
            else:
                merged_sites.append(site)
    user_sites = user.get("sites", [])
    if isinstance(user_sites, list):
        for site in user_sites:
            if isinstance(site, dict) and site.get("name") in index and isinstance(index[site.get("name")], dict):
                index[site.get("name")].update(site)
            else:
                merged_sites.append(site)
    merged["sites"] = merged_sites
    return _normalize_config(merged)


class ConfigManager:
    @staticmethod
    def load():
        if not os.path.exists(CONFIG_FILE):
            return _normalize_config({})
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return _normalize_config(data)
        except Exception as e:
            messagebox.showerror("错误", f"配置文件读取失败: {e}")
            return _normalize_config({})

    @staticmethod
    def save(data, remote_sync=True):
        if remote_sync:
            success, msg = auth_manager.save_user_config(data)
            if not success:
                messagebox.showerror("错误", f"配置同步失败: {msg}")
                return False
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            messagebox.showerror("错误", f"配置文件保存失败: {e}")
            return False

class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1000x700")
        
        self.process = None
        self.is_stopping = False  # 标记是否为用户主动停止
        self.config = ConfigManager.load()
        self.icon = None
        self.order_notify_dialog = None
        
        # 尝试设置窗口图标
        try:
            icon_path = os.path.join(os.path.dirname(CONFIG_FILE), 'logo.ico')
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except Exception:
            pass

        self.create_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_closing)
        
        # 启动前先检查授权
        if not self.ensure_license_valid():
            self.root.destroy()
            return
        
        # 修复: 启动时自动同步配置
        if not self.refresh_config_from_server():
            # 如果配置同步失败（例如网络问题但授权还在宽限期），是否允许继续？
            # 策略：如果本地有配置，可以允许继续；否则提示错误
            if not self.config or not self.config.get("sites"):
                retry = messagebox.askretrycancel("配置同步失败", "无法从服务器获取配置，且本地无配置。\n请检查网络后重试。")
                if not retry:
                    self.root.destroy()
                    return
                # 如果重试，其实应该重新走一遍流程，这里简化处理，允许进入但可能配置为空
            else:
                 # 有本地缓存，提示一下但不退出
                 pass

        # 初始化并启动托盘图标（常驻）
        self.start_tray_icon()
        
        # 启动授权心跳
        self.start_heartbeat()

    def ensure_license_valid(self):
        """启动时强制检查授权，无效则循环要求激活"""
        while True:
            code = auth_manager.load_license()
            
            # 检查是否过期
            if auth_manager.is_license_expired():
                 info = auth_manager.get_license_info()
                 expire_date = (info or {}).get('expire_date')
                 messagebox.showwarning("授权已过期", f"当前授权已于 {expire_date} 过期，请输入新的授权码续期")
                 # 过期后虽然有 code，但也要进入激活流程
            elif code:
                # 有本地授权且未过期，验证有效性
                # 为了不阻塞启动太久，这里设置较短超时，或者显示一个Splash
                # 简单起见，同步阻塞检查
                success, msg = auth_manager.heartbeat()
                if success:
                    return True
                else:
                    # 如果心跳失败，但可能是网络原因且在宽限期内
                    # auth.py 的 heartbeat 已经处理了宽限期逻辑 (返回 True)
                    # 所以如果返回 False，说明是真的无效或超过宽限期
                    if "连接验证服务器失败" in msg or "网络连接异常" in msg:
                         # 网络问题，且可能超过宽限期，或者没有本地缓存
                         # 这里可以给用户一个选择：重试或输入新码
                         retry = messagebox.askretrycancel("连接失败", f"无法连接验证服务器: {msg}\n是否重试？")
                         if retry:
                             continue
                         else:
                             return False
                    
                    messagebox.showwarning("授权失效", f"当前授权验证失败: {msg}\n请重新输入授权码")
            
            # 没有授权或验证失败，弹出输入框
            # 如果是首次运行，提示欢迎
            prompt_msg = "请输入授权码进行激活："
            new_code = simpledialog.askstring("激活软件", prompt_msg, parent=self.root)
            
            if not new_code:
                # 用户取消或关闭输入框，退出程序
                return False
                
            new_code = new_code.strip()
            if not new_code:
                continue
                
            success, msg = auth_manager.activate(new_code)
            if success:
                info = auth_manager.get_license_info()
                expire_date = info.get('expire_date', '未知')
                # 激活成功后，自动尝试获取通用配置
                self.refresh_config_from_server()
                messagebox.showinfo("激活成功", f"软件已激活，欢迎使用！\n有效期至: {expire_date}")
                # 循环继续，再次 heartbeat 确认
            else:
                messagebox.showerror("激活失败", f"错误: {msg}")

    def start_heartbeat(self):
        def _loop():
            while not self.is_stopping:
                time.sleep(300) # 5分钟心跳一次
                success, msg = auth_manager.heartbeat()
                if not success:
                    self.root.after(0, lambda: messagebox.showwarning("授权警告", f"授权验证失败: {msg}\n程序即将退出"))
                    # 给用户一点时间看提示
                    self.root.after(3000, lambda: self.on_close(confirm=False))
                    break
        threading.Thread(target=_loop, daemon=True).start()

    def refresh_config_from_server(self, show_success=False):
        success, data = auth_manager.fetch_config()
        if not success:
            messagebox.showerror("配置获取失败", f"无法获取配置: {data}")
            return False
        payload = data if isinstance(data, dict) else {}
        common_config = payload.get("common_config") or {}
        user_config = payload.get("user_config") or {}
        
        # 获取使用说明内容
        help_content = payload.get("help_content", "")
        if hasattr(self, 'update_help_content'):
             self.update_help_content(help_content)

        local_config = ConfigManager.load()
        server_has_sites = len(user_config.get('sites', [])) > 0
        local_has_sites = len(local_config.get('sites', [])) > 0
        
        should_sync_to_remote = False
        
        # 1. 场景三：服务器配置为空，但本地有配置 -> 保留本地，并标记需要同步到服务器
        if not server_has_sites and local_has_sites:
            print("[Info] 服务器配置为空，保留本地站点配置并计划上传")
            user_config['sites'] = local_config.get('sites', [])
            should_sync_to_remote = True
        
        # 2. 场景二：服务器和本地都有配置 -> 智能合并
        # 用户要求：如果站点、账号一致，不要覆盖本地（因为本地有密码，服务器没有）
        # 我们的策略：
        # - 以服务器配置为基础（因为可能包含了管理员的修改或用户在其他机器的修改）
        # - 但是！如果本地存在同名站点，且关键信息（URL/账号）一致，则保留本地的密码等敏感信息
        # - 甚至，如果本地有些字段（如密码）存在而服务器没有，务必回填
        
        merged = _merge_configs(common_config, user_config)
        
        if local_config.get('sites'):
            local_sites_map = {s.get('name'): s for s in local_config['sites'] if s.get('name')}
            for site in merged.get('sites', []):
                local_site = local_sites_map.get(site.get('name'))
                if local_site:
                    # 关键信息一致性检查（可选，目前假设同名即为同一站点）
                    # 回填敏感字段
                    sensitive_keys = ["password", "login_password", "pay_password", "pwd", "secret", "passwd"]
                    for key in sensitive_keys:
                        # 只要本地有值，且不为空，就优先使用本地的 (防止服务器同步下来的空值或掩码覆盖本地密码)
                        if key in local_site and local_site[key]:
                            site[key] = local_site[key]
                            
                    # 额外保护：如果用户说“配置消失”，可能是服务器返回了被篡改或空的非敏感字段
                    # 但这里我们信任服务器返回的结构，只补全密码。
                    # 如果服务器返回的站点比本地少（删除了站点），这里也会删除。
                    # 如果用户希望本地站点永远不被服务器删除，那逻辑就复杂了，目前假设同步删除是预期的。

        # 保存合并后的配置到本地
        if not ConfigManager.save(merged, remote_sync=False):
            return False
            
        # 如果是场景三，或者本地有新变更需要同步上去（虽然这里主要是拉取，但如果是单向覆盖导致本地更新，不需要推回去；
        # 但如果是“服务器空本地有”，则必须推上去）
        if should_sync_to_remote:
             print("[Info] 正在将本地配置同步到服务器...")
             ConfigManager.save(merged, remote_sync=True)

        self.config = ConfigManager.load()
        self.refresh_site_list()
        self.refresh_webhook_lists()
        if show_success:
            messagebox.showinfo("成功", "通用配置获取并更新成功！")
        return True

    def start_tray_icon(self):
        # 创建图标图像
        try:
            icon_path = os.path.join(os.path.dirname(CONFIG_FILE), 'logo.ico')
            if os.path.exists(icon_path):
                image = Image.open(icon_path)
            else:
                raise FileNotFoundError
        except Exception:
            image = Image.new('RGB', (64, 64), color=(0, 120, 215))
            d = ImageDraw.Draw(image)
            d.text((10, 10), "租", fill=(255, 255, 255))
        
        # 定义菜单
        menu = (
            pystray.MenuItem('显示主界面', self.show_window_from_tray),
            pystray.MenuItem('退出', self.quit_app_from_tray)
        )
        
        self.icon = pystray.Icon("name", image, "租帮宝", menu)
        
        # 在独立线程中运行托盘图标
        threading.Thread(target=self.icon.run, daemon=True).start()

    def show_window_from_tray(self, icon=None, item=None):
        self.root.after(0, self.root.deiconify)

    def quit_app_from_tray(self, icon=None, item=None):
        self.root.after(0, lambda: self.on_close(confirm=False))

    def on_window_closing(self):
        # 自定义关闭提示对话框
        dialog = tk.Toplevel(self.root)
        dialog.title("关闭提示")
        
        width = 300
        height = 120
        try:
            x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (width // 2)
            y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (height // 2)
        except:
            x = (self.root.winfo_screenwidth() // 2) - (width // 2)
            y = (self.root.winfo_screenheight() // 2) - (height // 2)
            
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="您点击了关闭按钮，请选择：", font=("微软雅黑", 10)).pack(pady=20)
        
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, padx=10)
        
        def do_minimize():
            dialog.destroy()
            self.root.withdraw() # 隐藏窗口，图标已常驻
            
        def do_exit():
            dialog.destroy()
            self.on_close(confirm=False)
            
        ttk.Button(btn_frame, text="最小化到托盘", command=do_minimize).pack(side=tk.LEFT, expand=True, padx=5)
        ttk.Button(btn_frame, text="退出程序", command=do_exit).pack(side=tk.LEFT, expand=True, padx=5)
        
        # 默认关闭对话框不做任何事
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def notify(self, title, message):
        """发送系统通知（通过托盘图标）"""
        if self.icon and self.config.get('desktop_notify', True):
            try:
                self.icon.notify(message, title)
            except Exception as e:
                print(f"通知发送失败: {e}")

    def create_widgets(self):
        # 底部版本号
        version_frame = ttk.Frame(self.root)
        version_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)
        ttk.Label(version_frame, text=APP_VERSION, foreground="gray").pack(side=tk.RIGHT)

        # 使用 Notebook 实现多 Tab 布局
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Tab 1: 运行监控
        self.monitor_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.monitor_tab, text="运行监控")
        self.init_monitor_tab(self.monitor_tab)
        
        # Tab 2: 站点管理
        self.site_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.site_tab, text="站点管理")
        self.init_site_tab(self.site_tab)
        
        # Tab 3: 高级设置
        self.settings_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.settings_tab, text="高级设置")
        self.init_settings_tab(self.settings_tab)
        
        # Tab 4: 运行日志
        self.log_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.log_tab, text="运行日志")
        self.init_log_tab(self.log_tab)

        # Tab 5: 用户信息
        self.user_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.user_tab, text="用户信息")
        self.init_user_tab(self.user_tab)

        # Tab 6: 使用说明
        self.help_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.help_tab, text="使用说明")
        self.init_help_tab(self.help_tab)

    def init_user_tab(self, parent):
        info_frame = ttk.LabelFrame(parent, text="当前授权信息", padding=20)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Grid layout for info
        ttk.Label(info_frame, text="激活码:", font=("微软雅黑", 10, "bold")).grid(row=0, column=0, sticky='e', padx=10, pady=10)
        self.lbl_code = ttk.Entry(info_frame, font=("Consolas", 10), width=40, state='readonly')
        self.lbl_code.grid(row=0, column=1, sticky='w', padx=10, pady=10)
        
        ttk.Label(info_frame, text="设备机器码:", font=("微软雅黑", 10, "bold")).grid(row=1, column=0, sticky='e', padx=10, pady=10)
        self.lbl_machine = ttk.Entry(info_frame, font=("Consolas", 10), width=40, state='readonly')
        self.lbl_machine.grid(row=1, column=1, sticky='w', padx=10, pady=10)
        
        ttk.Label(info_frame, text="有效期至:", font=("微软雅黑", 10, "bold")).grid(row=2, column=0, sticky='e', padx=10, pady=10)
        self.lbl_expire = ttk.Label(info_frame, text="Loading...", font=("Consolas", 10))
        self.lbl_expire.grid(row=2, column=1, sticky='w', padx=10, pady=10)
        
        ttk.Label(info_frame, text="当前状态:", font=("微软雅黑", 10, "bold")).grid(row=3, column=0, sticky='e', padx=10, pady=10)
        self.lbl_license_status = ttk.Label(info_frame, text="Loading...", font=("微软雅黑", 10))
        self.lbl_license_status.grid(row=3, column=1, sticky='w', padx=10, pady=10)
        
        btn_frame = ttk.Frame(info_frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=20)
        
        ttk.Button(btn_frame, text="刷新信息", command=self.refresh_user_info).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="复制机器码", command=self.copy_machine_id).pack(side=tk.LEFT, padx=10)
        
        self.refresh_user_info()

    def refresh_user_info(self):
        info = auth_manager.get_license_info()
        # 尝试从 info 获取 code，如果 info 为空（未激活），则 code 可能为 None
        code = info.get('code', '未激活')
        machine_id = auth_manager.machine_id
        expire_date = info.get('expire_date', '未知')
        
        # Update Entry widgets (need to set state to normal first)
        self.lbl_code.config(state='normal')
        self.lbl_code.delete(0, tk.END)
        self.lbl_code.insert(0, str(code))
        self.lbl_code.config(state='readonly')
        
        self.lbl_machine.config(state='normal')
        self.lbl_machine.delete(0, tk.END)
        self.lbl_machine.insert(0, str(machine_id))
        self.lbl_machine.config(state='readonly')
        
        self.lbl_expire.config(text=str(expire_date))
        
        # Check validity
        if not code or code == '未激活':
             self.lbl_license_status.config(text="未激活", foreground="red")
        else:
             self.lbl_license_status.config(text="已激活", foreground="green")

    def copy_machine_id(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(auth_manager.machine_id)
        messagebox.showinfo("提示", "机器码已复制到剪贴板")

    def init_monitor_tab(self, parent):
        # 顶部控制区
        control_frame = ttk.LabelFrame(parent, text="控制面板", padding=10)
        control_frame.pack(fill=tk.X, pady=5)
        
        self.btn_start = ttk.Button(control_frame, text="启动监控服务", command=self.toggle_service)
        self.btn_start.pack(side=tk.LEFT, padx=5)
        
        self.lbl_status = ttk.Label(control_frame, text="状态: 未运行", foreground="red")
        self.lbl_status.pack(side=tk.LEFT, padx=15)
        
        ttk.Separator(control_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        ttk.Button(control_frame, text="显示浏览器界面", command=self.show_browser).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="隐藏/移出屏幕", command=self.hide_browser).pack(side=tk.LEFT, padx=5)
        
        # 数据监控区 (Treeview)
        data_frame = ttk.LabelFrame(parent, text="实时订单数据", padding=5)
        data_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        columns = ("name", "count", "time", "action")
        self.monitor_tree = ttk.Treeview(data_frame, columns=columns, show='headings', selectmode='browse')
        self.monitor_tree.heading("name", text="站点名称")
        self.monitor_tree.heading("count", text="待处理订单")
        self.monitor_tree.heading("time", text="更新时间")
        self.monitor_tree.heading("action", text="操作")
        
        self.monitor_tree.column("name", width=150, anchor='center')
        self.monitor_tree.column("count", width=100, anchor='center')
        self.monitor_tree.column("time", width=150, anchor='center')
        self.monitor_tree.column("action", width=100, anchor='center')
        
        self.monitor_tree.pack(fill=tk.BOTH, expand=True)
        self.monitor_tree.bind('<Double-1>', self.on_monitor_double_click)
        
        # 存储跳转链接 {site_name: url}
        self.site_links = {}

    def init_site_tab(self, parent):
        # 站点列表
        columns = ("name", "url", "user", "status")
        self.tree = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        self.tree.heading("name", text="站点名称")
        self.tree.heading("url", text="登录地址")
        self.tree.heading("user", text="用户名")
        self.tree.heading("status", text="监控状态")
        self.tree.column("name", width=150)
        self.tree.column("url", width=350)
        self.tree.column("user", width=120)
        self.tree.column("status", width=80, anchor='center')
        
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind('<Double-1>', self.edit_site)
        self.tree.bind('<Button-1>', self.on_tree_click)
        
        # 右键菜单
        self.site_context_menu = tk.Menu(self.tree, tearoff=0)
        self.site_context_menu.add_command(label="编辑", command=self.edit_site)
        self.site_context_menu.add_command(label="删除", command=self.delete_site)
        self.site_context_menu.add_separator()
        self.site_context_menu.add_command(label="启用/禁用监控", command=self.toggle_site_status)
        self.tree.bind("<Button-3>", self.show_site_context_menu)
        
        btn_frame = ttk.Frame(parent, padding=5)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        
        ttk.Button(btn_frame, text="添加站点", command=self.add_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="编辑选中", command=self.edit_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="删除选中", command=self.delete_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="启用/禁用", command=self.toggle_site_status).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="获取通用配置", command=lambda: self.refresh_config_from_server(show_success=True)).pack(side=tk.LEFT, padx=5)
        
        self.refresh_site_list()

    def show_site_context_menu(self, event):
        try:
            item = self.tree.identify_row(event.y)
            if item:
                self.tree.selection_set(item)
                self.site_context_menu.post(event.x_root, event.y_root)
        except:
            pass

    def on_tree_click(self, event):
        try:
            region = self.tree.identify_region(event.x, event.y)
            if region == 'cell':
                column = self.tree.identify_column(event.x)
                if column == '#4':  # Status column
                    item_id = self.tree.identify_row(event.y)
                    if item_id:
                        self.toggle_site_status(item_id)
        except Exception:
            pass

    def toggle_site_status(self, item_id=None):
        if not item_id:
            sel = self.tree.selection()
            if not sel: return
            item_id = sel[0]
        
        item = self.tree.item(item_id)
        if not item or not item['values']: return
        name = item['values'][0]
        
        site = next((s for s in self.config['sites'] if s['name'] == name), None)
        if site:
            # 切换状态，默认为 True
            current_status = site.get('enabled', True)
            site['enabled'] = not current_status
            ConfigManager.save(self.config)
            self.refresh_site_list()
            
            # 尝试恢复选中
            for child in self.tree.get_children():
                if self.tree.item(child)['values'][0] == name:
                    self.tree.selection_set(child)
                    self.tree.focus(child)
                    break


    def init_settings_tab(self, parent):
        # 1. 运行参数
        param_frame = ttk.LabelFrame(parent, text="基础设置", padding=10)
        param_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(param_frame, text="检查间隔 (秒):").grid(row=0, column=0, padx=5, sticky='w')
        self.interval_var = tk.IntVar(value=self.config.get('interval', 60))
        ttk.Entry(param_frame, textvariable=self.interval_var, width=10).grid(row=0, column=1, padx=5, sticky='w')
        ttk.Label(param_frame, text="(最低 30 秒)").grid(row=0, column=2, padx=5, sticky='w')
        
        self.desktop_notify_var = tk.BooleanVar(value=self.config.get('desktop_notify', True))
        ttk.Checkbutton(param_frame, text="开启桌面气泡通知", variable=self.desktop_notify_var).grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky='w')

        self.headless_var = tk.BooleanVar(value=self.config.get('headless', False))
        ttk.Checkbutton(param_frame, text="无头模式 (不显示浏览器窗口)", variable=self.headless_var).grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky='w')

        # 夜间模式配置
        self.night_mode_var = tk.BooleanVar(value=self.config.get('night_mode', False))
        ttk.Checkbutton(param_frame, text="开启夜间免打扰模式", variable=self.night_mode_var).grid(row=3, column=0, columnspan=2, padx=5, pady=5, sticky='w')

        night_frame = ttk.Frame(param_frame)
        night_frame.grid(row=4, column=0, columnspan=3, padx=5, sticky='w')
        
        ttk.Label(night_frame, text="静默时段 (小时):").pack(side=tk.LEFT)
        
        self.night_start_var = tk.IntVar(value=self.config.get('night_period', {}).get('start', 0))
        ttk.Spinbox(night_frame, from_=0, to=23, textvariable=self.night_start_var, width=5).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(night_frame, text="至").pack(side=tk.LEFT)
        
        self.night_end_var = tk.IntVar(value=self.config.get('night_period', {}).get('end', 7))
        ttk.Spinbox(night_frame, from_=0, to=23, textvariable=self.night_end_var, width=5).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(night_frame, text="(结束小时不含)").pack(side=tk.LEFT)
        
        ttk.Button(param_frame, text="保存参数", command=self.save_settings).grid(row=0, column=3, rowspan=5, padx=20)

        # 2. 企微 Webhook
        wecom_frame = ttk.LabelFrame(parent, text="企业微信通知配置", padding=10)
        wecom_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.webhook_listbox = tk.Listbox(wecom_frame, height=5)
        self.webhook_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        
        w_btn_frame = ttk.Frame(wecom_frame)
        w_btn_frame.pack(fill=tk.X)
        ttk.Button(w_btn_frame, text="添加企微 Webhook", command=self.add_webhook).pack(side=tk.LEFT, padx=5)
        ttk.Button(w_btn_frame, text="删除选中", command=self.del_webhook).pack(side=tk.LEFT, padx=5)
        
        # 3. 飞书 Webhook
        feishu_frame = ttk.LabelFrame(parent, text="飞书通知配置", padding=10)
        feishu_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.feishu_listbox = tk.Listbox(feishu_frame, height=5)
        self.feishu_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        
        f_btn_frame = ttk.Frame(feishu_frame)
        f_btn_frame.pack(fill=tk.X)
        ttk.Button(f_btn_frame, text="添加飞书 Webhook", command=self.add_feishu_webhook).pack(side=tk.LEFT, padx=5)
        ttk.Button(f_btn_frame, text="删除选中", command=self.del_feishu_webhook).pack(side=tk.LEFT, padx=5)
        
        self.refresh_webhook_lists()

    def init_log_tab(self, parent):
        self.log_text = scrolledtext.ScrolledText(parent, state='disabled', font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def init_help_tab(self, parent):
        self.help_text_widget = scrolledtext.ScrolledText(parent, font=('微软雅黑', 10), padx=20, pady=20)
        self.help_text_widget.pack(fill=tk.BOTH, expand=True)
        
        # 默认说明
        default_help_text = """
【租帮宝 - 使用说明】

一、首次使用（必须激活）
   - 启动后按提示输入授权码完成激活。
   - 如果授权已过期，会先提示“已过期”，再让你输入新的授权码。
   - 需要报备设备信息时，到“用户信息”页点击“复制机器码”发给管理员。

二、添加站点
   - 打开“站点管理”页，点击“添加站点”。
   - 常规情况下只需要填写：站点名称、登录地址、账号、密码、订单地址。
   - 站点账号密码建议独立使用；多处共用同一账号容易互相顶号，导致反复掉线/重复人工登录。
   - 如果站点需要额外的页面定位配置（用于识别订单数量/跳转链接），联系交付人员协助配置。

三、通知与运行策略（可选但建议）
   - 打开“高级设置”，添加企业微信/飞书的 Webhook 地址（支持多个）。
   - Webhook 地址怎么拿：
     - 企业微信：群聊 → 右上角设置 → 群机器人 → 添加机器人 → 复制 Webhook
     - 飞书：群聊 → 群设置 → 机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook
   - 勾选“开启桌面气泡通知”，电脑端会弹出提醒。
   - 无头模式：一般在当天已成功登录一次、并且重启监控服务后开启才明显生效；效果是隐藏浏览器窗口，但无法人工介入登录。
   - 需要手动登录/验证码时，关闭无头模式，并使用“显示浏览器界面”完成操作。
   - 可设置检查间隔与夜间免打扰时段。

四、启动监控与处理订单
   - 回到“运行监控”页，点击“启动监控服务”。
   - 双击列表项可快速打开后台处理。
   - 操作完成后，可“隐藏/移出屏幕”，并允许软件最小化到托盘后台运行。

五、常见问题
   - 显示 0 单：可能确实没有订单；也可能需要重新登录。先点“显示浏览器界面”确认。
   - 收不到通知：确认已添加 Webhook 地址，且群机器人可正常发消息。
   - 找不到窗口：检查右下角托盘图标，右键可“显示主界面/退出”。
        """
        self.help_text_widget.insert(tk.END, default_help_text)
        self.help_text_widget.configure(state='disabled')

    def update_help_content(self, content):
        if not content:
            return
        self.help_text_widget.configure(state='normal')
        self.help_text_widget.delete('1.0', tk.END)
        self.help_text_widget.insert(tk.END, content)
        self.help_text_widget.configure(state='disabled')

    # === 逻辑处理 ===

    def refresh_site_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for site in self.config.get('sites', []):
            status = "启用" if site.get('enabled', True) else "禁用"
            self.tree.insert('', 'end', values=(site.get('name', ''), site.get('login_url', ''), site.get('username', ''), status))

    def refresh_webhook_lists(self):
        self.webhook_listbox.delete(0, tk.END)
        for url in self.config.get('webhook_urls', []):
            self.webhook_listbox.insert(tk.END, url)
            
        self.feishu_listbox.delete(0, tk.END)
        for url in self.config.get('feishu_webhook_urls', []):
            self.feishu_listbox.insert(tk.END, url)

    def save_settings(self):
        try:
            val = self.interval_var.get()
            if val < 30:
                val = 30
                self.interval_var.set(30)
                messagebox.showwarning("提示", "间隔时间不能少于 30 秒")
            
            self.config['interval'] = val
            self.config['desktop_notify'] = self.desktop_notify_var.get()
            self.config['headless'] = self.headless_var.get()
            
            # 保存夜间模式设置
            self.config['night_mode'] = self.night_mode_var.get()
            start = self.night_start_var.get()
            end = self.night_end_var.get()
            
            if start < 0 or start > 23 or end < 0 or end > 23:
                messagebox.showwarning("提示", "时间段必须在 0-23 之间")
                return
                
            self.config['night_period'] = {"start": start, "end": end}
            
            ConfigManager.save(self.config)
            
            # 重新加载配置，确保内存中的数据也是最新的（虽然上面 self.config 已经是新的了，但为了保险起见）
            self.config = ConfigManager.load()
            self.refresh_site_list()
            self.refresh_webhook_lists()

            if self.process and self.process.poll() is None:
                if messagebox.askyesno("提示", "配置已保存。是否立即重启监控服务以生效？"):
                     self.restart_service()
            else:
                messagebox.showinfo("成功", "设置已保存")
        except:
            messagebox.showerror("错误", "参数格式错误")

    def add_webhook(self):
        url = simpledialog.askstring("添加企微 Webhook", "请输入 Webhook URL:")
        if url and url not in self.config['webhook_urls']:
            self.config['webhook_urls'].append(url)
            ConfigManager.save(self.config)
            self.refresh_webhook_lists()

    def del_webhook(self):
        sel = self.webhook_listbox.curselection()
        if sel:
            self.config['webhook_urls'].pop(sel[0])
            ConfigManager.save(self.config)
            self.refresh_webhook_lists()

    def add_feishu_webhook(self):
        url = simpledialog.askstring("添加飞书 Webhook", "请输入 Webhook URL:")
        if url and url not in self.config['feishu_webhook_urls']:
            self.config['feishu_webhook_urls'].append(url)
            ConfigManager.save(self.config)
            self.refresh_webhook_lists()

    def del_feishu_webhook(self):
        sel = self.feishu_listbox.curselection()
        if sel:
            self.config['feishu_webhook_urls'].pop(sel[0])
            ConfigManager.save(self.config)
            self.refresh_webhook_lists()

    def add_site(self):
        self.open_site_editor()

    def edit_site(self, event=None):
        item_id = None
        if event is not None:
            try:
                # 拦截状态列的双击
                if self.tree.identify_column(event.x) == '#4':
                    return
                item_id = self.tree.identify_row(event.y)
            except Exception:
                item_id = None
        if not item_id:
            sel = self.tree.selection()
            if not sel:
                return
            item_id = sel[0]
        item = self.tree.item(item_id)
        values = item.get('values') or []
        if not values:
            return
        name = values[0]
        
        # 在打开编辑器之前，强制重新加载一次配置，确保获取的是最新的（包括后台自动更新的选择器）
        try:
            self.config = ConfigManager.load()
        except:
            pass

        site_conf = next((s for s in self.config['sites'] if s['name'] == name), None)
        if site_conf:
            self.open_site_editor(site_conf)

    def delete_site(self):
        sel = self.tree.selection()
        if not sel: return
        if messagebox.askyesno("确认", "确定要删除该站点配置吗？"):
            name = self.tree.item(sel[0])['values'][0]
            self.config['sites'] = [s for s in self.config['sites'] if s['name'] != name]
            ConfigManager.save(self.config)
            self.refresh_site_list()

    def open_site_editor(self, site_data=None):
        edit_win = tk.Toplevel(self.root)
        edit_win.title("编辑站点" if site_data else "新增站点")
        edit_win.geometry("600x650")
        
        row = 0
        
        # 启用状态
        enabled_var = tk.BooleanVar(value=site_data.get('enabled', True) if site_data else True)
        ttk.Checkbutton(edit_win, text="启用此站点监控", variable=enabled_var).grid(row=row, column=1, sticky='w', padx=10, pady=5)
        row += 1
        
        fields = [("站点名称", "name"), ("登录地址", "login_url"), ("用户名", "username"), ("密码", "password")]
        entries = {}
        for label, key in fields:
            ttk.Label(edit_win, text=label).grid(row=row, column=0, padx=10, pady=5, sticky='e')
            entry = ttk.Entry(edit_win, width=50)
            entry.grid(row=row, column=1, padx=10, pady=5)
            if site_data: entry.insert(0, site_data.get(key, ""))
            entries[key] = entry
            row += 1

        # 订单页地址
        ttk.Label(edit_win, text="订单页地址").grid(row=row, column=0, padx=10, pady=5, sticky='e')
        order_url_entry = ttk.Entry(edit_win, width=50)
        order_url_entry.grid(row=row, column=1, padx=10, pady=5)
        
        # 安全获取 selectors 数据
        selectors_data = {}
        if site_data:
            selectors_data = site_data.get('selectors') or {}
            if not isinstance(selectors_data, dict):
                selectors_data = {}

        try:
            order_url_entry.insert(0, selectors_data.get('order_menu_link', ""))
        except Exception as e:
            print(f"Error setting order url: {e}")
        row += 1
            
        # 选择器 JSON
        ttk.Label(edit_win, text="选择器配置 (JSON)").grid(row=row, column=0, padx=10, pady=5, sticky='ne')
        txt_selectors = scrolledtext.ScrolledText(edit_win, width=50, height=15)
        txt_selectors.grid(row=row, column=1, padx=10, pady=5)
        
        default_selectors = {"username_input": "", "password_input": "", "login_button": "", "pending_tab_selector": "", "pending_count_element": ""}
        
        try:
            current_selectors = selectors_data if selectors_data else default_selectors
            # 浅拷贝一份用于显示，避免修改原始数据
            display_selectors = current_selectors.copy()
            # 从显示中移除 order_menu_link，因为已有独立输入框
            if 'order_menu_link' in display_selectors:
                del display_selectors['order_menu_link']
                
            txt_selectors.insert('1.0', json.dumps(display_selectors, indent=2, ensure_ascii=False))
        except Exception as e:
            txt_selectors.insert('1.0', "{}")
            messagebox.showerror("错误", f"加载选择器配置失败: {e}")

        def save():
            new_data = {}
            # 保存启用状态
            new_data['enabled'] = enabled_var.get()
            
            for k, e in entries.items():
                new_data[k] = e.get()
                # 仅 name 和 login_url 为必填
                if not new_data[k] and k in ("name", "login_url"):
                    messagebox.showerror("错误", f"{k} 不能为空")
                    return
            
            try:
                sel_json = txt_selectors.get('1.0', tk.END).strip()
                # 兼容性处理：如果用户没有输入JSON，默认给空字典
                if not sel_json:
                    sel_json = "{}"
                # strict=False 允许字符串中包含控制字符（如换行符）
                new_data['selectors'] = json.loads(sel_json, strict=False)
            except json.JSONDecodeError as e:
                # 尝试更友好的错误提示
                err_msg = str(e)
                if "Expecting property name enclosed in double quotes" in err_msg:
                    err_msg += "\n\n提示：JSON 的键必须用双引号括起来，不能用单引号。"
                elif "Invalid control character" in err_msg:
                    err_msg += "\n\n提示：字符串中可能包含了未转义的换行符或特殊字符。已尝试放宽检查但仍失败。"
                
                messagebox.showerror("错误", f"选择器 JSON 格式错误:\n{err_msg}")
                return
            
            # 将订单页地址同步到 selectors 中
            order_url_val = order_url_entry.get().strip()
            if order_url_val:
                new_data['selectors']['order_menu_link'] = order_url_val
            elif 'order_menu_link' in new_data['selectors']:
                # 如果输入框为空，但 JSON 里有，是否要清除？
                # 这里假设输入框为空表示不强制覆盖，或者视为清空
                # 但为了避免误操作，如果输入框为空，而JSON里有值，可能是用户没填输入框
                # 既然是双向绑定，输入框的值应该优先
                new_data['selectors']['order_menu_link'] = ""

            if site_data:
                # 检查是否修改了账号密码
                old_user = site_data.get('username')
                old_pass = site_data.get('password')
                new_user = new_data.get('username')
                new_pass = new_data.get('password')
                
                if old_user != new_user or old_pass != new_pass:
                    safe_name = re.sub(r'[^\w\-]', '_', site_data['name'])
                    cookie_file = f"{safe_name}_state.json"
                    base_dir = os.path.dirname(CONFIG_FILE)
                    cookie_path = os.path.join(base_dir, 'cookies', cookie_file)
                    
                    if os.path.exists(cookie_path):
                        try:
                            os.remove(cookie_path)
                            messagebox.showinfo("提示", "检测到账号或密码已修改，已清除旧的 Cookie。\n下次运行时将触发重新登录。")
                        except Exception as e:
                            messagebox.showerror("错误", f"清除 Cookie 失败: {e}")

                self.config['sites'] = [s for s in self.config['sites'] if s['name'] != site_data['name']]
            self.config['sites'].append(new_data)
            ConfigManager.save(self.config)
            self.refresh_site_list()
            edit_win.destroy()
            
            if self.process and self.process.poll() is None:
                if messagebox.askyesno("提示", "站点配置已修改。是否立即重启监控服务以生效？"):
                     self.restart_service()

        ttk.Button(edit_win, text="保存", command=save).grid(row=row+1, column=1, pady=20)

    # === 运行控制 ===

    def log(self, message):
        # 检查是否为结构化数据更新
        if message.startswith("DATA_UPDATE:"):
            try:
                json_str = message.replace("DATA_UPDATE:", "", 1)
                data_pkg = json.loads(json_str)
                self.update_monitor_data(data_pkg)
                
                # 在接收到后端数据更新时，也检查一下配置是否有变化（比如后端更新了选择器）
                # 注意：频繁读取 IO 可能会有性能影响，但考虑到更新频率不高（60秒一次），是可以接受的
                # 为了防止 UI 闪烁，我们只在数据真正变化时更新 UI
                try:
                    new_config = ConfigManager.load()
                    # 简单比较 sites 的长度或特定字段，这里做全量比较
                    # 注意：直接比较 dict 可能会因为顺序不同而不等，但 json load 出来的通常顺序一致
                    # 为避免干扰，我们只在后端更新了 selector 时才需要刷新
                    # 这里简化逻辑：每次收到数据更新，都静默重新加载一次 config 到内存
                    # 这样下次点击“编辑站点”时，看到的就是最新的
                    self.config = new_config
                    # 不主动调用 refresh_site_list()，以免打断用户当前操作（如正在选行）
                except:
                    pass
                
                return
            except Exception as e:
                pass # 解析失败则照常打印
        
        # === 增强功能：检测配置变更通知 ===
        # 当后端打印 "选择器已写回配置" 时，说明本地 config.json 已被修改
        # 此时应立即重新加载内存中的配置，以便用户打开编辑窗口时能看到最新数据
        if "选择器已写回配置" in message:
            try:
                # print("检测到配置文件变更，正在刷新 UI 内存配置...")
                self.config = ConfigManager.load()
            except:
                pass

        # === 增强功能：检测人工介入请求并通知 ===
        # 匹配日志中的 ">>> 等待人工手动登录"
        if ">>> 等待人工手动登录" in message:
            # 提取站点名称 (假设格式: [站点名] >>> ...)
            match = re.search(r'\[(.*?)\]', message)
            site_name = match.group(1) if match else "某站点"
            # 切换为常驻弹窗提醒 (在主线程执行)
            self.root.after(0, lambda: self.show_manual_intervention_dialog(site_name))
        
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, message)
        self.log_text.see(tk.END)
        self.log_text.configure(state='disabled')

    def update_monitor_data(self, pkg):
        # 清空旧数据
        for item in self.monitor_tree.get_children():
            self.monitor_tree.delete(item)
        
        timestamp = pkg.get('timestamp', '')
        results = pkg.get('data', [])
        
        has_orders = False
        
        for res in results:
            name = res['name']
            count = res.get('count', 0)
            error = res.get('error')
            link = res.get('link')
            
            # 保存链接
            if link: self.site_links[name] = link
            
            display_count = str(count) if not error else "[X] 错误"
            action_text = "双击处理" if link else "-"
            
            if count > 0: has_orders = True
            
            self.monitor_tree.insert('', 'end', values=(name, display_count, timestamp, action_text))
            
        # 桌面通知 (使用 Tray Icon 通知)
        if has_orders:
            # 生成详细的通知消息
            notify_items = []
            for res in results:
                count = res.get('count', 0)
                if count and count > 0:
                    notify_items.append(f"{res.get('name')}: {count}单")
            
            notify_msg = "检测到有待处理订单：\n" + "\n".join(notify_items) if notify_items else "检测到有待处理订单，请及时查看！"
            
            self.notify("租帮宝 - 新订单提醒", notify_msg)
            self.show_order_notification(results, timestamp)

    def show_order_notification(self, results, timestamp):
        items = []
        for res in results:
            error = res.get('error')
            count = res.get('count', 0)
            if error:
                continue
            if count and count > 0:
                items.append(f"{res.get('name')}: {count} 单")
        if not items:
            return

        if self.order_notify_dialog and self.order_notify_dialog.winfo_exists():
            try:
                self.order_notify_dialog.destroy()
            except:
                pass

        dialog = tk.Toplevel(self.root)
        self.order_notify_dialog = dialog
        dialog.title("🔔 新订单提醒")
        width = 380
        height = 220

        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - width - 20
            y = sh - height - 80
            dialog.geometry(f"{width}x{height}+{x}+{y}")
        except:
            dialog.geometry(f"{width}x{height}")

        dialog.resizable(False, False)
        dialog.attributes('-topmost', True)

        content_frame = ttk.Frame(dialog, padding=20)
        content_frame.pack(fill=tk.BOTH, expand=True)

        header_frame = ttk.Frame(content_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))
        icon_lbl = ttk.Label(header_frame, text="🧾", font=("Segoe UI Emoji", 20))
        icon_lbl.pack(side=tk.LEFT, padx=(0, 10))
        title_lbl = ttk.Label(header_frame, text="发现待处理订单", font=("微软雅黑", 11, "bold"), foreground="#d9534f")
        title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        summary = "\n".join(items)
        summary_lbl = ttk.Label(content_frame, text=summary, font=("微软雅黑", 9), foreground="#333", wraplength=320)
        summary_lbl.pack(fill=tk.X, pady=(0, 8))
        time_lbl = ttk.Label(content_frame, text=f"更新时间：{timestamp}", font=("微软雅黑", 9), foreground="#999")
        time_lbl.pack(fill=tk.X)

        btn_frame = ttk.Frame(dialog, padding=10)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        auto_close_id = {"value": None}
        def schedule_auto_close():
            try:
                if auto_close_id["value"] is not None:
                    self.root.after_cancel(auto_close_id["value"])
            except:
                pass
            auto_close_id["value"] = self.root.after(60000, do_close)
        
        def do_view():
            try:
                if auto_close_id["value"] is not None:
                    self.root.after_cancel(auto_close_id["value"])
                dialog.destroy()
            except:
                pass
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
                self.notebook.select(self.monitor_tab)
            except:
                pass

        def do_close():
            try:
                if auto_close_id["value"] is not None:
                    self.root.after_cancel(auto_close_id["value"])
                dialog.destroy()
            except:
                pass

        style = ttk.Style()
        style.configure("Accent.TButton", foreground="blue")

        ttk.Button(btn_frame, text="去处理", command=do_view, style="Accent.TButton").pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="关闭", command=do_close).pack(side=tk.RIGHT, padx=5)
        
        # 绑定点击事件到整个弹窗区域，方便快速处理
        for widget in [content_frame, header_frame, icon_lbl, title_lbl, summary_lbl, time_lbl]:
            try:
                widget.bind("<Button-1>", lambda e: do_view())
                widget.configure(cursor="hand2")
            except:
                pass

        def on_dialog_close():
            try:
                if auto_close_id["value"] is not None:
                    self.root.after_cancel(auto_close_id["value"])
                dialog.destroy()
            except:
                pass

        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)
        schedule_auto_close()

    def on_monitor_double_click(self, event):
        item = self.monitor_tree.selection()
        if not item: return
        values = self.monitor_tree.item(item[0], 'values')
        name = values[0]
        link = self.site_links.get(name)
        if link:
            webbrowser.open(link)

    def start_process(self):
        # 强制检查账号密码
        sites = self.config.get('sites', [])
        if not sites:
            messagebox.showwarning("启动失败", "站点列表为空，请先同步或添加站点")
            return

        missing_creds = []
        for site in sites:
            # 如果站点未启用监控，跳过检查
            if not site.get('enabled', True):
                continue
            if not site.get('username') or not site.get('password'):
                missing_creds.append(site.get('name', '未知站点'))

        if missing_creds:
            msg = "以下站点未设置账号或密码，请先完成设置：\n\n" + "\n".join(missing_creds)
            messagebox.showwarning("启动失败", msg)
            self.notebook.select(self.site_tab) # 自动切换到站点设置页
            return

        self.is_stopping = False
        self.log("\n=== 正在启动监控服务... ===\n")
        self.lbl_status.config(text="状态: 运行中", foreground="green")
        self.btn_start.config(text="停止监控服务")
        success, msg = auth_manager.heartbeat()
        if not success:
            messagebox.showwarning("授权失效", f"授权验证失败: {msg}")
            self.lbl_status.config(text="状态: 未授权", foreground="red")
            self.btn_start.config(text="启动监控服务")
            return
        
        if getattr(sys, 'frozen', False):
            # 优先检查 backend 目录（onedir 模式）
            base_dir = os.path.dirname(sys.executable)
            target_exe = os.path.join(base_dir, "backend", "OrderMonitor.exe")
            if not os.path.exists(target_exe):
                # 回退检查同级目录（旧 onefile 模式兼容）
                target_exe = os.path.join(base_dir, "OrderMonitor.exe")
            
            if not os.path.exists(target_exe):
                self.log(f"错误: 找不到核心程序 {target_exe}\n")
                self.lbl_status.config(text="状态: 文件缺失", foreground="red")
                self.btn_start.config(text="启动监控服务")
                return
            cmd = [target_exe]
        else:
            cmd = [sys.executable, "main.py"]
        
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                encoding='utf-8',
                errors='replace',
                creationflags=creationflags
            )
            threading.Thread(target=self.read_process_output, daemon=True).start()
        except Exception as e:
            self.log(f"启动失败: {e}\n")
            self.lbl_status.config(text="状态: 启动失败", foreground="red")
            self.btn_start.config(text="启动监控服务")

    def read_process_output(self):
        """读取子进程输出并更新到日志窗口"""
        if not self.process:
            return
            
        try:
            for line in iter(self.process.stdout.readline, ''):
                if not line: break
                # 使用 after 在主线程更新 UI
                self.root.after(0, lambda l=line: self.log(l))
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda m=err: self.log(f"\n[系统] 读取进程输出出错: {m}\n"))
        finally:
            # 进程自然结束（非手动停止）
            if not self.is_stopping:
                self.root.after(0, lambda: self.lbl_status.config(text="状态: 意外停止", foreground="red"))
                self.root.after(0, lambda: self.btn_start.config(text="启动监控服务"))
                self.root.after(0, lambda: self.log("\n=== 监控服务已意外停止 ===\n"))

    def kill_process_tree(self):
        """强制终止进程及其所有子进程"""
        if self.process:
            pid = self.process.pid
            try:
                # 使用 taskkill 强制终止进程树
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], 
                             stdout=subprocess.DEVNULL, 
                             stderr=subprocess.DEVNULL, 
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            except Exception as e:
                print(f"终止进程失败: {e}")
            
            self.process = None

    def toggle_service(self):
        if self.process and self.process.poll() is None:
            if messagebox.askyesno("确认", "确定要停止监控服务吗？"):
                self.is_stopping = True
                self.kill_process_tree()
                self.lbl_status.config(text="状态: 未运行", foreground="red")
                self.btn_start.config(text="启动监控服务")
                self.log("\n=== 服务已停止 ===\n")
        else:
            self.start_process()

    def restart_service(self):
        if self.process:
            self.is_stopping = True
            self.kill_process_tree()
            
        def _start():
            self.start_process()
            
        # 延时 1 秒确保进程完全释放
        self.root.after(1000, _start)

    def on_close(self, confirm=True):
        if self.process and self.process.poll() is None:
            if confirm and not messagebox.askyesno("退出", "监控服务正在运行，确定要退出吗？\n(退出将停止监控)"):
                return
            self.kill_process_tree()
        
        if self.icon:
            self.icon.stop()
        self.root.destroy()

    def show_manual_intervention_dialog(self, site_name):
        """显示常驻的人工介入提醒弹窗"""
        dialog = tk.Toplevel(self.root)
        dialog.title("⚠️ 需要人工介入")
        width = 380
        height = 180
        
        # 尝试显示在屏幕右下角 (类似气泡位置)
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = sw - width - 20
            y = sh - height - 80 # 避开任务栏
            dialog.geometry(f"{width}x{height}+{x}+{y}")
        except:
            dialog.geometry(f"{width}x{height}")
            
        dialog.resizable(False, False)
        dialog.attributes('-topmost', True) # 置顶显示
        
        # 内容区域
        content_frame = ttk.Frame(dialog, padding=20)
        content_frame.pack(fill=tk.BOTH, expand=True)
        
        # 图标/标题
        header_frame = ttk.Frame(content_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header_frame, text="🔔", font=("Segoe UI Emoji", 20)).pack(side=tk.LEFT, padx=(0, 10))
        
        title_lbl = ttk.Label(header_frame, text=f"站点【{site_name}】需要协助", font=("微软雅黑", 11, "bold"), foreground="#d9534f")
        title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        # 说明文本
        ttk.Label(content_frame, text="检测到登录流程受阻（如验证码），请人工介入处理。\n处理完成后脚本将自动继续。", 
                 font=("微软雅黑", 9), foreground="#666", wraplength=320).pack(fill=tk.X, pady=5)
        
        countdown_seconds = 60
        countdown_lbl = ttk.Label(content_frame, text=f"窗口将在 {countdown_seconds}s 后自动关闭", 
                                  font=("微软雅黑", 9), foreground="#999")
        countdown_lbl.pack(fill=tk.X, pady=(0, 10))
        
        timer_id = {"value": None}
        def tick():
            if not dialog.winfo_exists():
                return
            nonlocal countdown_seconds
            countdown_seconds -= 1
            if countdown_seconds <= 0:
                try:
                    dialog.destroy()
                except:
                    pass
                return
            try:
                countdown_lbl.configure(text=f"窗口将在 {countdown_seconds}s 后自动关闭")
            except:
                pass
            timer_id["value"] = self.root.after(1000, tick)
        
        timer_id["value"] = self.root.after(1000, tick)
        
        # 按钮区域
        btn_frame = ttk.Frame(dialog, padding=10)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        def do_view():
            self.show_browser()
            try:
                if timer_id["value"] is not None:
                    self.root.after_cancel(timer_id["value"])
            except:
                pass
            try:
                dialog.destroy()
            except:
                pass
            # 尝试激活主窗口
            self.root.deiconify()
            
        def do_close():
            try:
                if timer_id["value"] is not None:
                    self.root.after_cancel(timer_id["value"])
            except:
                pass
            try:
                dialog.destroy()
            except:
                pass
        
        def on_dialog_close():
            try:
                if timer_id["value"] is not None:
                    self.root.after_cancel(timer_id["value"])
            except:
                pass
            try:
                dialog.destroy()
            except:
                pass
        
        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)
            
        # 样式调整
        style = ttk.Style()
        style.configure("Accent.TButton", foreground="blue")
        
        ttk.Button(btn_frame, text="立即查看处理", command=do_view, style="Accent.TButton").pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="稍后处理", command=do_close).pack(side=tk.RIGHT, padx=5)
        
        # 播放提示音 (Windows)
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except:
            pass

    def show_browser(self): self._call_browser_api("show")
    def hide_browser(self): self._call_browser_api("hide")
    
    def _call_browser_api(self, action):
        if not self.process or self.process.poll() is not None:
            messagebox.showwarning("提示", "请先启动监控服务")
            return
        def _req():
            try:
                url = f"http://localhost:5000/api/browser/{action}"
                resp = requests.post(url, timeout=3)
                if resp.status_code == 200:
                    self.root.after(0, lambda: self.log(f"指令发送成功: {action}\n"))
                else:
                    self.root.after(0, lambda: self.log(f"指令失败: {resp.text}\n"))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda m=err: self.log(f"请求失败 (服务可能未就绪): {m}\n"))
        threading.Thread(target=_req, daemon=True).start()

if __name__ == '__main__':
    root = tk.Tk()
    
    # --- 授权验证开始 ---
    root.withdraw() # 先隐藏主窗口
    
    # 1. 自动尝试加载本地授权并验证
    code = auth_manager.load_license()
    success = False
    
    if code:
        # 有本地存档，尝试激活验证（确保未过期且未被挤下线）
        try:
            success, data = auth_manager.activate(code)
        except:
            success = False
    
    if not success:
        # 需要用户输入
        while True:
            # 弹窗提示输入
            code = simpledialog.askstring("软件激活", "请输入授权码进行激活：\n(未激活或授权已过期)", parent=root)
            if not code:
                sys.exit() # 用户取消或关闭窗口，直接退出程序
            
            code = code.strip()
            success, data = auth_manager.activate(code)
            if success:
                license_info = data.get('license') if isinstance(data, dict) else None
                expire = (license_info or {}).get('expire_date', '未知')
                if expire == '未知':
                    info = auth_manager.get_license_info()
                    expire = info.get('expire_date', '未知')
                messagebox.showinfo("激活成功", f"授权激活成功！\n有效期至: {expire}", parent=root)
                break
            else:
                messagebox.showerror("激活失败", f"错误信息: {data}", parent=root)
    
    root.deiconify() # 验证通过，显示主窗口
    # --- 授权验证结束 ---

    app = App(root)
    root.mainloop()
