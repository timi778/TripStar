"""小红书搜索服务"""

import io
import json
import httpx
import re
import logging
import contextlib
from typing import List, Dict, Any
from xhs import XhsClient
from ..config import get_settings
from .llm_service import get_llm

# 静默 xhs 库的所有日志输出（它会通过 logging 打印原始 API 响应）
logging.getLogger("xhs").setLevel(logging.CRITICAL)
logging.getLogger("xhs.core").setLevel(logging.CRITICAL)

class XHSCookieExpiredError(Exception):
    """小红书 Cookie 过期致命异常，用于向前端报警"""
    pass

def normalize_xhs_cookie(cookie: str) -> str:
    """兼容 Cookie 请求头字符串和浏览器导出的 JSON Cookie 列表。"""
    normalized = cookie.strip()
    if not normalized:
        return normalized

    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        normalized = normalized[1:-1].strip()

    cookie_items = None
    if normalized.startswith("[") and normalized.endswith("]"):
        try:
            cookie_items = json.loads(normalized)
        except json.JSONDecodeError:
            cookie_items = None
    elif normalized.startswith("{") and '"name"' in normalized and '"value"' in normalized:
        try:
            cookie_items = json.loads(f"[{normalized}]")
        except json.JSONDecodeError:
            cookie_items = None

    if isinstance(cookie_items, list):
        pairs = []
        for item in cookie_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            value = str(item.get("value", "")).strip()
            if name:
                pairs.append(f"{name}={value}")
        if pairs:
            print("已将 JSON 格式的小红书 Cookie 转换为请求头字符串格式。")
            return "; ".join(pairs)

    return normalized

def get_xhs_client() -> XhsClient:
    """初始化并验证小红书客户端"""
    settings = get_settings()
    if not settings.xhs_cookie:
        raise XHSCookieExpiredError("小红书 Cookie 未配置，请在环境变量中设置 XHS_COOKIE")
    try:
        # 添加一个空白签名函数以避免 xhs 包在没有设置签名函数时抛出 NoneType object is not callable
        def dummy_sign(uri, data=None, a1="", web_session=""):
            return {"x-s": "", "x-t": ""}

        client = XhsClient(normalize_xhs_cookie(settings.xhs_cookie), sign=dummy_sign)
        # 简单测试一下客户端有效性（调用不敏感的接口）
        return client
    except Exception as e:
        print(f"小红书验证失败: {e}")
        raise XHSCookieExpiredError(f"小红书 Cookie 已过期或被拦截，请重置: {str(e)}")

def geocode_amap(address: str, city: str) -> dict:
    """内部静默使用高德Web服务进行地理编码补齐(为飞线服务)
    针对景点等 POI 名称，使用 place/text 接口比 geocode 更精准
    """
    settings = get_settings()
    if not settings.vite_amap_web_key:
        return {"longitude": 116.397128, "latitude": 39.916527} # 默认兜底
    
    url = f"https://restapi.amap.com/v3/place/text?keywords={address}&city={city}&offset=1&key={settings.vite_amap_web_key}"
    try:
        resp = httpx.get(url, timeout=5)
        data = resp.json()
        if data.get("status") == "1" and data.get("pois") and len(data["pois"]) > 0:
            location = data["pois"][0]["location"]
            lon, lat = location.split(",")
            return {"longitude": float(lon), "latitude": float(lat)}
    except Exception as e:
        print(f"高德地理编码查阅失败 ({address}): {e}")
    
    # 获取失败时给个默认兜底
    return {"longitude": 116.397128, "latitude": 39.916527}

def get_note_detail_ssr(note_id: str) -> dict:
    """通过网页抓取 SSR 状态提取笔记详情，规避风控"""
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=8)
        match = re.search(r'window\.__INITIAL_STATE__=({.*?})</script>', resp.text)
        if match:
            state_json = json.loads(match.group(1).replace('undefined', 'null'))
            return state_json.get("note", {}).get("noteDetailMap", {}).get(note_id, {}).get("note", {})
    except Exception as e:
        print(f"SSR详情提取失败 {note_id}: {e}")
    return {}

def search_xhs_attractions(city: str, keywords: str) -> str:
    """
    搜索小红书笔记，使用大模型极速提纯出结构化景点，
    并静默拼装经纬度和真实图片，回传给Planner。
    """
    print(f"🔍 [XHS_SERVICE] 正在呼叫小红书 API 搜索: {city} {keywords}")
    client = get_xhs_client()
    query = f"{city} {keywords} 旅游 景点攻略"
    
    try:
        # 静默调用，xhs 库内部会 print/logging 原始响应 dict，同时屏蔽 stdout 和 stderr
        _devnull = io.StringIO()
        with contextlib.redirect_stdout(_devnull), contextlib.redirect_stderr(_devnull):
            res = client.get_note_by_keyword(keyword=query)
        # 提取相关笔记，保守取前3~4篇核心高赞图文
        notes = res.get('items', [])[:4]
        
        combined_text = ""
        for i, note in enumerate(notes):
            if note.get('model_type') == 'note':
                note_card = note.get('note_card', {})
                title = note_card.get('display_title', '')
                try:
                    # 获取笔记详情，拿到长摘要（更方便大模型提取景点）规避风控
                    detail = get_note_detail_ssr(note['id'])
                    desc = detail.get('desc', '')
                except Exception:
                    desc = ""
                
                combined_text += f"\n笔记{i+1}:\n标题: {title}\n正文内容: {desc}\n"
    except Exception as e:
        print(f"❌ 小红书接口抓取崩盘: {e}")
        # 粗暴拦截所有可能的 Cookie 403 / 验证码风控报错
        raise XHSCookieExpiredError(f"小红书访问超时或 Cookie 失效(风控拦截)，抓取失败。请更新 XHS_COOKIE")
        
    if not combined_text:
        return f"未在小红书检索到关于 {city} {keywords} 的内容。"

    # ======== 轻量级提取过程 ========
    print(f"🧠 [XHS_SERVICE] 正在调用内联模型提纯小红书游记参数...")
    llm = get_llm()
    extract_prompt = f"""
请从以下真实的素人小红书打卡游记中，提纯出真实存在的【游玩景点】。
要求返回严格的 JSON 数组格式(哪怕只提取到了1个)，切勿返回除了JSON以外的任何冗余 markdown 文字！

数组中每个对象必须包含以下字段:
"name": 景点官方名称(必须能地理定位到)
"reason": 小红书用户的真实评价/避坑指南
"duration": 游玩时长(数字, 分钟)
"reservation_required": 是否需要提前预约(布尔值 true/false)。请根据游记中提到的"需要预约"、"提前预约"、"抢票"、"约满"、"官方预约"等关键词判断，如果游记未提及则默认为 false
"reservation_tips": 预约相关提示(字符串)。如果需要预约，请提取预约渠道、提前天数等具体信息；如果不需要预约则填空字符串

游记杂文内容如下:
{combined_text}

JSON 返回示例:
[
  {{"name": "故宫博物院", "reason": "必去打卡，建议走中轴线。", "duration": 240, "reservation_required": true, "reservation_tips": "需要提前7天在故宫官网或微信小程序预约，每日限流8万人"}},
  {{"name": "老君山金顶", "reason": "网红打卡点，夜景绝美，必须坐索道上山。", "duration": 180, "reservation_required": false, "reservation_tips": ""}}
]
"""
    try:
        response = llm._client.chat.completions.create(
            model=llm.model,
            messages=[{"role": "user", "content": extract_prompt}],
            temperature=0.1
        )
        content = response.choices[0].message.content
        
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            extracted = json.loads(json_match.group())
        else:
            extracted = json.loads(content)
            
        final_result = f"这是小红书热门精选游记的提取结果，附带确切坐标（图片由前端单独搜索获取）：\n"
        for item in extracted:
            name = item.get("name", "")
            if not name:
                continue
            # 在这里利用高德补齐地理缺漏
            loc = geocode_amap(name, city)
            item["location"] = loc
            final_result += json.dumps(item, ensure_ascii=False) + "\n"
        
        print(f"✅ [XHS_SERVICE] 小红书数据挖掘完毕，已装载进上下文。")
        return final_result

    except Exception as e:
        print(f"❌ 大模型提纯小红书数据异常: {e}")
        return "尝试提取小红书结构化数据失败，降级回常规处理。"


def get_xhs_photo_sync(keyword: str) -> str:
    """根据关键词从小红书搜索一张首图URL，通过抓取SSR结构规避接口风控"""
    # 构造一个带有 value 属性的假枚举，完美骗过 xhs 库底层的 `sort.value` 调用，同时无需强依赖它原本的类
    class TimeDescSort:
        value = "time_desc"
        
    try:
        client = get_xhs_client()
        # 搜图时强制按"最新"排序，避开综合高赞的含文字攻略图
        # 临时替换 builtins.print 来彻底屏蔽 xhs 库内部的 print 输出
        import builtins
        _original_print = builtins.print
        builtins.print = lambda *a, **kw: None
        try:
            res = client.get_note_by_keyword(keyword=keyword, sort=TimeDescSort())
        finally:
            builtins.print = _original_print
        notes = res.get('items', [])
        
        target_note_id = None
        for note in notes:
            if note.get('model_type') == 'note':
                target_note_id = note.get("id")
                break
                
        if not target_note_id:
            return ""

        # 直接请求网页 HTML
        url = f"https://www.xiaohongshu.com/explore/{target_note_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = httpx.get(url, headers=headers, timeout=10)
        
        # 解析 INITIAL_STATE
        match = re.search(r'window\.__INITIAL_STATE__=({.*?})</script>', resp.text)
        if match:
            # 兼容 js 中的 undefined
            state_json_str = match.group(1).replace('undefined', 'null')
            state_json = json.loads(state_json_str)
            note_data = state_json.get("note", {}).get("noteDetailMap", {}).get(target_note_id, {}).get("note", {})
            img_list = note_data.get("imageList", [])
            if img_list:
                # 寻找第一个高清直链
                first_img = img_list[0].get("urlDefault") or img_list[0].get("urlPattern") or img_list[0].get("url")
                if first_img:
                    return first_img
                    
    except Exception as e:
        print(f"小红书单图抓取失败 ({keyword}): {e}")
    return ""

async def get_photo_from_xhs(keyword: str) -> str:
    """供异步环境调用的小红书图片搜索API"""
    import asyncio
    return await asyncio.to_thread(get_xhs_photo_sync, keyword)
