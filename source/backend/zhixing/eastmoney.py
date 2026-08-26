"""东方财富交易页的**事实**:地址、字段名、脚本、返回值解析。

## 这个模块为什么存在

登录、采集、下单三件事都要和东财打交道,而它们共用的不是代码结构,是
**一堆只能从实践里换来的具体事实**:哪个 URL、哪个元素 id、返回的 JSON
里字段叫 ``Kyzj`` 还是 ``kyzj``、哪个状态码表示会话过期。这些事实散在三个
模块里,就会三处各写一份,然后各自过时。

## 「算」和「碰」在这里分家

本模块**只有算**:常量、脚本正文(字符串)、以及把东财返回的 JSON 变成
``AccountReport`` 的纯函数。一个浏览器都不碰。

这不是洁癖。东财的字段名是这整套里最容易写错、也最难发现写错的地方——
写错一个 ``Kyzj``,``可用资金`` 就变成 0,而 0 是个合法数字,校验会照常
通过,然后一整轮判断建立在"没钱"上。把解析摘成纯函数,自检就能拿一份
构造出来的返回值把它整个验一遍,不用登录、不用联网、不用真钱。

碰的部分在 ``login.py`` / ``collect.py`` / ``broker.py``。

## 三条用血换来的事实(来自二代,原样保留)

1. **登录页会把你填进去的值清空。** 东财的登录页在初始化之后重置输入框,
   填一次就走必然失败。要连续补写并最终校验——见 ``FILL_LOGIN_JS``。
2. **``Zxsz`` 返回的不是证券市值。** 实测贴近总资产。直接采信会让
   「可用资金 + 证券市值 = 总资产」这条恒等式不成立,而模型看到自相矛盾的
   账户会强制 hold,整轮白跑。真正的证券市值是持仓市值之和——见
   ``parse_asset_and_position``。
3. **持仓数量看 ``Zqsl`` 不看 ``Gfye``。** 日内成交之后 ``Gfye`` 可能还停在
   成交前的余额。别名顺序不是随手排的。

## 涉密

本模块不接触资金账号和交易密码。账号只在 ``login.py`` 里作为脚本参数
一次性传入;本模块唯一和账号沾边的是 ``mask_account_id``——**它是为了让
账号能出现在归档里而存在的**,不是装饰。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
#  地址(算)
# ---------------------------------------------------------------------------

#: 交易域名。**不是 eastmoney.com。** 这是二代实测出来的真实交易入口,
#: 从主站猜是猜不到的。
BASE_URL = "https://jywgmix.18.cn"

LOGIN_URL = f"{BASE_URL}/Login?el=1&clear=&returl=%2FTrade%2FBuy"
BUY_URL = f"{BASE_URL}/Trade/Buy"
SELL_URL = f"{BASE_URL}/Trade/Sale"
REVOKE_URL = f"{BASE_URL}/Trade/Revoke"
POSITION_URL = f"{BASE_URL}/Search/Position"

#: 账户 + 持仓一次拿全的接口。POST,表单头,空 body,靠 Cookie 认。
ASSET_ENDPOINT = "/Com/queryAssetAndPositionV1"

#: 委托 / 成交 / 可撤,三个查询接口。
ACTIVITY_ENDPOINTS: dict[str, str] = {
    "可撤委托": "/Trade/queryRevocableWEBV1",
    "当日委托": "/Search/queryTodayOrderWEB",
    "当日成交": "/Search/queryTodayMatchWEB",
}

#: 下单要用到的市场参数。东财的页面模块靠这几个值决定往哪个市场报单。
#: **只支持沪深**,别的市场不是"暂不支持",是这张表里压根没有,下单会明确拒绝。
MARKET_SETUP: dict[str, dict[str, str]] = {
    "SH": {"market_flag": "1", "trade_market": "HA", "delegate_buy": "B", "delegate_sell": "S"},
    "SZ": {"market_flag": "2", "trade_market": "SA", "delegate_buy": "B", "delegate_sell": "S"},
}

#: 验证码图片的元素 id。
CAPTCHA_IMG_ID = "imgValidCode"

#: 东财接口的「成功」。``Status`` 是字符串 ``"0"``,不是数字 0,也不是 200。
STATUS_OK = "0"

#: 会话过期的两个码。**它们和"失败"不是一回事**——这两个码意味着重新登录
#: 就能好,而其它失败重新登录也没用。分不开这两者,就会在真故障时反复登录。
STATUS_SESSION_EXPIRED = frozenset({"-2", "-99"})


# ---------------------------------------------------------------------------
#  脚本(算 —— 这里只是字符串,执行在别处)
# ---------------------------------------------------------------------------

#: 填账号和密码。**为什么是注脚本而不是定位元素点击:**
#: 东财的输入框绑了一堆自己的事件,``send_keys`` 触发不全,值填进去了但
#: 页面内部状态没跟上,提交时报"请输入账号"。用原生 setter 改值再手动派发
#: 一整串事件,是唯一稳的做法。
#:
#: **12 次循环补写不是重试,是必需。** 见模块开头第 1 条。
#:
#: 参数:``[账号, 交易密码]``。⚠️ 密码从这里过,调用方不许记 args。
FILL_LOGIN_JS = r"""
const done = arguments[arguments.length - 1];
const accountId = String(arguments[0] || "");
const password = String(arguments[1] || "");
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const pick = (selectors) => {
  for (const s of selectors) {
    try { const el = document.querySelector(s); if (el) return el; } catch (e) {}
  }
  return null;
};
const fill = (el, value) => {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, value);
  for (const type of ["keydown", "input", "keyup", "change", "blur"]) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }
};
(async () => {
  try {
    const account = pick(["#txtZjzh", "#zjzh", "#fundAccount", "#account", "#username",
                          "#userName", "#loginName", "input[name='zjzh']",
                          "input[name='fundAccount']", "input[name='account']",
                          "input[name='username']"]);
    const pwd = pick(["#txtPwd", "#txtPassword", "#password", "#pwd", "#tradePassword",
                      "#trdpwd", "input[name='password']", "input[name='pwd']",
                      "input[name='trdpwd']", "input[type='password']"]);
    if (!account) return done({ok:false, detail:"登录页上找不到账号输入框。"});
    if (!pwd) return done({ok:false, detail:"登录页上找不到密码输入框。"});
    // 东财登录页会在初始化后重置输入框。连续补写并最终校验,
    // 避免"填进去了但页面又变空",那种失败会以「请输入账号」的面目出现。
    for (let i = 0; i < 12; i += 1) {
      fill(account, accountId);
      await sleep(150 + Math.random() * 250);
      if (account.value === accountId && i >= 2) break;
    }
    if (password) {
      for (let i = 0; i < 12; i += 1) {
        fill(pwd, password);
        await sleep(120 + Math.random() * 200);
        if (pwd.value === password && i >= 2) break;
      }
    }
    done({
      ok: account.value === accountId && (!password || pwd.value === password),
      account_filled: account.value === accountId,
      // 只报"填没填上",**不回显值**。
      password_filled: !password || pwd.value === password,
      detail: "",
    });
  } catch (error) {
    done({ok:false, detail:String((error && error.message) || error)});
  }
})();
"""

#: 抓验证码。**canvas → PNG,不截屏不裁剪。**
#:
#: 二代有两条路:这条,和"整页截图再按坐标裁"。后者要 Pillow。
#: 走 canvas 拿到的是图片的原生分辨率(裁剪拿到的是屏幕像素,还受缩放影响),
#: 而且不用引入任何图像库——**零依赖是靠这一条守住的**。
#:
#: 返回 ``data:image/png;base64,...``;拿不到图返回空串。
CAPTURE_CAPTCHA_JS = r"""
const done = arguments[arguments.length - 1];
const selector = arguments[0];
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  try {
    let img = null;
    if (selector) { try { img = document.querySelector(selector); } catch (e) { img = null; } }
    if (!img) img = document.getElementById("__CAPTCHA_ID__");
    if (!img) return done({ok:false, detail:"页面上没有验证码图片元素。", data_url:""});
    if (!img.complete || !img.naturalWidth) {
      // 图还没加载完就画,得到的是一张空白图,而空白图会被识别成某个
      // 看似合理的答案,然后以「验证码错误」的面目出现。宁可等。
      await new Promise((resolve) => {
        let settled = false;
        const finish = () => { if (!settled) { settled = true; resolve(); } };
        img.addEventListener("load", finish, {once:true});
        img.addEventListener("error", finish, {once:true});
        setTimeout(finish, 5000);
      });
    }
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    if (!w || !h) return done({ok:false, detail:"验证码图片尺寸为 0,可能还没加载出来。", data_url:""});
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    canvas.getContext("2d").drawImage(img, 0, 0, w, h);
    done({ok:true, detail:"", width:w, height:h, data_url: canvas.toDataURL("image/png")});
  } catch (error) {
    // 跨域图片会让 toDataURL 抛 SecurityError。这要能和"没找到元素"分开,
    // 因为处置完全不同:一个要改抓法,一个要改选择器。
    done({ok:false, detail:"抓验证码失败:" + String((error && error.message) || error), data_url:""});
  }
})();
""".replace("__CAPTCHA_ID__", CAPTCHA_IMG_ID)

#: 填验证码并提交。参数:``[验证码答案]``。
SUBMIT_LOGIN_JS = r"""
const done = arguments[arguments.length - 1];
const answer = String(arguments[0] || "");
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const pick = (selectors) => {
  for (const s of selectors) {
    try { const el = document.querySelector(s); if (el) return el; } catch (e) {}
  }
  return null;
};
const fill = (el, value) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  setter.call(el, value);
  for (const type of ["keydown", "input", "keyup", "change", "blur"]) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }
};
// 现场:找不到元素时报出来的东西。**只报有没有、可不可见,不报值。**
const seen = (sel) => {
  const el = document.querySelector(sel);
  if (!el) return sel + "=无";
  const s = getComputedStyle(el), r = el.getBoundingClientRect();
  const vis = s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  return sel + (vis ? "=可见" : "=隐藏");
};
const scene = () => [
  "地址" + location.href,
  "input×" + document.querySelectorAll("input").length,
  "button×" + document.querySelectorAll("button").length,
  "iframe×" + document.querySelectorAll("iframe").length,
  seen("#txtZjzh"), seen("#txtValidCode"), seen("#imgValidCode"), seen("#btnConfirm"),
].join(" | ");
(async () => {
  try {
    const box = pick(["#txtValidCode", "#validCode", "#vcode", "#captcha",
                      "input[name='validCode']", "input[name='vcode']",
                      "input[name='captcha']"]);
    if (!box) return done({ok:false, detail:"登录页上找不到验证码输入框。当时的页面:" + scene()});
    for (let i = 0; i < 6; i += 1) {
      fill(box, answer);
      await sleep(120);
      if (box.value === answer && i >= 1) break;
    }
    if (box.value !== answer) return done({ok:false, detail:"验证码填不进去,页面反复清空输入框。"});
    // #btnConfirm 是东财登录页上实测的那个 id(2026-08-20 抓过 DOM)。
    let submit = pick(["#btnConfirm", "#btnLogin", "#loginBtn", "#btn_login", "#login",
                       "#submit", "#submitBtn", ".login-btn", ".btn-login", ".submit-btn"]);
    if (!submit) {
      // ⚠️ 这里**不能**按 type 属性去选:那是属性选择器,而东财的
      // <button id="btnConfirm" class="btn"> 根本没写 type 属性——el.type 读出来
      // 的 "submit" 是 DOM 默认值。用属性选,一个都选不中,报的却是"找不到按钮"。
      const visible = (el) => {
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
      };
      submit = Array.from(document.querySelectorAll("button, input")).find(
        el => el.type === "submit" && visible(el) && !el.disabled);
    }
    if (!submit) return done({ok:false, detail:"登录页上找不到提交按钮。当时的页面:" + scene()});
    submit.click();
    await sleep(2500);
    done({ok:true, detail:"", current_url: location.href});
  } catch (error) {
    done({ok:false, detail:String((error && error.message) || error)});
  }
})();
"""

#: 查账户 + 持仓。走页面自己的 Cookie,所以必须在东财页面上执行。
#:
#: 15 秒超时后 abort:接口卡死和登录失效在网络层长得一样,不设超时就会
#: 挂在那里等到脚本超时,报出来的是「脚本超时」——一句什么都没说的话。
QUERY_ASSET_JS = r"""
const done = arguments[arguments.length - 1];
const endpoint = arguments[0];
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  await sleep(800);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
      },
      body: "",
      signal: controller.signal,
    });
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch (e) {}
    if (!payload || typeof payload !== "object") {
      return done({ok:false, detail:"账户接口没返回 JSON,多半是还没登录或页面已跳走。",
                   http_status: response.status, body: String(text || "").slice(0, 300),
                   current_url: location.href});
    }
    done({ok:true, response: payload, current_url: location.href});
  } catch (error) {
    const name = (error && error.name) ? String(error.name) : "Error";
    done({ok:false,
          detail: name === "AbortError"
            ? "账户接口 15 秒没响应。可能登录已失效,也可能券商那头就是慢。"
            : String((error && error.message) || error),
          current_url: location.href});
  } finally { clearTimeout(timer); }
})();
"""

#: 查委托 / 成交 / 可撤。参数:``[{名字: 路径}]``。**一个失败不影响其余**,
#: 每个接口各自带回自己的成败——一次报全部,不是撞上第一个错就停。
QUERY_ACTIVITY_JS = r"""
const done = arguments[arguments.length - 1];
const endpoints = arguments[0] || {};
const result = {};
const call = (path) => new Promise((resolve) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  fetch(path, {
    method: "POST", credentials: "include",
    headers: {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
              "X-Requested-With": "XMLHttpRequest"},
    body: "", signal: controller.signal,
  }).then(r => r.text()).then(text => {
    clearTimeout(timer);
    try { resolve({ok:true, response: JSON.parse(text)}); }
    catch (e) { resolve({ok:false, detail:"返回的不是 JSON", body:String(text||"").slice(0,200)}); }
  }).catch(error => {
    clearTimeout(timer);
    resolve({ok:false, detail:String((error && error.message) || error)});
  });
});
(async () => {
  for (const [name, path] of Object.entries(endpoints)) {
    result[name] = await call(path);
  }
  done({ok:true, result, current_url: location.href});
})();
"""

#: 下单。参数:``[指令, 市场参数]``。指令形如
#: ``{action, symbol, qty, limit_price, name}``。
#:
#: **委托一旦被接受就是真金白银,所以这段脚本任何一步不确定都直接 done 掉,
#: 绝不"尽力而为"地继续。** 模块级约束:写操作绝不自动重试。
SUBMIT_ORDER_JS = r"""
const done = arguments[arguments.length - 1];
const instruction = arguments[0];
const marketSetup = arguments[1];
const side = instruction.action;
const pageSide = side === "buy" ? "buy" : "sale";
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const applyValue = (selector, value) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  el.value = String(value === null || value === undefined ? "" : value);
  for (const type of ["input", "change", "blur"]) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }
  return true;
};
(async () => {
  try {
    if (!applyValue("#stockCode", instruction.symbol)) {
      return done({accepted:false, detail:"交易页上找不到证券代码输入框,可能没登录。"});
    }
    document.querySelector("#stockCode").dispatchEvent(
      new KeyboardEvent("keydown", {key:"Tab", bubbles:true}));
    await sleep(1800);
    if (!(window.UC && UC.Module)) {
      return done({accepted:false, detail:"东方财富交易模块未就绪,多半尚未登录。"});
    }
    const stockDic = (UC.Setting && UC.Setting.stockDic) || {};
    UC.Setting.tradeMarket = marketSetup.trade_market;
    const secuType = String(stockDic.SecuType || stockDic.realType || stockDic.OriginalSecuType || "1");
    UC.Module.setSelectBox(pageSide, marketSetup.market_flag, secuType);
    const select = document.querySelector("#delegateWay");
    if (!select) return done({accepted:false, detail:"未找到委托方式选择框。"});
    select.value = side === "buy" ? marketSetup.delegate_buy : marketSetup.delegate_sell;
    if (typeof window.$ === "function") window.$("#delegateWay").trigger("change");
    if (typeof UC.Module.updateMaxCount === "function") UC.Module.updateMaxCount(pageSide);
    applyValue("#iptPrice", Number(instruction.limit_price).toFixed(3));
    applyValue("#iptCount", String(instruction.qty));
    await sleep(500);
    const payload = {
      stockCode: (document.querySelector("#stockCode").value || "").trim() || instruction.symbol,
      price: (document.querySelector("#iptPrice").value || "").trim()
             || Number(instruction.limit_price).toFixed(3),
      amount: (document.querySelector("#iptCount").value || "").trim() || String(instruction.qty),
      tradeType: select.value,
      zqmc: ((document.querySelector("#iptbdName") || {}).value || "").trim() || instruction.name || "",
      market: UC.Setting.tradeMarket || marketSetup.trade_market,
    };
    if (side === "sell") payload.gddm = UC.Setting.gddm || "";
    // 提交前最后一道:页面上的价和量必须和我们要下的一致。
    // 东财会自己改写输入框(涨跌停修正、最小变动价位),改写之后
    // **成交的是页面上那个数,不是我们算的那个数**。
    if (payload.amount !== String(instruction.qty)) {
      return done({accepted:false, detail:"页面把委托数量改成了 " + payload.amount
                   + ",与指令的 " + instruction.qty + " 不一致,已放弃提交。"});
    }
    if (!(window.utools && typeof utools.loadAjax === "function")) {
      return done({accepted:false, detail:"东方财富 utools.loadAjax 不可用。", prepared: payload});
    }
    const finish = (path, finalPayload) => {
      utools.loadAjax(path, finalPayload, function (response) {
        const accepted = !!(response && String(response.Status) === "0");
        done({accepted,
              detail: accepted ? "东方财富已接受委托。"
                               : ((response && response.Message) || "东方财富拒绝了委托。"),
              endpoint: path, prepared: finalPayload, response});
      }, true);
    };
    if (side === "buy" && typeof UC.Module.isSpecialCode === "function"
        && UC.Module.isSpecialCode(payload.stockCode)) {
      finish("/Trade/SubmitSpecialTrade", {...payload, price: String(payload.price || "").split(".")[0]});
    } else {
      finish("/Trade/SubmitTradeV2", payload);
    }
  } catch (error) {
    done({accepted:false, detail:String((error && error.message) || error)});
  }
})();
"""

#: 撤单。参数:``[{wtbh}]``。先查可撤列表拿到配套字段,再撤——
#: 只凭一个委托编号撤不掉,东财要日期 / 市场 / 买卖标记一起给。
CANCEL_ORDER_JS = r"""
const done = arguments[arguments.length - 1];
const instruction = arguments[0];
try {
  if (!(window.utools && typeof utools.loadAjax === "function")) {
    done({accepted:false, detail:"东方财富 utools.loadAjax 不可用。"});
  } else {
    utools.loadAjax("/Trade/queryRevocableWEBV1", {}, function (query) {
      const rows = (query && query.Data) || [];
      const want = String(instruction.wtbh || "").trim();
      const matched = rows.find(item =>
        String(item.Wtbh || item.wtbh || item.ordersno || "").trim() === want);
      if (!matched) {
        // 撤不到不一定是错:可能已经成交了,也可能已经撤过。
        // 这两者和"撤单失败"不是一回事,所以单独给个 detail。
        done({accepted:false, not_found:true,
              detail:"可撤列表里没有委托 " + want + "(可能已成交或已撤销)。"});
        return;
      }
      const payload = {
        wtrq: matched.Wtrq || matched.wtrq || "",
        wtbh: matched.Wtbh || matched.wtbh || want,
        market: matched.Market || matched.market || "",
        mmlb: matched.Mmbz || matched.mmlb || "",
      };
      utools.loadAjax("/Trade/cancelStockWEB", payload, function (response) {
        const accepted = !!(response && String(response.Status) === "0");
        done({accepted,
              detail: accepted ? "东方财富已接受撤单。"
                               : ((response && response.Message) || "东方财富拒绝了撤单。"),
              prepared: payload, response});
      }, true);
    }, true);
  }
} catch (error) {
  done({accepted:false, detail:String((error && error.message) || error)});
}
"""


#: 读登录页上的报错文字。
#:
#: **为什么值得单独一趟:** 登录失败之后,"验证码认错了"和"密码不对"在
#: 程序看来长得一模一样——都是没登进去。可这两件事的处置完全相反:
#: 前者换张图重来就行,后者重来一百次也是白搭,而且**东财会锁账户**。
#: 页面上那句话是唯一能把它们分开的东西,所以要去读。
READ_LOGIN_ERROR_JS = r"""
const done = arguments[arguments.length - 1];
try {
  const texts = [];
  // #ertips 是 2026-08-20 对着真登录页抓下来的:东财把报错放在
  // <li id="ertips">您输入的信息有误，请重新输入!</li> 里。
  // 后面那几个是当初凭印象写的,实测**一个都没命中过**——留着当兜底,
  // 但别再往这个列表里添没验过的猜测了。
  const selectors = ["#ertips", "#errMsg", "#errorMsg", ".error-msg", ".err-msg",
                     ".login-error", ".tip-error", "#loginError",
                     "[class*='error']", "[class*='err']"];
  // ⚠️ 这里**不能**用元素的定位父节点是不是 null 来判可见:那个写法
  // 从没对着真页面验过,和上面那批 #errMsg 选择器是同一批猜测。
  // 换成实测能看见 #ertips 的这一套:算出来的样式 + 真实占位。
  const 可见 = (el) => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
  };
  for (const s of selectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(s)); } catch (e) { continue; }
    for (const n of nodes) {
      const t = (n.textContent || "").trim();
      // 只要看得见的。隐藏的报错框在页面上一直存在,读进来全是噪音。
      if (t && t.length <= 120 && 可见(n)) texts.push(t);
    }
  }
  done({ok:true, messages: Array.from(new Set(texts)).slice(0, 5), current_url: location.href});
} catch (error) {
  done({ok:false, messages: [], current_url: location.href});
}
"""

#: 登录失败的原因分类。**看到这些字样就别再重试了。**
#:
#: 密码错在券商那边是有次数的,试满会锁卡。自动化最容易犯的错就是
#: 把"密码不对"当成"网络抖了"然后重试五次——那不是重试,那是把账户试锁。
FATAL_LOGIN_HINTS = ("密码", "锁定", "冻结", "不存在", "已锁", "次数")

#: 这些字样说明只是验证码没认对,换张图重来是对的。
RETRYABLE_LOGIN_HINTS = ("验证码", "校验码", "图片")

#: **东财不告诉你错的是哪一项。** 账号错、密码错、验证码错,回的都是
#: 同一句「您输入的信息有误，请重新输入!」(2026-08-20 实测抓到)。
#:
#: 这句话不能简单归到上面任何一边:
#:   - 归可重试 → 密码真错时,一天六轮 × 3 次 = 18 次错误密码,把卡试锁;
#:   - 归致命   → 认错一张验证码(几个百分点是常态)就废掉一整轮。
#:
#: 所以它单独一类,交给 ``password_proven`` 去分:密码被证明可用过,
#: 那每次尝试唯一在变的就是验证码;没证明过,就一次都不多试。
AMBIGUOUS_LOGIN_HINTS = ("信息有误", "输入有误", "重新输入")


def classify_login_error(
    messages: Iterable[str], *, password_proven: bool = False
) -> tuple[bool, str]:
    """页面报错 → ``(还能不能重试, 给人看的话)``。

    **判不出来的时候返回"不能重试"。** 这是有意的保守:判错方向的代价
    不对称——把可重试的当成致命,顶多是这一轮没登上;把致命的当成可重试,
    是拿账户去撞券商的错误次数上限。

    ``password_proven`` 是**这套账号+密码此前成功登进去过**。只有含混
    提示(见 ``AMBIGUOUS_LOGIN_HINTS``)会用到它,原因见那里的注释。
    默认 False:没人告诉我"成功过",就当没成功过。
    """
    joined = " / ".join(m.strip() for m in messages if str(m).strip())
    if not joined:
        return False, "登录没成功,页面上也没有给出原因。"
    if any(h in joined for h in FATAL_LOGIN_HINTS):
        return False, f"登录被拒且不可重试(再试会撞券商的错误次数上限):{joined}"
    if any(h in joined for h in RETRYABLE_LOGIN_HINTS):
        return True, f"验证码没认对:{joined}"
    if any(h in joined for h in AMBIGUOUS_LOGIN_HINTS):
        if password_proven:
            return True, (
                f"东财只说了「{joined}」,没说错的是哪一项。"
                "这套账号密码此前成功登进去过,而每次尝试唯一在变的是验证码,"
                "所以按验证码没认对处理,换一张图重来。"
            )
        return False, (
            f"东财只说了「{joined}」,没说错的是哪一项;"
            "而这套账号密码**还没有一次成功登录的记录**(刚配好或刚改过)。"
            "此时重试就是拿可能错的密码去撞券商的次数上限,所以一次就停。"
            "请先人工确认账号和密码填对了。"
        )
    return False, f"登录没成功,原因未能归类,按不可重试处理:{joined}"


# ---------------------------------------------------------------------------
#  取值(算)
# ---------------------------------------------------------------------------

_KEY_NOISE = re.compile(r"[\s_:\-：]+")


def normalize_key(value: str) -> str:
    """把字段名压成可比较的形式。``Kyzj`` / ``kyzj`` / ``KY_ZJ`` 视作同一个。

    东财同一个字段在不同接口里大小写和下划线都不一样,按字面查会漏。
    """
    return _KEY_NOISE.sub("", str(value or "")).lower()


def lookup(payload: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    """按别名表取值,**顺序即优先级**。

    别名顺序是有含义的,不是凑数:``Zqsl`` 排在 ``Gfye`` 前面,是因为日内
    成交后 ``Gfye`` 可能还停在成交前的余额。改动顺序等于改动语义。
    """
    normalized = {normalize_key(str(k)): v for k, v in payload.items()}
    for alias in aliases:
        value = normalized.get(normalize_key(alias))
        if value is not None and str(value).strip() != "":
            return value
    return None


def text_of(payload: Mapping[str, Any], aliases: Iterable[str]) -> str:
    value = lookup(payload, aliases)
    return "" if value is None else str(value).strip()


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def number_of(payload: Mapping[str, Any], aliases: Iterable[str]) -> Decimal | None:
    """取一个数。**取不到返回 None,不返回 0。**

    这是本模块最要紧的一条。``可用资金`` 拿不到和 ``可用资金 = 0`` 在界面上
    和在校验里都必须分得开:前者是"没采到",后者是"确实没钱"。用 0 顶替,
    校验会照常通过,然后整轮判断建立在一个假事实上。
    """
    value = lookup(payload, aliases)
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "-", "—"}:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def int_of(payload: Mapping[str, Any], aliases: Iterable[str]) -> int | None:
    value = number_of(payload, aliases)
    return None if value is None else int(value)


def market_of(raw: str, symbol: str) -> str:
    """判市场。先信东财给的,给不出就按代码前缀推。

    推的规则:``6``/``5``/``9`` 开头是沪,``0``/``1``/``2``/``3`` 开头是深。
    **推不出来返回空串,不默认某一个市场**——下单时市场错了就是报到错的
    交易所,直接被拒还算好的。
    """
    text = str(raw or "").strip().upper()
    if text in {"SH", "SZ"}:
        return text
    if text in {"1", "HA", "SA1", "SHA"}:
        return "SH"
    if text in {"2", "SA", "SZA"}:
        return "SZ"
    code = str(symbol or "").strip()
    if code[:1] in {"6", "5", "9"}:
        return "SH"
    if code[:1] in {"0", "1", "2", "3"}:
        return "SZ"
    return ""


def mask_account_id(value: str) -> str:
    """遮资金账号。``123****6789``。

    **存在的理由是让账号能进归档。** 归档要能回答"这是哪个账户跑的",
    但完整账号绝不能落进文件——遮掉之后既能对得上,又不构成泄露。
    七位及以下不遮:遮了就剩不下什么,反而假装安全。
    """
    text = str(value or "").strip()
    if len(text) <= 7:
        return text
    return f"{text[:3]}****{text[-4:]}"


def decode_data_url(data_url: str) -> bytes:
    """``data:image/png;base64,...`` → 原始字节。

    解不出来抛 ``ValueError``。**不返回空字节**:空图交给识别模型,模型会
    给出一个看似合理的四位答案,然后以「验证码错误」的面目出现,人会去查
    识别接口——而问题在于压根没抓到图。
    """
    text = str(data_url or "").strip()
    if not text:
        raise ValueError("验证码图片是空的(抓图那步没拿到东西)")
    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1]
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"验证码图片不是合法的 base64:{exc}") from exc
    if not raw:
        raise ValueError("验证码图片解出来是 0 字节")
    return raw


# ---------------------------------------------------------------------------
#  账户与持仓(算)
# ---------------------------------------------------------------------------


class SessionExpired(RuntimeError):
    """券商会话失效了。**和"接口失败"是两件事**,重新登录能好。"""


class EastmoneyError(RuntimeError):
    """东财那头返回了不能用的东西。重新登录也不会好。"""


@dataclass(frozen=True)
class Position:
    """一个持仓。数量取不到就是 ``None``,不是 0。"""

    symbol: str
    name: str
    market: str
    holding_qty: int | None
    available_qty: int | None
    frozen_qty: int | None
    cost_price: Decimal | None
    last_price: Decimal | None
    market_value: Decimal | None
    profit: Decimal | None = None
    today_profit: Decimal | None = None
    #: 采集时发现的可疑之处,原样带给人看。**不代表数据被改过。**
    notes: tuple[str, ...] = ()

    def as_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "证券代码": self.symbol,
            "证券名称": self.name,
            "市场": self.market,
            "持仓数量": self.holding_qty,
            "可用数量": self.available_qty,
            "冻结数量": self.frozen_qty,
            "成本价": _plain(self.cost_price),
            "最新价": _plain(self.last_price),
            "市值": _plain(self.market_value),
            "持仓盈亏": _plain(self.profit),
            "当日盈亏": _plain(self.today_profit),
        }
        if self.notes:
            entry["说明"] = list(self.notes)
        return entry


@dataclass(frozen=True)
class AccountReport:
    """一次账户采集的产物。"""

    account_id_masked: str
    total_asset: Decimal | None
    available_cash: Decimal | None
    cash_balance: Decimal | None
    frozen_cash: Decimal | None
    securities_value: Decimal | None
    positions: tuple[Position, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        """够不够拿去做校验。

        **可用资金是唯一的硬要求**——买入校验拿它比。其余字段缺了只影响
        展示,缺可用资金就等于不知道有没有钱,那一轮一条买入都不该放行。
        """
        return self.available_cash is not None

    def as_summary(self) -> dict[str, Any]:
        """契约 1.3 的账户摘要。

        ⚠️ 账号那一项的键名是 ``账户标识``,**不是 ``账户ID``**。这不是措辞
        偏好,三处都认死了这个名字:

        1. 契约 1.3 与前端 ``types.ts`` / ``RecentPage.tsx`` 读的是 ``账户标识``。
           叫别的名字,界面上那一格是 ``undefined``——而 fixture 是对的,
           所以本地怎么点都看不出来,只有接上真账户才崩。
        2. ``archive._MUST_BE_MASKED`` 认的也是 ``账户标识``。叫别的名字,
           **出站机密扫描根本不会检查这一项**,完整账号明文进归档不会有人拦。

        第 2 条是要命的那条:换个键名,一道安全闸就静默失效了,而且它失效
        的样子和正常的样子一模一样。
        """
        summary: dict[str, Any] = {
            "账户标识": self.account_id_masked,
            "总资产": _plain(self.total_asset),
            "可用资金": _plain(self.available_cash),
            "资金余额": _plain(self.cash_balance),
            "冻结资金": _plain(self.frozen_cash),
            "证券市值": _plain(self.securities_value),
            "持仓数量": len(self.positions),
            "持仓列表": [p.as_entry() for p in self.positions],
        }
        if self.notes:
            summary["说明"] = list(self.notes)
        return summary


def _plain(value: Decimal | None) -> float | None:
    """``Decimal`` → JSON 能装的东西。``None`` 原样传下去。"""
    return None if value is None else float(value)


def status_of(payload: Mapping[str, Any]) -> str:
    return str(payload.get("Status", payload.get("status", ""))).strip()


def check_status(payload: Mapping[str, Any]) -> None:
    """看一眼状态码。会话过期抛 ``SessionExpired``,别的失败抛 ``EastmoneyError``。

    分开抛是为了让上层能"重新登录再来一次"而不是"重试同一个失败的请求"。
    """
    status = status_of(payload)
    if status in STATUS_SESSION_EXPIRED:
        raise SessionExpired(f"券商会话已失效(Status={status}),需要重新登录。")
    if status and status != STATUS_OK:
        message = str(payload.get("Message") or payload.get("message") or "").strip()
        raise EastmoneyError(f"东财接口返回 Status={status}" + (f":{message}" if message else ""))


def parse_position(item: Any) -> Position | None:
    """一行持仓。**代码不是六位数字的行直接丢掉**——那些是表头、合计行之类。"""
    if not isinstance(item, Mapping):
        return None
    symbol = text_of(item, ["Zqdm", "zqdm", "stkcode", "stockcode", "证券代码"])
    if not re.fullmatch(r"\d{6}", symbol):
        return None

    notes: list[str] = []

    # 成本价:东财在某些情况下给 0 或负数,这时候用参考成本价。
    raw_cost = number_of(item, ["Cbjg", "Ckcbj", "cost_price", "成本价"])
    ref_cost = number_of(item, ["Cbjgex", "参考成本价", "摊薄成本价"])
    cost_price = raw_cost
    if raw_cost is not None and raw_cost <= 0 and ref_cost is not None and ref_cost > 0:
        cost_price = ref_cost
        notes.append("原始成本价 ≤ 0,已改用东财的参考成本价。")

    # ⚠️ Zqsl 在前,Gfye 在后。日内成交之后 Gfye 可能还是成交前的余额。
    holding = int_of(item, ["Zqsl", "Gfye", "gfye", "持仓数量", "股份余额", "股票余额"])
    available = int_of(item, ["Kysl", "kysl", "Ksssl", "可用数量", "可卖数量"])
    last_price = number_of(item, ["Zxjg", "zxjg", "last_price", "最新价"])
    market_value = number_of(item, ["Zxsz", "zxsz", "market_value", "市值"])

    # 市值 / 最新价 能反推出一个持仓数量。二代在两者对不上时会**直接改写**
    # 持仓数量;三代不改,只记一笔。
    #
    # 理由:真正决定"能不能卖"的是 Kysl(可用数量),那是东财自己算的,
    # 而下单校验用的正是它。持仓数量只进展示和模型上下文。在这种位置上
    # 悄悄改写一个数字,换来的是"看着对了",代价是从此没人知道原始值是多少。
    # 有出入就说有出入,让人看见。
    if holding is not None and last_price and last_price > 0 and market_value is not None:
        implied = int(round(market_value / last_price))
        if abs(implied - holding) > max(1, int(abs(holding) * 0.01)):
            notes.append(
                f"股份余额({holding})与市值/最新价推算({implied})对不上,"
                "东财日内成交后余额字段可能未刷新。此处不改写,原样呈现。"
            )

    return Position(
        symbol=symbol,
        name=text_of(item, ["Zqmc", "zqmc", "stkname", "stockname", "证券名称"]) or symbol,
        market=market_of(text_of(item, ["Market", "market", "Jysc", "market_ex"]), symbol),
        holding_qty=holding,
        available_qty=available,
        frozen_qty=int_of(item, ["Djsl", "djsl", "冻结数量"]),
        cost_price=cost_price,
        last_price=last_price,
        market_value=market_value,
        profit=number_of(item, ["Ljyk", "Ckyk", "ljyk", "持仓盈亏", "累计盈亏"]),
        today_profit=number_of(item, ["Dryk", "dryk", "当日盈亏"]),
        notes=tuple(notes),
    )


def parse_asset_and_position(payload: Mapping[str, Any]) -> AccountReport:
    """把 ``queryAssetAndPositionV1`` 的返回变成 ``AccountReport``。**纯函数。**

    会话过期抛 ``SessionExpired``,格式不对抛 ``EastmoneyError``。

    ### 证券市值为什么要自己加

    ``Zxsz`` 这个字段在**账户节点**上返回的不是证券市值,实测贴近总资产
    (在**持仓行**上它倒是真的市值)。照单全收会得到
    「可用资金 + 证券市值 ≫ 总资产」,而模型看见自相矛盾的账户会强制 hold,
    整轮白跑,还很难查——因为每个数字单看都像真的。

    所以这里用持仓市值之和。加不出来(没有持仓行)就退回字段值,并记一笔。
    """
    check_status(payload)
    data = payload.get("Data")
    node: Mapping[str, Any] | None = None
    if isinstance(data, Mapping):
        node = data
    elif isinstance(data, list):
        node = next((x for x in data if isinstance(x, Mapping)), None)
    if node is None:
        raise EastmoneyError("账户接口没返回账户数据(Data 是空的)。")

    raw_positions = node.get("positions")
    if not isinstance(raw_positions, list):
        raw_positions = node.get("Positions")
    if not isinstance(raw_positions, list):
        raw_positions = []
    positions = tuple(p for p in (parse_position(i) for i in raw_positions) if p is not None)

    account_id = text_of(node, ["Zjzh", "zjzh", "fundaccount", "account_id", "资金账号"])
    if not account_id and raw_positions and isinstance(raw_positions[0], Mapping):
        account_id = text_of(raw_positions[0], ["Zjzh", "zjzh"])

    notes: list[str] = []
    values = [p.market_value for p in positions if p.market_value is not None]
    if values:
        securities_value: Decimal | None = sum(values, Decimal("0"))
    else:
        securities_value = number_of(node, ["Zxsz", "totalSecMkval", "证券市值"])
        if positions:
            notes.append("持仓行没有市值字段,证券市值退回用账户节点的 Zxsz,该字段实测不可靠。")

    return AccountReport(
        account_id_masked=mask_account_id(account_id),
        total_asset=number_of(node, ["Zzc", "RMBZzc", "zzc", "总资产"]),
        available_cash=number_of(node, ["Kyzj", "kyzj", "可用资金"]),
        cash_balance=number_of(node, ["Zjye", "zjye", "资金余额"]),
        frozen_cash=number_of(node, ["Djzj", "djzj", "冻结资金"]),
        securities_value=securities_value,
        positions=positions,
        notes=tuple(notes),
    )


def looks_logged_out(url: str) -> bool:
    """光看地址判断有没有掉登录。

    **这是个便宜的预检,不是判据。** 真正的判据是查一次账户接口看 Status。
    地址对不代表登录着(可能页面还没跳),但地址里有 Login 就一定没登录。
    """
    text = str(url or "")
    return "Login" in text or BASE_URL not in text


__all__ = [
    "BASE_URL", "LOGIN_URL", "BUY_URL", "SELL_URL", "REVOKE_URL", "POSITION_URL",
    "ASSET_ENDPOINT", "ACTIVITY_ENDPOINTS", "MARKET_SETUP", "CAPTCHA_IMG_ID",
    "STATUS_OK", "STATUS_SESSION_EXPIRED",
    "FILL_LOGIN_JS", "CAPTURE_CAPTCHA_JS", "SUBMIT_LOGIN_JS", "QUERY_ASSET_JS",
    "QUERY_ACTIVITY_JS", "SUBMIT_ORDER_JS", "CANCEL_ORDER_JS", "READ_LOGIN_ERROR_JS",
    "FATAL_LOGIN_HINTS", "RETRYABLE_LOGIN_HINTS", "AMBIGUOUS_LOGIN_HINTS",
    "classify_login_error",
    "normalize_key", "lookup", "text_of", "number_of", "int_of", "market_of",
    "mask_account_id", "decode_data_url",
    "SessionExpired", "EastmoneyError", "Position", "AccountReport",
    "status_of", "check_status", "parse_position", "parse_asset_and_position",
    "looks_logged_out",
]
