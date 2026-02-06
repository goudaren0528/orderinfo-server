import json
import time
import schedule
import requests
import os
import re
import threading
import queue
import random
import subprocess
import sys
import socket
from urllib.parse import urlparse, urljoin
from typing import Any, cast

# 强制 stdout 使用行缓冲，确保 GUI 能实时获取日志
if sys.stdout is not None:
    try:
        stdout = cast(Any, sys.stdout)
        if hasattr(stdout, "reconfigure"):
            stdout.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

# 在导入 playwright 之前设置浏览器路径环境变量
# 这样打包后可以读取内置的浏览器
if getattr(sys, 'frozen', False):
    # 打包环境 (sys.executable 是 exe 路径)
    # 优先检查 EXE 同级目录下的 playwright-browsers (便携模式)
    # 如果不存在，不设置环境变量，让 Playwright 使用默认系统路径 (用户本地安装的)
    base_dir = os.path.dirname(sys.executable)
    
    # 兼容后端 onedir 模式：如果在 backend 子目录下，向上查找一级
    if os.path.basename(base_dir).lower() == 'backend':
        base_dir = os.path.dirname(base_dir)
        
    bundled_browsers = os.path.join(base_dir, 'playwright-browsers')
    if os.path.exists(bundled_browsers) and os.path.isdir(bundled_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled_browsers
        print(f"使用内置浏览器路径: {bundled_browsers}")
    else:
        print("未检测到内置浏览器，尝试使用系统默认路径...")
elif os.path.exists(os.path.join(os.getcwd(), 'playwright-browsers')):
    # 开发环境 (如果当前目录下有 playwright-browsers 文件夹)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(os.getcwd(), 'playwright-browsers')

from playwright.sync_api import sync_playwright
from datetime import datetime
from web_server import run_server as start_web_server
import shared
from auth import auth_manager

# 企业微信机器人的 Webhook 地址
# 1. 订单通知机器人 (日常战报) - 支持配置多个 Webhook URL (列表格式)
WECOM_WEBHOOK_URL: list[str] = []

# 2. 人工介入通知机器人 (紧急提醒) - 支持配置多个 Webhook URL
# 如果需要区分通知群，请修改此处的 Key；如果不需要，保持与上面一致即可
WECOM_WEBHOOK_URL_ALERT: list[str] = []

# 本机 IP 或服务器公网 IP (用于生成更新链接)
SERVER_IP = os.environ.get("WEB_SERVER_HOST_IP", "localhost") # 如果在云服务器，请修改为公网IP
SERVER_PORT = int(os.environ.get("WEB_SERVER_PORT", 5000))

def get_config_path():
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        # 兼容后端 onedir 模式：如果在 backend 子目录下，优先读取上级目录的 config.json
        if os.path.basename(exe_dir).lower() == 'backend':
            parent_config = os.path.join(os.path.dirname(exe_dir), 'config.json')
            if os.path.exists(parent_config):
                return parent_config
        return os.path.join(exe_dir, 'config.json')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


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


_config_cache = None
_config_cache_ts = 0
_last_runtime_auth_ts = 0
_last_runtime_auth_ok = False


def load_config():
    """读取配置文件"""
    global _config_cache, _config_cache_ts
    if _config_cache is not None and time.time() - _config_cache_ts < 120:
        return _config_cache
    if not auth_manager.load_license():
        _config_cache = _normalize_config({})
        _config_cache_ts = time.time()
        return _config_cache
    success, data = auth_manager.fetch_config()
    if success:
        payload = data if isinstance(data, dict) else {}
        common_config = payload.get("common_config") or {}
        user_config = payload.get("user_config") or {}
        merged = _merge_configs(common_config, user_config)
        try:
            _atomic_write_json(get_config_path(), merged)
        except Exception:
            pass
        _config_cache = merged
        _config_cache_ts = time.time()
        return _config_cache
    _config_cache = _normalize_config({})
    _config_cache_ts = time.time()
    return _config_cache


def _ensure_runtime_authorized():
    global _last_runtime_auth_ts, _last_runtime_auth_ok
    now = time.time()
    if _last_runtime_auth_ok and now - _last_runtime_auth_ts < 120:
        return True
    ok, msg = auth_manager.heartbeat()
    _last_runtime_auth_ts = now
    _last_runtime_auth_ok = ok
    if not ok:
        print(f"授权验证失败: {msg}")
    return ok

def _atomic_write_json(file_path, data):
    tmp_path = f"{file_path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)

def _sanitize_selector_value(v):
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("`") and s.endswith("`"):
            s = s[1:-1].strip()
        while s.endswith(")"):
            s = s[:-1].rstrip()
        return s
    return v

def _update_site_selectors_in_config(site_name, selectors_update):
    if not site_name or not isinstance(selectors_update, dict):
        return False

    sanitized = {k: _sanitize_selector_value(v) for k, v in selectors_update.items() if v}
    selectors_update = {k: v for k, v in sanitized.items() if v}
    if not selectors_update:
        return False
    
    print(f"[{site_name}] 正在尝试保存 selectors: {json.dumps(selectors_update, ensure_ascii=False)}")

    config_path = get_config_path()
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        raw = {"sites": []}

    is_list_format = isinstance(raw, list)
    config = {"sites": raw} if is_list_format else (raw if isinstance(raw, dict) else {"sites": []})
    sites = config.get("sites", [])
    if not isinstance(sites, list):
        sites = []
        config["sites"] = sites

    updated = False
    target_name = (site_name or "").strip()
    for s in sites:
        if not isinstance(s, dict):
            continue
        if (s.get("name") or "").strip() != target_name:
            continue
        existing = s.get("selectors", {})
        if not isinstance(existing, dict):
            existing = {}
        existing.update(selectors_update)
        s["selectors"] = existing
        updated = True
        break

    if not updated:
        print(f"[{site_name}] 未在配置中找到同名站点，未写回: {config_path}")
        return False

    try:
        to_write = sites if is_list_format else config
        _atomic_write_json(config_path, to_write)
        print(f"[{site_name}] 选择器已写回配置: {config_path}")
        if auth_manager.load_license():
            auth_manager.save_user_config(config if not is_list_format else {"sites": sites})
        return True
    except Exception as e:
        print(f"[{site_name}] 站点选择器写回配置失败: {e}")
        return False

def _css_selector_for_element(page, element_handle, prefer_clickable=False):
    if element_handle is None:
        return None

    js = r"""
    (el, preferClickable) => {
      const cssEscape = (v) => {
        if (window.CSS && CSS.escape) return CSS.escape(v);
        return String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
      };

      const cssPath = (node) => {
        if (!node || node.nodeType !== 1) return null;
        if (node.id) return "#" + cssEscape(node.id);
        const parts = [];
        let cur = node;
        let depth = 0;
        while (cur && cur.nodeType === 1 && depth < 8) {
          let part = cur.nodeName.toLowerCase();

          const attrCandidates = [
            ["name", cur.getAttribute && cur.getAttribute("name")],
            ["aria-label", cur.getAttribute && cur.getAttribute("aria-label")],
            ["placeholder", cur.getAttribute && cur.getAttribute("placeholder")],
            ["role", cur.getAttribute && cur.getAttribute("role")],
            ["type", cur.getAttribute && cur.getAttribute("type")]
          ];
          for (const [k, v] of attrCandidates) {
            if (v && typeof v === "string" && v.length <= 64) {
              part += `[${k}="${cssEscape(v)}"]`;
              break;
            }
          }

          let nth = 1;
          let sib = cur;
          while ((sib = sib.previousElementSibling)) {
            if (sib.nodeName === cur.nodeName) nth++;
          }
          part += `:nth-of-type(${nth})`;
          parts.unshift(part);

          if (cur.parentElement && cur.parentElement.id) {
            parts.unshift("#" + cssEscape(cur.parentElement.id));
            break;
          }
          cur = cur.parentElement;
          depth++;
        }
        return parts.join(" > ");
      };

      const clickableSel = 'button,a,[role="button"],[role="tab"],input[type="submit"],div[role="button"]';
      const base = preferClickable ? (el.closest(clickableSel) || el) : el;
      return cssPath(base);
    }
    """
    try:
        return page.evaluate(js, element_handle, bool(prefer_clickable))
    except Exception:
        return None

def _auto_discover_order_selectors(page):
    text_candidates = ["待审核", "待处理", "待审批", "待确认"]
    for txt in text_candidates:
        try:
            loc = page.get_by_text(txt, exact=False).first
            if not loc.is_visible(timeout=800):
                continue
            eh = loc.element_handle()
            if eh is None:
                continue
            tab_css = _css_selector_for_element(page, eh, prefer_clickable=True)
            if not tab_css:
                continue

            count_css = None
            try:
                count_css = page.evaluate(r"""
                (el) => {
                  const cssEscape = (v) => {
                    if (window.CSS && CSS.escape) return CSS.escape(v);
                    return String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
                  };
                  const cssPath = (node) => {
                    if (!node || node.nodeType !== 1) return null;
                    if (node.id) return "#" + cssEscape(node.id);
                    const parts = [];
                    let cur = node;
                    let depth = 0;
                    while (cur && cur.nodeType === 1 && depth < 8) {
                      let part = cur.nodeName.toLowerCase();
                      let nth = 1;
                      let sib = cur;
                      while ((sib = sib.previousElementSibling)) {
                        if (sib.nodeName === cur.nodeName) nth++;
                      }
                      part += `:nth-of-type(${nth})`;
                      parts.unshift(part);
                      if (cur.parentElement && cur.parentElement.id) {
                        parts.unshift("#" + cssEscape(cur.parentElement.id));
                        break;
                      }
                      cur = cur.parentElement;
                      depth++;
                    }
                    return parts.join(" > ");
                  };

                  const clickableSel = 'button,a,[role="button"],[role="tab"],input[type="submit"],div[role="button"]';
                  const base = el.closest(clickableSel) || el;
                  const nodes = base.querySelectorAll("*");
                  for (const n of nodes) {
                    const t = (n.textContent || "").trim();
                    if (!t) continue;
                    if (t.length > 12) continue;
                    if (/\d+/.test(t)) {
                      return cssPath(n);
                    }
                  }
                  return null;
                }
                """, eh)
            except Exception:
                count_css = None

            if not count_css:
                count_css = tab_css
            return {"pending_tab_selector": tab_css, "pending_count_element": count_css}
        except Exception:
            continue
    return {}

def get_webhook_urls(alert=False):
    """获取 Webhook URLs"""
    config = load_config()
    if alert:
        return config.get("alert_webhook_urls", [])
    return config.get("webhook_urls", [])

def get_feishu_webhook_urls():
    """获取飞书 Webhook URLs"""
    config = load_config()
    return config.get("feishu_webhook_urls", [])

def is_night_mode_active():
    """检查是否处于夜间静默模式"""
    try:
        config = load_config()
        if not config.get("night_mode", False):
            return False
            
        period = config.get("night_period", {"start": 0, "end": 7})
        current_hour = datetime.now().hour
        
        start = int(period.get("start", 0))
        end = int(period.get("end", 7))
        
        # 处理跨天情况 (例如 23:00 到 07:00)
        if start > end:
            if current_hour >= start or current_hour < end:
                return True
        else:
            if start <= current_hour < end:
                return True
                
        return False
    except Exception as e:
        print(f"检查夜间模式失败: {e}")
        return False

def send_feishu_notification(content, title="租帮宝通知", webhook_url=None):
    """发送飞书通知
    Args:
        content: 通知内容
        title: 消息标题
        webhook_url: 指定的 Webhook URL (支持字符串或列表)
    """
    # 检查夜间模式
    if is_night_mode_active():
        print(f"夜间静默模式生效中，跳过飞书通知: {title}")
        return

    target_urls = webhook_url if webhook_url else get_feishu_webhook_urls()
    
    if isinstance(target_urls, str):
        target_urls = [target_urls]
    elif not isinstance(target_urls, list):
        return

    headers = {"Content-Type": "application/json"}
    
    # 构造富文本消息
    data = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [
                        [
                            {"tag": "text", "text": content}
                        ]
                    ]
                }
            }
        }
    }
    
    for url in target_urls:
        if not url: continue
        try:
            requests.post(url, json=data, headers=headers)
        except Exception as e:
            print(f"飞书通知发送失败: {e}")

def send_wecom_notification(content, msg_type="text", webhook_url=None):
    """发送企业微信通知
    Args:
        content: 通知内容
        msg_type: 消息类型 ("text" 或 "markdown")
        webhook_url: 指定的 Webhook URL (支持字符串或列表)，如果不传则使用默认的订单通知 URL
    """
    # 检查夜间模式
    if is_night_mode_active():
        print(f"夜间静默模式生效中，跳过企业微信通知")
        return

    target_urls = webhook_url if webhook_url else get_webhook_urls()
    
    # 统一转换为列表处理
    if isinstance(target_urls, str):
        target_urls = [target_urls]
    elif not isinstance(target_urls, list):
        print(f"无效的 Webhook URL 格式: {type(target_urls)}")
        return

    headers = {"Content-Type": "application/json"}
    
    if msg_type == "markdown":
        data = {
            "msgtype": "markdown",
            "markdown": {
                "content": content
            }
        }
    else:
        data = {
            "msgtype": "text",
            "text": {
                "content": content,
                "mentioned_list": ["@all"]
            }
        }
        
    for url in target_urls:
        if "YOUR_KEY_HERE" in url:
            print("请在脚本中配置正确的企业微信 Webhook URL")
            print(f"模拟发送通知: {content}")
            continue

        try:
            response = requests.post(url, json=data, headers=headers)
            if response.status_code == 200:
                print(f"通知发送成功 (Key: ...{url[-6:]})")
            else:
                print(f"通知发送失败 (Key: ...{url[-6:]}): {response.text}")
        except Exception as e:
            print(f"发送通知出错 (Key: ...{url[-6:]}): {e}")

def is_url(text):
    """判断字符串是否为URL"""
    return text and (text.startswith('http://') or text.startswith('https://'))

def _is_login_like_url(url):
    u = (url or "").lower()
    return ("login" in u) or ("signin" in u) or ("sign-in" in u)

def _is_order_like_url(url):
    u = (url or "").lower()
    if not u:
        return False
    keywords = [
        "order", "orders",
        "trade", "list", "order.list", "r=order.list",
        "/order", "/orders"
    ]
    for k in keywords:
        if k in u:
            return True
    return False

def _should_accept_order_url(login_url, candidate_url):
    if not is_url(candidate_url):
        return False
    if _is_login_like_url(candidate_url):
        return False
    if login_url and is_url(login_url):
        host_login = _extract_hostname(login_url)
        host_candidate = _extract_hostname(candidate_url)
        if host_login and host_candidate and host_login != host_candidate:
            return False
    return True

def _extract_hostname(url: str):
    try:
        parsed = urlparse(url)
        return (parsed.hostname or "").lower() or None
    except Exception:
        return None

def clear_site_cookies_preserve_others(context, site, selectors):
    try:
        hosts = set()
        login_url = site.get('login_url')
        if login_url and is_url(login_url):
            host = _extract_hostname(login_url)
            if host:
                hosts.add(host)

        order_menu_link = selectors.get('order_menu_link') if selectors else None
        if order_menu_link and is_url(order_menu_link):
            host = _extract_hostname(order_menu_link)
            if host:
                hosts.add(host)

        if not hosts:
            return

        all_cookies = context.cookies()
        keep_cookies = []
        removed_any = False

        for cookie in all_cookies:
            domain = (cookie.get('domain') or "").lstrip('.').lower()
            if not domain:
                keep_cookies.append(cookie)
                continue

            belongs = False
            for host in hosts:
                if domain == host or domain.endswith("." + host) or host.endswith("." + domain):
                    belongs = True
                    break

            if belongs:
                removed_any = True
            else:
                keep_cookies.append(cookie)

        if not removed_any:
            return

        normalized_keep = []
        for cookie in keep_cookies:
            c = dict(cookie)
            if c.get('expires') == -1:
                c.pop('expires', None)
            normalized_keep.append(c)

        context.clear_cookies()
        if normalized_keep:
            context.add_cookies(normalized_keep)
    except Exception:
        return

def save_global_cookies(context):
    """保存当前所有 Cookies (包括会话 Cookie) 到文件"""
    try:
        if not os.path.exists('cookies'):
            os.makedirs('cookies')
        # 获取 storage_state (包含 cookies 和 localStorage)
        state = context.storage_state()
        with open('cookies/global_state.json', 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"保存全局状态失败: {e}")

def load_global_cookies(context):
    """从文件恢复 Cookies 和 LocalStorage"""
    try:
        if os.path.exists('cookies/global_state.json'):
            with open('cookies/global_state.json', 'r', encoding='utf-8') as f:
                state = json.load(f)
            
            # 恢复 Cookies
            if 'cookies' in state:
                context.add_cookies(state['cookies'])
                print(f"已恢复 {len(state['cookies'])} 个 Cookies (含会话 Cookie)")

            # 恢复 LocalStorage (需要在页面上下文中执行)
            # 注意：这需要一个 Page 对象，或者我们在每个页面打开时注入。
            # 由于 context 本身没有 add_init_script 来直接注入 localStorage 到所有域名（它按域名隔离），
            # 这里的最佳实践是：BrowserManager 在创建新页面时，如果发现有 localStorage 数据，尝试注入。
            # 但更简单的办法是：launch_persistent_context 应该自动处理了 user_data_dir 里的数据。
            # 如果我们依赖 global_state.json，说明 user_data_dir 可能失效或我们想强制覆盖。
            
            # 考虑到 Playwright Python API 的限制，恢复 localStorage 比较麻烦。
            # 既然我们用了 persistent_context，主要还是依赖 user_data_dir。
            # 这里先不强行注入 localStorage，以免覆盖了 user_data_dir 里可能更新的数据。
            # 但为了解决“诚赁”掉登录问题，我们可以在 BrowserManager.get_page 创建新页面后，
            # 针对该特定域名尝试恢复 localStorage。
            
            # 暂时保持现状，但依赖下面的逻辑优化：总是尝试直接访问订单页。
    except Exception as e:
        print(f"恢复全局状态失败: {e}")

def handle_popups(page, site_name=""):
    """尝试关闭常见的弹窗/遮罩"""
    try:
        # 1. 尝试按 ESC 键 (通用的关闭弹窗方式)
        page.keyboard.press('Escape')
        time.sleep(0.5)

        # 2. 查找并点击常见的关闭按钮 (Element UI, Ant Design 等)
        # 常见的关闭按钮选择器
        close_selectors = [
            '.el-message-box__headerbtn',       # Element UI 弹窗关闭按钮
            '.el-dialog__headerbtn',            # Element UI 对话框关闭按钮
            'button[aria-label="Close"]',       # 通用
            '.ant-modal-close',                 # Ant Design
            '.close-btn',                       # 通用类名
            '.layui-layer-close'                # Layui
        ]
        
        for selector in close_selectors:
            if page.is_visible(selector):
                print(f"[{site_name}] 发现弹窗关闭按钮: {selector}，尝试点击...")
                page.click(selector)
                time.sleep(1)
                
    except Exception as e:
        print(f"[{site_name}] 处理弹窗时出错 (非致命): {e}")

def process_window_events(manager):
    """处理窗口控制队列"""
    try:
        while not shared.window_control_queue.empty():
            cmd = shared.window_control_queue.get_nowait()
            if manager:
                if cmd == "show":
                    manager.move_browser_onscreen()
                elif cmd == "hide":
                    manager.move_browser_offscreen()
    except Exception as e:
        print(f"处理窗口队列出错: {e}")

def check_orders(context_or_manager=None):
    """核心任务：轮询所有后台并抓取数据
    Args:
        context_or_manager: 可选的 BrowserManager 实例或 Context，如果提供则复用
    """
    # 0. (已移除) 时间检查：00:00 到 08:00 期间不再跳过监控，保持脚本运行以维护 Cookie 活性
    # 但通知发送环节会进行静默处理
    
    # 兼容性处理：区分 BrowserManager 和 Context
    manager = None
    context = None
    
    if hasattr(context_or_manager, 'get_page'):
        manager = context_or_manager
        context = manager.get_context()
    else:
        context = context_or_manager

    if not _ensure_runtime_authorized():
        return
    config = load_config()
    sites = config.get('sites', [])
    config_keep_page_alive_all = bool(config.get('keep_page_alive_all'))
    config_headless = bool(config.get('headless', False))
    if not sites:
        print("未找到配置，跳过本次执行")
        return

    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始检查订单...")
    
    # 确保 cookies 目录存在
    if not os.path.exists('cookies'):
        os.makedirs('cookies')

    results = []
    
    # 如果没有传入 manager/context (通常是单独运行此函数调试时)，则创建临时的
    local_playwright = None
    if context is None:
        local_playwright = sync_playwright().start()
        # 确保 browser_data 目录存在
        user_data_dir = os.path.join(os.path.expanduser('~'), 'order_info_browser_data')
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir)
        
        print(f"浏览器数据目录: {user_data_dir}")
        try:
            context = local_playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=config_headless,
                args=[
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-infobars',
                    '--disable-blink-features=AutomationControlled'
                ]
            )
        except Exception as e:
            print(f"[X] 启动浏览器失败: {e}")
            if local_playwright: local_playwright.stop()
            return

    try:
        # 1. 优先尝试处理窗口事件（避免阻塞后续的长时间页面加载）
        process_window_events(manager)

        for site in sites:
            if not isinstance(site, dict):
                 print(f"警告: site 配置格式错误 (类型: {type(site)})，跳过: {site}")
                 continue

            if not site.get('enabled', True):
                print(f"[{site.get('name', '未知')}] 监控已禁用，跳过。")
                continue

            page = None
            try:
                print(f"[{site['name']}] 正在处理...")
                
                # 1. 尝试加载 Cookie (兼容旧逻辑)
                safe_name = re.sub(r'[^\w\-]', '_', site['name'])
                cookie_path = os.path.join('cookies', f"{safe_name}_state.json")
                
                # 获取页面：如果提供了 manager 则复用，否则新建
                if manager:
                    page = manager.get_page(site['name'])
                else:
                    page = context.new_page()
                    
                selectors = site['selectors']
                auto_selectors = {}
                if 'keep_page_alive' in site:
                    keep_page_alive = bool(site.get('keep_page_alive'))
                else:
                    keep_page_alive = config_keep_page_alive_all

                # 2. 尝试直接访问业务页面
                # 策略调整：不再依赖旧的 cookie 文件判断，而是假设我们有状态，优先尝试访问业务页面。
                # 如果 BrowserManager 保持了状态（user_data_dir），那么就能直接进去。
                # 如果进不去（被重定向到登录页），我们在后面会被检测到，然后再走登录流程。
                
                target_url = site['login_url']
                direct_access_attempted = False
                
                # 如果配置了订单菜单链接，优先尝试直接访问
                order_menu_link = selectors.get('order_menu_link')
                if order_menu_link and is_url(order_menu_link):
                    target_url = order_menu_link
                    direct_access_attempted = True
                    print(f"[{site['name']}] 尝试直接访问订单页面: {target_url}")
                else:
                    # 如果 order_menu_link 不是 URL (是选择器)，那只能先去登录页或者主页
                    print(f"[{site['name']}] 访问入口页: {target_url}")

                # 判断是否需要导航 (用户要求避免强制刷新)
                should_navigate = True
                if keep_page_alive:
                    try:
                        current_url = page.url
                        # 如果当前URL包含 target_url (忽略协议头等差异) 或者已经在订单页
                        # 简单的包含关系判断
                        check_target = target_url.split('://')[-1] if '://' in target_url else target_url
                        check_menu = order_menu_link.split('://')[-1] if order_menu_link and '://' in order_menu_link else (order_menu_link or "")
                        
                        if (check_target in current_url) or (check_menu and check_menu in current_url):
                            # 且当前不是登录页
                            login_part = site['login_url'].split('://')[-1] if '://' in site['login_url'] else site['login_url']
                            # 简单的包含检查可能不准，但对于带 path 的 login_url 应该够用
                            # 如果 current_url 包含 login_part，说明在登录页，必须刷新或重试
                            # 但注意：Zero-zero Enjoy 的 login_url 是 .../user/login，order 是 .../OrderList
                            # 它们可能有公共前缀。所以我们要看 login_part 是否 *特定* 于登录页
                            
                            is_login_page = False
                            if "login" in current_url.lower() or "signin" in current_url.lower():
                                is_login_page = True
                                
                            if not is_login_page:
                                print(f"[{site['name']}] 页面已在目标地址，跳过刷新 (URL: {current_url})")
                                should_navigate = False
                    except:
                        pass

                if should_navigate:
                    # 增加重试机制 (最多 3 次)
                    max_retries = 3
                    for attempt in range(1, max_retries + 1):
                        try:
                            # 使用 domcontentloaded 以防止被无关资源（如图片、统计代码）阻塞
                            print(f"[{site['name']}] 正在加载页面 (尝试 {attempt}/{max_retries})...")
                            page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
                            
                            # 显式等待 Tab 出现，给页面加载留出时间
                            if selectors.get('pending_tab_selector'):
                                try:
                                    page.wait_for_selector(selectors['pending_tab_selector'], timeout=5000, state='visible')
                                except:
                                    pass # 就算没等到，后面也会有 check_selector 的判断逻辑
                            else:
                                # 兼容旧逻辑，等待网络空闲
                                try:
                                    page.wait_for_load_state('networkidle', timeout=5000)
                                except:
                                    pass
                            
                            # 如果成功加载（没有抛出异常），跳出重试循环
                            break
                        except Exception as e:
                            print(f"[{site['name']}] 页面加载第 {attempt} 次失败: {e}")
                            if attempt < max_retries:
                                print(f"[{site['name']}] 等待 3 秒后重试...")
                                time.sleep(3)
                            else:
                                print(f"[{site['name']}] 页面加载最终失败，跳过此站点。")
                    else:
                        # 如果循环正常结束（即没有 break），说明最终失败
                        continue # 跳过当前 site，进入下一个 loop
                else:
                    # 如果跳过导航，仍需刷新页面以获取最新数据 (用户提示：不刷新看不到新订单)
                    host = _extract_hostname(page.url or "") or _extract_hostname(target_url) or ""
                    if "llxzu.com" in host:
                        print(f"[{site['name']}] 检测到 llxzu.com，跳过硬刷新，改为轻量等待以避免掉线")
                        page.wait_for_timeout(1500)
                    else:
                        print(f"[{site['name']}] 页面已在目标地址，执行原位刷新 (Reload) 以更新数据...")
                        try:
                            page.reload(wait_until='domcontentloaded', timeout=30000)
                            page.wait_for_timeout(2000)
                        except Exception as e:
                            print(f"[{site['name']}] 刷新失败: {e}")

                # 3. 检查是否已经登录
                # 优先使用 Tab 标签作为登录判断依据（因为它通常总是可见的，而具体数量元素可能在非激活 Tab 中被隐藏）
                check_selector = selectors.get('pending_tab_selector')
                
                # 如果找不到 Tab 选择器，回退到旧逻辑
                if not check_selector:
                    check_selector = selectors.get('order_menu_link')
                    if is_url(check_selector):
                        check_selector = selectors.get('pending_count_element')
                    else:
                        check_selector = check_selector or selectors.get('pending_count_element')

                is_logged_in = False

                # === 新增：检测“登录过期”弹窗 ===
                # 针对诚赁等站点，页面背景还在，但弹出模态框提示过期
                # 策略：如果发现含有“登录过期”或“重新登录”文本的可见元素，直接判定为 Cookie 失效
                try:
                    # 使用文本定位，更通用
                    expired_text_locators = [
                        page.get_by_text("登录过期", exact=False),
                        page.get_by_text("请重新登录", exact=False),
                        page.get_by_text("身份验证失败", exact=False),
                        page.get_by_text("登录失效", exact=False),
                        page.get_by_text("重新登陆", exact=False)
                    ]
                    for locator in expired_text_locators:
                        if locator.is_visible(timeout=500):
                            print(f"[{site['name']}] 检测到“登录过期”提示，准备清理 Cookie 重试...")
                            is_logged_in = False
                            
                            # 强制清理 Cookie 和 Storage
                            try:
                                clear_site_cookies_preserve_others(page.context, site, selectors)
                                page.evaluate("try { localStorage.clear(); sessionStorage.clear(); } catch(e) {}")
                                print(f"[{site['name']}] 已强制清理 Cookie 和 LocalStorage")
                            except Exception as clear_err:
                                print(f"[{site['name']}] 清理缓存失败: {clear_err}")
                            
                            # 既然过期了，就不必继续检测登录状态了，直接跳过下面的 check
                            # 但为了逻辑统一，我们让 is_logged_in = False 自然流转到后面的登录流程
                            # 甚至可以直接刷新页面或跳转 login_url，防止 url 没变
                            print(f"[{site['name']}] 跳转登录页...")
                            page.goto(site['login_url'])
                            page.wait_for_load_state('domcontentloaded')
                            time.sleep(2)
                            break
                except Exception as e:
                    pass # 检测出错不影响主流程
                
                # 增强的登录检测：只要能找到任意一个关键元素，就认为已登录
                # 1. 检查 Tab
                if check_selector and not is_url(check_selector) and page.is_visible(check_selector):
                    is_logged_in = True
                
                # 2. 如果没找到 Tab，尝试找“退出登录”按钮或头像等通用元素（如果有配置）
                # 这里暂时用 order_menu_link（如果是选择器且可见）作为辅助判断
                if not is_logged_in:
                     order_menu_selector = selectors.get('order_menu_link')
                     if order_menu_selector and not is_url(order_menu_selector) and page.is_visible(order_menu_selector):
                         is_logged_in = True

                # 3. 如果前两者都没找到，尝试找具体的数量元素（容错：可能 Tab 选择器变了或者被隐藏）
                if not is_logged_in:
                    count_selector = selectors.get('pending_count_element')
                    if count_selector and page.is_visible(count_selector):
                         print(f"[{site['name']}] 未找到 Tab，但找到了数量元素，判定为已登录")
                         is_logged_in = True
                
                # URL 兜底：对于 SPA/选择器不稳定的站点，如果仍在订单页且未出现登录框，视为已登录
                if not is_logged_in:
                    try:
                        current_url = page.url or ""
                        is_login_like = ("login" in current_url.lower() or "signin" in current_url.lower())
                        user_input_sel = selectors.get('username_input')
                        login_form_visible = False
                        if user_input_sel:
                            try:
                                login_form_visible = page.is_visible(user_input_sel)
                            except:
                                login_form_visible = False

                        order_menu_link = selectors.get('order_menu_link')
                        if order_menu_link and is_url(order_menu_link):
                            if (order_menu_link in current_url) and (not is_login_like) and (not login_form_visible):
                                print(f"[{site['name']}] 通过 URL 判定仍在订单页，视为已登录 (URL: {current_url})")
                                is_logged_in = True
                    except:
                        pass

                if is_logged_in:
                    print(f"[{site['name']}] 状态: 已登录 (Cookie 有效)")
                    handle_popups(page, site_name=site['name'])
                    save_global_cookies(context)
                
                # 如果直接访问失败，强制跳转登录页
                if not is_logged_in and direct_access_attempted:
                    print(f"[{site['name']}] Cookie 可能失效，重新进入登录页...")
                    try:
                        # 移除全局清理逻辑，避免误伤其他站点的 Cookie
                        # context.clear_cookies() <--- 这是一个危险操作，会清除所有站点的 Cookie！

                        # 只需要跳转登录页
                        print(f"[{site['name']}] 访问登录页: {site['login_url']}")
                        page.goto(site['login_url'])
                        # 等待页面导航完成，确保旧页面（订单页）已被替换
                        page.wait_for_load_state('domcontentloaded') 
                        
                        # 智能等待：检测是跳回了订单页（Cookie有效），还是真的到了登录页
                        # 轮询检测，最多等待 10 秒
                        found_state = False
                        for _ in range(20): # 20 * 0.5s = 10s
                            if check_selector and not is_url(check_selector) and page.is_visible(check_selector):
                                print(f"[{site['name']}] 检测到订单页面元素，判定 Cookie 依然有效（被重定向回订单页）")
                                is_logged_in = True
                                found_state = True
                                break
                            
                            user_sel = selectors.get('username_input')
                            if user_sel and page.is_visible(user_sel):
                                print(f"[{site['name']}] 成功抵达登录页，准备重新登录")
                                found_state = True
                                break
                            
                            page.wait_for_timeout(500)
                            
                        if not found_state:
                            print(f"[{site['name']}] 等待页面状态超时（既无登录框也无订单元素），可能页面加载缓慢或选择器不匹配")

                    except Exception as e:
                        print(f"[{site['name']}] 跳转登录页失败: {e}")

                # 4. 如果未登录，执行登录流程
                if not is_logged_in:
                    print(f"[{site['name']}] 状态: 未登录，尝试自动填写账号密码...")
                    
                    try:
                        # === 智能登录逻辑 ===
                        # 1. 尝试使用配置的选择器
                        found_user_input = False
                        
                        # 尝试等待配置的选择器
                        try:
                            if selectors.get('username_input'):
                                page.wait_for_selector(selectors['username_input'], timeout=5000)
                                if page.is_visible(selectors['username_input']):
                                    found_user_input = True
                        except:
                            pass

                        # 2. 如果配置的选择器未找到，尝试智能查找
                        if not found_user_input:
                            print(f"[{site['name']}] 配置的账号框未找到，尝试智能查找...")
                            # 常见的账号框特征
                            user_locators = [
                                # 属性匹配
                                "input[placeholder*='账号']",
                                "input[placeholder*='手机']",
                                "input[placeholder*='用户名']",
                                "input[name*='user']",
                                "input[name*='phone']",
                                "input[name*='mobile']",
                                "input[name*='account']",
                                # 类型匹配
                                "input[type='text']",
                                "input[type='tel']",
                                "input:not([type])" # 默认为 text
                            ]
                            
                            for loc in user_locators:
                                try:
                                    # 查找所有匹配的元素
                                    elements = page.locator(loc).all()
                                    for el in elements:
                                        if el.is_visible():
                                            # 如果是通用的 type=text，进一步检查是否像密码框（排除）
                                            # 或者是否已经在 form 中
                                            # 这里简单粗暴：只要可见且不是 hidden/disabled
                                            try:
                                                eh = page.locator(loc).first.element_handle()
                                                css = _css_selector_for_element(page, eh, prefer_clickable=False) if eh else None
                                            except Exception:
                                                css = None
                                            selectors['username_input'] = css or loc
                                            # 无论是否有 css，都保存 selector (loc 是有效的 Playwright selector)
                                            auto_selectors['username_input'] = css or loc
                                            found_user_input = True
                                            print(f"[{site['name']}] 智能匹配到账号框: {loc}")
                                            break
                                    if found_user_input: break
                                except:
                                    continue

                        if found_user_input:
                            username_value = site.get('username', '')
                            password_value = site.get('password', '')
                            # 填写账号
                            # 有些输入框可能是 React/Vue 受控组件，fill 后 value 可能没变
                            # 或者需要触发 input 事件
                            user_input_el = page.locator(selectors['username_input']).first
                            user_input_el.click()
                            user_input_el.fill(username_value)
                            
                            # 校验是否填写成功
                            if not user_input_el.input_value():
                                print(f"[{site['name']}] 检测到 fill 失败（受控组件），尝试模拟键盘输入...")
                                user_input_el.click()
                                page.keyboard.type(username_value, delay=50)

                            # 3. 智能查找密码框
                            found_pwd_input = False
                            if password_value:
                                # 优先配置
                                if selectors.get('password_input') and page.is_visible(selectors['password_input']):
                                    found_pwd_input = True
                                else:
                                    # 智能查找：type="password" 是最强的特征
                                    print(f"[{site['name']}] 尝试智能查找密码框...")
                                    try:
                                        pwd_loc = "input[type='password']"
                                        if page.locator(pwd_loc).first.is_visible():
                                            try:
                                                eh = page.locator(pwd_loc).first.element_handle()
                                                css = _css_selector_for_element(page, eh, prefer_clickable=False) if eh else None
                                            except Exception:
                                                css = None
                                            selectors['password_input'] = css or pwd_loc
                                            auto_selectors['password_input'] = css or pwd_loc
                                            found_pwd_input = True
                                            print(f"[{site['name']}] 智能匹配到密码框: {pwd_loc}")
                                    except:
                                        pass
                                
                                if found_pwd_input:
                                    pwd_input_el = page.locator(selectors['password_input']).first
                                    pwd_input_el.click()
                                    pwd_input_el.fill(password_value)
                                    
                                    # 校验
                                    if not pwd_input_el.input_value():
                                        pwd_input_el.click()
                                        page.keyboard.type(password_value, delay=50)
                                        
                                    print(f"[{site['name']}] >>> 正在点击登录...")
                                    
                                    # 4. 智能查找登录按钮
                                    # 优先配置
                                    clicked_login = False
                                    if selectors.get('login_button') and page.is_visible(selectors['login_button']):
                                        page.click(selectors['login_button'])
                                        clicked_login = True
                                    else:
                                        print(f"[{site['name']}] 尝试智能查找登录按钮...")
                                        login_btn_locators = [
                                            "button:has-text('登录')",
                                            "button:has-text('Login')",
                                            "input[type='submit']",
                                            "div[role='button']:has-text('登录')"
                                        ]
                                        for btn_loc in login_btn_locators:
                                            try:
                                                if page.locator(btn_loc).first.is_visible():
                                                    try:
                                                        eh = page.locator(btn_loc).first.element_handle()
                                                        css = _css_selector_for_element(page, eh, prefer_clickable=True) if eh else None
                                                    except Exception:
                                                        css = None
                                                    page.click(btn_loc)
                                                    print(f"[{site['name']}] 智能点击登录按钮: {btn_loc}")
                                                    selectors['login_button'] = css or btn_loc
                                                    auto_selectors['login_button'] = css or btn_loc
                                                    clicked_login = True
                                                    break
                                            except:
                                                continue
                                    
                                    if not clicked_login:
                                        # 尝试回车
                                        print(f"[{site['name']}] 未找到登录按钮，尝试按回车...")
                                        page.keyboard.press('Enter')

                                    page.wait_for_load_state('networkidle')
                                    time.sleep(2)
                                else:
                                     print(f"[{site['name']}] 未找到密码框，无法登录")
                            else:
                                print(f"[{site['name']}] 密码为空，跳过密码填充及自动登录点击")
                        else:
                            print(f"[{site['name']}] 未找到输入框，跳过自动填表")
                            
                    except Exception as e:
                        print(f"[{site['name']}] 自动填表失败: {e}")

                    # 再次检查是否登录成功
                    check_passed = False
                    if check_selector and not is_url(check_selector) and page.is_visible(check_selector):
                        check_passed = True
                    else:
                        login_btn_sel = selectors.get('login_button')
                        user_sel = selectors.get('username_input')
                        login_btn_visible = True
                        user_visible = True
                        try:
                            if login_btn_sel:
                                login_btn_visible = page.is_visible(login_btn_sel)
                        except:
                            pass
                        try:
                            if user_sel:
                                user_visible = page.is_visible(user_sel)
                        except:
                            pass
                        if (login_btn_sel and not login_btn_visible) and (user_sel and not user_visible):
                            check_passed = True

                    if check_passed:
                        is_logged_in = True
                        print(f"[{site['name']}] 自动登录成功！")
                    else:
                        # === 人工介入 (本地模式) ===
                        print(f"[{site['name']}] ⚠️ 自动登录未成功（可能需要验证码）。")
                        
                        # 尝试将浏览器移到屏幕中间方便操作
                        if manager:
                            try:
                                manager.move_browser_onscreen()
                            except:
                                pass
                                
                        print(f"[{site['name']}] >>> 等待人工手动登录 (限时 60 秒)...")
                        
                        # 准备用于检测的 selectors（合并配置的和智能发现的）
                        effective_selectors = selectors.copy()
                        effective_selectors.update(auto_selectors)

                        start_wait = time.time()
                        while time.time() - start_wait < 60:
                            # 处理窗口控制队列 (在交互等待期间也要响应显示/隐藏指令)
                            try:
                                while not shared.window_control_queue.empty():
                                    win_cmd = shared.window_control_queue.get_nowait()
                                    if manager:
                                        if win_cmd == "show":
                                            manager.move_browser_onscreen()
                                        elif win_cmd == "hide":
                                            manager.move_browser_offscreen()
                            except:
                                pass

                            check_passed_interactive = False
                            # 1. 检测是否出现了特定的登录后元素（如 Tab）
                            if check_selector and not is_url(check_selector) and page.is_visible(check_selector):
                                check_passed_interactive = True
                            
                            # 2. 检测登录框是否消失
                            if not check_passed_interactive:
                                login_btn_sel = effective_selectors.get('login_button')
                                user_sel = effective_selectors.get('username_input')
                                login_btn_visible = True
                                user_visible = True
                                
                                # 如果我们甚至不知道登录框长什么样，就没法通过“消失”来判断
                                # 但如果我们有 auto_selectors，或者配置里有，就可以判断
                                has_login_form_info = bool(login_btn_sel or user_sel)
                                
                                if has_login_form_info:
                                    try:
                                        if login_btn_sel:
                                            login_btn_visible = page.is_visible(login_btn_sel)
                                    except:
                                        pass
                                    try:
                                        if user_sel:
                                            user_visible = page.is_visible(user_sel)
                                    except:
                                        pass
                                    
                                    if (not login_btn_visible) and (not user_visible):
                                        check_passed_interactive = True
                                        print(f"[{site['name']}] 检测到登录框/按钮消失，判定为登录成功")

                            # 3. URL 兜底检测 (URL 变更且不含 login)
                            if not check_passed_interactive:
                                try:
                                    current_url_int = page.url or ""
                                    is_login_like = "login" in current_url_int.lower() or "signin" in current_url_int.lower()
                                    # 如果当前 URL 既不是配置的登录页，也不包含 login 关键字
                                    # 且登录框确实找不到了（或者本来就没找到）
                                    if (current_url_int != site['login_url']) and (not is_login_like):
                                        # 再次确认一下登录框真的不在了
                                        login_btn_sel = effective_selectors.get('login_button')
                                        user_sel = effective_selectors.get('username_input')
                                        form_visible = False
                                        if login_btn_sel and page.is_visible(login_btn_sel): form_visible = True
                                        if user_sel and page.is_visible(user_sel): form_visible = True
                                        
                                        if not form_visible:
                                            check_passed_interactive = True
                                            print(f"[{site['name']}] URL 已变更且未发现登录框，判定为登录成功 (URL: {current_url_int})")
                                except:
                                    pass

                            if check_passed_interactive:
                                is_logged_in = True
                                print(f"[{site['name']}] 人工介入成功！已登录。")
                                break
                            
                            time.sleep(0.5)
                        
                        # 操作结束，将浏览器移回屏幕外
                        if manager:
                            try:
                                manager.move_browser_offscreen()
                            except:
                                pass
                        
                        if not is_logged_in:
                            print(f"[{site['name']}] ❌ 人工介入超时，放弃本次抓取。")

                    if is_logged_in:
                        # 保存智能发现的 selector (无论是自动登录成功还是人工介入成功)
                        if auto_selectors:
                            print(f"[{site['name']}] 登录成功，保存智能发现的 selectors...")
                            try:
                                _update_site_selectors_in_config(site['name'], auto_selectors)
                                # 更新内存中的 site 对象和 selectors，避免后续逻辑使用旧数据
                                if 'selectors' not in site: site['selectors'] = {}
                                site['selectors'].update(auto_selectors)
                                selectors.update(auto_selectors)
                            except Exception as e:
                                print(f"[{site['name']}] 保存 selectors 失败: {e}")

                        # 尝试关闭可能存在的弹窗 (如工单提醒、活动通知等)

                        handle_popups(page, site_name=site['name'])
                        
                        save_global_cookies(context)
                        print(f"[{site['name']}] 全局 Cookie 已更新")

                # 4.5 智能查找订单页面入口 (如果未配置且当前不在订单页)
                if not selectors.get('order_menu_link'):
                    print(f"[{site['name']}] 未配置订单页链接，尝试查找导航菜单...")
                    menu_keywords = ["订单管理", "租赁订单", "交易管理", "我的订单", "订单列表", "全部订单"]
                    for kw in menu_keywords:
                        try:
                            target = page.get_by_text(kw, exact=True).first
                            if not target.is_visible():
                                target = page.get_by_text(kw, exact=False).first
                            
                            if target.is_visible():
                                href = None
                                try:
                                    eh = target.element_handle()
                                    if eh:
                                        href = page.evaluate("el => (el.closest('a') && el.closest('a').getAttribute('href')) || el.getAttribute('href')", eh)
                                except:
                                    href = None
                                href = _sanitize_selector_value(href)
                                if href:
                                    if not is_url(href):
                                        try:
                                            href = urljoin(page.url, href)
                                        except:
                                            pass
                                    if href and _is_order_like_url(href):
                                        print(f"[{site['name']}] 发现订单链接: {href}")
                                        selectors['order_menu_link'] = href
                                        auto_selectors['order_menu_link'] = href
                                        break

                                print(f"[{site['name']}] 发现可能的订单入口: {kw}，尝试点击...")
                                target.click()
                                page.wait_for_load_state('networkidle')
                                time.sleep(2)
                                
                                curr_url = _sanitize_selector_value(page.url or "")
                                if curr_url and curr_url != site['login_url']:
                                    order_signs = False
                                    try:
                                        if page.get_by_text("订单编号", exact=False).first.is_visible(timeout=800):
                                            order_signs = True
                                        elif page.get_by_text("订单列表", exact=False).first.is_visible(timeout=800):
                                            order_signs = True
                                        elif page.get_by_text("订单管理", exact=False).first.is_visible(timeout=800):
                                            order_signs = True
                                    except:
                                        pass
                                    
                                    if order_signs or _is_order_like_url(curr_url):
                                        print(f"[{site['name']}] 跳转成功，保存为订单页链接: {curr_url}")
                                        selectors['order_menu_link'] = curr_url
                                        auto_selectors['order_menu_link'] = curr_url
                                        break
                        except:
                            continue

                # 5. 进入订单管理
                order_link = selectors.get('order_menu_link')
                if order_link:
                    # 无论是否可见，都尝试先进入订单页面，确保我们在正确的页面上处理弹窗
                    # 注意：如果已经在订单页，goto 可能只会刷新，也是好事
                    if is_url(order_link):
                        should_navigate = True
                        if keep_page_alive:
                            try:
                                current_url = page.url or ""
                                if order_link in current_url:
                                    should_navigate = False
                            except:
                                pass
                        if should_navigate:
                            print(f"[{site['name']}] 跳转到订单页面: {order_link}")
                            page.goto(order_link)
                            # 等待页面加载，并检测是否被重定向到登录页
                            try:
                                page.wait_for_load_state('domcontentloaded')
                                time.sleep(2)
                                # 检查是否出现了登录框（说明 cookie 失效被重定向了）
                                user_sel = selectors.get('username_input')
                                if user_sel and page.is_visible(user_sel):
                                    print(f"[{site['name']}] 跳转订单页后发现回到了登录页，Cookie 已失效")
                                    
                                    # 清理当前站点的 Cookie (只清理当前域名的，避免误伤)
                                    print(f"[{site['name']}] 正在清理该站点的 Cookie...")
                                    
                                    clear_site_cookies_preserve_others(page.context, site, selectors)
                                    print(f"[{site['name']}] 已清理当前会话 Cookie")

                                    is_logged_in = False # 标记为未登录
                                    print(f"[{site['name']}] 准备重新跳转登录页...")
                                    page.goto(site['login_url'])
                            except:
                                pass
                    else:
                        # 只有当不在订单页时才点击菜单
                        count_sel = selectors.get('pending_count_element')
                        should_click_menu = True
                        try:
                            if count_sel:
                                should_click_menu = not page.is_visible(count_sel)
                        except:
                            pass
                        if should_click_menu:
                            print(f"[{site['name']}] 进入订单菜单...")
                            page.click(order_link)
                            page.wait_for_load_state('networkidle')
                            time.sleep(2)
                
                # 如果在跳转订单页的过程中发现登录失效
                if not is_logged_in:
                     # 重新触发登录流程 (代码结构限制，这里需要一个 goto 标签的效果，但 Python 没有)
                     # 我们可以抛出一个特殊的 RetryLoginException，或者简单的递归调用（不推荐），
                     # 或者在这里直接执行登录逻辑。
                     # 为了保持代码整洁，我们把下面的“未登录处理逻辑”封装成函数会更好。
                     # 但为了快速修复，我们在这里做一个简单的标记，让下一轮循环处理，或者直接抛错。
                     # 实际上，如果这里发现未登录，直接抛错，下一轮定时任务会自动重试登录，这是最安全的。
                     raise Exception("Cookie 在访问订单页时失效，请等待下一轮自动重新登录")

                # 再次尝试关闭弹窗 (因为进入新页面可能会有新的弹窗，如“诚赁”的工单提醒)
                handle_popups(page, site_name=site['name'])

                # === 增强稳定性：刷新页面并等待，确保数据最新 ===
                # 用户需求：如果cookies没过期，访问到订单页面后，刷新页面，等待5秒，判断页面加载好了再抓取
                print(f"[{site['name']}] 刷新页面并等待 5 秒...")
                
                page_load_failed = False # 标记页面是否加载失败
                
                try:
                    if not keep_page_alive:
                        page.reload(wait_until='domcontentloaded', timeout=30000)
                        # 基础等待
                        try:
                            page.wait_for_load_state('networkidle', timeout=5000)
                        except:
                            pass # 忽略 networkidle 超时，继续往下走
                        
                        # 强制等待 5 秒
                        time.sleep(5)
                    else:
                        try:
                            page.wait_for_load_state('domcontentloaded', timeout=15000)
                        except:
                            pass
                        time.sleep(2)
                    
                    # 刷新后再次处理可能出现的弹窗
                    handle_popups(page, site_name=site['name'])
                    
                    # 显式等待关键元素，确保页面渲染完成
                    # 优先等待 Tab，如果没有 Tab 则等待数量元素
                    wait_target = selectors.get('pending_tab_selector') or selectors.get('pending_count_element')
                    if wait_target:
                        print(f"[{site['name']}] 等待关键元素加载: {wait_target}")
                        try:
                            page.wait_for_selector(wait_target, state='visible', timeout=15000) # 增加等待时间到15秒
                            print(f"[{site['name']}] 页面加载确认完成")
                        except Exception as wait_err:
                            is_actually_visible = False
                            try:
                                if page.locator(wait_target).first.is_visible():
                                    print(f"[{site['name']}] 等待超时但发现第一个目标元素可见，判定为加载成功，继续执行...")
                                    is_actually_visible = True
                            except:
                                pass
                            
                            if not is_actually_visible:
                                fallback_targets = []
                                if selectors.get('pending_tab_selector'):
                                    fallback_targets.append(selectors.get('pending_tab_selector'))
                                if selectors.get('pending_count_element'):
                                    fallback_targets.append(selectors.get('pending_count_element'))
                                fallback_targets = [t for t in fallback_targets if t and t != wait_target]
                                for fallback in fallback_targets:
                                    try:
                                        if page.locator(fallback).first.is_visible():
                                            print(f"[{site['name']}] 等待超时但发现备选元素可见: {fallback}")
                                            is_actually_visible = True
                                            break
                                    except:
                                        continue
                            
                            if not is_actually_visible:
                                try:
                                    if page.get_by_text("待审核", exact=False).first.is_visible():
                                        print(f"[{site['name']}] 等待超时但发现“待审核”文本可见")
                                        is_actually_visible = True
                                except:
                                    pass

                            if not is_actually_visible:
                                discovered = _auto_discover_order_selectors(page)
                                if discovered:
                                    for k, v in discovered.items():
                                        if v:
                                            selectors[k] = v
                                            auto_selectors[k] = v
                                    new_wait_target = selectors.get('pending_tab_selector') or selectors.get('pending_count_element')
                                    if new_wait_target:
                                        try:
                                            page.wait_for_selector(new_wait_target, state='visible', timeout=15000)
                                            print(f"[{site['name']}] 已自动发现订单关键元素并确认加载完成")
                                            is_actually_visible = True
                                        except:
                                            pass

                            if not is_actually_visible:
                                print(f"[{site['name']}] ⚠️ 等待关键元素超时: {wait_err}")
                                page_load_failed = True # 标记失败
                    else:
                        print(f"[{site['name']}] 未配置关键等待元素，跳过显式等待")
                        
                except Exception as e:
                    print(f"[{site['name']}] 刷新流程异常: {e}")
                    page_load_failed = True

                # 如果页面加载失败，跳过后续操作并报错
                if page_load_failed:
                    raise Exception("页面加载失败或关键元素未出现（可能网络卡顿或需要验证码）")

                if not is_url(selectors.get('order_menu_link')):
                    candidate_url = page.url or ""
                    if _should_accept_order_url(site.get('login_url'), candidate_url):
                        visible_ok = False
                        try:
                            pending_sel = selectors.get('pending_tab_selector')
                            count_sel = selectors.get('pending_count_element')
                            if pending_sel and page.is_visible(pending_sel):
                                visible_ok = True
                            if not visible_ok and count_sel and page.is_visible(count_sel):
                                visible_ok = True
                        except:
                            pass
                        if not visible_ok:
                            try:
                                if page.get_by_text("待审核", exact=False).first.is_visible(timeout=500):
                                    visible_ok = True
                            except:
                                pass
                        if visible_ok:
                            selectors['order_menu_link'] = candidate_url
                            auto_selectors['order_menu_link'] = candidate_url

                # === 特殊检测：如果配置了列表容器且不可见，直接视为 0 单 (针对兜来租等) ===
                list_container = selectors.get('order_list_container')
                if list_container:
                    # 尝试等待容器出现 (短时间)
                    try:
                        page.wait_for_selector(list_container, timeout=3000, state='visible')
                    except:
                        pass # 超时说明可能真没有
                        
                    if not page.is_visible(list_container):
                         print(f"[{site['name']}] 未检测到订单列表容器，但将尝试检查 Tab 上的数量...")
                         # 之前逻辑是直接返回 0，现在改为继续往下走，尝试从 Tab 获取数量
                         # results.append({
                         #    "name": site['name'], 
                         #    "count": 0, 
                         #    "error": None,
                         #    "link": site['selectors'].get('order_menu_link')
                         # })
                         # save_global_cookies(context)
                         # continue  

                # 5. 尝试定位并解析“待审核” Tab
                # 很多系统（如 ewei_shopv2）会在 Tab 上直接显示数量，例如 "待发货(3)"
                # 即使页面内容为空，Tab 上的数字也是准确的。
                
                tab_count = None
                
                # 增强策略：不仅查找配置的 pending_tab_selector，还尝试模糊查找包含“待审核”、“待发货”等关键词的元素
                # 并且优先信任 Tab 里的数字
                
                try:
                    # 关键词列表 (移除待发货、待收货等非审核类状态)
                    keywords = ["待审核", "待处理", "待审批", "待确认", "审核", "维权中", "待租"]
                    
                    for kw in keywords:
                        # 查找包含关键词的元素 (通常是 a 标签或 li 标签)
                        elements = page.get_by_text(kw, exact=False).all()
                        
                        found_number_match = None
                        found_pure_text_match = False
                        
                        # 第一轮扫描：寻找带数字的匹配项 (如 "待审核(3)")
                        for el in elements:
                            try:
                                if not el.is_visible(): continue
                                text = el.inner_text().strip()
                                
                                # 尝试提取括号里的数字，如 "待发货(3)" -> 3
                                match = re.search(r'[\(\uff08](\d+)[\)\uff09]', text)
                                if not match:
                                    match = re.search(r'(\d+)$', text) # 结尾的数字
                                
                                if match:
                                    found_number_match = int(match.group(1))
                                    print(f"[{site['name']}] 从 Tab [{text}] 提取到数量: {found_number_match}")
                                    break
                                elif len(text) <= len(kw) + 4:
                                    # 记录是否发现了纯文本匹配 (长度接近关键词，且无数字)
                                    # 例如 "待审核"
                                    found_pure_text_match = True
                            except:
                                continue
                        
                        if found_number_match is not None:
                            tab_count = found_number_match
                            break
                        
                        # 如果没有找到带数字的，但找到了纯文本匹配，且是高优先级关键词，则视为 0
                        # 这样可以防止回退到低优先级的 "待发货"
                        if found_pure_text_match and kw in ["待审核", "待处理", "待审批", "待确认", "审核"]:
                             print(f"[{site['name']}] 找到 Tab 关键词 '{kw}' 但未包含数字，默认视为 0")
                             tab_count = 0
                             break
                             
                except Exception as e:
                    print(f"[{site['name']}] Tab 数量智能提取失败: {e}")

                # 5.1 点击待审核 Tab
                if 'pending_tab_selector' in selectors and selectors['pending_tab_selector']:
                    print(f"[{site['name']}] 点击待审核 Tab...")
                    try:
                        # 获取 Tab 元素文本，尝试从中直接提取数量 (例如 "待审核(6)")
                        # 这可以作为一种备选方案，特别是当列表加载失败或分页元素不稳定时
                        try:
                            # 只有当我们上面没有智能抓取到 tab_count 时，才尝试从配置的 selector 提取
                            if tab_count is None:
                                tab_el = page.locator(selectors['pending_tab_selector']).first
                                if tab_el.is_visible():
                                    tab_text = tab_el.inner_text()
                                    print(f"[{site['name']}] Tab 文本: {tab_text}")
                                    # 尝试提取括号内的数字
                                    match_tab = re.search(r'[\(\uff08](\d+)[\)\uff09]', tab_text)
                                    if match_tab:
                                        tab_count = int(match_tab.group(1))
                                        print(f"[{site['name']}] 从配置 Tab 文本提取到数量: {tab_count}")
                        except Exception as tab_err:
                            print(f"[{site['name']}] 提取 Tab 文本失败: {tab_err}")
                            
                        page.click(selectors['pending_tab_selector'])
                        page.wait_for_load_state('networkidle')
                        time.sleep(2)
                    except Exception as e:
                        print(f"[{site['name']}] 点击 Tab 失败: {e}")
                
                # 6. 获取待审核数量
                count_sel = selectors.get('pending_count_element')
                if count_sel and page.is_visible(count_sel):
                    count_text = page.inner_text(count_sel)
                    match = re.search(r'\d+', count_text)
                    count = int(match.group()) if match else 0
                    
                    # 确保有 URL
                    final_link = site['selectors'].get('order_menu_link')
                    if not is_url(final_link):
                        try:
                            curr = page.url
                            if _should_accept_order_url(site.get('login_url'), curr):
                                final_link = curr
                                selectors['order_menu_link'] = curr
                                auto_selectors['order_menu_link'] = curr
                                print(f"[{site['name']}] 抓取成功，自动补充订单页 URL: {curr}")
                        except:
                            pass

                    results.append({
                        "name": site['name'], 
                        "count": count, 
                        "error": None,
                        "link": final_link
                    })
                    print(f"[{site['name']}] 抓取结果: {count}")
                    if auto_selectors:
                        _update_site_selectors_in_config(site['name'], auto_selectors)
                elif tab_count is not None:
                    # 如果常规元素不可见，但我们从 Tab 上提取到了数字，就用 Tab 的数字
                    print(f"[{site['name']}] 常规数量元素未找到，使用 Tab 上的数量: {tab_count}")
                    results.append({
                        "name": site['name'], 
                        "count": tab_count, 
                        "error": None,
                        "link": site['selectors'].get('order_menu_link')
                    })
                    if auto_selectors:
                        _update_site_selectors_in_config(site['name'], auto_selectors)
                else:
                    try:
                        if not any(k in auto_selectors for k in ('pending_tab_selector', 'pending_count_element')):
                            discovered = _auto_discover_order_selectors(page)
                            if discovered:
                                for k, v in discovered.items():
                                    if v:
                                        selectors[k] = v
                                        auto_selectors[k] = v
                                # 新增：如果发现了订单元素，且当前没有 URL 配置，则保存当前 URL
                                if not is_url(selectors.get('order_menu_link')):
                                    try:
                                        candidate_url = page.url or ""
                                        if _should_accept_order_url(site.get('login_url'), candidate_url):
                                            selectors['order_menu_link'] = candidate_url
                                            auto_selectors['order_menu_link'] = candidate_url
                                            print(f"[{site['name']}] 自动关联订单页 URL: {candidate_url}")
                                    except:
                                        pass
                    except Exception:
                        pass

                    try:
                        if selectors.get('pending_tab_selector'):
                            tab_el = page.locator(selectors['pending_tab_selector']).first
                            if tab_el.is_visible(timeout=800):
                                tab_text = tab_el.inner_text()
                                match_tab = re.search(r'\((\d+)\)', tab_text)
                                if match_tab:
                                    tab_count = int(match_tab.group(1))
                    except Exception:
                        tab_count = None

                    try:
                        if selectors.get('pending_count_element') and page.is_visible(selectors['pending_count_element']):
                            count_text = page.inner_text(selectors['pending_count_element'])
                            match = re.search(r'\d+', count_text)
                            count = int(match.group()) if match else 0
                            
                            # 确保有 URL
                            final_link = site['selectors'].get('order_menu_link')
                            if not is_url(final_link):
                                try:
                                    curr = page.url
                                    if _should_accept_order_url(site.get('login_url'), curr):
                                        final_link = curr
                                        selectors['order_menu_link'] = curr
                                        auto_selectors['order_menu_link'] = curr
                                        print(f"[{site['name']}] 抓取成功，自动补充订单页 URL: {curr}")
                                except:
                                    pass

                            results.append({
                                "name": site['name'], 
                                "count": count, 
                                "error": None,
                                "link": final_link
                            })
                            print(f"[{site['name']}] 抓取结果: {count}")
                            if auto_selectors:
                                _update_site_selectors_in_config(site['name'], auto_selectors)
                            save_global_cookies(context)
                            continue
                    except Exception:
                        pass

                    if 'tab_count' in locals() and tab_count is not None:
                        print(f"[{site['name']}] 使用 Tab 上的数量: {tab_count}")
                        results.append({
                            "name": site['name'], 
                            "count": tab_count, 
                            "error": None,
                            "link": site['selectors'].get('order_menu_link')
                        })
                        if auto_selectors:
                            _update_site_selectors_in_config(site['name'], auto_selectors)
                        save_global_cookies(context)
                        continue

                    # 如果找不到数量元素，通常意味着没有订单（即数量为0）
                    # 只有当确实无法判断时才报错，但根据用户反馈，诚赁等平台没单时就是不显示角标
                    print(f"[{site['name']}] 未找到数量元素，默认视为 0 单")
                    count = 0
                    results.append({
                        "name": site['name'], 
                        "count": 0, 
                        "error": None,
                        "link": site['selectors'].get('order_menu_link')
                    })
                    if auto_selectors:
                        _update_site_selectors_in_config(site['name'], auto_selectors)

                # 更新 Cookie
                save_global_cookies(context)
                
            except Exception as e:
                error_msg = f"{str(e)}"
                print(f"{site['name']} 异常: {error_msg}")
                results.append({
                    "name": site['name'], 
                    "count": None, 
                    "error": error_msg,
                    "link": site['selectors'].get('order_menu_link')
                })
            finally:
                # 总是关闭页面，防止 Tab 堆积
                # BrowserManager 会在下次 get_page 时发现页面已关闭并自动创建新页面
                if page:
                    try:
                        if not manager or not keep_page_alive:
                            page.close()
                    except:
                        pass

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"检查订单过程中发生未捕获异常: {e}")
        # 不重新抛出异常，以便继续执行后面的通知逻辑

    finally:
        # 如果是临时创建的 context，用完就关；如果是外部传入的，由外部管理
        if local_playwright:
            if context: context.close()
            local_playwright.stop()
    
    # 汇总并发送通知
    if results:
        # 输出结构化数据供 launcher 捕获
        # 增加时间戳字段
        data_update = {
            "type": "data_update",
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "data": results
        }
        print(f"DATA_UPDATE:{json.dumps(data_update, ensure_ascii=False)}")

        # 计算总订单数
        total_count = sum(r['count'] for r in results if r['count'] is not None)
        
        # 策略调整：
        # 如果所有平台都没有订单 (total_count == 0) 且没有报错，只发送简短的鼓励语（或者根据需求决定是否打扰）
        # 这里我们选择：如果有订单，发送详细战报；
        # 如果全为0，发送一条简单的“暂时无单，大家辛苦了”之类的提示，或者用更轻松的语气。
        
        if total_count == 0 and not any(r['error'] for r in results):
             # 全 0 且无报错的情况，不发送通知
             print("\n=== 暂无订单，跳过通知 ===")
             
        else:
            # 有订单 或者 有报错，发送详细列表
            
            # 随机文案库
            prefixes = [
                "### 🌞 又是充满阳光的一天，来看看订单吧~",
                "### 🌞 忙碌之余，也要记得喝水休息哦~",
                "### 🌞 温暖的阳光洒下来，工作也更有动力了~",
                "### 🌞 保持好心情，效率会更高哦~",
                "### 🌞 愿今天的你，心里也住着一个小太阳~"
            ]
        
            suffixes = [
                "**🌞 加油，每一个努力的日子都闪闪发光。**",
                "**🌞 慢慢来，比较快，一切都会好起来的。**",
                "**🌞 愿你的心情和今天的阳光一样明媚。**",
                "**🌞 处理完工作，记得去晒晒太阳哦。**",
                "**🌞 保持热爱，奔赴山海，今天也要开心呀。**"
            ]
            
            header = random.choice(prefixes)
            footer = random.choice(suffixes)
            
            # 构建 Markdown 表格/列表样式
            # 由于企业微信对 Markdown 表格支持有限，这里使用引用块+颜色高亮来模拟整齐的效果
            
            body_lines = []
            for res in results:
                name = res['name']
                link = res.get('link')
                action_text = ""
                if link and is_url(link):
                     action_text = f"  [去处理]({link})"

                if res['error']:
                    # 错误状态，用灰色
                    line = f"> <font color=\"comment\">{name}</font>：<font color=\"comment\">{res['error']}</font>{action_text}"
                else:
                    count = res['count']
                    # 数量大于0用橙红色(warning)，等于0用绿色(info)
                    if count > 0:
                        color = "warning"
                        count_str = f"**{count}**" # 加粗
                    else:
                        color = "info"
                        count_str = str(count)
                    
                    line = f"> {name}：<font color=\"{color}\">{count_str}</font> 单{action_text}"
                
                body_lines.append(line)
                
            msg = f"{header}\n\n" + "\n".join(body_lines) + f"\n\n{footer}"
            
            print("\n=== 发送通知内容 ===")
            print(msg)
            
            # 00:00 - 08:00 期间静默运行，不发送通知
            current_hour = datetime.now().hour
            if 0 <= current_hour < 8:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 深夜模式 (00:00-08:00): 保持运行但不发送通知。")
            else:
                # 使用 markdown 格式发送企业微信通知
                send_wecom_notification(msg, msg_type="markdown")
                # 发送飞书通知 (飞书的 markdown 格式略有不同，这里简化处理)
                # 飞书 Post 消息内容
                feishu_content = ""
                for res in results:
                    name = res['name']
                    count = res.get('count', 0)
                    error = res.get('error')
                    link = res.get('link')
                    
                    if error:
                        feishu_content += f"{name}: {error}\n"
                    else:
                        feishu_content += f"{name}: {count} 单\n"
                    if link:
                        feishu_content += f"链接: {link}\n"
                
                if total_count > 0 or any(r['error'] for r in results):
                    send_feishu_notification(feishu_content, title="租帮宝 - 订单监控")
    
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 本次检查结束，7分钟后继续...")

class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.context = None
        
        # 修改 user_data_dir 路径策略
        # 优先使用程序运行目录下的 browser_data，避免 C 盘权限或中文用户名路径问题
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
            # 兼容 backend 目录结构
            if os.path.basename(base_dir).lower() == 'backend':
                base_dir = os.path.dirname(base_dir)
            self.user_data_dir = os.path.join(base_dir, 'browser_data')
        else:
            self.user_data_dir = os.path.join(os.getcwd(), 'browser_data')
            
        print(f"浏览器数据目录: {self.user_data_dir}")
        self.pages = {}  # 存储各站点的持久化页面 {site_name: page}
        self.cdp_port = 9222 # 定义 CDP 端口

    def _get_browser_executable_path(self):
        """获取浏览器可执行文件路径，优先查找本地便携版"""
        # 1. 检查当前目录下的 playwright-browsers
        base_paths = []
        if getattr(sys, 'frozen', False):
            base_paths.append(os.path.dirname(sys.executable))
        else:
            base_paths.append(os.path.dirname(os.path.abspath(__file__)))
            
        for base_path in base_paths:
            browsers_dir = os.path.join(base_path, 'playwright-browsers')
            if os.path.exists(browsers_dir):
                # 递归查找 chrome.exe
                for root, dirs, files in os.walk(browsers_dir):
                    if 'chrome.exe' in files:
                        path = os.path.join(root, 'chrome.exe')
                        print(f"找到内置浏览器: {path}")
                        return path

        # 2. 尝试使用 Playwright 默认逻辑
        try:
            default_path = self.playwright.chromium.executable_path
            if os.path.exists(default_path):
                return default_path
        except:
            pass

        # 3. 检查系统路径
        system_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        ]
        
        for path in system_paths:
            if os.path.exists(path):
                print(f"找到系统浏览器: {path}")
                return path
                
        return None

    def _kill_zombie_browsers(self):
        """清理可能残留的、占用 User Data Dir 或端口的浏览器进程"""
        print("正在检查并清理残留的浏览器进程...")
        try:
            # 1. 按端口清理 (即使 connect 失败，也可能处于半死状态)
            cmd_port = 'netstat -ano | findstr :9222'
            proc = subprocess.Popen(cmd_port, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _ = proc.communicate()
            
            if stdout:
                lines = stdout.decode('utf-8', errors='ignore').splitlines()
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 5 and 'LISTENING' in line:
                        pid = parts[-1]
                        print(f"发现占用端口 9222 的进程 (PID: {pid})，正在终止...")
                        os.system(f"taskkill /F /PID {pid} >nul 2>&1")

            # 2. 按命令行参数清理 (User Data Dir)
            # 匹配 browser_data 目录名
            target_str = "browser_data"
            
            # 使用 wmic 查找 PID
            cmd_wmic = f"wmic process where \"name='chrome.exe' and CommandLine like '%{target_str}%'\" get ProcessId"
            
            proc = subprocess.Popen(cmd_wmic, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _ = proc.communicate()
            
            if stdout:
                lines = stdout.decode('utf-8', errors='ignore').splitlines()
                for line in lines:
                    line = line.strip()
                    if line.isdigit():
                        pid = line
                        print(f"发现残留浏览器进程 (PID: {pid})，正在终止...")
                        os.system(f"taskkill /F /PID {pid} >nul 2>&1")
                        
        except Exception as e:
            print(f"清理残留进程失败: {e}")

    def start(self):
        """启动浏览器和上下文"""
        if not self.playwright:
            self.playwright = sync_playwright().start()
        
        if not os.path.exists(self.user_data_dir):
            os.makedirs(self.user_data_dir)
            
        if not self.context:
            try:
                # 1. 尝试连接已存在的浏览器实例 (CDP)
                print(f"尝试连接已运行的浏览器 (端口 {self.cdp_port})...")
                try:
                    # 注意：connect_over_cdp 返回的是 Browser 实例，不是 Context
                    browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
                    # 获取默认上下文 (通常第一个)
                    if browser.contexts:
                        self.context = browser.contexts[0]
                    else:
                        # 如果没有上下文，创建一个新的
                        self.context = browser.new_context()
                    print("成功连接到现有浏览器！")
                except Exception as cdp_err:
                    print(f"连接现有浏览器失败: {cdp_err}")
                    print("这可能是因为现有浏览器未开启远程调试端口(9222)，或者处于无响应状态。")
                    print("准备清理残留进程并启动新实例...")
                    
                    # === 新增：启动前清理残留进程 ===
                    self._kill_zombie_browsers()
                    
                    # 2. 启动新的浏览器进程 (独立进程，脚本退出后不关闭)
                    print("正在启动独立浏览器进程...")
                    
                    # 获取 Chromium 可执行文件路径
                    executable_path = self._get_browser_executable_path()
                    print(f"浏览器可执行文件路径: {executable_path}")
                    
                    if not executable_path:
                        raise FileNotFoundError("未找到可用的浏览器 (内置或系统)。请确保 playwright-browsers 目录存在或已安装 Chrome。")
                    
                    try:
                        headless = bool(load_config().get('headless', False))
                    except:
                        headless = False

                    # 构造启动命令
                    args = [
                        executable_path,
                        f"--user-data-dir={self.user_data_dir}",
                        f"--remote-debugging-port={self.cdp_port}",
                        "--remote-debugging-address=127.0.0.1",
                        "--no-first-run",
                        "--no-default-browser-check",
                        # "--window-size=1920,1080", # 暂时移除分辨率设置，排查崩溃
                        # "--window-position=-2400,-2400", # 暂时移除位置设置，排查崩溃
                        "--disable-infobars",
                        "--disable-blink-features=AutomationControlled",
                        # "--start-maximized", # 不需要最大化
                        # 模拟 UA
                        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "--ignore-certificate-errors",
                        # 禁用后台网络和 Google 服务，减少报错
                        "--disable-background-networking",
                        "--disable-sync",
                        "--disable-translate",
                        "--disable-client-side-phishing-detection",
                        "--no-service-autorun",
                        # 增加稳定性参数，解决部分环境崩溃问题 (错误码 2147483651 / 0x80000003)
                        "--no-sandbox",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-dev-shm-usage" # 避免共享内存不足
                    ]
                    
                    if headless:
                        args.append("--headless=new")
                    
                    print(f"启动命令: {' '.join(args)}")
                    
                    # 使用 subprocess.Popen 启动 (detaches from python script)
                    # 恢复显示控制台窗口 (用户反馈 cmd 窗口没关系)，并移除日志重定向以便查看
                    # 使用 DETACHED_PROCESS (0x00000008) 替代 CREATE_NEW_CONSOLE
                    # 注意：这两个标志不能同时使用，否则会报 [WinError 87] 参数错误
                    # DETACHED_PROCESS 已经隐含了不继承父控制台，且对于 GUI 程序 (Chrome) 不会影响窗口显示
                    flags = 0
                    if sys.platform == 'win32':
                        flags = 0x00000008 # DETACHED_PROCESS

                    proc = subprocess.Popen(args, creationflags=flags)
                    
                    print("浏览器进程已启动，等待初始化...")
                    
                    # 3. 循环尝试连接 (最多 15 秒)
                    print(f"尝试连接新启动的浏览器 (最多等待 15 秒)...")
                    browser = None
                    for i in range(15):
                        # 检查进程是否意外退出
                        if proc.poll() is not None:
                            print(f"[X] 浏览器进程已退出，返回码: {proc.returncode}")
                            raise Exception("浏览器启动失败 (进程意外退出)")

                        try:
                            browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
                            if browser:
                                print(f"第 {i+1} 次尝试: 连接成功！")
                                break
                        except Exception as e:
                            # 仅打印前几次的错误，避免刷屏，或者只打印点号
                            if i < 3 or i % 5 == 0:
                                print(f"第 {i+1} 次尝试连接失败，继续等待...")
                            time.sleep(1)
                    
                    if not browser:
                        raise Exception("浏览器启动超时或连接失败")
                        
                    if browser.contexts:
                        self.context = browser.contexts[0]
                    else:
                        self.context = browser.new_context()

                    # 注入反爬虫/反检测脚本 (Stealth JS)
                    try:
                        stealth_js = """
                            try {
                                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                            } catch (e) {}
                            try {
                                if (!window.chrome) { window.chrome = {}; }
                                if (!window.chrome.runtime) { window.chrome.runtime = {}; }
                            } catch (e) {}
                            try {
                                const originalQuery = window.navigator.permissions.query;
                                window.navigator.permissions.query = (parameters) => (
                                    parameters.name === 'notifications' ?
                                        Promise.resolve({ state: Notification.permission }) :
                                        originalQuery(parameters)
                                );
                            } catch (e) {}
                        """
                        self.context.add_init_script(stealth_js)
                        print("已注入反检测脚本 (Stealth JS)")
                    except Exception as e:
                        print(f"注入反检测脚本失败: {e}")

                    print("浏览器启动并连接成功。")

                load_global_cookies(self.context)
                try:
                    if not bool(load_config().get('headless', False)):
                        self.move_browser_offscreen()
                except:
                    pass
            except Exception as e:
                print(f"[X] 启动/连接浏览器失败: {e}")
                self.stop()
                raise e

    def stop(self):
        """关闭连接 (不关闭浏览器进程)"""
        self.pages.clear() # 清空页面记录
        
        if self.context:
            try:
                # 这里的 close 只是断开连接还是关闭 Context？
                # 对于 connect_over_cdp 获取的 browser，browser.close() 会关闭浏览器。
                # 但 context.close() 通常只是关闭页面。
                # 既然我们希望“脚本退出后浏览器不关”，这里最好只做清理工作，不主动调 close。
                # 或者调用 browser.disconnect()。
                
                # 由于 self.context 是从 browser.contexts 获取的，
                # 我们需要找到 browser 对象来 disconnect。
                browser = self.context.browser
                if browser:
                    browser.disconnect()
                    print("已断开与浏览器的连接。")
            except Exception as e:
                print(f"断开连接时出错: {e}")
            self.context = None
            
        if self.playwright:
            try:
                self.playwright.stop()
            except:
                pass
            self.playwright = None

    def restart(self):
        """重启浏览器"""
        print("正在重启浏览器...")
        self.stop()
        
        # 清理旧进程
        self._kill_zombie_browsers()

        time.sleep(2)
        self.start()

    def get_context(self):
        """获取当前上下文，如果不存在或已关闭则尝试重启"""
        if self.context:
            try:
                # 简单检查 context 是否存活
                # 注意：Playwright 的 context 没有 is_connected() 方法，
                # 但如果浏览器断开了，访问 browser.contexts 会报错或者 context.pages 会报错
                # 这里我们尝试一个轻量级操作
                _ = self.context.pages
            except Exception:
                 print("检测到 BrowserContext 已失效，准备重连...")
                 self.context = None

        if not self.context:
            self.start()
        return self.context

    def get_page(self, site_name):
        """获取指定站点的持久化页面"""
        # 确保 context 是活的
        self.get_context()
            
        page = self.pages.get(site_name)
        # 检查页面是否有效
        if page:
            try:
                if page.is_closed():
                    page = None
                else:
                    # 再次确认连接状态 (CDP 模式下有时候 page 对象还在但连接断了)
                    # 尝试一个无副作用的操作，比如获取 url
                    _ = page.url
            except:
                page = None
                
        if not page:
            print(f"[{site_name}] 创建新页面...")
            try:
                page = self.context.new_page()
                self.pages[site_name] = page
            except Exception as e:
                print(f"[{site_name}] 创建页面失败 (可能是浏览器连接断开): {e}")
                # 尝试一次重启/重连
                self.restart()
                # 递归重试一次 (避免无限递归)
                if self.context:
                     print(f"[{site_name}] 重连后再次尝试创建页面...")
                     page = self.context.new_page()
                     self.pages[site_name] = page
            
        return page

    def perform_heartbeat(self):
        """执行随机心跳，模拟用户活跃"""
        if not self.pages:
            return
        
        # print("[Heartbeat] 执行随机活跃心跳...")
        for site_name, page in list(self.pages.items()):
            try:
                if not page or page.is_closed():
                    continue
                
                # 随机动作: 1=滚动, 2=鼠标微动, 3=获取标题
                action = random.randint(1, 3)
                if action == 1:
                    # 极微小的滚动，几乎不可见
                    page.evaluate("window.scrollBy(0, 1); setTimeout(() => window.scrollBy(0, -1), 100);")
                elif action == 2:
                    # 移动鼠标到随机位置 (安全区域)
                    page.mouse.move(random.randint(100, 500), random.randint(100, 500))
                else:
                    # 访问属性
                    _ = page.title()
            except:
                pass

    def set_window_position(self, left, top):
        """通过 CDP 控制浏览器窗口位置"""
        try:
            if not self.context:
                print("尝试调整窗口位置，但 BrowserContext 未初始化")
                return
            
            page = None
            # 1. 优先查找现有可用页面
            if self.pages:
                # 过滤掉已关闭的页面
                valid_pages = [p for p in self.pages.values() if not p.is_closed()]
                if valid_pages:
                    page = valid_pages[0]
            
            # 2. 如果没有，检查 context.pages
            if not page:
                if self.context.pages:
                    # 过滤掉已关闭的
                    valid_ctx_pages = [p for p in self.context.pages if not p.is_closed()]
                    if valid_ctx_pages:
                        page = valid_ctx_pages[0]
            
            # 3. 还是没有，创建一个新页面 (为了能控制窗口)
            if not page:
                print("没有可用页面用于控制窗口，正在创建临时页面...")
                try:
                    page = self.context.new_page()
                except Exception as e:
                    print(f"创建临时页面失败: {e}")
                    return

            if page:
                try:
                    session = self.context.new_cdp_session(page)
                    # 获取当前窗口 ID
                    window = session.send("Browser.getWindowForTarget")
                    window_id = window.get('windowId')
                    if window_id:
                        # 设置窗口边界
                        session.send("Browser.setWindowBounds", {
                            "windowId": window_id,
                            "bounds": {
                                "left": left,
                                "top": top,
                                "width": 1920,
                                "height": 1080,
                                "windowState": "normal"
                            }
                        })
                        print(f"窗口位置已调整: ({left}, {top})")
                        
                        # 尝试使用 win32gui 强制前置 (仅限 Windows)
                        if sys.platform == 'win32' and left >= 0:
                            try:
                                import ctypes
                                user32 = ctypes.windll.user32
                                
                                # 枚举所有窗口，找到标题包含 Chrome 或 Edge 的
                                # 由于我们无法直接从 Playwright 获取 HWND，只能通过标题模糊匹配
                                # 这不是完美的，但通常够用
                                
                                def enum_windows_callback(hwnd, lParam):
                                    if user32.IsWindowVisible(hwnd):
                                        length = user32.GetWindowTextLengthW(hwnd)
                                        buff = ctypes.create_unicode_buffer(length + 1)
                                        user32.GetWindowTextW(hwnd, buff, length + 1)
                                        title = buff.value
                                        
                                        # 匹配常见的浏览器标题
                                        # 注意：这里可能会误伤用户其他的浏览器窗口，但在人工介入模式下，
                                        # 用户通常就是想看这个窗口，所以置顶所有相关窗口也是可以接受的
                                        if "Chrome" in title or "Edge" in title or "Chromium" in title:
                                            # 恢复并置顶
                                            user32.ShowWindow(hwnd, 9) # SW_RESTORE
                                            user32.SetForegroundWindow(hwnd)
                                    return True
                                
                                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_long)
                                user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)
                            except Exception as win_err:
                                print(f"强制置顶窗口失败: {win_err}")
                                
                    else:
                        print("无法获取 windowId，调整窗口失败")
                except Exception as e:
                    print(f"调整窗口位置失败 (CDP错误): {e}")
        except Exception as e:
            print(f"设置窗口位置时出错: {e}")

    def move_browser_onscreen(self):
        """将浏览器移回屏幕可见区域"""
        # 检查是否在主线程
        if threading.current_thread() is threading.main_thread():
            # 移到左上角 (0, 0) 或者居中
            self.set_window_position(0, 0)
        else:
            print("非主线程调用 move_browser_onscreen，已加入队列")
            shared.window_control_queue.put("show")

    def move_browser_offscreen(self):
        """将浏览器移出屏幕"""
        if threading.current_thread() is threading.main_thread():
            self.set_window_position(-4800, -4800)
        else:
            print("非主线程调用 move_browser_offscreen，已加入队列")
            shared.window_control_queue.put("hide")


# 全局浏览器管理器实例
browser_manager = BrowserManager()
# 共享给 Web Server 使用，以便远程控制
shared.browser_manager = browser_manager

def ensure_single_instance():
    """确保单实例运行 (通过绑定端口)"""
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 绑定一个特定的本地端口 (选择一个不常用的端口)
        # 注意：这里绑定的是 12345，请确保没有其他重要服务使用此端口
        # UDP 端口绑定不会影响 TCP 服务 (web_server 使用的是 TCP 5000)
        port = int(os.environ.get("SINGLE_INSTANCE_PORT", 12345))
        lock_socket.bind(('127.0.0.1', port))
        return lock_socket
    except socket.error:
        print(f"检测到程序已经在运行 (端口 {port} 被占用)！")
        print("请不要重复启动监控脚本。")
        sys.exit(1)

def run_scheduler():
    """定时任务调度"""
    print("监控脚本已启动 (Ctrl+C 停止)...")
    
    # 启动 Cookie Web 服务 (后台线程)
    print(f"启动 Cookie 更新服务: http://{SERVER_IP}:{SERVER_PORT}")
    server_thread = threading.Thread(target=start_web_server, daemon=True)
    server_thread.start()

    # 初始化浏览器
    try:
        browser_manager.start()
    except Exception:
        print("初始化浏览器失败，将在首次任务执行时重试。")

    # 定义一个包装函数来处理异常，防止浏览器崩溃导致脚本退出
    def safe_check_orders():
        try:
            # 传入管理器实例，以便 check_orders 能复用页面
            check_orders(browser_manager)
        except Exception as e:
            import traceback
            traceback.print_exc()
            error_str = str(e)
            print(f"执行任务时发生错误: {error_str}")
            
            # 检测是否是浏览器关闭/崩溃导致的错误
            error_lower = error_str.lower()
            critical_errors = [
                "target page, context or browser has been closed",
                "event loop is closed",
                "connection closed",
                "session closed",
                "broken pipe",
                "connection refused"
            ]
            
            if any(err in error_lower for err in critical_errors):
                print("检测到浏览器异常关闭或连接断开，准备重启...")
                try:
                    browser_manager.restart()
                except Exception as restart_error:
                    print(f"重启浏览器失败: {restart_error}")
            
    # 立即执行一次
    safe_check_orders()
    
    # 1. 每天早上08:00准时触发一次（确保8点收到通知）
    schedule.every().day.at("08:00").do(safe_check_orders)

    # 2. 周期性执行 (读取配置)
    config = load_config()
    interval = config.get('interval', 420)
    try:
        interval = int(interval)
    except:
        interval = 420
        
    if interval < 30:
        interval = 30
        
    print(f"任务执行间隔: {interval} 秒")
    schedule.every(interval).seconds.do(safe_check_orders)
    
    # 心跳控制变量
    last_heartbeat_time = time.time()
    next_heartbeat_interval = random.randint(30, 90)

    try:
        while True:
            # 检查夜间模式 - 如果处于夜间模式，直接退出循环（结束程序）
            if is_night_mode_active():
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 到达夜间静默时段，停止监控服务。")
                # 发送通知（可选）
                break

            schedule.run_pending()
            
            # 随机心跳检测 (在等待期间保持活跃)
            current_time = time.time()
            if current_time - last_heartbeat_time > next_heartbeat_interval:
                try:
                    browser_manager.perform_heartbeat()
                except Exception as e:
                    print(f"心跳执行出错: {e}")
                
                last_heartbeat_time = current_time
                next_heartbeat_interval = random.randint(30, 90)
            
            # 处理浏览器窗口控制队列
            try:
                while not shared.window_control_queue.empty():
                    cmd = shared.window_control_queue.get_nowait()
                    if cmd == "show":
                        browser_manager.move_browser_onscreen()
                    elif cmd == "hide":
                        browser_manager.move_browser_offscreen()
            except Exception as e:
                print(f"处理窗口控制队列出错: {e}")

            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        browser_manager.stop()

if __name__ == '__main__':
    # === 授权校验 (双重保险) ===
    print("正在检查授权...")
    license_code = auth_manager.load_license()
    if not license_code:
        print("[错误] 未找到有效的授权信息，请从启动器启动本程序！")
        time.sleep(3)
        sys.exit(1)
    
    # 启动后台心跳线程 (每5分钟一次)
    def _auth_heartbeat_loop():
        while True:
            # 首次心跳在启动后 10 秒执行，之后每 5 分钟执行一次
            # 这样可以快速拦截非法启动，又不会拖慢启动速度
            time.sleep(10)
            while True:
                try:
                    success, msg = auth_manager.heartbeat()
                    if not success:
                        print(f"[严重] 授权验证失败: {msg}，程序即将退出...")
                        os._exit(1) # 强制退出整个进程
                except Exception as e:
                    print(f"心跳检查异常: {e}")
                time.sleep(300)

    auth_thread = threading.Thread(target=_auth_heartbeat_loop, daemon=True)
    auth_thread.start()

    # 确保单实例运行
    _instance_lock = ensure_single_instance()
    
    run_scheduler()
