import requests
import sys
import os

# 从 main.py 复制的 Webhook 配置
WECOM_WEBHOOK_URL = [
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=d954ddc2-fe59-47ae-b0ae-44621040f33d",
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=69e5af6a-1d92-4b73-8bf9-157c22480833"
]

def send_notification(content):
    """发送通知到所有配置的 Webhook"""
    if not content:
        print("内容为空，取消发送")
        return

    headers = {"Content-Type": "application/json"}
    
    # 构造文本消息，并 @所有人
    # 企业微信限制：Markdown 类型不支持 @all，必须使用 Text 类型才能 @所有人
    data = {
        "msgtype": "text",
        "text": {
            "content": content,
            "mentioned_list": ["@all"]
        }
    }
    
    # 同时发送文本格式作为备选（防止 Markdown 渲染问题，或者根据喜好）
    # 这里为了简单，我们优先使用 Markdown，因为主脚本也是用的 Markdown
    # 如果用户想发纯文本，可以改用 text 类型
    
    print(f"正在发送通知: \n{content}\n")
    
    for url in WECOM_WEBHOOK_URL:
        try:
            # 简单的 Key 掩码用于打印
            key_suffix = url.split("key=")[-1][-6:]
            
            response = requests.post(url, json=data, headers=headers)
            if response.status_code == 200:
                print(f"✅ 发送成功 (Key: ...{key_suffix})")
            else:
                print(f"❌ 发送失败 (Key: ...{key_suffix}): {response.text}")
        except Exception as e:
            print(f"❌ 发送异常: {e}")

def main():
    # 1. 尝试从命令行参数获取内容
    if len(sys.argv) > 1:
        # 如果参数是一个文件路径，则读取文件内容
        input_path = sys.argv[1]
        if os.path.exists(input_path) and os.path.isfile(input_path):
            try:
                with open(input_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                print(f"已读取文件内容: {input_path}")
            except Exception as e:
                print(f"读取文件失败: {e}")
                return
        else:
            # 否则将参数作为普通文本
            content = " ".join(sys.argv[1:])
            # 处理命令行输入的换行符 (用户可以用 \n 显式换行)
            content = content.replace('\\n', '\n')
    else:
        # 2. 交互式输入
        print("=== 手动发送企业微信通知 ===")
        print("请输入通知内容 (输入空行结束输入):")
        
        lines = []
        while True:
            try:
                line = input()
                if not line:
                    break
                lines.append(line)
            except EOFError:
                break
        
        content = "\n".join(lines)

    if content.strip():
        send_notification(content)
    else:
        print("未输入内容。")

if __name__ == "__main__":
    main()
