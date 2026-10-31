# web2.0 Vercel 远程部署完整指南

本指南带你从零开始，将 miniQMT Web 界面部署到 Vercel，通过远程隧道连接 Windows 上的 QMT 服务。

---

## 0. 前置准备清单

| 项目 | 说明 | 如何获取 |
|------|------|---------|
| GitHub 账号 | 存放代码，连接 Vercel | [github.com](https://github.com) 免费注册 |
| Vercel 账号 | 部署前端 | [vercel.com](https://vercel.com) 用 GitHub 登录 |
| Node.js | 构建前端 (≥18) | [nodejs.org](https://nodejs.org) 下载 LTS 版 |
| Windows 电脑 | 运行 QMT 客户端 | 已安装 QMT 并登录 |
| Python 3.9 | 运行 xtquant_manager | 项目自带 conda env |

**全程耗时估算**: 首次约 30 分钟，后续更新约 5 分钟。

---

## 1. Windows 端 — 启动 xtquant_manager 网关

### 1.1 创建配置文件

在项目根目录（与 `main.py` 同级）确认 `account_config.json` 中已有账号配置：

```json
{
  "account_id": "你的QMT账号",
  "qmt_path": "C:/QMT/userdata_mini",
  "account_type": "STOCK"
}
```

再新建或修改 `xtquant_manager_config.json`，只保存网关运行参数：

```json
{
  "host": "0.0.0.0",
  "port": 8888,
  "api_token": "生成一个随机字符串填入此处",
  "rate_limit": 60,
  "enable_stop_profit": true
}
```

> **重要**: `api_token` 是唯一的安全防线，请用密码管理器生成一个强随机字符串（至少 32 位）。
> 例如用 PowerShell 生成：`powershell -c "[Convert]::ToBase64String((1..32|%{Get-Random -Max 256}))"`

### 1.2 启动网关

```bash
# 打开 miniqmt.bat
miniqmt.bat

# 在菜单中选择 [d] 启动 xtquant_manager 服务
```

看到 `✓ xtquant_manager 已启动: http://127.0.0.1:8888` 即成功。

### 1.3 本地验证

```bash
# 新开一个终端，测试公开健康检查（不带 Token 只返回汇总计数）
curl http://127.0.0.1:8888/api/v1/health

# 带 Token 可查看账号明细
curl -H "X-API-Token: 生成一个随机字符串填入此处" http://127.0.0.1:8888/api/v1/health
```

不带 Token 应返回 `total` / `healthy` 等汇总信息；带 Token 时才会返回 `accounts` 明细。

---

## 2. Windows 端 — 创建公网隧道

Vercel 托管的前端在**用户浏览器**中运行，浏览器需要能直接访问 QMT 服务器。
因此需要将 `localhost:8888` 暴露到公网。

### 方案 A：Cloudflare Tunnel（推荐，免费，无需信用卡）

```bash
# 1. 下载 cloudflared
# 访问 https://github.com/cloudflare/cloudflared/releases
# 下载 Windows 64-bit 版本 (cloudflared-windows-amd64.exe)
# 重命名为 cloudflared.exe 放到 C:\Windows\System32\ 或项目目录

# 2. 启动隧道
cloudflared tunnel --url http://localhost:8888
```

**首次运行会输出**:
```
INF Thank you for trying Cloudflare Tunnel.
INF +--------------------------------------------------------------------------------------------+
INF |  Your quick tunnel URL: https://xxxx-xxxx-xxxx.trycloudflare.com                           |
INF +--------------------------------------------------------------------------------------------+
```

> 记下这个 URL（如 `https://xxx.trycloudflare.com`），后续步骤要用。
> 注意：免费隧道 URL 每次重启会变。如需固定域名，可注册 Cloudflare 账号使用 Named Tunnel。

### 方案 B：Tailscale Funnel（已有 Tailscale 网络）

```bash
# 安装 Tailscale 并加入网络后
tailscale funnel 8888
# → https://your-machine.tailnet-name.ts.net
```

### 方案 C：frp 内网穿透（自建服务器/VPS）

在 VPS 上部署 frps，Windows 上运行 frpc：

```ini
# frpc.ini
[common]
server_addr = 你的VPS公网IP
server_port = 7000

[qmt]
type = tcp
local_ip = 127.0.0.1
local_port = 8888
remote_port = 8888
```

### 验证隧道

```bash
# 用隧道 URL 测试健康检查（替换为你的实际 URL）
curl https://xxx.trycloudflare.com/api/v1/health
```

返回健康数据即隧道正常。

---

## 3. Vercel 端 — 部署前端

### 3.1 本地构建

```bash
# 进入 web2.0 目录
cd web2.0

# 安装依赖（仅首次）
npm install

# 生产构建
npm run build
```

构建成功输出：
```
✓ built in 2.xx s
dist/index.html         0.67 kB
dist/assets/index-*.css  ~36 kB
dist/assets/index-*.js   ~119 kB
```

### 3.2 部署到 Vercel

**项目已包含 `vercel.json`，Vercel 会自动识别正确的构建命令和输出目录。**

**方式一：Vercel CLI（推荐首次部署）**

```bash
# 安装 Vercel CLI（全局安装，仅首次）
npm install -g vercel

# 在 web2.0 目录下部署
cd web2.0
vercel deploy dist/ --prod
```

CLI 会引导你登录，确认项目设置后直接部署。输出类似：
```
🔗  Deployment URL: https://your-app.vercel.app
```

**方式二：GitHub 自动部署（推荐后续更新）**

1. 将项目推送到 GitHub
2. 打开 [vercel.com/new](https://vercel.com/new)，导入你的仓库
3. Vercel 会自动读取根目录 `vercel.json` 中的配置，无需手动设置
4. 点击 Deploy。以后每次 `git push` 自动触发部署。

**方式三：手动上传**

1. 打开 [vercel.com/new](https://vercel.com/new)
2. 直接拖拽 `web2.0/dist/` 文件夹到页面
3. 点击 Deploy

### 3.3 验证部署

浏览器打开 Vercel 分配的 URL（如 `https://xxx.vercel.app`），应看到 miniQMT 2.0 界面。

---

## 4. 浏览器端 — 配置连接

### 4.1 打开设置面板

在部署好的页面中，点击顶部栏 **齿轮 ⚙ 图标** → 「连接设置」。

### 4.2 填写连接信息

| 字段 | 值 |
|------|-----|
| 后端模式 | **网关模式** (xtquant_manager) |
| 网关地址 | `https://xxx.trycloudflare.com`（第 2 步的隧道 URL） |
| API Token | 第 1.1 步设置的 `api_token` |

### 4.3 测试连接

点击 **「测试连接」** 按钮。

- ✓ 成功 → `✓ 连接成功 — N 个账号, N 个在线`
- ✗ 失败 → 检查：
  - 隧道是否在运行（Windows 终端上 cloudflared 进程存活）
  - URL 是否正确复制（注意不要漏 `https://`）
  - Token 是否匹配配置文件

### 4.4 添加账户

保存连接配置后，web2.0 会携带 Token 自动调用 `discoverAccounts()`，从网关同步真实账号 ID 到下拉列表。若未填 Token，健康检查可能仍能测通，但账号发现和持仓/成交等数据端点会返回 401。

如需手动维护账户条目，可在顶部账户选择器中编辑本地保存的账户名称和 Flask 直连地址；远程网关模式推荐优先使用自动发现结果。

---

## 5. 安全清单

| 检查项 | 状态 | 说明 |
|--------|------|------|
| `api_token` 已设置 | ☐ | 在 `xtquant_manager_config.json` 中，使用强随机字符串 |
| Token 不在 Git 仓库中 | ☐ | `account_config.json` 和 `xtquant_manager_config.json` 已被 `.gitignore` 排除 |
| 使用 HTTPS 隧道 | ☐ | Cloudflare Tunnel 自动提供；frp 需自行配置 SSL |
| 速率限制已启用 | ☐ | 默认 60 req/min，可在配置中调低 |
| Token 定期更换 | ☐ | 建议每月更换一次 |
| 访问日志已开启 | ☐ | xtquant_manager 自动记录所有 API 请求 |

### 安全警告说明

连接设置面板会自动检测安全问题并显示警告：
- **Mixed Content**: HTTPS 页面请求 HTTP 后端——浏览器会阻止。解决方案：使用 Cloudflare Tunnel（自动 HTTPS）。
- **无 Token 远程连接**: 任何人知道 URL 都能访问你的 QMT。解决方案：设置强 `api_token`。

---

## 6. 故障排查

### 隧道不可达

```
现象: 测试连接报 "fetch failed" 或 "Network error"
```

| 可能原因 | 检查方法 | 解决 |
|---------|---------|------|
| cloudflared 进程退出 | Windows 终端看是否还在运行 | 重新执行 `cloudflared tunnel --url http://localhost:8888` |
| 本地网关未启动 | `curl http://127.0.0.1:8888/api/v1/health` | 用 miniqmt.bat 菜单 [d] 启动 |
| Windows 防火墙阻止 | 临时关闭防火墙测试 | 添加 cloudflared 到防火墙例外 |

### 401 Unauthorized

```
现象: 页面能加载但 API 请求返回 401
```

| 可能原因 | 解决 |
|---------|------|
| Token 不匹配 | 确认连接设置中的 Token 与 `xtquant_manager_config.json` 中完全一致 |
| Token 包含空白字符 | 不要复制多余的空格或换行 |

### 页面空白 / 接口无数据

```
现象: 界面能打开但表格为空、余额显示 0
```

| 可能原因 | 解决 |
|---------|------|
| QMT 未登录 | 在 Windows 上确认 QMT 客户端已启动并登录 |
| 账号 ID 不匹配 | 确认 `account_config.json` 中 `account_id` 与 QMT 账号一致 |
| QMT 路径错误 | 确认 `account_config.json` 中的 `qmt_path` 指向正确的 `userdata_mini` 目录 |

### CORS 错误

```
现象: 浏览器 F12 Console 显示 CORS 相关错误
```

xtquant_manager 已配置 `allow_origins=["*"]`，正常情况下不会出现。
如果仍有问题，检查是否有反向代理修改了 CORS 头。

### 网关模式能力边界

web2.0 通过 xtquant_manager 网关连接时，以下功能为**只读展示**：

| 功能 | 网关模式 | 说明 |
|------|---------|------|
| 持仓查看 (名称/盈亏/涨跌幅/止损/建仓) | ✅ 完整 | QMT实时数据 + SQLite持久化元数据合并；涨跌幅按 tick `lastPrice` / `lastClose` 计算，股票/ETF/基金裸代码会自动补 `.SZ` / `.SH` 后缀 |
| 多账号切换 | ✅ 完整 | X-Account-Id 请求头隔离 |
| 账号资产/状态 | ✅ 完整 | 可用余额/市值/总资产 |
| 交易记录 | ✅ 完整 | 成交和委托记录 |
| 参数设置面板 | 🔒 只读 | 修改保存需 Flask 直连模式 |
| 自动操作总开关 | 🔒 不可用 | 需 Flask 直连模式 |
| 模拟买入/清空/导入/初始化 | 🔒 不可用 | 需 Flask 直连模式 |
| 动态止盈控制 | 🔒 不可用 | 需 Flask 直连模式 |
| SSE 实时推送 | 🔒 不可用 | 依赖 3s/10s 轮询更新 |

如需完整功能（配置管理/自动操作总开关/模拟交易），请使用 Flask 直连模式：
- 通过 miniqmt.bat 菜单 [7] → 选择 `web1.0 (Flask :5000起)`
- 或在连接设置中切换到「直连模式」并为每个账户配置独立 Flask 地址

---

## 7. 后续更新部署

### v3.9.6 版本号与浏览器缓存

miniQMT 发布版本由仓库根目录 `release_version.json` 提供，当前为 **v3.9.6**。它在 Vite 构建阶段注入页面标题；更新源码后必须重新 `npm run build` 并部署新的 `dist/`，只改 JSON 不会更新已部署页面。

`web2.0/package.json` 的 `2.0.0` 是前端包元数据，不用于 miniQMT 发布版本。部署后检查浏览器标题为 `MiniQMT Web2 - v3.9.6`；已安装 PWA 的用户应关闭旧页面并重新打开，必要时刷新以接收新的 Service Worker。web2.0 仍保持只读，配置、交易和运行数据清理使用各自操作入口。


### 方式 A: GitHub 自动部署（推荐）

```bash
git add .
git commit -m "更新 web2.0"
git push
# Vercel 自动检测并重新构建部署，约 1 分钟生效
```

### 方式 B: 手动重新部署

```bash
cd web2.0
npm run build
vercel deploy dist/ --prod
```

---

## 附：环境变量参考

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VITE_DEFAULT_BACKEND` | `auto` | 后端模式 (flask/xtquant/auto) |
| `VITE_DEFAULT_XTQUANT_URL` | `http://127.0.0.1:8888` | xtquant_manager 地址 |
| `VITE_DEFAULT_TOKEN` | (空) | 默认 API Token |
| `VITE_APP_TITLE` | `miniQMT 2.0` | 页面标题 |

环境变量可在 `.env.production` 中修改，构建时打包进 JS。
建议留空，改为在 UI 连接设置面板中动态配置（保存到 localStorage）。
