# 天气预警推送提醒（青岛各区县）

定时拉取和风天气「实时天气预警」信号，命中下雨/大风/雷电/冰雹/高温/寒潮/雾霾等类型时，通过 **PushPlus** 推送到你的个人微信。同一条预警只推一次。

- 数据源：和风天气（v1 实时天气预警接口）
- 推送：PushPlus（微信公众号接收）
- 运行：GitHub Actions 定时任务，**完全免费**，7×24 自动运行，不依赖你的电脑

---

## 一、准备两个免费账号

### 1. PushPlus（微信接收端）
1. 打开 <https://www.pushplus.plus>，微信扫码登录
2. 复制首页的 `token`
3. 微信里**关注公众号「pushplus 推送加」**（不关注收不到）

### 2. 和风天气（天气数据源）
1. 打开 <https://console.qweather.com> 注册并登录
2. 「项目管理」→「创建项目」→「创建凭据」→ 身份认证选 **API KEY**，复制 **32 位 API Key**
3. 左侧「设置」（<https://console.qweather.com/setting?lang=zh>）→ 复制专属 **API Host**（形如 `xxx.re.qweatherapi.com`）

> ⚠️ 和风天气 2025 年起改版，旧公共域名已停服，必须用自己的 API Host；旧 v7 预警接口也于 2026-10 停用，本程序已适配新接口。

---

## 二、部署到 GitHub Actions（完全免费）

### 第 1 步：注册 GitHub
打开 <https://github.com>，点 Sign up 注册账号（用邮箱即可）。

### 第 2 步：新建仓库
1. 右上角 **+** → **New repository**
2. 仓库名随便填，如 `weather-alert`
3. **必须选 Public（公共仓库）**——公共仓库 Actions 才完全免费
4. 其他不用勾，点 **Create repository**

### 第 3 步：上传文件
在新仓库页面点 **Add file → Upload files**，把下面这些**拖进去**（共 3 项）：

| 上传内容 | 说明 |
| --- | --- |
| `weather_alert.py` | 主程序 |
| `sent_alerts.json` | 去重记录（初始为空） |
| `.github` 文件夹 | 定时任务配置（含 workflows/weather.yml） |

> 🚨 **绝对不要上传 `config.json`**！那里面有你真实的密钥，传上去就泄露了。密钥会放在下一步的 Secrets 里。

拖完后点 **Commit changes** 提交。

### 第 4 步：添加密钥（Secrets）
1. 仓库页面点 **Settings** 标签
2. 左侧 **Secrets and variables → Actions**
3. 点 **New repository secret**，依次添加下面 **3 个**：

| Name（必须一字不差） | 填什么 |
| --- | --- |
| `PUSHPLUS_TOKEN` | 你的 PushPlus token |
| `QWEATHER_KEY` | 你的和风天气 32 位 API Key |
| `QWEATHER_HOST` | 你的和风天气 API Host（`xxx.re.qweatherapi.com`） |

### 第 5 步：测试运行
1. 点仓库顶部 **Actions** 标签
2. 左侧选「天气预警推送」这个工作流
3. 点右侧 **Run workflow** 按钮 → **Run workflow** 确认
4. 等几十秒，出现绿色 ✅ 就成功了，同时你微信会收到推送（当前无预警则只会记录日志，不推消息）

之后它会**每 15 分钟自动跑一次**，无需你管。

---

## 三、本地验证（可选，先在你电脑上跑通再上云）

在项目目录下执行：

```bash
# 自检（发测试消息 + 验证 key）
python weather_alert.py --selfcheck

# 跑一轮真实检查
python weather_alert.py

# 本地常驻（每 15 分钟查一次）
python weather_alert.py --loop
```

本地需要把 `config.json` 填好三个值：`pushplus_token`、`qweather_key`、`qweather_host`。

---

## 四、常用调整

### 加区县
编辑 `.github/workflows/weather.yml`，改这两行（加城市名 + 经纬度）：
```yaml
CITIES: '["崂山区", "即墨区", "胶州市"]'
LOCATION_MAP: '{"崂山区": {"lat": 36.10, "lon": 120.47}, "即墨区": {"lat": 36.39, "lon": 120.45}, "胶州市": {"lat": 36.26, "lon": 120.03}}'
```
（新城市经纬度可用 <https://www.qweather.com> 搜索该地名，网页 URL 里能看到）

### 改推送频率
改 workflow 里的 `cron`。`*/15 * * * *` 是每 15 分钟，`*/30 * * * *` 是每 30 分钟。

### 改预警类型
本地改 `config.json` 的 `include_types`；云端改脚本里的关键词（或后续把类型也做成 Secret）。

---

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `weather_alert.py` | 主程序（零第三方依赖，纯标准库） |
| `.github/workflows/weather.yml` | GitHub 定时任务配置 |
| `sent_alerts.json` | 去重记录，脚本自动更新并提交回仓库 |
| `config.json` | 本地配置（含真实密钥，**不上传 GitHub**） |
| `.gitignore` | 防止误传 config.json |
