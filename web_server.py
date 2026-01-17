import json
import os
from flask import Flask, render_template_string, request, Response, jsonify
import shared

app = Flask(__name__)

# 远程控制页面模板
REMOTE_CONTROL_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>远程辅助登录 - {{ site_name }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { 
            font-family: sans-serif; 
            background: #222; 
            color: white; 
            text-align: center;
            margin: 0;
            padding: 10px;
            user-select: none; /* 防止长按选中文字 */
        }
        #screen-container {
            position: relative;
            display: inline-block;
            border: 2px solid #555;
            cursor: crosshair;
            touch-action: none; /* 禁止浏览器默认的滚动/缩放行为，由JS接管 */
        }
        #screen {
            max-width: 100%;
            display: block;
            pointer-events: none; /* 让事件穿透到 container */
        }
        .controls {
            margin-top: 15px;
            display: flex;
            gap: 10px;
            justify-content: center;
            flex-wrap: wrap;
        }
        input[type="text"] {
            padding: 10px;
            border-radius: 4px;
            border: none;
            width: 200px;
        }
        button {
            padding: 10px 20px;
            background: #007bff;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }
        button.refresh { background: #28a745; }
        .status { margin-bottom: 10px; color: #aaa; font-size: 14px; }
        .mode-switch {
            margin-bottom: 10px;
        }
    </style>
</head>
<body>
    <h3>正在操作: {{ site_name }}</h3>
    <div class="status">
        ⚠️ <b>操作指南：</b><br>
        1. <b>点击</b>：直接在屏幕上点击（用于输入框聚焦、按钮点击）<br>
        2. <b>拖拽</b>：在屏幕上按住并滑动（用于<b>滑块验证码</b>）<br>
    </div>
    
    <div id="screen-container">
        <img id="screen" src="/screenshot_stream">
    </div>

    <div class="controls">
        <input type="text" id="inputText" placeholder="输入账号/密码/验证码...(按回车发送+确认)">
        <button id="btnSend" onclick="sendText()">输入文字</button>
        <button onclick="sendBackspace()">退格(Backspace)</button>
        <button onclick="sendEnter()">回车(Enter)</button>
        <button class="refresh" onclick="sendRefresh()">刷新远程页面(F5)</button>
    </div>

    <script>
        const container = document.getElementById('screen-container');
        const img = document.getElementById('screen');
        
        let startX = 0, startY = 0;
        let isDragging = false;
        let startTime = 0;

        // 统一处理坐标计算
        function getCoords(event) {
            const rect = img.getBoundingClientRect();
            let clientX, clientY;
            
            if (event.touches && event.touches.length > 0) {
                clientX = event.touches[0].clientX;
                clientY = event.touches[0].clientY;
            } else if (event.changedTouches && event.changedTouches.length > 0) {
                clientX = event.changedTouches[0].clientX;
                clientY = event.changedTouches[0].clientY;
            } else {
                clientX = event.clientX;
                clientY = event.clientY;
            }

            return {
                x: (clientX - rect.left) / rect.width,
                y: (clientY - rect.top) / rect.height,
                rawX: clientX,
                rawY: clientY
            };
        }

        // === 鼠标/触摸事件处理 ===
        
        function handleStart(event) {
            event.preventDefault();
            const coords = getCoords(event);
            startX = coords.x;
            startY = coords.y;
            startTime = new Date().getTime();
            isDragging = true;
        }

        function handleEnd(event) {
            if (!isDragging) return;
            event.preventDefault();
            isDragging = false;
            
            const coords = getCoords(event);
            const endX = coords.x;
            const endY = coords.y;
            const duration = new Date().getTime() - startTime;
            
            // 计算移动距离
            const dist = Math.sqrt(Math.pow(endX - startX, 2) + Math.pow(endY - startY, 2));
            
            // 阈值判断：如果移动很小且时间很短，视为点击；否则视为拖拽
            // 距离阈值设为 0.01 (约1%的屏幕宽度)，时间设为 200ms
            if (dist < 0.01 && duration < 300) {
                // Click
                showClickEffect(coords.rawX, coords.rawY, 'red');
                sendAction({
                    type: 'click',
                    x_pct: endX,
                    y_pct: endY
                });
            } else {
                // Drag
                showClickEffect(coords.rawX, coords.rawY, 'blue'); // 蓝色表示拖拽结束
                sendAction({
                    type: 'drag',
                    start_x_pct: startX,
                    start_y_pct: startY,
                    end_x_pct: endX,
                    end_y_pct: endY
                });
            }
        }
        
        // 绑定事件
        container.addEventListener('mousedown', handleStart);
        container.addEventListener('mouseup', handleEnd);
        // container.addEventListener('mouseleave', handleEnd); // 移出区域也算结束

        container.addEventListener('touchstart', handleStart, {passive: false});
        container.addEventListener('touchend', handleEnd, {passive: false});

        function showClickEffect(x, y, color) {
            const dot = document.createElement('div');
            dot.style.position = 'fixed';
            dot.style.left = (x - 5) + 'px';
            dot.style.top = (y - 5) + 'px';
            dot.style.width = '10px';
            dot.style.height = '10px';
            dot.style.background = color;
            dot.style.borderRadius = '50%';
            dot.style.pointerEvents = 'none';
            dot.style.zIndex = 9999;
            document.body.appendChild(dot);
            setTimeout(() => dot.remove(), 500);
        }

        function sendAction(data) {
            return fetch('/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
        }

        function sendText() {
            const input = document.getElementById('inputText');
            const btn = document.getElementById('btnSend');
            const text = input.value;
            if (!text) return Promise.resolve();
            
            const originalText = btn.innerText;
            btn.innerText = "发送中...";
            btn.disabled = true;

            return sendAction({
                type: 'type',
                text: text
            }).then(() => {
                // 模拟延迟恢复
                setTimeout(() => {
                    input.value = '';
                    btn.innerText = "已发送";
                    setTimeout(() => {
                        btn.innerText = originalText;
                        btn.disabled = false;
                    }, 1000);
                }, 500);
            });
        }

        function sendEnter() {
             sendAction({type: 'press', key: 'Enter'});
        }

        // 监听输入框回车事件：自动发送文字 + 回车
        document.getElementById('inputText').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                sendText().then(() => {
                    // 文字发送请求成功后，追加发送回车指令
                    // 给一点点间隔让后端队列处理
                    setTimeout(() => {
                        sendEnter();
                        showClickEffect(window.innerWidth/2, window.innerHeight/2, 'green'); // 视觉反馈
                    }, 100);
                });
            }
        });

        function sendBackspace() {
             sendAction({type: 'press', key: 'Backspace'});
        }
        
        function sendRefresh() {
             if(confirm("确定要刷新远程浏览器页面吗？")) {
                 sendAction({type: 'refresh'});
             }
        }

        // 全局监听 F5 刷新
        document.addEventListener('keydown', function(e) {
            if (e.key === 'F5') {
                e.preventDefault(); // 阻止浏览器本身的刷新
                sendRefresh();
            }
        });
        
        // 简单的防抖刷新，防止图片卡死
        setInterval(() => {
            const img = document.getElementById('screen');
            // img.src = "/screenshot_stream?" + new Date().getTime(); 
            // 还是用流式传输比较好，不需要前端轮询
        }, 5000);
    </script>
</body>
</html>
"""

def get_config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return []

@app.route('/control/<site_name>')
def control_page(site_name):
    if not shared.is_interactive_mode:
        return "当前脚本未处于交互模式（可能正在后台运行或休眠），请等待通知唤起。", 404
    
    if shared.current_site_name != site_name:
        return f"脚本当前正在处理 [{shared.current_site_name}]，请稍后或检查链接是否过期。", 403

    return render_template_string(REMOTE_CONTROL_HTML, site_name=site_name)

@app.route('/screenshot_stream')
def screenshot_stream():
    """返回 MJPEG 视频流"""
    def generate():
        while shared.is_interactive_mode:
            frame = shared.get_screenshot()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                # 如果没有截图，发个空帧防止断开
                pass
            import time
            time.sleep(0.5) # 限制帧率，每秒2帧，节省带宽
            
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/action', methods=['POST'])
def handle_action():
    if not shared.is_interactive_mode:
        return jsonify({"error": "Not interactive mode"}), 400
        
    data = request.json
    shared.command_queue.put(data)
    return jsonify({"status": "ok"})

@app.route('/api/browser/show', methods=['POST'])
def browser_show():
    if shared.browser_manager:
        shared.browser_manager.move_browser_onscreen()
        return jsonify({"status": "ok", "message": "Browser moved onscreen"})
    return jsonify({"status": "error", "message": "Browser manager not initialized"}), 503

@app.route('/api/browser/hide', methods=['POST'])
def browser_hide():
    if shared.browser_manager:
        shared.browser_manager.move_browser_offscreen()
        return jsonify({"status": "ok", "message": "Browser moved offscreen"})
    return jsonify({"status": "error", "message": "Browser manager not initialized"}), 503

@app.route('/')
def index():
    return "多后台监控脚本 - 远程控制服务运行中"

def run_server():
    # 生产环境部署建议用 gevent 或其他 WSGI，这里开发用默认
    # threaded=True 允许并发请求（视频流会占用连接）
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)

if __name__ == '__main__':
    run_server()
