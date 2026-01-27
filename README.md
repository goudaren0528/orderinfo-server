# 多平台订单监控通知脚本 (Multi-Backend Order Notification)

这是一个自动化脚本，用于监控多个电商/业务后台的待处理订单，并通过企业微信（WeCom）发送通知。支持自动登录、Cookie 持久化、异常自动恢复以及人工远程介入。

## ✨ 主要功能

- **多站点监控**：通过 `config.json` 灵活配置多个监控站点。
- **自动登录 & Cookie 保持**：自动处理登录表单，并持久化 Cookie 避免频繁登录。
- **智能异常恢复**：
    - 自动检测浏览器崩溃或意外关闭，并自动重启恢复。
    - 遇到验证码等无法自动处理的情况，自动发送人工介入通知。
- **人工远程介入**：提供 Web 界面远程控制浏览器（查看屏幕、点击、输入），无需服务器 GUI 环境。
- **多渠道通知**：
    - 支持配置多个企业微信 Webhook 地址。
    - 每日早安/晚安问候及正能量语录。
    - 通知包含“去处理”直达链接，一键跳转后台。
- **Docker 支持**：提供 Dockerfile 和 docker-compose 配置，一键部署。

## 🚀 快速开始

### 方式一：Windows 直接运行

1.  **安装依赖**：
    确保已安装 Python 3.8+，然后运行：
    ```bash
    pip install -r requirements.txt
    playwright install
    ```

2.  **配置**：
    - 将 `config.sample.json` 复制为 `config.json`。
    - 编辑 `config.json`，填入你的后台网址、账号密码和 CSS 选择器。
    - 编辑 `main.py`，在文件顶部 `WECOM_WEBHOOK_URL` 处填入你的企业微信机器人 Webhook 地址（支持多个）。

3.  **运行**：
    双击运行 `start.bat` 即可。

### 方式二：Docker 部署

1.  **构建并启动**：
    ```bash
    docker-compose up -d
    ```

2.  **查看日志**：
    ```bash
    docker-compose logs -f
    ```

## ⚙️ 配置文件说明 (`config.json`)

```json
[
  {
    "name": "平台名称",
    "login_url": "登录页面URL",
    "username": "账号",
    "password": "密码",
    "selectors": {
      "username_input": "用户名输入框CSS选择器",
      "password_input": "密码输入框CSS选择器",
      "login_button": "登录按钮CSS选择器",
      "order_menu_link": "订单列表页URL（用于点击通知中的'去处理'跳转）",
      "pending_tab_selector": "待处理订单Tab的选择器（用于判断是否登录成功）",
      "pending_count_element": "显示订单数量的元素选择器"
    }
  }
]
```

## 🔔 通知配置 (`main.py`)

在 `main.py` 顶部可以配置通知机器人：

```python
# 1. 订单通知机器人 (日常战报) - 支持多个
WECOM_WEBHOOK_URL = [
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY_1",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY_2"
]

# 2. 人工介入通知机器人 (紧急提醒)
WECOM_WEBHOOK_URL_ALERT = [
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY_ALERT"
]
```

## 🛠️ 远程人工介入

当脚本遇到无法自动处理的情况（如滑块验证码）时，会发送一条“人工介入”通知。
点击通知中的链接，即可打开远程控制台：
- **查看实时屏幕**：网页上会实时显示浏览器画面。
- **鼠标操作**：点击画面即可控制远程浏览器点击。
- **键盘输入**：在下方输入框输入文字并回车，可发送文字到浏览器。
- **特殊按键**：支持 ESC, Enter, Backspace 等按键模拟。

## 📝 更新日志

- **Latest**:
    - 新增 `BrowserManager`，大幅提升稳定性，支持浏览器崩溃自动重启。
    - 通知新增“去处理”跳转链接。
    - 支持配置多个通知群组。
    - 新增 Docker 部署支持。
    - 优化远程控制体验（支持拖拽、回车键优化）。
