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
CMD ["python", "app.py"]
