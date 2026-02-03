FROM python:3.11-slim

WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
# 禁用 pip 版本检查和 root 用户警告
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY . .

# 暴露端口
EXPOSE 5005

# 启动命令
# 使用 Gunicorn 启动 Flask 应用
# -w 4: 启动 4 个工作进程 (根据 CPU 核心数调整)
# -b 0.0.0.0:5005: 绑定端口
# app:app: 指定模块名和应用实例名
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5005", "app:app"]
