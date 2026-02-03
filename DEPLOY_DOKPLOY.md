# Dokploy 部署指南 (PostgreSQL 版)

本指南将指导你如何使用 Dokploy 部署租帮宝授权服务端，并连接 PostgreSQL 数据库。

## 1. 准备工作

确保你的代码已经提交并推送到 GitHub、GitLab 或其他 Git 仓库。

## 2. 创建 PostgreSQL 数据库

在部署应用之前，我们需要先在 Dokploy 中创建一个数据库。

1.  登录 Dokploy 面板。
2.  点击左侧菜单的 **"Database"**。
3.  点击 **"Create Database"**。
4.  选择 **"PostgreSQL"**。
5.  填写名称（例如 `auth-db`），其他保持默认或根据需要修改。
6.  点击 **"Create"**。
7.  创建完成后，进入该数据库详情页，找到 **"Connection Info"** (连接信息)。
8.  复制 **"Internal Connection URL"** (通常以 `postgresql://` 开头)。我们稍后会用到它。

## 3. 创建应用 (Application)

1.  点击左侧菜单的 **"Application"**。
2.  点击 **"Create Application"**。
3.  输入应用名称（例如 `auth-server`）。
4.  点击 **"Create"**。

## 4. 配置代码源与构建

进入刚创建的应用详情页：

1.  **General (常规)**:
    - **Repository**: 选择你的 Git 仓库。
    - **Branch**: 选择你的分支（通常是 `main` 或 `master`）。
    - **Build Path / Base Directory**: 输入 `/server`
        - **重要**: 因为我们的服务端代码都在 `server` 目录下，必须设置此项，否则找不到 Dockerfile。

2.  **Docker**:
    - **Dockerfile Path**: 输入 `./Dockerfile`
        - 因为我们将 Build Path 设置为了 `/server`，所以 Dockerfile 就在当前目录下。

3.  **Network (网络)**:
    - **Container Port**: 输入 `5005`
        - 我们的 `server/Dockerfile` 暴露的是 5005 端口。

## 5. 配置环境变量 (Environment)

点击 **"Environment"** 标签页，添加以下变量：

| 变量名 | 示例值 / 说明 |
| :--- | :--- |
| `DATABASE_URL` | 粘贴第 2 步中复制的 **Internal Connection URL** |
| `SECRET_KEY` | 生成一个随机字符串 (例如: `dk_8f7a...`) |
| `ADMIN_PASSWORD` | 设置你的管理员登录密码 |
| `ADMIN_API_KEY` | 设置一个复杂的 API 密钥，用于管理接口 |
| `LICENSE_PRIVATE_KEY` | (可选) 你的 Ed25519 私钥，用于生成授权码 |
| `LICENSE_PUBLIC_KEY` | (可选) 对应的公钥 |

**提示**: 如果没有现成的 Key，你可以先不填 `LICENSE_...` 相关的，系统会自动生成临时的（但重启后会变，建议生成固定的一对）。

## 6. 部署 (Deployment)

1.  回到 **"Deployments"** 标签页。
2.  点击 **"Deploy"** 按钮。
3.  查看 **Logs**，等待构建和启动完成。

## 7. 验证

部署成功后，Dokploy 会提供一个 **Domain** (域名)。
访问 `http://<你的域名>/login`，如果能看到登录页面，说明部署成功！

---

### 常见问题

**Q: 部署失败，提示 `COPY requirements.txt .` 找不到文件？**
A: 请检查 **Build Path** 是否正确设置为 `/server`。

**Q: 数据库连接失败？**
A: 确保 `DATABASE_URL` 填写正确，且 Dokploy 的应用和数据库在同一个网络下（默认通常是的）。

**Q: 如何查看日志？**
A: 在 Dokploy 应用详情页的 "Logs" 标签可以查看实时日志。
