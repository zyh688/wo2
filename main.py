"""
体育频道整理脚本（Xtream Codes 面板版，支持多个订阅链接）

流程：读取 sources.txt 里的订阅链接 -> 逐个调用面板 API 取分类和频道
     -> 名称白名单/黑名单过滤 -> 清洗名字 -> 合并去重 -> 分组排序 -> 输出 tr.m3u
说明：不做测活（Actions 在国外机房，测出的结果不准，也会占用面板连接数）。
"""
import os
import re
import sys
import logging
from urllib.parse import urlparse, parse_qs

import requests
from requests.adapters import HTTPAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    datefmt="%H:%M:%S",
)

# ---------------- 可调配置 ----------------
SOURCES_FILE = "sources.txt"
OUT_FILE = "tr.m3u"
STREAM_EXT = os.environ.get("STREAM_EXT", "ts")  # 播放格式：ts 或 m3u8
MAX_PER_NAME = 3   # 合并后，同名频道（HD/FHD/备用清洗后同名）最多保留几条
TIMEOUT = 20

# 你自己的台标对照表：{"关键词": "图片地址"}，面板自带台标为空时才用
LOGO_MAP = {
    # "SKY SPORTS": "https://raw.githubusercontent.com/你的用户名/仓库/main/logos/sky.png",
}

# 白名单：只匹配频道名（不匹配 URL、不匹配分组）
WHITELIST = re.compile(
    r"(SKY\s*SPORTS|PREMIER\s*LEAGUE|CHAMPIONS\s*LEAGUE|SERIE\s*A|LA\s*LIGA|"
    r"NOW\s*SPORTS|NOW\s*TV|\bEPL\b|\bUCL\b|TNT\s*SPORTS|BEIN\s*SPORTS|"
    r"SUPERSPORT|SPORTS?|FOOTBALL|SOCCER)",
    re.IGNORECASE,
)

# 黑名单：只匹配频道名和分类名，短词都带词边界，避免误杀
BLACKLIST = re.compile(
    r"\b(?:ADULT|XXX|PORN|PLAYBOY|PENTHOUSE|HUSTLER|EROTIC|RADIO|RADYO|"
    r"MOVIES?|FILMS?|SERIES|DIZI|SINEMA|NETFLIX)\b|\+18|18\+|"
    r"TURK|TÜRK|TRT|BEINTRK|\bTR\b",
    re.IGNORECASE,
)

# 分组规则：从上到下，先匹配到的先用
GROUP_RULES = [
    ("英超", re.compile(r"PREMIER\s*LEAGUE|\bEPL\b", re.I)),
    ("欧冠", re.compile(r"CHAMPIONS\s*LEAGUE|\bUCL\b", re.I)),
    ("意甲", re.compile(r"SERIE\s*A", re.I)),
    ("西甲", re.compile(r"LA\s*LIGA", re.I)),
    ("BEIN", re.compile(r"BEIN", re.I)),
    ("SKY SPORTS", re.compile(r"SKY\s*SPORTS", re.I)),
    ("NOW", re.compile(r"\bNOW\b", re.I)),
    ("TNT", re.compile(r"TNT\s*SPORTS", re.I)),
]
DEFAULT_GROUP = "其他体育"
GROUP_ORDER = [g for g, _ in GROUP_RULES] + [DEFAULT_GROUP]

# 名字清洗
QUALITY_RE = re.compile(
    r"\b(FHD|HD|SD|UHD|4K|HEVC|RAW|PLUS|1080P|720P|\d+FPS|FPS|EUROPE|EURO|EUR|EU|VIP|MOBILE|HQ)\b",
    re.I,
)
BACKUP_RE = re.compile(r"\b(YEDEK|BACKUP|ALT|TEST)\b", re.I)
BRACKETS_RE = re.compile(r"\[.*?\]|\(.*?\)")
AD_RE = re.compile(r"\b[\w\-]+\.(COM|NET|ORG|TV|SITE|ONLINE|CLUB|INFO|XYZ|ME)\b", re.I)
SYMBOL_RE = re.compile(r"[^\w\s]", re.UNICODE)

# ---------------- 网络会话 ----------------
session = requests.Session()
session.mount("http://", HTTPAdapter(max_retries=2))
session.mount("https://", HTTPAdapter(max_retries=2))
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0"})


def read_sources():
    """读取 sources.txt：一行一个链接，# 开头的是注释，重复的行只算一次"""
    if not os.path.exists(SOURCES_FILE):
        return []
    links = []
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                links.append(line)
    return list(dict.fromkeys(links))


def parse_source(url: str):
    """从 get.php?username=..&password=.. 链接拆出面板地址和账号密码"""
    p = urlparse(url.strip())
    q = parse_qs(p.query)
    user = q.get("username", [""])[0]
    pwd = q.get("password", [""])[0]
    if not (p.scheme and p.netloc and user and pwd):
        raise ValueError("链接格式不对，应为 http://host:port/get.php?username=..&password=..")
    return f"{p.scheme}://{p.netloc}", user, pwd


def api(base: str, auth: dict, action: str = None):
    """调用 player_api.php。异常只保留类型名，避免带密码的 URL 出现在日志里"""
    params = dict(auth)
    if action:
        params["action"] = action
    try:
        r = session.get(f"{base}/player_api.php", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise RuntimeError(f"面板接口请求失败（{action or 'user_info'}）：{type(e).__name__}") from None


def clean_name(raw: str) -> str:
    n = BRACKETS_RE.sub(" ", raw)
    n = AD_RE.sub(" ", n)
    n = BACKUP_RE.sub(" ", n)
    n = QUALITY_RE.sub(" ", n)
    n = SYMBOL_RE.sub(" ", n)
    n = " ".join(n.split()).upper()
    return n or raw.strip().upper()


def pick_group(name: str) -> str:
    for group, rule in GROUP_RULES:
        if rule.search(name):
            return group
    return DEFAULT_GROUP


def pick_logo(icon: str, name: str) -> str:
    if icon:
        return icon
    for key, url in LOGO_MAP.items():
        if key in name:
            return url
    return ""


def attr(value: str) -> str:
    """m3u 属性里不能有双引号"""
    return str(value).replace('"', "'")


def collect(source: str, idx: int):
    """处理一个订阅链接，返回 (候选频道列表, EPG 地址)。失败时抛异常，由外面跳过这个源"""
    base, user, pwd = parse_source(source)

    # 让 Actions 日志自动遮盖账号密码
    print(f"::add-mask::{user}")
    print(f"::add-mask::{pwd}")

    auth = {"username": user, "password": pwd}

    info = api(base, auth)
    ui = info.get("user_info", {}) if isinstance(info, dict) else {}
    logging.info(f"[源{idx}] 账号状态: {ui.get('status')} | 最大连接数: {ui.get('max_connections')} | 到期(时间戳): {ui.get('exp_date')}")

    cats = api(base, auth, "get_live_categories")
    streams = api(base, auth, "get_live_streams")
    if not isinstance(streams, list) or not streams:
        raise RuntimeError("面板没有返回频道列表（账号可能过期或被封）")

    cat_map = {str(c.get("category_id")): c.get("category_name", "") for c in cats} if isinstance(cats, list) else {}

    items = []
    hit_cats = {}
    for s in streams:
        raw_name = (s.get("name") or "").strip()
        cat_name = cat_map.get(str(s.get("category_id")), "")
        stream_id = s.get("stream_id")
        if not raw_name or stream_id is None:
            continue
        if not WHITELIST.search(raw_name):
            continue
        if BLACKLIST.search(raw_name) or BLACKLIST.search(cat_name):
            continue

        name = clean_name(raw_name)
        hit_cats[cat_name] = hit_cats.get(cat_name, 0) + 1
        items.append({
            "name": name,
            "group": pick_group(name),
            "logo": pick_logo(s.get("stream_icon") or "", name),
            "epg_id": s.get("epg_channel_id") or "",
            "url": f"{base}/live/{user}/{pwd}/{stream_id}.{STREAM_EXT}",
        })

    logging.info(f"[源{idx}] 共 {len(cat_map)} 个分类、{len(streams)} 个频道，筛选出 {len(items)} 个")
    if hit_cats:
        logging.info(f"[源{idx}] 命中的分类: " + "，".join(f"{k or '无分类'}({v})" for k, v in hit_cats.items()))

    epg_url = f"{base}/xmltv.php?username={user}&password={pwd}"
    return items, epg_url


def main():
    sources = read_sources()
    if not sources:
        logging.error(f"没有在 {SOURCES_FILE} 里找到订阅链接")
        sys.exit(1)
    logging.info(f"读取到 {len(sources)} 个订阅链接")

    all_items = []
    epg_urls = []
    for idx, src in enumerate(sources, start=1):
        try:
            items, epg = collect(src, idx)
        except (ValueError, RuntimeError) as e:
            logging.warning(f"[源{idx}] 跳过：{e}")
            continue
        all_items.extend(items)
        epg_urls.append(epg)

    # 合并去重：同一个播放地址只留一次；同名频道最多留 MAX_PER_NAME 条（靠前的源优先）
    kept = []
    seen_urls = set()
    name_count = {}
    for c in all_items:
        if c["url"] in seen_urls:
            continue
        if name_count.get(c["name"], 0) >= MAX_PER_NAME:
            continue
        seen_urls.add(c["url"])
        name_count[c["name"]] = name_count.get(c["name"], 0) + 1
        kept.append(c)

    if not kept:
        logging.error("所有源合并后没有任何频道，不生成文件。请检查链接是否有效、白名单/黑名单规则是否合适。")
        sys.exit(1)

    kept.sort(key=lambda x: (GROUP_ORDER.index(x["group"]), x["name"]))

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(f'#EXTM3U url-tvg="{",".join(epg_urls)}"\n')
        for c in kept:
            f.write(
                f'#EXTINF:-1 tvg-id="{attr(c["epg_id"])}" tvg-name="{attr(c["name"])}" '
                f'tvg-logo="{attr(c["logo"])}" group-title="{c["group"]}",{c["name"]}\n'
            )
            f.write(c["url"] + "\n")

    summary = {}
    for c in kept:
        summary[c["group"]] = summary.get(c["group"], 0) + 1
    logging.info(f"完成，共输出 {len(kept)} 个频道：" + "，".join(f"{g} {n}" for g, n in summary.items()))
    no_epg = sum(1 for c in kept if not c["epg_id"])
    no_logo = sum(1 for c in kept if not c["logo"])
    logging.info(f"其中没有 EPG ID 的 {no_epg} 个，没有台标的 {no_logo} 个")


if __name__ == "__main__":
    main()