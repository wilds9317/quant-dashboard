"""
金工生的量价盯盘仪表盘
------------------------------------------------------------
数据源：akshare（东方财富为主，新浪备援，双源兜底）
运行：streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import inspect
import re
import time

import numpy as np
import pandas as pd
import streamlit as st

# ----------------------------------------------------------------------------
# 时间基准：Streamlit Cloud 的服务器走 UTC，所有“今天/现在”必须换算成北京时间，
# 否则 14:30 闸门、交易日判断、自动刷新窗口在云上全部错 8 小时。
# ----------------------------------------------------------------------------
CN_TZ = dt.timezone(dt.timedelta(hours=8))


def _now_cn() -> dt.datetime:
    """当前北京时间（带时区）。"""
    return dt.datetime.now(CN_TZ)


def _today_cn() -> dt.date:
    """北京时间今天。"""
    return _now_cn().date()


def _market_phase(now: dt.datetime | None = None) -> str:
    """open = 连续竞价中 / break = 午休 / closed = 收盘或非交易日。"""
    now = now or _now_cn()
    if now.weekday() >= 5:  # 周六日
        return "closed"
    t = now.time()
    if dt.time(9, 15) <= t <= dt.time(11, 30):
        return "open"
    if dt.time(13, 0) <= t <= dt.time(15, 0):
        return "open"
    if dt.time(11, 30) < t < dt.time(13, 0):
        return "break"
    return "closed"


def _cache_ttl() -> int:
    """盘中 45 秒就要新数据；午休/收盘后没必要反复打接口。"""
    phase = _market_phase()
    if phase == "open":
        return 45
    if phase == "break":
        return 120
    return 900


def _cache_bucket() -> int:
    """把时间切成桶；桶号参与缓存键，从而做到“动态 TTL”。"""
    return int(time.time() // _cache_ttl())

# 云同步依赖 cloudsync/ 与 cloud_config.py 两个本地文件。
# 部署到 Streamlit Community Cloud 时可能只上传 app.py，缺这两个文件不能让整个页面起不来。
try:
    import cloudsync
    from cloud_config import CLOUD_CONFIG

    CLOUD_READY = True
except Exception:  # 缺文件 / 缺依赖都走这里
    cloudsync = None
    CLOUD_CONFIG = None
    CLOUD_READY = False


def _detect_mobile() -> bool:
    """从请求头 User-Agent 判断是不是手机。

    服务端拿不到屏幕宽度，只能看 UA；手机 Safari / Android 浏览器都会带这些字样。
    读不到（测试环境、本地裸跑）就当桌面端处理，反正用户可以在侧边栏手动切换。
    """
    try:
        ua = str(st.context.headers.get("User-Agent", "") or "")
    except Exception:
        return False
    return bool(
        re.search(r"Mobile|Android|iPhone|iPod|HarmonyOS|Windows Phone|MicroMessenger", ua, re.I)
    )


IS_MOBILE = _detect_mobile()

st.set_page_config(
    page_title="金工生的量价盯盘仪表盘",
    page_icon="📈",
    layout="wide",  # 宽屏：手机上也占满整宽，不留两侧空白
    # 手机上侧边栏展开会遮住整屏，默认收起；点左上角箭头才拉出来
    initial_sidebar_state="collapsed" if IS_MOBILE else "expanded",
)


def _full_width(func) -> dict:
    """新版 Streamlit 用 width='stretch'，旧版用 use_container_width=True。"""
    if "width" in inspect.signature(func).parameters:
        return {"width": "stretch"}
    return {"use_container_width": True}


# ----------------------------------------------------------------------------
# 0. 依赖检查
# ----------------------------------------------------------------------------
AK_ERROR = None
try:
    import akshare as ak
except Exception as exc:  # pragma: no cover
    ak = None
    AK_ERROR = exc


# ----------------------------------------------------------------------------
# 1. 监控标的：默认 9 只 + 用户手动添加
# ----------------------------------------------------------------------------
DEFAULT_WATCHLIST = [
    {"symbol": "510300.SH", "name": "沪深300ETF",  "kind": "etf"},
    {"symbol": "515450.SH", "name": "红利低波ETF", "kind": "etf"},
    {"symbol": "588000.SH", "name": "科创50ETF",   "kind": "etf"},
    {"symbol": "159915.SZ", "name": "创业板ETF",   "kind": "etf"},
    {"symbol": "512480.SH", "name": "半导体ETF",   "kind": "etf"},
    {"symbol": "300059.SZ", "name": "东方财富",    "kind": "stock"},
    {"symbol": "300750.SZ", "name": "宁德时代",    "kind": "stock"},
    {"symbol": "002475.SZ", "name": "立讯精密",    "kind": "stock"},
    {"symbol": "300308.SZ", "name": "中际旭创",    "kind": "stock"},
]

DEFAULT_NAMES = {w["symbol"]: w["name"] for w in DEFAULT_WATCHLIST}
MARKET_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
MAX_WATCHLIST = 30

# 排序方式：key 存 URL，value 是下拉框里显示的文字
SORT_MODES = {
    "signal": "交易信号（狙击 → 量比 → 涨跌幅）",
    "vol": "量比 ↓",
    "chg": "涨跌幅 ↓",
    "pnl": "盈亏比例 ↓",
}

# 展示形式：手机用卡片，电脑用表格，也可以手动锁定
VIEW_MODES = {
    "auto": "自动（按设备判断）",
    "card": "卡片（手机推荐）",
    "table": "表格（电脑推荐）",
}


def _normalize_symbol(text: str) -> str | None:
    """把各种写法统一成 6 位代码.交易所：600519 / sh600519 / 600519.sh / 600519.SH。"""
    s = str(text or "").strip().upper().replace(" ", "").replace("．", ".")
    if not s:
        return None

    s = s.replace(".", "")
    for suf in ("SH", "SZ", "BJ"):
        if s.startswith(suf) and len(s) > 2:
            s = f"{s[2:]}.{suf}"
            break
        if s.endswith(suf) and len(s) > 2:
            s = f"{s[:-2]}.{suf}"
            break

    code, _, market = s.partition(".")
    if not code.isdigit() or len(code) != 6:
        return None

    if market not in MARKET_PREFIX:
        # 没写交易所时按代码段推断：5/6/9 沪，0/1/2/3 深，4/8 北
        head = code[0]
        market = "SH" if head in "569" else "BJ" if head in "48" else "SZ"
    return f"{code}.{market}"


def _guess_kind(symbol: str) -> str:
    """按代码段猜 ETF / 个股；猜错不要紧，抓取时会自动换另一类再试一遍。"""
    code = symbol.split(".")[0]
    return "etf" if code.startswith(("5", "1")) else "stock"


@st.cache_data(ttl=86400, show_spinner=False)
def _etf_name_map() -> dict[str, str]:
    """ETF 名称表（一次拉全量，缓存一天）。仅在添加 ETF 时才会触发。"""
    try:  # 全市场基金名录，单次请求约 0.5s
        table = ak.fund_name_em()
        return {
            str(r["基金代码"]).zfill(6): str(r["基金简称"]) for _, r in table.iterrows()
        }
    except Exception:
        try:  # 备选：ETF 实时行情表（分页较慢，约 20s）
            table = ak.fund_etf_spot_em()
            return {
                str(r["代码"]).zfill(6): str(r["名称"]) for _, r in table.iterrows()
            }
        except Exception:
            return {}


@st.cache_data(ttl=86400, show_spinner=False)
def _resolve_name(symbol: str, kind: str) -> str:
    """查标的中文名：默认表 → 东财个股资料 / ETF 名称表 → 兜底用代码。"""
    code = symbol.split(".")[0]
    if symbol in DEFAULT_NAMES:
        return DEFAULT_NAMES[symbol]
    if ak is None:
        return code
    try:
        if kind == "stock":
            info = ak.stock_individual_info_em(symbol=code)
            kv = dict(zip(info["item"], info["value"]))
            name = str(kv.get("股票简称") or "")
        else:
            name = _etf_name_map().get(code, "")
        # 源数据里常有全角空格对齐（"五  粮  液"），统一去掉
        name = "".join(name.split())
        if name:
            return name
    except Exception:
        pass
    return code


def _parse_watchlist(raw: str) -> list[dict]:
    """把 "600519.SH,000858.SZ" 解析成标的列表（去重、跳过非法代码）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for part in str(raw or "").split(","):
        symbol = _normalize_symbol(part)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        kind = _guess_kind(symbol)
        out.append({"symbol": symbol, "name": _resolve_name(symbol, kind), "kind": kind})
    return out


def _sync_url() -> None:
    """把列表和排序方式同步到地址栏，这样按 F5 刷新也能恢复（轻量持久化）。"""
    try:
        symbols = [w["symbol"] for w in st.session_state.watchlist]
        if symbols == [w["symbol"] for w in DEFAULT_WATCHLIST]:
            if "wl" in st.query_params:
                del st.query_params["wl"]
        elif st.query_params.get("wl") != ",".join(symbols):
            st.query_params["wl"] = ",".join(symbols)

        mode = st.session_state.get("sort_mode", "signal")
        if mode == "signal":
            if "sort" in st.query_params:
                del st.query_params["sort"]
        elif st.query_params.get("sort") != mode:
            st.query_params["sort"] = mode

        vmode = st.session_state.get("view_mode", "auto")
        if vmode == "auto":
            if "view" in st.query_params:
                del st.query_params["view"]
        elif st.query_params.get("view") != vmode:
            st.query_params["view"] = vmode
    except Exception:
        pass  # 老版本 Streamlit 没有 query_params，忽略即可


def _init_state() -> None:
    """初始化会话状态：刷新后从 URL 恢复，否则用默认 9 只。"""
    if "watchlist" not in st.session_state:
        restored = _parse_watchlist(st.query_params.get("wl", ""))
        st.session_state.watchlist = restored or [dict(w) for w in DEFAULT_WATCHLIST]

    # 排序方式也要能扛住刷新：URL 里带了合法值就用它
    if "sort_mode" not in st.session_state:
        wanted = st.query_params.get("sort", "signal")
        st.session_state.sort_mode = wanted if wanted in SORT_MODES else "signal"

    if "view_mode" not in st.session_state:
        wanted_v = st.query_params.get("view", "auto")
        st.session_state.view_mode = wanted_v if wanted_v in VIEW_MODES else "auto"

    st.session_state.setdefault("costs", {})
    st.session_state.setdefault("wl_msg", None)
    st.session_state.setdefault("add_rev", 0)
    st.session_state.setdefault("cloud_rev", 0)
    st.session_state.setdefault("cloud_flash", None)
    st.session_state.setdefault("cloud_nonce", None)


def _add_symbol(text: str) -> None:
    """校验 → 查名称 → 追加到 session_state，并同步 URL。"""
    symbol = _normalize_symbol(text)
    if not symbol:
        st.session_state.wl_msg = (
            "err",
            "代码格式不对。要 6 位数字 + 交易所，例如 600519.SH / 000858.SZ / 159915.SZ",
        )
        return

    symbols = [w["symbol"] for w in st.session_state.watchlist]
    if symbol in symbols:
        st.session_state.wl_msg = ("warn", f"{symbol} 已经在监控列表里了")
        return
    if len(symbols) >= MAX_WATCHLIST:
        st.session_state.wl_msg = ("err", f"最多监控 {MAX_WATCHLIST} 只，先移除一些再添加")
        return

    kind = _guess_kind(symbol)
    with st.spinner(f"正在查询 {symbol} 的名称…"):
        name = _resolve_name(symbol, kind)
    st.session_state.watchlist.append({"symbol": symbol, "name": name, "kind": kind})
    st.session_state.wl_msg = ("ok", f"已添加 {name}（{symbol}）")
    _sync_url()


# ----------------------------------------------------------------------------
# 2. 样式
# ----------------------------------------------------------------------------
def _is_dark_theme() -> bool:
    """读取浏览器当前主题；读不到（如测试环境）按浅色处理。"""
    try:
        return str(st.context.theme.type).lower() == "dark"
    except Exception:
        return False


DARK = _is_dark_theme()
# 预警条配色：浅色主题用深红字，深色主题用亮红字，保证两边都看得清
SN_TITLE = "#ff6b6b" if DARK else "#c1121f"
SN_IDLE_FG = "#9ba7b4" if DARK else "#57606a"
SN_IDLE_KEY = "#e6edf3" if DARK else "#1f2328"
SN_BORDER = "rgba(255,75,75,.55)" if DARK else "rgba(217,45,32,.40)"

st.markdown(
    """
<style>
  /* ========== 黑曜石基座：所有面板共用同一层材质 ========== */
  .hero, .kpi, .idx, .mcard {
      position: relative; overflow: hidden;
      font-family: -apple-system, "SF Pro Text", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background-image:
          radial-gradient(120% 110% at 8% 0%, rgba(255,255,255,.05) 0%, rgba(255,255,255,0) 52%),
          linear-gradient(180deg, #171b20 0%, #050609 88%);
      border: 1px solid rgba(255,255,255,.085);
      box-shadow:
          inset 0 1px 0 rgba(255,255,255,.07),
          0 1px 2px rgba(0,0,0,.45),
          0 20px 40px -22px rgba(0,0,0,.85);
  }
  /* 顶边一道掠光：模拟光擦过金属边缘 */
  .hero::before, .kpi::before, .idx::before, .mcard::before {
      content: ""; position: absolute; left: 0; right: 0; top: 0; height: 1px;
      pointer-events: none;
      background: linear-gradient(90deg,
          rgba(255,255,255,0) 0%,
          rgba(255,255,255,.30) 26%,
          rgba(255,255,255,.10) 68%,
          rgba(255,255,255,0) 100%);
  }
  .block-container { padding-top: 3.6rem; padding-bottom: 2rem; max-width: 1480px; }
  .hero {
      display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
      padding: 22px 26px; border-radius: 16px; margin-bottom: 18px;
  }
  .hero-t {
      font-size: 27px; font-weight: 600; letter-spacing: .02em; color: #f7f9fb;
      line-height: 1.2;
  }
  .hero-s { font-size: 12.5px; color: rgba(236,239,243,.42); margin-top: 8px; letter-spacing: .02em; }
  .hero-r { text-align: right; white-space: nowrap; }
  .hero-d {
      font-size: 21px; font-weight: 600; color: #f7f9fb; letter-spacing: .05em;
      font-variant-numeric: tabular-nums;
  }
  .hero-m { font-size: 11.5px; color: rgba(236,239,243,.30); margin-top: 6px; letter-spacing: .05em; }
  .kpi-row { display: flex; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }
  .kpi { flex: 1 1 0; min-width: 150px; padding: 15px 17px; border-radius: 13px; }
  .kpi-l {
      font-size: 11.5px; font-weight: 500; color: rgba(236,239,243,.42);
      letter-spacing: .09em;
  }
  .kpi-v {
      font-size: 25px; font-weight: 600; margin-top: 9px; color: #f7f9fb;
      font-variant-numeric: tabular-nums; letter-spacing: -.01em;
  }
  .kpi-s { font-size: 11px; color: rgba(236,239,243,.26); margin-top: 7px; letter-spacing: .02em; }
  section[data-testid="stSidebar"] { border-right: 1px solid rgba(148,163,184,.14); }
  section[data-testid="stSidebar"] { min-width: 320px; width: 320px; }
  .sniper-hit {
      display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
      padding: 14px 20px; margin: 0 0 16px 0; border-radius: 12px;
      border: 1px solid __SN_BORDER__; background: rgba(255,75,75,.10);
      animation: sniper-pulse 2.4s ease-in-out infinite;
  }
  @keyframes sniper-pulse {
      0%, 100% { box-shadow: 0 0 0 1px rgba(255,75,75,.10), 0 0 14px rgba(255,75,75,.14); }
      50%      { box-shadow: 0 0 0 1px rgba(255,75,75,.28), 0 0 26px rgba(255,75,75,.32); }
  }
  .sniper-t {
      font-size: 19px; font-weight: 700; letter-spacing: .6px;
      color: __SN_TITLE__; white-space: nowrap;
  }
  .sniper-n {
      font-size: 12px; font-weight: 600; color: #fff; padding: 2px 9px;
      border-radius: 999px; background: #d92d20; white-space: nowrap;
  }
  .sniper-list { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .sniper-item {
      font-size: 13px; color: #fff; padding: 5px 11px; border-radius: 7px;
      background: #d92d20; font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .sniper-item b { color: #fff; font-weight: 700; }
  .sniper-idle {
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
      padding: 12px 18px; margin: 0 0 16px 0; border-radius: 12px;
      border: 1px solid rgba(255,255,255,.085);
      background-image: linear-gradient(180deg, #171b20 0%, #0a0c0f 88%);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.06), 0 12px 26px -18px rgba(0,0,0,.8);
      font-size: 13px; color: rgba(236,239,243,.55);
  }
  .sniper-idle b { color: #eef1f5; font-weight: 600; }
  .idx-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
  .idx {
      flex: 1 1 0; min-width: 190px; display: flex; align-items: baseline; gap: 12px;
      padding: 14px 18px; border-radius: 13px;
  }
  .idx-n {
      font-size: 12px; color: rgba(236,239,243,.40); letter-spacing: .09em;
      white-space: nowrap; font-weight: 500;
  }
  .idx-p {
      font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums;
      line-height: 1.1; white-space: nowrap; letter-spacing: -.02em;
  }
  .idx-c {
      font-size: 11.5px; color: rgba(236,239,243,.32); margin-left: auto;
      font-variant-numeric: tabular-nums; white-space: nowrap; letter-spacing: .02em;
  }
  .gate {
      padding: 12px 16px; border-radius: 10px; margin-bottom: 14px;
      font-size: 15px; font-weight: 700; letter-spacing: .5px; color: #fff;
  }
  .gate-off { background: #d92d20; box-shadow: 0 0 0 1px rgba(217,45,32,.35); }
  .gate-on  { background: #15803d; box-shadow: 0 0 0 1px rgba(21,128,61,.35); }

  /* ---------- 移动端卡片 ---------- */
  .mcards {
      display: grid; gap: 10px; margin-bottom: 10px;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
  }
  .mcard {
      border-radius: 14px; padding: 12px 14px;
      border-left: 4px solid #6b7280;
  }
  .mcard.hit {
      border-left-color: #f6465d;
      box-shadow:
          inset 0 1px 0 rgba(255,255,255,.07),
          0 0 0 1px rgba(246,70,93,.22),
          0 14px 34px -16px rgba(246,70,93,.45);
  }
  .mcard.bull { border-left-color: #f6465d; }
  .mcard.bear { border-left-color: #2ebd85; }
  .mcard-h { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .mcard-n { font-size: 16px; font-weight: 650; color: #f7f9fb; letter-spacing: .01em; }
  .mcard-c {
      font-size: 11px; color: rgba(236,239,243,.36); margin-left: 6px;
      font-variant-numeric: tabular-nums;
  }
  .mcard-tag {
      font-size: 11px; font-weight: 700; padding: 3px 8px;
      border-radius: 999px; white-space: nowrap; flex: none;
  }
  .mcard-tag.hit { background: #d92d20; color: #fff; }
  .mcard-tag.idle {
      background: rgba(255,255,255,.08); color: rgba(236,239,243,.55);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.06);
  }
  .mcard-p { display: flex; align-items: baseline; gap: 10px; margin: 8px 0 10px; }
  .mcard-px {
      font-size: 25px; font-weight: 700; color: #f7f9fb;
      font-variant-numeric: tabular-nums; line-height: 1.05; letter-spacing: -.01em;
  }
  .mcard-chg { font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .mcard-g { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px 12px; }
  .mg {
      display: flex; justify-content: space-between; gap: 6px; font-size: 12px;
      border-top: 1px dashed rgba(255,255,255,.075); padding-top: 5px;
  }
  .mg span { color: rgba(236,239,243,.38); white-space: nowrap; }
  .mg b { font-weight: 600; color: #eef1f5; font-variant-numeric: tabular-nums; }
  .mcard-f {
      margin-top: 8px; font-size: 11px; color: rgba(236,239,243,.26);
      font-variant-numeric: tabular-nums; letter-spacing: .02em;
  }
  .t-up { color: #f6465d; }
  .t-down { color: #2ebd85; }
  .t-flat { color: rgba(236,239,243,.40); }

  /* ---------- 窄屏（手机）适配 ---------- */
  @media (max-width: 760px) {
      .block-container { padding: 3.5rem 0.8rem 1.8rem !important; }
      .hero { flex-direction: column; align-items: flex-start; gap: 8px; padding: 13px 15px; }
      .hero-t { font-size: 19px; }
      .hero-s { font-size: 11.5px; line-height: 1.45; }
      .hero-r { text-align: left; }
      .hero-d { font-size: 15px; }
      /* 三个指数挤在一行，不变成三条大块 */
      .idx-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
      .idx {
          flex-direction: column; align-items: flex-start; gap: 2px;
          padding: 8px 9px; min-width: 0; border-radius: 10px;
      }
      .idx-n { font-size: 10.5px; white-space: normal; line-height: 1.2; }
      .idx-p { font-size: 18px; }
      .idx-c { margin-left: 0; font-size: 9.5px; }
      .kpi-row { gap: 8px; }
      .kpi { min-width: 0; flex: 1 1 46%; padding: 10px 12px; border-radius: 10px; }
      .kpi-l { font-size: 11px; }
      .kpi-v { font-size: 19px; margin-top: 5px; }
      .kpi-s { font-size: 10px; margin-top: 4px; }
      .sniper-hit { padding: 11px 13px; gap: 8px; border-radius: 10px; }
      .sniper-t { font-size: 16px; }
      .sniper-idle { padding: 10px 12px; font-size: 12px; }
      .sniper-item { font-size: 12px; padding: 4px 9px; }
      .gate { font-size: 13.5px; padding: 10px 12px; }
      /* 侧边栏宽度别顶满屏幕，留出关闭区域 */
      section[data-testid="stSidebar"] { min-width: 0 !important; width: 88vw !important; }
      section[data-testid="stSidebar"] .block-container { padding: 0.7rem 0.8rem !important; }
      .mcards { grid-template-columns: 1fr; }
  }
  @media (max-width: 420px) {
      .mcard-px { font-size: 23px; }
      .idx-p { font-size: 16px; }
  }
</style>
""".replace("__SN_BORDER__", SN_BORDER)
    .replace("__SN_TITLE__", SN_TITLE)
    .replace("__SN_IDLE_FG__", SN_IDLE_FG)
    .replace("__SN_IDLE_KEY__", SN_IDLE_KEY),
    unsafe_allow_html=True,
)

UP, DOWN, FLAT = "#f6465d", "#2ebd85", "#8b949e"  # A股习惯：涨红跌绿


# ----------------------------------------------------------------------------
# 3. 数据抓取
# ----------------------------------------------------------------------------
def _pick(raw: pd.DataFrame, name: str) -> str:
    for col in raw.columns:
        if str(col).strip().lower() == name.lower():
            return col
    raise KeyError(f"缺少字段 {name}")


def _maybe_pick(raw: pd.DataFrame, *names: str) -> str | None:
    """可选字段：依次尝试多个别名，都找不到返回 None（不抛错）。"""
    for name in names:
        for col in raw.columns:
            if str(col).strip().lower() == name.lower():
                return col
    return None


def _to_frame(
    raw: pd.DataFrame,
    date_col: str = "日期",
    close_col: str = "收盘",
    pct_col: str | None = "涨跌幅",
    vol_col: str | None = "成交量",
) -> pd.DataFrame:
    """把不同数据源的返回统一成 日期 / 收盘 / 开盘 / 涨跌幅 / 成交量。"""
    if raw is None or len(raw) == 0:
        raise ValueError("数据源返回空表")
    out = pd.DataFrame(
        {
            "日期": pd.to_datetime(raw[_pick(raw, date_col)], errors="coerce"),
            "收盘": pd.to_numeric(raw[_pick(raw, close_col)], errors="coerce"),
        }
    )
    open_col = _maybe_pick(raw, "开盘", "open", "今开")
    out["开盘"] = (
        pd.to_numeric(raw[open_col], errors="coerce") if open_col else np.nan
    )
    out["涨跌幅"] = (
        pd.to_numeric(raw[_pick(raw, pct_col)], errors="coerce")
        if pct_col and any(str(c).lower() == pct_col.lower() for c in raw.columns)
        else out["收盘"].pct_change() * 100
    )
    out["成交量"] = (
        pd.to_numeric(raw[_pick(raw, vol_col)], errors="coerce")
        if vol_col and any(str(c).lower() == vol_col.lower() for c in raw.columns)
        else np.nan
    )
    return (
        out.dropna(subset=["日期", "收盘"])
        .sort_values("日期")
        .reset_index(drop=True)
    )


def _fetch_raw(symbol: str, kind: str) -> tuple[pd.DataFrame, str]:
    """主源：东方财富。返回 (数据帧, 来源名)。"""
    code6 = symbol.split(".")[0]
    end = _today_cn()
    start = end - dt.timedelta(days=420)
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    if kind == "etf":
        raw = ak.fund_etf_hist_em(
            symbol=code6, period="daily", start_date=s, end_date=e, adjust=""
        )
        return _to_frame(raw), "东方财富·ETF日线"

    raw = ak.stock_zh_a_hist(
        symbol=code6, period="daily", start_date=s, end_date=e, adjust="qfq"
    )
    return _to_frame(raw), "东方财富·A股日线(前复权)"


def _fetch_backup(symbol: str, kind: str) -> tuple[pd.DataFrame, str]:
    """备援源：新浪。字段名不同，重新映射。"""
    code6 = symbol.split(".")[0]
    prefix = MARKET_PREFIX.get(symbol.split(".")[-1].upper(), "sz")

    if kind == "etf":
        raw = ak.fund_etf_hist_sina(symbol=f"{prefix}{code6}")
        return _to_frame(raw, "date", "close", None, "volume"), "新浪·ETF日线"
    raw = ak.stock_zh_a_daily(symbol=f"{prefix}{code6}", adjust="qfq")
    return _to_frame(raw, "date", "close", None, "volume"), "新浪·A股日线(前复权)"


def _metrics(frame: pd.DataFrame, symbol: str, name: str, source: str) -> dict:
    close = frame["收盘"]
    vol = frame["成交量"]

    try:
        vol_ratio = float(vol.iloc[-1] / vol.iloc[-6:-1].mean())
        if not np.isfinite(vol_ratio):
            vol_ratio = np.nan
    except Exception:
        vol_ratio = np.nan

    try:
        open_px = float(frame["开盘"].iloc[-1])
        if not np.isfinite(open_px):
            open_px = np.nan
    except Exception:
        open_px = np.nan

    return {
        "代码": symbol,
        "名称": name,
        "现价": float(close.iloc[-1]),
        "开盘": open_px,
        "涨跌幅(%)": float(frame["涨跌幅"].iloc[-1]) if pd.notna(frame["涨跌幅"].iloc[-1]) else np.nan,
        "量比": vol_ratio,
        "MA5": float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else np.nan,
        "MA20": float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else np.nan,
        "数据日期": frame["日期"].iloc[-1],
        "数据源": source,
        "备注": "",
    }


@st.cache_data(ttl=900, show_spinner=False)
def load_all(specs: tuple[tuple[str, str, str], ...], bucket: int = 0) -> list[dict]:
    """逐个标的抓取，单只失败不影响整体。

    specs 是 ((代码, 名称, 类型), ...) —— 用元组做缓存键，
    列表一变缓存自动失效，不用手动 clear。
    bucket 是时间桶（盘中 45 秒一换），让缓存能按交易/非交易时段变频率。
    """
    del bucket  # 仅作缓存键
    results: list[dict] = []

    for symbol, name, kind in specs:
        other = "stock" if kind == "etf" else "etf"
        # 先按猜的类型试主源/备援，再换另一类重试 —— 猜错代码类型也能救回来
        chain = (
            (_fetch_raw, kind, "主"),
            (_fetch_backup, kind, "备"),
            (_fetch_raw, other, "主·另一类"),
            (_fetch_backup, other, "备·另一类"),
        )
        errors: list[str] = []

        for fetcher, k, tag in chain:
            try:
                frame, source = fetcher(symbol, k)
                results.append(_metrics(frame, symbol, name, source))
                break
            except Exception as exc:  # 网络 / 接口变更 / 停牌 / 代码不存在 都走这里
                errors.append(f"{tag}:{type(exc).__name__}")
        else:
            results.append(
                {
                    "代码": symbol,
                    "名称": name,
                    "现价": np.nan,
                    "开盘": np.nan,
                    "涨跌幅(%)": np.nan,
                    "量比": np.nan,
                    "MA5": np.nan,
                    "MA20": np.nan,
                    "数据日期": pd.NaT,
                    "数据源": "—",
                    "备注": " / ".join(errors),
                }
            )

    return results


# ----------------------------------------------------------------------------
# 3b. 大盘指数（上证 / 深证 / 创业板）
# ----------------------------------------------------------------------------
INDEX_LIST = [
    ("000001", "上证指数", "sh000001"),
    ("399001", "深证成指", "sz399001"),
    ("399006", "创业板指", "sz399006"),
]
INDEX_FORCE_RED = False   # True = 涨跌幅永远红色；False = 按 A 股习惯红涨绿跌


def _idx_from_em() -> pd.DataFrame:
    """主源：东财「沪深重要指数」，一次请求拿全部。"""
    spot = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
    out = spot[["代码", "名称", "最新价", "涨跌幅"]].copy()
    out["代码"] = out["代码"].astype(str).str.zfill(6)
    return out


def _idx_from_sina() -> pd.DataFrame:
    """备援：新浪指数行情（代码形如 sh000001）。东财挂了就靠它。"""
    spot = ak.stock_zh_index_spot_sina()
    out = spot[["代码", "名称", "最新价", "涨跌幅"]].copy()
    out["代码"] = out["代码"].astype(str).str.lower().str[-6:]
    return out


@st.cache_data(ttl=900, show_spinner=False)
def load_indices(bucket: int = 0) -> tuple[list[dict], str]:
    """三个指数的涨跌幅；主源失败自动换备援。

    bucket 只用来做缓存分桶（盘中 45 秒一换），不参与计算。
    返回 (数据, 错误说明)；错误说明为空表示正常。
    """
    del bucket  # 仅作缓存键
    empty = [{"名称": n, "点位": np.nan, "涨跌幅": np.nan} for _, n, _ in INDEX_LIST]
    errs: list[str] = []

    for fetcher, tag in ((_idx_from_em, "东财"), (_idx_from_sina, "新浪")):
        try:
            spot = fetcher()
        except Exception as exc:
            errs.append(f"{tag} {type(exc).__name__}")
            continue

        rows: list[dict] = []
        for code, fb_name, _sina in INDEX_LIST:
            row = spot[spot["代码"] == code]
            if row.empty:
                rows.append({"名称": fb_name, "点位": np.nan, "涨跌幅": np.nan})
            else:
                r = row.iloc[0]
                rows.append(
                    {
                        "名称": str(r["名称"]),
                        "点位": float(r["最新价"]),
                        "涨跌幅": float(r["涨跌幅"]),
                    }
                )

        if sum(pd.notna(x["涨跌幅"]) for x in rows) >= 2:
            return rows, ""
        errs.append(f"{tag} 只返回 {sum(pd.notna(x['涨跌幅']) for x in rows)}/3 个")

    return empty, " / ".join(errs)



# ----------------------------------------------------------------------------
# 4. 侧边栏：监控列表 + 持仓成本
# ----------------------------------------------------------------------------
_init_state()

# 展示形式：auto = 按设备判断（手机卡片 / 电脑表格），也可手动锁定
DISPLAY = st.session_state.get("view_mode", "auto")
USE_CARDS = DISPLAY == "card" or (DISPLAY == "auto" and IS_MOBILE)

with st.sidebar:
    # --- 时间闸门：14:30 前禁手（一律按北京时间算，云上是 UTC 服务器也一样） ---
    _now = _now_cn()
    _cutoff = _now.replace(hour=14, minute=30, second=0, microsecond=0)
    if _now < _cutoff:
        st.markdown(
            '<div class="gate gate-off">❌ 禁手时间，管住手！</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="gate gate-on">✅ 狙击时间，准备开火！</div>',
            unsafe_allow_html=True,
        )
    _phase_txt = {"open": "盘中 · 每 45 秒自动刷新", "break": "午休 · 每 2 分钟刷新", "closed": "已收盘 · 15 分钟刷新"}
    st.caption(
        f"现在 {_now:%H:%M:%S}（北京时间） · 14:30 为分界线\n\n"
        f"{_phase_txt[_market_phase(_now)]}"
    )
    st.checkbox(
        "盘中自动刷新",
        value=True,
        key="auto_refresh_on",
        help="交易时段每 45 秒自动拉一次行情；收盘后不刷新。页面关掉就不再请求。",
    )

    st.divider()
    st.markdown("#### 显示模式")
    st.selectbox(
        "显示模式",
        options=list(VIEW_MODES),
        format_func=lambda k: VIEW_MODES[k],
        key="view_mode",
        label_visibility="collapsed",
    )
    st.caption(
        f"当前判断为{'手机' if IS_MOBILE else '电脑'}端，"
        f"正在用{'卡片' if USE_CARDS else '表格'}展示。窄屏下表格要左右滑，选「卡片」更顺手。"
    )

    st.divider()
    st.markdown("#### 表格排序")
    st.selectbox(
        "排序方式",
        options=list(SORT_MODES),
        format_func=lambda k: SORT_MODES[k],
        key="sort_mode",
        label_visibility="collapsed",
    )
    _sync_url()  # 排序方式也写进地址栏，刷新后不丢

    st.divider()
    st.markdown("#### 监控列表")
    st.caption(
        f"当前 {len(st.session_state.watchlist)} 只。支持 600519.SH / 000858.SZ，"
        "不写交易所会自动补全；添加后均线 / 量比 / 狙击预警全部自动计算。"
    )

    # 用会变的 key 让输入框在添加后自动清空（st.text_input 无法直接改 widget 值）
    _rev = st.session_state.add_rev
    code_in = st.text_input(
        "股票代码",
        placeholder="请输入股票代码（如 600519.SH 或 000858.SZ）",
        label_visibility="collapsed",
        key=f"code_input_{_rev}",
    )

    _c1, _c2 = st.columns(2)
    if _c1.button("添加", type="primary", **_full_width(st.button)):
        _add_symbol(code_in)
        st.session_state.add_rev += 1
        st.rerun()

    if _c2.button("重置列表", **_full_width(st.button)):
        st.session_state.watchlist = [dict(w) for w in DEFAULT_WATCHLIST]
        st.session_state.wl_msg = ("ok", "已恢复默认 9 只标的")
        _sync_url()
        st.rerun()

    _msg = st.session_state.wl_msg
    if _msg:
        _tone, _text = _msg
        if _tone == "ok":
            st.success(_text)
        elif _tone == "warn":
            st.warning(_text)
        else:
            st.error(_text)

    with st.expander("移除标的"):
        _to_drop = st.multiselect(
            "选择要移出的标的",
            options=[w["symbol"] for w in st.session_state.watchlist],
            format_func=lambda s: next(
                (f"{w['name']} {s}" for w in st.session_state.watchlist if w["symbol"] == s),
                s,
            ),
            label_visibility="collapsed",
        )
        if st.button("移出监控", disabled=not _to_drop, **_full_width(st.button)):
            st.session_state.watchlist = [
                w for w in st.session_state.watchlist if w["symbol"] not in _to_drop
            ]
            st.session_state.wl_msg = ("ok", f"已移除 {len(_to_drop)} 只")
            _sync_url()
            st.rerun()

    st.divider()
    st.markdown("#### 持仓成本")
    st.caption("双击「持仓成本」单元格直接改，改完按回车生效。填 0 表示不持仓。")

    _wl = st.session_state.watchlist
    _costs = st.session_state.costs
    _syms = [w["symbol"] for w in _wl]

    default_costs = pd.DataFrame(
        {
            "代码": _syms,
            "持仓成本": [float(_costs.get(s, 0.0)) for s in _syms],
        }
    )

    # key 里带上标的指纹：增删标的时重建表格，且新表格从 session_state.costs 取初值，
    # 所以已经填好的成本不会因为列表变化而丢失
    _sig = hashlib.md5("|".join(_syms).encode()).hexdigest()[:8]
    edited = st.data_editor(
        default_costs,
        key=f"cost_editor_{st.session_state.cloud_rev}_{_sig}",
        hide_index=True,
        height=min(420, 45 + 35 * len(_syms)),
        disabled=["代码"],
        column_config={
            "代码": st.column_config.TextColumn("代码", width=95),
            "持仓成本": st.column_config.NumberColumn(
                "持仓成本", min_value=0.0, step=0.001, format="%.3f", width=90
            ),
        },
        **_full_width(st.data_editor),
    )

    # 把编辑结果回写到会话状态，作为成本的唯一真相来源
    for _, _row in edited.iterrows():
        _costs[str(_row["代码"])] = float(_row["持仓成本"] or 0.0)

    if st.session_state.cloud_flash:
        _tone, _text = st.session_state.cloud_flash
        (st.success if _tone == "ok" else st.caption)(_text)

    st.divider()
    st.markdown("#### 云同步")
    if not CLOUD_READY:
        st.caption("云同步模块未随本次部署上传，改用本地会话保存成本（关掉浏览器会清空）。")
    else:
        st.caption("登录后把成本存到云端，换设备或清缓存都不会丢。")

        local_costs = [
            {"symbol": str(row["代码"]), "cost": float(row["持仓成本"] or 0.0)}
            for _, row in edited.iterrows()
        ]
        cloud_state = cloudsync.cloud_sync_panel(config=CLOUD_CONFIG, costs=local_costs)

        # 只处理新回传（避免每次 rerun 重复应用）
        if cloud_state and cloud_state.get("nonce"):
            if cloud_state["nonce"] != st.session_state.cloud_nonce:
                st.session_state.cloud_nonce = cloud_state["nonce"]
                action = cloud_state.get("action") or ""
                if action in ("load", "restore"):
                    rows = cloud_state.get("costs") or []
                    st.session_state.costs.update(
                        {r["symbol"]: float(r["cost"]) for r in rows if r.get("symbol")}
                    )
                    st.session_state.cloud_rev += 1
                    st.session_state.cloud_flash = ("ok", f"已从云端载入 {len(rows)} 条成本")
                    st.rerun()
                elif action == "save":
                    st.session_state.cloud_flash = (
                        "ok",
                        cloud_state.get("message") or "已保存到云端",
                    )
                    st.rerun()
                elif action == "signout":
                    st.session_state.cloud_flash = ("muted", "已退出登录，成本回到仅本会话")
                    st.rerun()

    st.divider()
    if st.button("立即刷新数据", **_full_width(st.button)):
        st.cache_data.clear()
        st.rerun()
    st.caption("行情缓存 15 分钟，避免频繁请求被封。")

COST = {str(row["代码"]): float(row["持仓成本"] or 0.0) for _, row in edited.iterrows()}


# ----------------------------------------------------------------------------
# 5. 抓数据 + 拼表
# ----------------------------------------------------------------------------
if AK_ERROR is not None:
    st.error(
        "**akshare 未安装或导入失败**，无法获取行情。\n\n"
        f"请先执行：`pip install akshare --upgrade`\n\n错误信息：`{AK_ERROR}`"
    )
    st.stop()

with st.spinner("正在抓取行情数据…"):
    raw_rows = load_all(
        tuple((w["symbol"], w["name"], w["kind"]) for w in st.session_state.watchlist),
        _cache_bucket(),
    )
    st.session_state["_last_load"] = time.time()   # 给自动刷新判断“距上次抓数多久”

df = pd.DataFrame(raw_rows)

if df["现价"].isna().all():
    st.error(
        "**所有标的都抓取失败**，常见原因：\n\n"
        "1. 本机连不上东方财富 / 新浪的数据接口（网络或防火墙）\n"
        "2. akshare 版本过旧，接口字段已变 —— 试 `pip install akshare --upgrade`\n\n"
        "请检查网络后点侧边栏「立即刷新数据」。"
    )
    with st.expander("看详细报错"):
        st.dataframe(
            df[["代码", "名称", "备注"]], hide_index=True, **_full_width(st.dataframe)
        )
    st.stop()

df["持仓成本"] = df["代码"].map(COST).fillna(0.0)

# 盈亏比例（未填成本则为空）
df["盈亏比例(%)"] = np.where(
    df["持仓成本"] > 0,
    (df["现价"] / df["持仓成本"].replace(0, np.nan) - 1) * 100,
    np.nan,
)
df.loc[df["持仓成本"] <= 0, "持仓成本"] = np.nan

# 均线多空状态
def _trend(row: pd.Series) -> str:
    if pd.isna(row["MA5"]) or pd.isna(row["MA20"]):
        return "—"
    return "多头" if row["MA5"] >= row["MA20"] else "空头"


df["均线"] = df.apply(_trend, axis=1)

# ----------------------------------------------------------------------------
# 5b. 狙击预警：均线 / 量能 / 乖离 三条件同时满足
# ----------------------------------------------------------------------------
SNIPER_HIT = "🔥 准备狙击"
SNIPER_IDLE = "观望"
VOL_RATIO_MIN = 1.5      # 量比门槛：放量才算数
BIAS_MAX = 2.0           # 距离 MA5 的乖离上限（%），超过就是追高

# 乖离率 =（现价 / MA5 − 1）× 100
df["乖离率(%)"] = np.where(
    df["MA5"] > 0,
    (df["现价"] / df["MA5"].replace(0, np.nan) - 1) * 100,
    np.nan,
)


def _sniper(row: pd.Series) -> str:
    """三条件全过才报警，任一不满足或数据缺失就观望。"""
    price, ma5, open_px = row["现价"], row["MA5"], row["开盘"]
    vol_ratio, bias = row["量比"], row["乖离率(%)"]

    if pd.isna(price) or pd.isna(vol_ratio) or pd.isna(bias):
        return "—"          # 数据缺失（含 MA5 不足 5 日）不做判断

    # 条件 1：站上 5 日线，或站上当日开盘价（任一成立即可）
    trend_ok = (pd.notna(ma5) and price > ma5) or (
        pd.notna(open_px) and price > open_px
    )
    # 条件 2：量比 > 1.5，今天确实放量
    volume_ok = vol_ratio > VOL_RATIO_MIN
    # 条件 3：|乖离率| <= 2%，不追高
    bias_ok = abs(bias) <= BIAS_MAX

    return SNIPER_HIT if (trend_ok and volume_ok and bias_ok) else SNIPER_IDLE


df["狙击预警"] = df.apply(_sniper, axis=1)


# ----------------------------------------------------------------------------
# 5c. 排序：信号 → 量比 → 涨跌幅（可切换）
# ----------------------------------------------------------------------------
def _sort_view(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
    """按侧边栏选的方式排序。mergesort 是稳定排序，同值时保持原顺序。"""
    out = frame.copy()
    # 信号档位：0 狙击 / 1 观望 / 2 无数据（没数据的沉到最底）
    out["_signal_rank"] = (
        out["狙击预警"].map({SNIPER_HIT: 0, SNIPER_IDLE: 1}).fillna(2).astype(int)
    )

    if mode == "vol":
        by, asc = ["量比"], [False]
    elif mode == "chg":
        by, asc = ["涨跌幅(%)"], [False]
    elif mode == "pnl":
        by, asc = ["盈亏比例(%)"], [False]
    else:  # 默认：信号优先，同档内先看放量、再看涨幅
        by, asc = ["_signal_rank", "量比", "涨跌幅(%)"], [True, False, False]

    out = out.sort_values(by=by, ascending=asc, na_position="last", kind="mergesort")
    return out.drop(columns=["_signal_rank"]).reset_index(drop=True)


SORT_MODE = st.session_state.get("sort_mode", "signal")
df = _sort_view(df, SORT_MODE)

# 列序按「信息密度」排：先信号、再身份与价格，常用指标跟上，
# MA20 / 均线这类一眼就能从走势看出的判断放最后（手机上先被裁掉也不心疼）。
VIEW_COLS = [
    "狙击预警",      # 1 最重要：今天要不要动手
    "名称",          # 2 是谁
    "代码",
    "现价",          # 3 现在多少钱
    "涨跌幅(%)",     # 4 今天涨跌
    "量比",          # 5 放量程度（信号依据）
    "乖离率(%)",     # 6 离均线多远（信号依据）
    "MA5",
    "持仓成本",
    "盈亏比例(%)",
    "MA20",          # 不常看，放最后
    "均线",          # 多空一句话结论，放最后
]
view = df[VIEW_COLS].copy()


# ----------------------------------------------------------------------------
# 6. 顶部信息条
# ----------------------------------------------------------------------------
last_dates = df["数据日期"].dropna()
data_date = last_dates.max() if len(last_dates) else pd.NaT
today = _today_cn()
is_stale = pd.notna(data_date) and data_date.date() < today

date_txt = data_date.strftime("%Y-%m-%d") if pd.notna(data_date) else "—"
st.markdown(
    f"""
<div class="hero">
  <div>
    <div class="hero-t">金工生的量价盯盘仪表盘</div>
    <div class="hero-s">A股 · ETF 双市场 · {len(view)} 个标的（可自定义） · 均线结构 + 量价狙击 + 持仓盈亏监控</div>
  </div>
  <div class="hero-r">
    <div class="hero-d">{today.strftime('%Y-%m-%d')}</div>
    <div class="hero-m">数据日期 {date_txt}</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# --- 大盘指数条（title 下方，红色大字） ---
def _index_card(item: dict) -> str:
    pct = item["涨跌幅"]
    price = item["点位"]
    if pd.isna(pct):
        color, pct_txt = FLAT, "—"
    elif INDEX_FORCE_RED:
        color, pct_txt = UP, f"{pct:+.2f}%"
    else:  # A 股习惯：涨红跌绿，但字号同样醒目
        color = UP if pct >= 0 else DOWN
        pct_txt = f"{pct:+.2f}%"
    price_txt = f"{price:,.2f}" if pd.notna(price) else "—"
    return (
        f'<div class="idx"><div class="idx-n">{item["名称"]}</div>'
        f'<div class="idx-p" style="color:{color}">{pct_txt}</div>'
        f'<div class="idx-c">{price_txt}</div></div>'
    )


with st.spinner("读取大盘指数…"):
    index_rows, index_err = load_indices(_cache_bucket())

st.markdown(
    f'<div class="idx-row">{"".join(_index_card(i) for i in index_rows)}</div>',
    unsafe_allow_html=True,
)

# 指数取不到数时把原因说出来，避免被误当成“休市所以没有数据”
if index_err:
    st.caption(
        f"⚠️ 大盘指数取数失败（{index_err}）。"
        f"当前北京时间 {_now_cn():%H:%M}，"
        f"{'盘中' if _market_phase() == 'open' else '非交易时段'}；"
        "这是数据源问题，不是休市。点侧边栏「立即刷新数据」可重试。"
    )

if is_stale:
    st.info(
        f"今天（{today:%Y-%m-%d}）不是交易日，或行情尚未更新。"
        f"以下为最近交易日 **{date_txt}** 的收盘数据。"
    )

# ----------------------------------------------------------------------------
# 7. 概览 KPI
# ----------------------------------------------------------------------------
valid = view.dropna(subset=["涨跌幅(%)"])
up_cnt = int((valid["涨跌幅(%)"] > 0).sum())
down_cnt = int((valid["涨跌幅(%)"] < 0).sum())
above_ma20 = int((view["现价"] > view["MA20"]).sum())

strongest = valid.loc[valid["涨跌幅(%)"].idxmax()] if len(valid) else None
weakest = valid.loc[valid["涨跌幅(%)"].idxmin()] if len(valid) else None

pnl_series = view["盈亏比例(%)"].dropna()
avg_pnl = float(pnl_series.mean()) if len(pnl_series) else None


def _kpi(label: str, value: str, sub: str = "", color: str = "#e6edf3") -> str:
    return (
        f'<div class="kpi"><div class="kpi-l">{label}</div>'
        f'<div class="kpi-v" style="color:{color}">{value}</div>'
        f'<div class="kpi-s">{sub}</div></div>'
    )


def _signed(v: float | None) -> tuple[str, str]:
    if v is None or pd.isna(v):
        return "—", FLAT
    return f"{v:+.2f}%", (UP if v > 0 else DOWN if v < 0 else FLAT)


pnl_txt, pnl_color = _signed(avg_pnl)
cards = [
    _kpi("涨 / 跌", f"{up_cnt} / {down_cnt}", f"共 {len(valid)} 只有效报价",
         UP if up_cnt > down_cnt else DOWN if down_cnt > up_cnt else FLAT),
    _kpi("站上 MA20", f"{above_ma20} / {len(view)}", "价格在 20 日均线之上",
         UP if above_ma20 * 2 > len(view) else DOWN),
]
if strongest is not None:
    t, c = _signed(float(strongest["涨跌幅(%)"]))
    cards.append(_kpi("今日最强", t, f"{strongest['名称']} · {strongest['代码']}", c))
if weakest is not None:
    t, c = _signed(float(weakest["涨跌幅(%)"]))
    cards.append(_kpi("今日最弱", t, f"{weakest['名称']} · {weakest['代码']}", c))
cards.append(
    _kpi("等权盈亏", pnl_txt, f"{len(pnl_series)} 只已填成本" if len(pnl_series) else "尚未填写持仓成本", pnl_color)
)

st.markdown(f'<div class="kpi-row">{"".join(cards)}</div>', unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# 8. 狙击预警条（st.markdown 渲染，红色脉冲高亮）
# ----------------------------------------------------------------------------
hits = df[df["狙击预警"] == SNIPER_HIT]


def _hit_item(r: pd.Series) -> str:
    return (
        f'<div class="sniper-item"><b>{r["名称"]}</b> {r["代码"]}'
        f'　量比 {r["量比"]:.2f}　乖离 {r["乖离率(%)"]:+.2f}%</div>'
    )


if len(hits):
    st.markdown(
        '<div class="sniper-hit">'
        f'<div class="sniper-t">{SNIPER_HIT}</div>'
        f'<div class="sniper-n">{len(hits)} / {len(df)} 只</div>'
        f'<div class="sniper-list">{"".join(_hit_item(r) for _, r in hits.iterrows())}</div>'
        "</div>",
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="sniper-idle">'
        "🔥 <b>狙击预警</b>　当前无标的满足全部条件"
        f"（均线/开盘价之上 · 量比 &gt; {VOL_RATIO_MIN} · 距 MA5 乖离 ≤ {BIAS_MAX:.0f}%）"
        "</div>",
        unsafe_allow_html=True,
    )


# ----------------------------------------------------------------------------
# 9. 主表格
# ----------------------------------------------------------------------------
def _as_float(v) -> float:
    """把显示用的字符串（如「+2.73」「—」）转回 float，失败给 NaN。"""
    try:
        return float(str(v).replace("+", "").replace("%", "").replace(",", ""))
    except Exception:
        return float("nan")


def _pct_color(v) -> str:
    f = _as_float(v)
    if not np.isfinite(f) or f == 0:
        return f"color:{FLAT}"
    return f"color:{UP};font-weight:600" if f > 0 else f"color:{DOWN};font-weight:600"


def _trend_color(v) -> str:
    if v == "多头":
        return f"color:{UP}"
    if v == "空头":
        return f"color:{DOWN}"
    return f"color:{FLAT}"


def _sniper_color(v) -> str:
    if v == SNIPER_HIT:
        return (
            "background-color:rgba(255,75,75,.20);"
            f"color:{UP};font-weight:700"
        )
    return f"color:{FLAT}"


def _apply(styler, func, subset):
    method = getattr(styler, "map", None) or styler.applymap
    return method(func, subset=subset)


# Streamlit 1.66 + pandas 3 的坑：NaN 在表格里会被渲染成字面量「None」，
# na_rep 不生效。所以先把数值列格式化成字符串，空值统一写成「—」再交给表格。
_TABLE_FMT = {
    "现价": "{:.3f}",
    "涨跌幅(%)": "{:+.2f}",
    "量比": "{:.2f}",
    "MA5": "{:.3f}",
    "MA20": "{:.3f}",
    "乖离率(%)": "{:+.2f}",
    "持仓成本": "{:.3f}",
    "盈亏比例(%)": "{:+.2f}",
}

disp = view.copy()
for _col, _spec in _TABLE_FMT.items():
    disp[_col] = view[_col].map(lambda v, _s=_spec: "—" if pd.isna(v) else _s.format(v))

styler = disp.style
styler = _apply(styler, _pct_color, ["涨跌幅(%)", "盈亏比例(%)"])
styler = _apply(styler, _trend_color, ["均线"])
styler = _apply(styler, _pct_color, ["乖离率(%)"])
styler = _apply(styler, _sniper_color, ["狙击预警"])


# ----------------------------------------------------------------------------
# 9b. 卡片布局（手机）
# ----------------------------------------------------------------------------
def _num(v, digits: int = 3, plus: bool = False) -> str:
    """数值格式化，NaN 统一显示破折号，避免手机上出现 nan / <NA>。"""
    if v is None:
        return "—"
    try:
        if pd.isna(v):
            return "—"
        f = float(v)
    except Exception:
        return "—"
    if not np.isfinite(f):
        return "—"
    return f"{f:+.{digits}f}" if plus else f"{f:.{digits}f}"


def _tone(v) -> str:
    """红涨绿跌的 class，缺失值走灰。"""
    try:
        if pd.isna(v) or float(v) == 0:
            return "t-flat"
        return "t-up" if float(v) > 0 else "t-down"
    except Exception:
        return "t-flat"


def _card_html(row: pd.Series) -> str:
    signal = str(row["狙击预警"])
    hit = signal == SNIPER_HIT
    chg = row["涨跌幅(%)"]

    # 左边框：命中狙击用红，否则跟着当日涨跌走
    if hit:
        cls = "hit"
    elif pd.notna(chg) and float(chg) > 0:
        cls = "bull"
    elif pd.notna(chg) and float(chg) < 0:
        cls = "bear"
    else:
        cls = ""

    tag_cls = "hit" if hit else "idle"
    name = html.escape(str(row["名称"]))
    code = html.escape(str(row["代码"]))

    pnl = row["盈亏比例(%)"]
    pnl_txt = _num(pnl, 2, plus=True)
    cost = _num(row["持仓成本"], 3)

    return (
        f'<div class="mcard {cls}">'
        f'<div class="mcard-h">'
        f'<div><span class="mcard-n">{name}</span><span class="mcard-c">{code}</span></div>'
        f'<span class="mcard-tag {tag_cls}">{html.escape(signal)}</span>'
        f"</div>"
        f'<div class="mcard-p">'
        f'<span class="mcard-px">{_num(row["现价"], 3)}</span>'
        f'<span class="mcard-chg {_tone(chg)}">{_num(chg, 2, plus=True)}%</span>'
        f"</div>"
        f'<div class="mcard-g">'
        f'<div class="mg"><span>量比</span><b>{_num(row["量比"], 2)}</b></div>'
        f'<div class="mg"><span>乖离</span><b class="{_tone(row["乖离率(%)"])}">{_num(row["乖离率(%)"], 2, plus=True)}%</b></div>'
        f'<div class="mg"><span>成本</span><b>{cost}</b></div>'
        f'<div class="mg"><span>盈亏</span><b class="{_tone(pnl)}">{pnl_txt}{"%" if pnl_txt != "—" else ""}</b></div>'
        f"</div>"
        f'<div class="mcard-f">MA5 {_num(row["MA5"], 3)} · MA20 {_num(row["MA20"], 3)} · '
        f'{html.escape(str(row["均线"]))}</div>'
        f"</div>"
    )


if USE_CARDS:
    st.markdown(
        '<div class="mcards">' + "".join(_card_html(r) for _, r in view.iterrows()) + "</div>",
        unsafe_allow_html=True,
    )
else:
    st.dataframe(styler, height=420, **_full_width(st.dataframe))

st.caption(
    "量比 = 最新成交量 ÷ 前 5 日均量；盘中为未完成成交量，收盘后才有意义。"
    "涨跌幅与盈亏按 A 股习惯红涨绿跌。"
    f"狙击预警 = 站上 MA5 或开盘价 且 量比 > {VOL_RATIO_MIN} 且 |乖离率| ≤ {BIAS_MAX:.0f}%，三者同时满足才点亮。"
    f"当前排序：{SORT_MODES.get(SORT_MODE, SORT_MODES['signal'])}（可在左侧切换）"
)

# ----------------------------------------------------------------------------
# 10. 异常与数据源明细
# ----------------------------------------------------------------------------
failed = df[df["现价"].isna()]
backup_used = df[df["数据源"].str.contains("新浪", na=False)]

with st.expander("数据源与异常明细"):
    st.dataframe(
        df[["代码", "名称", "数据源", "数据日期", "备注"]].assign(
            数据日期=lambda d: d["数据日期"].dt.strftime("%Y-%m-%d").fillna("—")
        ),
        hide_index=True,
        **_full_width(st.dataframe),
    )
    if len(backup_used):
        st.warning(f"以下 {len(backup_used)} 只主源失败、已切换到新浪备援：" + "、".join(backup_used["名称"]))
    if len(failed):
        st.error(f"以下 {len(failed)} 只两个数据源都没取到，表格中显示为「—」：" + "、".join(failed["名称"]))

st.caption("数据来源：akshare（东方财富 / 新浪）。仅供个人盯盘参考，不构成投资建议。")


# ----------------------------------------------------------------------------
# 11. 盘中自动刷新
#    用 st.fragment(run_every=...) 定时触发整页重跑；只在交易时段、且用户没关掉时生效。
#    缓存桶（_cache_bucket）保证重跑时真的会去拉新数据，而不是命中旧缓存。
# ----------------------------------------------------------------------------
if (
    st.session_state.get("auto_refresh_on", True)
    and _market_phase() in ("open", "break")
    and hasattr(st, "fragment")
):
    _AUTO_SEC = _cache_ttl()

    @st.fragment(run_every=_AUTO_SEC)
    def _auto_refresh() -> None:
        # 关键：只有“确实过了刷新间隔”才触发整页重跑。
        # 否则每次正常渲染时 fragment 也会立刻 rerun，形成自激死循环（页面永远渲染不出来）。
        if time.time() - st.session_state.get("_last_load", 0.0) < _AUTO_SEC - 0.5:
            return
        try:
            st.rerun(scope="app")     # Streamlit >= 1.37：重跑整个脚本
        except TypeError:             # 老版本没有 scope 参数
            st.rerun()

    _auto_refresh()
