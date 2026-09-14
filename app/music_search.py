"""QQ 音乐公共 fcg 接口（搜索、推荐歌单、歌单歌曲抓取）。

与播放后端无关（纯 HTTP），desktop/web 后端与前端搜索选择器共用。
"""
import html
import json
import re
import urllib.request

SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
DISS_TAG_URL = "https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg"
TAG_CONF_URL = "https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_tag_conf.fcg"

_HEADERS = {"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _fetch_json(url: str) -> dict:
    """GET JSON（c.y.qq.com 部分接口按 GBK 回包，自动兼容）。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("gbk", errors="replace"))


def _clean_text(s: str) -> str:
    """去掉搜索高亮 <em> 标签并反转义 HTML 实体（如 R&#38;B → R&B）。"""
    return html.unescape(re.sub(r"</?em>", "", s or "")).strip()


def fcg_search(keyword: str, limit: int = 10) -> list:
    """按关键词搜索歌曲，优先使用官方桌面端 search RPC，失败或无结果时多渠道降级重试。"""
    clean_kw = keyword.strip()
    if not clean_kw:
        return []

    # 1. 尝试主接口：DoSearchForQQMusicDesktop
    for attempt in range(2):
        try:
            payload = {
                "comm": {"ct": 19, "cv": 1859, "uin": 0},
                "request": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {"search_type": 0, "query": clean_kw, "page_num": 1, "num_per_page": limit},
                },
            }
            req = urllib.request.Request(
                SEARCH_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Referer": "https://y.qq.com/",
                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = []
            song_list = data.get("request", {}).get("data", {}).get("body", {}).get("song", {}).get("list", [])
            for s in song_list:
                mid = s.get("mid") or s.get("file", {}).get("media_mid", "")
                if not mid:
                    continue
                out.append({
                    "songMid": mid,
                    "title": s.get("name") or s.get("title", ""),
                    "singer": "/".join(x.get("name", "") for x in s.get("singer", [])),
                })
            if out:
                return out
        except Exception as e:
            if attempt == 1:
                print(f"[search] DoSearchForQQMusicDesktop failed for '{clean_kw}': {e}")

    # 2. 降级备用接口：客户端/网页通用搜索 c.y.qq.com
    try:
        url = f"https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w={urllib.parse.quote(clean_kw)}&p=1&n={limit}&format=json"
        req = urllib.request.Request(
            url,
            headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for s in data.get("data", {}).get("song", {}).get("list", []):
            mid = s.get("songmid") or s.get("mid", "")
            if not mid:
                continue
            out.append({
                "songMid": mid,
                "title": s.get("songname") or s.get("name", ""),
                "singer": "/".join(x.get("name", "") for x in s.get("singer", [])),
            })
        if out:
            return out
    except Exception as e:
        print(f"[search] client_search_cp fallback failed for '{clean_kw}': {e}")

    # 3. 简化关键词降级（例如原搜索词为 "晴天 周杰伦"，若合并搜索未命中，尝试仅用歌名 "晴天" 搜索）
    parts = clean_kw.split()
    if len(parts) >= 2 and parts[0] != clean_kw:
        fallback_title = parts[0]
        try:
            sub_res = fcg_search(fallback_title, limit=limit)
            if sub_res:
                return sub_res
        except Exception:
            pass

    return []


def fcg_get_recommend_playlists(category_id: int = 10000000, sort_id: int = 5, limit: int = 12) -> list:
    """获取 QQ 音乐推荐播放列表 (官方精选/热门).

    上游接口会在两个内容状态间轮换（偶发只回 1 个），带间隔重试提高拿到完整列表的概率；
    仍不足时用官方排行榜（巅峰热歌/新歌等，同属官方推荐播放列表）补齐。
    """
    import time as _time
    last = []
    for attempt in range(3):
        try:
            last = _fetch_recommend_playlists(category_id, sort_id, limit)
            if len(last) >= min(limit, 6):
                return last
        except Exception:
            if attempt == 2:
                raise
        _time.sleep(0.4)
    if len(last) < min(limit, 6):
        # 精选位被上游降级时，用官方榜单补齐（dissid 带 top: 前缀，取歌时走榜单接口）
        try:
            tops = fcg_get_toplists()
            have = {p["dissid"] for p in last}
            for t in tops:
                if t["dissid"] not in have:
                    last.append(t)
                    have.add(t["dissid"])
                if len(last) >= limit:
                    break
        except Exception:
            pass
    return last


TOPLIST_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_myqq_toplist.fcg"
TOP_SONGS_URL = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg"


def fcg_get_toplists(limit: int = 8) -> list:
    """QQ 音乐官方排行榜（巅峰热歌 26 / 新歌 27 / 流行指数 4 等），id 带 top: 前缀."""
    req = urllib.request.Request(
        f"{TOPLIST_URL}?format=json",
        headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        data = json.loads(text)
    out = []
    for t in data.get("data", {}).get("topList", []):
        top_id = t.get("id") or t.get("topId")
        if not top_id:
            continue
        out.append({
            "dissid": f"top:{top_id}",
            "title": t.get("topTitle") or t.get("name") or "官方榜单",
            "cover": t.get("picUrl") or "",
            "creator": "官方榜单",
            "listennum": t.get("listenCount") or 0,
        })
        if len(out) >= limit:
            break
    return out


def _fetch_toplist_songs(top_id: str, limit: int) -> list:
    req = urllib.request.Request(
        f"{TOP_SONGS_URL}?topid={top_id}&format=json",
        headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        data = json.loads(text)
    out = []
    for item in data.get("songlist", [])[:limit]:
        s = item.get("data", {}) or {}
        mid = s.get("songmid") or s.get("mid") or ""
        if not mid:
            continue
        out.append({
            "songMid": mid,
            "title": s.get("songname") or s.get("title", ""),
            "singer": "/".join(x.get("name", "") for x in s.get("singer", [])),
            "interval": s.get("interval", 0),
        })
    return out


def _fetch_recommend_playlists(category_id: int, sort_id: int, limit: int) -> list:
    # 该接口 ein 过小（<5）会返回空列表，这里保底再裁剪
    fetch_n = max(6, limit)
    url = f"{DISS_TAG_URL}?sin=0&ein={fetch_n - 1}&categoryId={category_id}&sortId={sort_id}&format=json"
    req = urllib.request.Request(
        url,
        headers={"Referer": "https://y.qq.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw_bytes = resp.read()
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = raw_bytes.decode("gbk", errors="replace")
        data = json.loads(text)

    diss_list = data.get("data", {}).get("list", [])
    out = []
    for item in diss_list:
        if len(out) >= limit:
            break
        diss_id = str(item.get("dissid") or "")
        if not diss_id:
            continue
        out.append({
            "dissid": diss_id,
            "title": item.get("dissname") or "精选歌单",
            "cover": item.get("imgurl") or "",
            "creator": item.get("creator", {}).get("name") or "",
            "listennum": item.get("listennum") or 0,
        })
    return out


def fcg_get_playlist_songs(dissid: str, limit: int = 30) -> list:
    """根据 dissid 获取歌单歌曲列表。dissid 形如 top:26 时走官方榜单接口."""
    if str(dissid).startswith("top:"):
        return _fetch_toplist_songs(str(dissid)[4:], limit)
    try:
        diss_num = int(dissid)
    except (ValueError, TypeError):
        diss_num = dissid

    payload = {
        "comm": {"ct": 24, "cv": 0},
        "playlist": {
            "method": "CgiGetDiss",
            "module": "srf_diss_info.DissInfoServer",
            "param": {
                "disstid": diss_num,
                "onlysonglist": 1,
                "song_begin": 0,
                "song_num": limit,
            },
        },
    }
    req = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Referer": "https://y.qq.com/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    songlist = data.get("playlist", {}).get("data", {}).get("songlist", [])
    out = []
    for s in songlist:
        mid = s.get("mid") or s.get("file", {}).get("media_mid", "")
        if not mid:
            continue
        out.append({
            "songMid": mid,
            "title": s.get("name") or s.get("title", ""),
            "singer": "/".join(x.get("name", "") for x in s.get("singer", [])),
            "interval": s.get("interval", 0),
        })
    return out


# ---------------- 歌单广场：分类 / 分页浏览 / 歌单搜索 ----------------
def fcg_get_playlist_categories() -> list:
    """歌单分类分组：[{"group": "语种", "items": [{"id": 165, "name": "国语"}, ...]}, ...]"""
    data = _fetch_json(f"{TAG_CONF_URL}?format=json")
    groups = []
    for g in data.get("data", {}).get("categories", []):
        items = []
        for it in g.get("items", []):
            cid, name = it.get("categoryId"), _clean_text(it.get("categoryName", ""))
            if cid and name:
                items.append({"id": cid, "name": name})
        if items:
            groups.append({"group": g.get("categoryGroupName") or "推荐", "items": items})
    return groups


def fcg_get_playlists_by_category(category_id: int = 10000000, page: int = 1,
                                  per_page: int = 20, sort_id: int = 5) -> dict:
    """按分类分页取歌单：{"playlists": [...], "total": 总数}。sort_id: 5=最热 2=最新。"""
    page = max(1, page)
    # 该接口 ein 过小（<5）会返回空列表，per_page 至少 6
    per_page = max(6, min(50, per_page))
    sin = (page - 1) * per_page
    url = (f"{DISS_TAG_URL}?sin={sin}&ein={sin + per_page - 1}"
           f"&categoryId={category_id}&sortId={sort_id}&format=json")
    data = _fetch_json(url).get("data", {})
    out = []
    for item in data.get("list", []):
        diss_id = str(item.get("dissid") or "")
        if not diss_id:
            continue
        out.append({
            "dissid": diss_id,
            "title": _clean_text(item.get("dissname") or "歌单"),
            "cover": item.get("imgurl") or "",
            "creator": item.get("creator", {}).get("name") or "",
            "listennum": item.get("listennum") or 0,
        })
    return {"playlists": out, "total": data.get("sum") or 0}


def fcg_search_playlists(keyword: str, page: int = 1, per_page: int = 20) -> dict:
    """歌单搜索（官方桌面端搜索 RPC，search_type=3=歌单）。
    上游不回总数，以「本页是否满载」推断 has_more。"""
    kw = keyword.strip()
    if not kw:
        return {"playlists": [], "has_more": False}
    per_page = max(1, min(50, per_page))
    payload = {
        "comm": {"ct": 19, "cv": 1859, "uin": 0},
        "request": {
            "method": "DoSearchForQQMusicDesktop",
            "module": "music.search.SearchCgiService",
            "param": {"search_type": 3, "query": kw,
                      "page_num": max(1, page), "num_per_page": per_page},
        },
    }
    req = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **_HEADERS},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    songlist = data.get("request", {}).get("data", {}).get("body", {}).get("songlist", {})
    out = []
    for item in songlist.get("list", []):
        diss_id = str(item.get("dissid") or item.get("docid") or "")
        if not diss_id:
            continue
        out.append({
            "dissid": diss_id,
            "title": _clean_text(item.get("dissname") or "歌单"),
            "cover": item.get("imgurl") or "",
            "creator": item.get("creator", {}).get("name") or "",
            "listennum": item.get("listennum") or 0,
        })
    return {"playlists": out, "has_more": len(out) >= per_page}


# Aliases for convenience
get_recommended_playlists = fcg_get_recommend_playlists
get_playlist_songs = fcg_get_playlist_songs
get_playlist_categories = fcg_get_playlist_categories
get_playlists_by_category = fcg_get_playlists_by_category
search_playlists = fcg_search_playlists
