# gpt-card

一个基于 **FastAPI + 协议请求** 的 Stripe Checkout 自动化服务，内置：

- 协议级 checkout 流程（不依赖 Playwright）
- hCaptcha 自动解题（可选）
- 人工接管（manual handoff）+ 自动状态检测
- 本地可视化 UI（`/`）

---

## 1. 功能概览

### 核心接口

- `POST /api/v1/checkout`：发起支付主流程
- `POST /api/v1/checkout/manual-handoff/complete`：人工验证后回传 `captcha_token`
- `POST /api/v1/checkout/manual-handoff/status`：检查人工流程是否已在 Stripe 页面完成
- `GET /healthz`：健康检查
- `GET /`：本地调试 UI

### 人工接管流程（简化后）

当没有 `captcha_token` 且自动解题不可用/失败时，`/api/v1/checkout` 返回：

- `status: "manual_required"`
- `handoff.handoff_id`
- `handoff.manual_verify_url`
- `handoff.hcaptcha.site_key/rqdata`

你可以两种方式继续：

1. **自动检测模式（推荐）**
   - 打开 `manual_verify_url`，在 Stripe 页面完成人工验证/支付
   - 前端会自动轮询 `manual-handoff/status`，检测到完成后自动结束接管

2. **手动回传 token 模式**
   - 人工拿到 token 后，调用 `manual-handoff/complete` 回传

> 注意：点击 `manual_verify_url` 后是否需要补填信息由 Stripe 会话状态决定（可能直接验证，也可能要求补一部分字段）。

---

## 2. 快速启动

### 2.1 本地 Python 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

访问：

- UI: `http://127.0.0.1:8888/`
- 健康检查: `http://127.0.0.1:8888/healthz`

### 2.2 Docker 启动

```bash
docker build -t gpt-card:latest .
docker run -d --name gpt-card -p 8888:8888 gpt-card:latest
```

### 2.3 Docker Compose 启动

```bash
docker compose up -d --build
```

---

## 3. 配置项（环境变量）

| 变量名 | 默认值 | 说明 |
|---|---:|---|
| `APP_HOST` | `0.0.0.0` | 服务监听地址 |
| `APP_PORT` | `8888` | 服务监听端口 |
| `MANUAL_HANDOFF_TTL_SECONDS` | `900` | 人工接管会话有效期（秒） |

示例：

```bash
export APP_HOST=0.0.0.0
export APP_PORT=8888
export MANUAL_HANDOFF_TTL_SECONDS=1200
python server.py
```

---

## 4. API 示例

### 4.1 发起 checkout

```bash
curl -X POST http://127.0.0.1:8888/api/v1/checkout \
  -H 'Content-Type: application/json' \
  -d '{
    "checkout_url": "https://checkout.stripe.com/c/pay/cs_xxx",
    "card": {
      "number": "4242424242424242",
      "exp_month": "12",
      "exp_year": "2030",
      "cvc": "123",
      "name": "Test User",
      "email": "test@example.com",
      "address": {
        "line1": "123 Test St",
        "line2": "Apt 8",
        "city": "New York",
        "state": "NY",
        "postal_code": "10001",
        "country": "US"
      }
    }
  }'
```

### 4.2 人工回传 token

```bash
curl -X POST http://127.0.0.1:8888/api/v1/checkout/manual-handoff/complete \
  -H 'Content-Type: application/json' \
  -d '{"handoff_id":"xxx","captcha_token":"xxx"}'
```

### 4.3 检查人工接管状态

```bash
curl -X POST http://127.0.0.1:8888/api/v1/checkout/manual-handoff/status \
  -H 'Content-Type: application/json' \
  -d '{"handoff_id":"xxx"}'
```

---

## 5. 部署到 DigitalOcean（DO）教程

下面给你一套 **Ubuntu Droplet + Docker + Nginx + HTTPS** 的可落地方案。

### 5.1 准备 Droplet

建议配置：

- Ubuntu 22.04+
- 1 vCPU / 2GB RAM 起步
- 开放端口：`22`、`80`、`443`

登录服务器：

```bash
ssh root@<your_droplet_ip>
```

### 5.2 安装 Docker / Compose

```bash
apt update && apt install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 5.3 拉代码并启动服务

```bash
mkdir -p /opt/gpt-card && cd /opt/gpt-card
# 你可以改成自己的仓库地址
git clone <your_repo_url> .

# 可选：创建环境变量文件
cat > .env <<EOL
APP_HOST=0.0.0.0
APP_PORT=8888
MANUAL_HANDOFF_TTL_SECONDS=900
EOL

docker compose up -d --build
```

检查服务：

```bash
docker compose ps
curl http://127.0.0.1:8888/healthz
```

### 5.4 安装并配置 Nginx 反向代理

```bash
apt install -y nginx
```

创建站点配置 `/etc/nginx/sites-available/gpt-card.conf`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

启用并重载：

```bash
ln -sf /etc/nginx/sites-available/gpt-card.conf /etc/nginx/sites-enabled/gpt-card.conf
nginx -t && systemctl reload nginx
```

### 5.5 申请 HTTPS（Let’s Encrypt）

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

证书自动续期测试：

```bash
certbot renew --dry-run
```

### 5.6 常用运维命令

```bash
# 查看日志
docker compose logs -f

# 重启
docker compose restart

# 更新代码并重建
cd /opt/gpt-card
git pull
docker compose up -d --build
```

---

## 6. 还能怎么继续优化（建议）

1. **持久化 handoff 状态**
   - 现在是内存存储，服务重启会丢失。建议接 Redis（带 TTL）或 SQLite。

2. **结构化日志 + 请求追踪 ID**
   - 加 request_id，便于在异常时定位完整链路。

3. **限流与鉴权**
   - 给 API 增加 token 鉴权和每 IP 限流，防止滥用。

4. **更严格的状态机**
   - 将 `manual_required/pending/completed/error` 抽成明确状态机，降低边界问题。

5. **自动化测试**
   - 增加至少 3 类测试：数据模型校验、handoff 生命周期、接口集成测试。

---

## 7. 免责声明

请确保你的使用场景符合 Stripe、目标站点、以及所在地法律法规要求。该项目仅用于授权环境下的技术研究与系统集成。
