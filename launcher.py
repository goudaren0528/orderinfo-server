import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog
import json
import subprocess
import threading
import os
import sys
import time
import requests
import pystray
from PIL import Image, ImageDraw
import webbrowser
import re

CONFIG_FILE = 'config.json'
if getattr(sys, 'frozen', False):
    # 如果是打包后的 exe，配置文件在 exe 同级目录
    CONFIG_FILE = os.path.join(os.path.dirname(sys.executable), 'config.json')
else:
    # 如果是源码运行，配置文件在脚本同级目录
    CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

APP_TITLE = "租帮宝 - 多后台订单监控助手"

class ConfigManager:
    @staticmethod
    def load():
        if not os.path.exists(CONFIG_FILE):
            return {"sites": [], "webhook_urls": [], "feishu_webhook_urls": [], "alert_webhook_urls": [], "interval": 60, "desktop_notify": True}
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"sites": data, "webhook_urls": [], "feishu_webhook_urls": [], "alert_webhook_urls": [], "interval": 60, "desktop_notify": True}
                # 补全默认字段
                if "webhook_urls" not in data: data["webhook_urls"] = []
                if "feishu_webhook_urls" not in data: data["feishu_webhook_urls"] = []
                if "alert_webhook_urls" not in data: data["alert_webhook_urls"] = []
                if "interval" not in data: data["interval"] = 60
                if "desktop_notify" not in data: data["desktop_notify"] = True
                return data
        except Exception as e:
            messagebox.showerror("错误", f"配置文件读取失败: {e}")
            return {"sites": [], "webhook_urls": [], "feishu_webhook_urls": [], "alert_webhook_urls": [], "interval": 60, "desktop_notify": True}

    @staticmethod
    def save(data):
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
        
        self.create_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_closing)
        
        # 初始化并启动托盘图标（常驻）
        self.start_tray_icon()

    def start_tray_icon(self):
        # 创建图标图像
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

        # Tab 5: 使用说明
        self.help_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(self.help_tab, text="使用说明")
        self.init_help_tab(self.help_tab)

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
        columns = ("name", "url", "user")
        self.tree = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        self.tree.heading("name", text="站点名称")
        self.tree.heading("url", text="登录地址")
        self.tree.heading("user", text="用户名")
        self.tree.column("name", width=150)
        self.tree.column("url", width=400)
        self.tree.column("user", width=150)
        
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scrollbar.set)
        
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        btn_frame = ttk.Frame(parent, padding=5)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        
        ttk.Button(btn_frame, text="添加站点", command=self.add_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="编辑选中", command=self.edit_site).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="删除选中", command=self.delete_site).pack(side=tk.LEFT, padx=5)
        
        self.refresh_site_list()

    def init_settings_tab(self, parent):
        # 1. 运行参数
        param_frame = ttk.LabelFrame(parent, text="基础设置", padding=10)
        param_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(param_frame, text="监控轮询间隔 (秒):").grid(row=0, column=0, padx=5, sticky='w')
        self.interval_var = tk.IntVar(value=self.config.get('interval', 60))
        ttk.Entry(param_frame, textvariable=self.interval_var, width=10).grid(row=0, column=1, padx=5, sticky='w')
        ttk.Label(param_frame, text="(最低 30 秒)").grid(row=0, column=2, padx=5, sticky='w')
        
        self.desktop_notify_var = tk.BooleanVar(value=self.config.get('desktop_notify', True))
        ttk.Checkbutton(param_frame, text="开启桌面气泡通知", variable=self.desktop_notify_var).grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky='w')

        # 夜间模式配置
        self.night_mode_var = tk.BooleanVar(value=self.config.get('night_mode', False))
        ttk.Checkbutton(param_frame, text="开启夜间免打扰模式", variable=self.night_mode_var).grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky='w')

        night_frame = ttk.Frame(param_frame)
        night_frame.grid(row=3, column=0, columnspan=3, padx=5, sticky='w')
        
        ttk.Label(night_frame, text="静默时段 (小时):").pack(side=tk.LEFT)
        
        self.night_start_var = tk.IntVar(value=self.config.get('night_period', {}).get('start', 0))
        ttk.Spinbox(night_frame, from_=0, to=23, textvariable=self.night_start_var, width=5).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(night_frame, text="至").pack(side=tk.LEFT)
        
        self.night_end_var = tk.IntVar(value=self.config.get('night_period', {}).get('end', 7))
        ttk.Spinbox(night_frame, from_=0, to=23, textvariable=self.night_end_var, width=5).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(night_frame, text="(结束小时不含)").pack(side=tk.LEFT)
        
        ttk.Button(param_frame, text="保存参数", command=self.save_settings).grid(row=0, column=3, rowspan=4, padx=20)

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
        help_text = """
【租帮宝 - 用户操作指南】

1. 快速开始
   - 在“站点管理”中添加您的后台账号。
   - 在“高级设置”中配置接收通知的 Webhook 地址（支持企业微信和飞书）。
   - 切换回“运行监控”页，点击“启动监控服务”。

2. 运行监控
   - 列表会实时显示各平台的待处理订单数。
   - 双击列表项或查看“操作”列，可快速跳转到后台处理。
   - 状态栏显示服务运行状态。

3. 桌面交互
   - 点击右上角关闭按钮，可以选择“最小化到托盘”或“退出程序”。
   - 托盘图标（右下角）常驻运行，右键可显示主界面或退出。
   - 有新订单或需要人工介入时，右下角会弹出气泡提示（需在高级设置中开启）。

4. 浏览器辅助
   - 默认浏览器在后台运行。
   - 如果遇到验证码或需要人工登录，点击“显示浏览器界面”。
   - 操作完成后，点击“隐藏/移出屏幕”即可。

5. 常见问题
   - 为什么显示 0 单？可能是因为账号未登录或确实没有订单。尝试显示浏览器界面确认登录状态。
   - 为什么收不到通知？请检查 Webhook 地址是否正确，以及是否开启了通知开关。
        """
        txt = scrolledtext.ScrolledText(parent, font=('微软雅黑', 10), padx=20, pady=20)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert(tk.END, help_text)
        txt.configure(state='disabled')

    # === 逻辑处理 ===

    def refresh_site_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for site in self.config['sites']:
            self.tree.insert('', 'end', values=(site['name'], site['login_url'], site['username']))

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
            
            # 保存夜间模式设置
            self.config['night_mode'] = self.night_mode_var.get()
            start = self.night_start_var.get()
            end = self.night_end_var.get()
            
            if start < 0 or start > 23 or end < 0 or end > 23:
                messagebox.showwarning("提示", "时间段必须在 0-23 之间")
                return
                
            self.config['night_period'] = {"start": start, "end": end}
            
            ConfigManager.save(self.config)
            
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

    def edit_site(self):
        sel = self.tree.selection()
        if not sel: return
        item = self.tree.item(sel[0])
        name = item['values'][0]
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
        edit_win.geometry("600x600")
        
        fields = [("站点名称", "name"), ("登录地址", "login_url"), ("用户名", "username"), ("密码", "password")]
        entries = {}
        row = 0
        for label, key in fields:
            ttk.Label(edit_win, text=label).grid(row=row, column=0, padx=10, pady=5, sticky='e')
            entry = ttk.Entry(edit_win, width=50)
            entry.grid(row=row, column=1, padx=10, pady=5)
            if site_data: entry.insert(0, site_data.get(key, ""))
            entries[key] = entry
            row += 1
            
        ttk.Label(edit_win, text="选择器配置 (JSON)").grid(row=row, column=0, padx=10, pady=5, sticky='ne')
        txt_selectors = scrolledtext.ScrolledText(edit_win, width=50, height=15)
        txt_selectors.grid(row=row, column=1, padx=10, pady=5)
        
        default_selectors = {"username_input": "", "password_input": "", "login_button": "", "order_menu_link": "", "pending_tab_selector": "", "pending_count_element": ""}
        if site_data:
            txt_selectors.insert('1.0', json.dumps(site_data.get('selectors', {}), indent=2, ensure_ascii=False))
        else:
            txt_selectors.insert('1.0', json.dumps(default_selectors, indent=2, ensure_ascii=False))

        def save():
            new_data = {}
            for k, e in entries.items():
                new_data[k] = e.get()
                if not new_data[k] and k != "password":
                    messagebox.showerror("错误", f"{k} 不能为空")
                    return
            
            try:
                sel_json = txt_selectors.get('1.0', tk.END).strip()
                new_data['selectors'] = json.loads(sel_json)
            except json.JSONDecodeError as e:
                messagebox.showerror("错误", f"选择器 JSON 格式错误: {e}")
                return

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
                return
            except Exception as e:
                pass # 解析失败则照常打印
        
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
            self.notify("租帮宝 - 新订单提醒", "检测到有待处理订单，请及时查看！")

    def on_monitor_double_click(self, event):
        item = self.monitor_tree.selection()
        if not item: return
        values = self.monitor_tree.item(item[0], 'values')
        name = values[0]
        link = self.site_links.get(name)
        if link:
            webbrowser.open(link)

    def start_process(self):
        self.is_stopping = False
        self.log("\n=== 正在启动监控服务... ===\n")
        self.lbl_status.config(text="状态: 运行中", foreground="green")
        self.btn_start.config(text="停止监控服务")
        
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
        
        # 按钮区域
        btn_frame = ttk.Frame(dialog, padding=10)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        def do_view():
            self.show_browser()
            dialog.destroy()
            # 尝试激活主窗口
            self.root.deiconify()
            
        def do_close():
            dialog.destroy()
            
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
                self.root.after(0, lambda: self.log(f"请求失败 (服务可能未就绪): {e}\n"))
        threading.Thread(target=_req, daemon=True).start()

if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    root.mainloop()
