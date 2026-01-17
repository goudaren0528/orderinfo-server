# Use the official Playwright Python image which includes browsers and dependencies
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

# Set work directory
WORKDIR /app

# Install system dependencies for Chinese fonts and timezone
RUN apt-get update && apt-get install -y \
    fonts-noto-cjk \
    fonts-wqy-zenhei \
    tzdata \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose the web server port
EXPOSE 5000

# Run the application
CMD ["python", "main.py"]
