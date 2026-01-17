import queue
import threading

# 线程安全的指令队列
# 格式: {'type': 'click'|'type'|'refresh', 'x': int, 'y': int, 'text': str}
command_queue = queue.Queue()

# 浏览器窗口控制队列 (用于解决多线程调用 CDP 报错问题)
# 格式: 'show' | 'hide'
window_control_queue = queue.Queue()

# 最新截图数据 (bytes)
# 使用 Lock 保护并发读写
screenshot_lock = threading.Lock()
latest_screenshot = None

# 当前正在交互的站点名称
current_site_name = None

# 标记是否处于交互模式
is_interactive_mode = False

# 全局 BrowserManager 实例引用，用于远程控制窗口位置
browser_manager = None

def set_screenshot(data):
    global latest_screenshot
    with screenshot_lock:
        latest_screenshot = data

def get_screenshot():
    with screenshot_lock:
        return latest_screenshot
