#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天气预警推送提醒（本地验证版）

功能：
  1. 从和风天气拉取指定区县的「实时天气预警」信号（v1 新接口）
  2. 命中预警类型（下雨/大风/雷电/冰雹/高温/寒潮/雾霾等）后，通过 PushPlus 推送到微信
  3. 同一条预警只推一次（本地 sent_alerts.json 去重），预警更新/解除会再次推送
  4. 附带自检模式，本地验证「PushPlus 是否通、和风 key 是否有效、当前有哪些预警」

零第三方依赖，纯 Python 标准库，方便以后直接搬上云函数。

用法：
  python weather_alert.py                # 跑一轮：查预警 → 命中则推送
  python weather_alert.py --selfcheck    # 自检：测试推送通道 + 天气 key + 列出当前预警
  python weather_alert.py --loop         # 本地常驻：每隔 check_interval 分钟自动跑一轮

说明：
  和风天气 2026-10-01 起停用了旧版 v7 预警接口，现改用 v1 实时天气预警接口：
  GET /weatheralert/v1/current/{纬度}/{经度}?key=xxx
"""

import json
import os
import sys
import time
import gzip
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# 尽量统一控制台为 UTF-8，避免 Windows 下中文乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
# 去重记录存放位置：云函数无本地持久化，可设环境变量 SENT_PATH 指向挂载的 CFS 目录（如 /mnt）
SENT_PATH = Path(os.environ.get("SENT_PATH") or (BASE_DIR / "sent_alerts.json"))

# 和风天气接口路径（域名用 config.json 的 qweather_host）
GEO_PATH = "/geo/v2/city/lookup"
WARNING_PATH_TMPL = "/weatheralert/v1/current/{lat}/{lon}"

# PushPlus 推送接口（推给自己）
PUSHPLUS_URL = "http://www.pushplus.plus/send"

# WxPusher 推送接口（免费群发：推给所有关注应用的人，供几十位同事接收）
WXPUSHER_URL = "https://wxpusher.zjiecode.com/api/send/message"

# 本地循环模式：每多少分钟查一次（上云后由云函数定时触发器接管，此参数仅本地用）
CHECK_INTERVAL_MIN = 15

# 去重记录最多保留多少条预警 id，避免文件无限变大
MAX_SENT_IDS = 1000

# 预警颜色/严重程度 → 中文等级
COLOR_CN = {
    "blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色",
    "green": "绿色", "amber": "橙色", "purple": "紫色",
    "white": "白色", "gray": "灰色", "black": "黑色",
}
SEVERITY_CN = {
    "minor": "蓝色", "moderate": "黄色", "severe": "橙色", "extreme": "红色",
    "unknown": "未知",
}

# 天气类型 → 防范/处置措施（通用底稿，供参考，请按单位规章制度修订）
# 说明：这是面向地铁/轨道交通行车安全起草的通用安全常识，非官方条文；
#       高架/地面段的具体限速值、处置流程等，请按单位应急预案替换或补充。
_ACT_WIND = (
    "1. 加强线路瞭望，注意接触网异物、彩钢瓦、广告牌等高空坠物\n"
    "2. 高架/地面段按大风应急预案限速运行\n"
    "3. 关注弓网状态，发现异物侵入限界立即报告并处置\n"
    "4. 必要时申请临时扣车或停运"
)
_ACT_RAIN = (
    "1. 加强瞭望，注意线路积水、边坡溜坍、排水设施状态\n"
    "2. 通过积水区段控制车速，防止水淹轨面\n"
    "3. 关注车站出入口、隧道口、风井防汛情况\n"
    "4. 发现险情立即报告，必要时限速或停运"
)
_ACT_THUNDER = (
    "1. 户外作业人员立即停止作业，撤离至安全区域\n"
    "2. 关注接触网、信号设备防雷及跳闸情况\n"
    "3. 加强设备监控，防范雷击影响行车"
)
_ACT_SNOW = (
    "1. 加强瞭望，注意轨面结冰、积雪\n"
    "2. 高架/地面段按冰雪天预案限速运行\n"
    "3. 关注道岔融雪装置与防滑措施\n"
    "4. 车辆段、折返线注意防滑、除冰除雪"
)
_ACT_FOG = (
    "1. 低能见度时加强瞭望，按规定限速运行\n"
    "2. 加强鸣笛示警\n"
    "3. 必要时申请临时扣车"
)
_ACT_HEAT = (
    "1. 注意防暑降温，合理安排作业与休息\n"
    "2. 关注钢轨胀轨、接触网弛度变化\n"
    "3. 加强设备防过热检查"
)
_ACT_COLD = (
    "1. 注意防寒保暖\n"
    "2. 关注设备防冻、管道防裂\n"
    "3. 高架/地面段注意结冰风险"
)
DEFAULT_ACTION_MAP = {
    "大风": _ACT_WIND, "台风": _ACT_WIND, "强对流": _ACT_WIND,
    "暴雨": _ACT_RAIN, "大雨": _ACT_RAIN, "冰雹": _ACT_RAIN,
    "雷电": _ACT_THUNDER, "雷暴": _ACT_THUNDER,
    "暴雪": _ACT_SNOW, "道路结冰": _ACT_SNOW, "霜冻": _ACT_SNOW, "低温": _ACT_SNOW,
    "大雾": _ACT_FOG, "霾": _ACT_FOG,
    "高温": _ACT_HEAT,
    "寒潮": _ACT_COLD, "降温": _ACT_COLD,
}


# ---------------------------------------------------------------- 基础工具

def log(msg: str):
    """带时间戳的日志输出。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def _read_body(resp) -> bytes:
    """读取响应体，自动处理 gzip 压缩。"""
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return raw


def http_get_json(url: str, params: dict, timeout: int = 15) -> dict:
    """发送 GET 请求并解析 JSON。失败时抛出带提示的异常。"""
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(
        full_url,
        headers={"User-Agent": "weather-alert/1.0", "Accept-Encoding": "gzip"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(_read_body(resp).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = _read_body(e)
        detail = raw.decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code}：{detail}") from e
    except Exception as e:
        raise RuntimeError(f"网络请求失败：{e}") from e


def http_post_json(url: str, data: dict, timeout: int = 15) -> dict:
    """发送 POST JSON 请求（PushPlus 用）。"""
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "weather-alert/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(_read_body(resp).decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = _read_body(e)
        raise RuntimeError(f"HTTP {e.code}：{raw.decode('utf-8', 'ignore')[:300]}") from e
    except Exception as e:
        raise RuntimeError(f"网络请求失败：{e}") from e


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    """
    读取配置。
    优先用环境变量（GitHub Actions 通过 Secrets 注入），否则读 config.json。
    这样本地用 config.json，云端用环境变量，同一份代码通用。
    """
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    # 环境变量覆盖（键名 -> 配置字段）
    env_map = {
        "PUSHPLUS_TOKEN": "pushplus_token",
        "QWEATHER_KEY": "qweather_key",
        "QWEATHER_HOST": "qweather_host",
        "WXPUSHER_APP_TOKEN": "wxpusher_app_token",
        "WECOM_WEBHOOK_URL": "wecom_webhook_url",
    }
    for env_name, key in env_map.items():
        val = os.environ.get(env_name, "").strip()
        if val:
            cfg[key] = val

    cities_env = os.environ.get("CITIES", "").strip()
    if cities_env:
        try:
            cfg["cities"] = json.loads(cities_env)
        except Exception:
            cfg["cities"] = [c.strip() for c in cities_env.split(",") if c.strip()]

    loc_env = os.environ.get("LOCATION_MAP", "").strip()
    if loc_env:
        try:
            cfg["location_map"] = json.loads(loc_env)
        except Exception:
            pass

    # 防范措施库：默认用代码里的 DEFAULT_ACTION_MAP，config.json 或 env 可覆盖
    action_env = os.environ.get("ACTION_MAP", "").strip()
    if action_env:
        try:
            cfg["action_map"] = json.loads(action_env)
        except Exception:
            pass
    elif "action_map" not in cfg:
        cfg["action_map"] = DEFAULT_ACTION_MAP

    return cfg


# ---------------------------------------------------------------- 天气

def resolve_coords(city: str, key: str, host: str, loc_map: dict) -> tuple:
    """
    解析城市的经纬度（两位小数，用于 v1 预警接口）。
    优先用 config 里预置的 location_map，没有就调 GeoAPI 自动查。
    返回 (lat, lon) 字符串。
    """
    if city in loc_map:
        m = loc_map[city]
        lat, lon = float(m.get("lat", 0)), float(m.get("lon", 0))
        if lat and lon:
            return f"{lat:.2f}", f"{lon:.2f}"

    log(f"  · 预置表里没有「{city}」，调用 GeoAPI 自动查询经纬度 …")
    data = http_get_json(f"https://{host}{GEO_PATH}", {"location": city, "key": key})
    if data.get("code") != "200":
        raise RuntimeError(f"GeoAPI 查询「{city}」失败：code={data.get('code')}")

    locations = data.get("location") or []
    if not locations:
        raise RuntimeError(f"GeoAPI 查不到「{city}」，请确认城市名是否正确")

    chosen = None
    # 优先选山东省/青岛的匹配，避免同名异地（如青岛崂山 vs 其他省份）
    for loc in locations:
        if loc.get("adm1") == "山东省" and loc.get("adm2") == "青岛":
            chosen = loc
            break
    if chosen is None:
        base = city.rstrip("区市县")
        for loc in locations:
            if base in (loc.get("name") or ""):
                chosen = loc
                break
    if chosen is None:
        chosen = locations[0]

    lat, lon = float(chosen.get("lat")), float(chosen.get("lon"))
    log(f"  · 「{city}」→ {chosen.get('name')}（{lat:.2f}, {lon:.2f}）")
    return f"{lat:.2f}", f"{lon:.2f}"


def fetch_alerts(lat: str, lon: str, key: str, host: str) -> list:
    """拉取某经纬度当前生效的天气预警列表（v1 接口）。"""
    path = WARNING_PATH_TMPL.format(lat=lat, lon=lon)
    data = http_get_json(f"https://{host}{path}", {"key": key, "lang": "zh"})
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"预警接口错误：{err.get('title', '')} {err.get('detail', '')}")
    return data.get("alerts") or []


def match_types(type_name: str, include_types: list) -> bool:
    """判断预警类型是否命中白名单。白名单为空 = 推所有。"""
    if not include_types:
        return True
    return any(kw in (type_name or "") for kw in include_types)


def alert_level(a: dict) -> str:
    """从预警里解析中文等级（优先颜色码，其次严重程度）。"""
    color = (a.get("color") or {}).get("code", "")
    if color in COLOR_CN:
        return COLOR_CN[color]
    return SEVERITY_CN.get(a.get("severity", ""), "")


# ---------------------------------------------------------------- 去重

def load_sent_ids() -> set:
    if SENT_PATH.exists():
        try:
            with open(SENT_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f).get("sent_ids", []))
        except Exception:
            return set()
    return set()


def save_sent_ids(sent_ids: set):
    ids = list(sent_ids)[-MAX_SENT_IDS:]
    with open(SENT_PATH, "w", encoding="utf-8") as f:
        json.dump({"sent_ids": ids}, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 推送

def pushplus_send(token: str, title: str, content: str) -> bool:
    """通过 PushPlus 推送。返回是否成功。"""
    data = {"token": token, "title": title, "content": content, "template": "txt"}
    resp = http_post_json(PUSHPLUS_URL, data)
    if resp.get("code") == 200:
        return True
    log(f"  ! PushPlus 返回异常：code={resp.get('code')}，msg={resp.get('msg')}")
    return False


def wxpusher_send(app_token: str, title: str, content: str) -> bool:
    """
    通过 WxPusher 群发（免费、无实名，推给所有关注应用的人）。
    不传 uids/topicIds，即默认推送给应用的全部关注用户。
    contentType=1 表示纯文字（各端兼容性最好，微信渠道同样支持）。
    返回是否成功（成功码为 1000）。
    """
    data = {
        "appToken": app_token,
        "summary": title[:100],  # 摘要最长 100 字符
        "content": content,
        "contentType": 1,        # 1=文字 2=html 3=markdown
    }
    resp = http_post_json(WXPUSHER_URL, data)
    if resp.get("code") == 1000:
        return True
    log(f"  ! WxPusher 返回异常：code={resp.get('code')}，msg={resp.get('msg')}")
    return False


def _truncate_bytes(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节数安全截断字符串，避免截断到多字节字符中间。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def wecom_webhook_send(webhook_url: str, title: str, content: str) -> bool:
    """
    通过企业微信群机器人（消息推送 Webhook）推送。
    这是免费群发方案：群里所有人自动收到，无需装 App、无需同事任何操作。
    文本消息 content 最长 2048 字节（UTF-8），超出自动截断。
    成功时响应 errcode == 0。
    """
    text = _truncate_bytes(f"{title}\n\n{content}", 2000)
    data = {"msgtype": "text", "text": {"content": text}}
    resp = http_post_json(webhook_url, data)
    if resp.get("errcode") == 0:
        return True
    log(f"  ! 企业微信返回异常：errcode={resp.get('errcode')}，errmsg={resp.get('errmsg')}")
    return False


def format_alert(a: dict) -> str:
    """把一条预警格式化成可读文本。"""
    type_name = (a.get("eventType") or {}).get("name", "未知")
    lines = [
        f"类型：{type_name}",
        f"等级：{alert_level(a) or '未知'}",
        f"发布机构：{a.get('senderName') or '未知'}",
        f"发布时间：{a.get('issuedTime') or '未知'}",
    ]
    if a.get("effectiveTime") or a.get("expireTime"):
        lines.append(f"生效时段：{a.get('effectiveTime') or '?'} 至 {a.get('expireTime') or '?'}")

    desc = (a.get("description") or "").strip()
    if desc:
        lines.append("")
        lines.append(desc)

    instruction = (a.get("instruction") or "").strip()
    if instruction:
        lines.append("")
        lines.append("【防御指南】")
        lines.append(instruction)
    return "\n".join(lines)


def match_actions(type_name: str, action_map: dict) -> list:
    """
    按预警类型匹配防范措施，返回 [(关键词, 措施文本), ...]。
    用子串匹配：action_map 的 key 出现在 type_name 里即命中。
    """
    matched = []
    for kw, action in action_map.items():
        if kw and kw in (type_name or ""):
            matched.append((kw, action))
    return matched


def append_actions(content: str, type_name: str, action_map: dict) -> str:
    """在预警正文末尾追加防范/处置措施。命中多条时逐条列出。"""
    actions = match_actions(type_name, action_map)
    if not actions:
        return content
    parts = [content, "", "──────────", "【防范措施】"]
    for kw, act in actions:
        if len(actions) > 1:
            parts.append(f"〔{kw}〕")
        parts.append(act)
    return "\n".join(parts)


# ---------------------------------------------------------------- 主流程

def run_once(cfg: dict) -> dict:
    """执行一轮：查各城市预警 → 过滤 → 去重 → 推送。返回统计结果。"""
    key = cfg.get("qweather_key", "")
    token = cfg.get("pushplus_token", "")
    app_token = cfg.get("wxpusher_app_token", "")
    wecom_url = cfg.get("wecom_webhook_url", "")
    host = cfg.get("qweather_host", "")
    cities = cfg.get("cities", [])
    loc_map = cfg.get("location_map", {})
    include_types = cfg.get("include_types", [])
    action_map = cfg.get("action_map") or DEFAULT_ACTION_MAP

    if not host:
        log("✗ 未配置 qweather_host —— 请到和风天气控制台「设置」里复制你的专属 API Host 填进 config.json")
        return {"checked": 0, "sent": 0}

    sent_ids = load_sent_ids()
    newly_sent = 0

    for city in cities:
        try:
            lat, lon = resolve_coords(city, key, host, loc_map)
            alerts = fetch_alerts(lat, lon, key, host)
        except Exception as e:
            log(f"✗ 处理「{city}」出错：{e}")
            continue

        if not alerts:
            log(f"·「{city}」当前无生效预警")
            continue

        for a in alerts:
            type_name = (a.get("eventType") or {}).get("name", "")
            if not match_types(type_name, include_types):
                log(f"  - 跳过非目标类型：{type_name}")
                continue

            aid = a.get("id", "")
            if aid in sent_ids:
                continue  # 已推过，去重

            level = alert_level(a)
            msg_code = (a.get("messageType") or {}).get("code", "alert")
            if msg_code == "cancel":
                title = f"【预警解除】{city} {type_name}"
                head = f"{city}的{type_name}预警已解除。\n\n"
                content = head + format_alert(a)
            else:
                title = f"【天气预警】{city} {type_name}{level}预警"
                head = f"{city}发布{type_name}预警，请注意防范。\n\n"
                content = head + format_alert(a)
                # 附加对应的防范/处置措施
                content = append_actions(content, type_name, action_map)

            # 多通道推送：企业微信（群，主推）/ PushPlus（自己）/ WxPusher（群发关注者）；任一成功即算成功
            ok_wecom = bool(wecom_url) and wecom_webhook_send(wecom_url, title, content)
            ok_pushplus = bool(token) and pushplus_send(token, title, content)
            ok_wxpusher = bool(app_token) and wxpusher_send(app_token, title, content)

            if ok_wecom:
                log(f"✓ 企业微信群已推送：{title}")
            elif wecom_url:
                log(f"✗ 企业微信推送失败：{title}")

            if ok_pushplus:
                log(f"✓ PushPlus 已推送给你：{title}")
            elif token:
                log(f"✗ PushPlus 推送失败：{title}")

            if ok_wxpusher:
                log(f"✓ WxPusher 已群发给所有关注者：{title}")
            elif app_token:
                log(f"✗ WxPusher 推送失败：{title}")

            if ok_wecom or ok_pushplus or ok_wxpusher:
                newly_sent += 1
                if aid:
                    sent_ids.add(aid)
            elif not (wecom_url or token or app_token):
                log(f"· [未配置任何推送通道，跳过推送] {title}")

    save_sent_ids(sent_ids)
    return {"checked": len(cities), "sent": newly_sent}


def selfcheck(cfg: dict) -> None:
    """自检模式：逐项验证配置与连通性，方便本地排查。"""
    log("=" * 50)
    log("开始自检")
    log("=" * 50)

    token = cfg.get("pushplus_token", "")
    app_token = cfg.get("wxpusher_app_token", "")
    wecom_url = cfg.get("wecom_webhook_url", "")
    key = cfg.get("qweather_key", "")
    host = cfg.get("qweather_host", "")

    # 1. 检查配置是否填写
    if not token:
        log("✗ pushplus_token 为空 —— 请到 www.pushplus.plus 登录后复制 token 填进 config.json")
    else:
        log(f"✓ pushplus_token 已配置（{token[:6]}…）")

    if not wecom_url:
        log("· wecom_webhook_url 未配置 —— 如需推送到企业微信群，请让群主建「消息推送」机器人后把 webhook 地址填进来")
    else:
        log(f"✓ wecom_webhook_url 已配置（{wecom_url[:40]}…）")

    if not app_token:
        log("· wxpusher_app_token 未配置 —— 如需给同事群发，请到 wxpusher.zjiecode.com/admin 创建应用后填入 AT_ 开头的 token")
    else:
        log(f"✓ wxpusher_app_token 已配置（{app_token[:6]}…）")

    if not key:
        log("✗ qweather_key 为空 —— 请到和风天气控制台复制 API Key 填进 config.json")
    else:
        log(f"✓ qweather_key 已配置（{key[:6]}…）")

    if not host:
        log("✗ qweather_host 为空 —— 请到和风天气控制台「设置」里复制你的专属 API Host 填进 config.json")
    else:
        log(f"✓ qweather_host 已配置（{host}）")

    # 2. 测试企业微信群机器人通道
    if wecom_url:
        try:
            ok = wecom_webhook_send(wecom_url, "天气预警·自检", "这是一条测试消息，收到说明企业微信群推送通道正常。")
            log("✓ 已发测试消息到企业微信群，请检查群内是否收到" if ok else "✗ 企业微信测试消息发送失败，请检查 webhook 地址是否正确")
        except Exception as e:
            log(f"✗ 企业微信测试异常：{e}")

    # 2.5 测试 PushPlus 通道
    if token:
        try:
            ok = pushplus_send(token, "天气预警·自检", "这是一条测试消息，收到说明推送通道正常。")
            log("✓ 已发送测试消息到你的微信，请检查是否收到" if ok else "✗ 测试消息发送失败，请检查 token 是否正确")
        except Exception as e:
            log(f"✗ PushPlus 测试异常：{e}")

    # 2.6 测试 WxPusher 通道（会推给所有关注应用的人）
    if app_token:
        try:
            ok = wxpusher_send(app_token, "天气预警·自检", "这是一条测试消息，收到说明 WxPusher 群发通道正常。")
            log("✓ 已通过 WxPusher 群发测试消息（所有关注者都会收到）" if ok else "✗ WxPusher 测试消息发送失败，请检查 appToken 是否正确")
        except Exception as e:
            log(f"✗ WxPusher 测试异常：{e}")

    # 3. 测试和风 key + 列出各城市当前预警
    if key and host:
        cities = cfg.get("cities", [])
        loc_map = cfg.get("location_map", {})
        for city in cities:
            try:
                lat, lon = resolve_coords(city, key, host, loc_map)
                alerts = fetch_alerts(lat, lon, key, host)
                log(f"✓「{city}」（{lat}, {lon}）key 有效，当前生效预警 {len(alerts)} 条")
                for a in alerts:
                    tn = (a.get("eventType") or {}).get("name", "")
                    log(f"    - [{alert_level(a)}]{tn}：{a.get('headline', '')}")
            except Exception as e:
                log(f"✗「{city}」查询失败：{e}")

    log("=" * 50)
    log("自检完成")
    log("=" * 50)


def handler(event, context):
    """腾讯云函数入口（上云时直接复用，本地不调用）。"""
    cfg = load_config()
    result = run_once(cfg)
    return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}


def main(argv: list):
    cfg = load_config()

    if "--selfcheck" in argv:
        selfcheck(cfg)
        return

    if "--loop" in argv:
        log(f"进入循环模式，每 {CHECK_INTERVAL_MIN} 分钟检查一次（Ctrl+C 退出）")
        while True:
            try:
                run_once(cfg)
            except Exception as e:
                log(f"本轮执行出错：{e}")
            time.sleep(CHECK_INTERVAL_MIN * 60)
        return

    # 默认：跑一轮
    result = run_once(cfg)
    log(f"本轮完成：检查 {result['checked']} 个区县，新推送 {result['sent']} 条预警")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except FileNotFoundError as e:
        log(str(e))
    except Exception as e:
        log(f"程序异常退出：{e}")
        sys.exit(1)
