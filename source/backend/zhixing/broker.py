"""券商下单(碰)—— ``execution.BrokerAdapter`` 的真实实现。

## 这是全系统唯一花钱的地方

其余每一层写错了,后果是数据不对、界面难看、这一轮白跑。**这一层写错了,
后果是真金白银按错误的价和量成交出去。** 所以这里的规矩比别处严:

1. **绝不重试。** 下单是写操作。超时不代表没成交——委托可能已经在券商
   那边排上了,重试一次就是两笔。这条是全项目的通用约束,在这里是死线。
   一次不成就抛,让人去看委托列表。
2. **提交前再核一遍页面上的数。** 东财会自己改写输入框(涨跌停修正、
   最小变动价位对齐),而**成交的是页面上那个数,不是我们算出来的那个数**。
   核对在 ``eastmoney.SUBMIT_ORDER_JS`` 里,对不上就放弃提交。
3. **市场不在表里就拒绝,不猜。** 猜错市场是报到错的交易所,被拒还算好的。

## 为什么不在这里做本地风控

按使用者要求,资金、持仓、时段、整手、涨跌停、金额与价格偏离等本地风控
已经全部拆除。能到 ``place_order`` 的仍只有 ``guards.ValidatedOrder``;
这个类型只保证数量和价格已经转换成券商调用需要的类型,不代表通过任何
本地风险判断。券商自己的参数要求和拒单结果仍以实际接口为准。

## 会话从哪来

``BrokerAdapter`` 不自己登录。会话由 ``login.ensure_session`` 给,理由是
下单和采集要用**同一个**会话——各登各的会在券商那边变成两个终端,
东财会把先登的踢掉。谁踢掉谁取决于时序,那种 bug 查起来能要人命。

掉登录时本模块**抛 ``em.SessionExpired`` 而不是自己重登**:重登要过验证码,
可能要几十秒,而这中间价格在动。这一单该不该按新价格下,是上层的决定,
不是这一层能替它做的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from . import eastmoney as em
from . import webdriver as wd
from .guards import ValidatedOrder

logger = logging.getLogger("zhixing.broker")


class BrokerError(RuntimeError):
    """下单没成。

    ``submitted_unknown`` 为真表示**委托可能已经发出去了但结果不明**——
    这种情况绝不能当成"没下成"然后重来。分出这个标志的唯一目的就是
    让上层不敢重试。
    """

    def __init__(self, message: str, *, submitted_unknown: bool = False) -> None:
        super().__init__(message)
        self.submitted_unknown = submitted_unknown


def order_ref(response: Any) -> str:
    """从券商返回里挖委托编号。挖不到返回空串。

    **挖不到不等于没下成。** 东财偶尔返回 Status=0 但 Data 是空的,
    这时委托是成立的,只是编号得去委托列表里找。所以这里不抛异常——
    在"已接受"之后抛异常,会让上层以为没成交。
    """
    if not isinstance(response, Mapping):
        return ""
    data = response.get("Data")
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return str(data[0].get("Wtbh") or data[0].get("wtxh") or data[0].get("wtbh") or "").strip()
    if isinstance(data, Mapping):
        return str(data.get("Wtbh") or data.get("wtxh") or data.get("wtbh") or "").strip()
    return ""


@dataclass
class EastmoneyBroker:
    """在一个**已经登录**的浏览器会话上下单。

    ``session`` 由 ``login.ensure_session`` 提供。本类不管登录,见模块开头。
    """

    session: wd.Session

    # -- 下单 -------------------------------------------------------------

    def place_order(self, order: ValidatedOrder) -> str:
        """下单,返回委托编号。**不重试,失败即抛。**"""
        market = str(order.market or "").upper()
        setup = em.MARKET_SETUP.get(market)
        if setup is None:
            raise BrokerError(
                f"不支持的市场 {market!r}(只有沪深两个市场有下单参数)。"
                "这不是暂不支持,是这条指令的市场字段本身就不对。"
            )

        url = em.BUY_URL if order.action == "buy" else em.SELL_URL
        self.session.navigate(url)
        if em.looks_logged_out(self.session.current_url()):
            raise em.SessionExpired("打开交易页时被弹回登录页,券商会话已失效。")

        payload = {
            "action": order.action,
            "symbol": order.symbol,
            "name": order.name,
            "qty": order.qty,
            # 价格转成字符串再进 JS。**Decimal 直接序列化会变成浮点**,
            # 而浮点在 0.1+0.2 这种地方会多出尾数,东财按最小变动价位一对
            # 就报"价格不合法"——错得毫无线索。
            "limit_price": str(order.limit_price),
        }

        # 从这里往下,**任何异常都要当成"可能已经发出去了"**。
        # 脚本跑到一半超时、会话断掉、浏览器崩了——委托都可能已经在
        # 券商那边排上。分不清就一律按"发出去了"处置,这是唯一安全的方向。
        try:
            result = self.session.execute_async(em.SUBMIT_ORDER_JS, payload, setup)
        except wd.WebDriverError as exc:
            raise BrokerError(
                f"提交委托时浏览器出错:{exc}。"
                "**委托可能已经发出去了**,不要重试,去券商的委托列表里确认。",
                submitted_unknown=True,
            ) from exc

        if not isinstance(result, Mapping):
            raise BrokerError(
                "提交委托的脚本没返回对象。**委托可能已经发出去了**,不要重试。",
                submitted_unknown=True,
            )

        detail = str(result.get("detail") or "").strip()
        if not result.get("accepted"):
            # 明确被拒是好事:券商说了不要,那就是没下成,可以放心记失败。
            logger.info("委托被拒:%s %s %s×%s —— %s",
                        order.action, order.symbol, order.limit_price, order.qty, detail)
            raise BrokerError(detail or "东方财富拒绝了委托,但没说原因。")

        wtbh = order_ref(result.get("response"))
        logger.info("委托已被接受:%s %s %s×%s 委托编号=%s",
                    order.action, order.symbol, order.limit_price, order.qty, wtbh or "(未返回)")
        return wtbh

    # -- 撤单 -------------------------------------------------------------

    def cancel_order(self, wtbh: str) -> None:
        """撤单。撤不到就抛。

        「可撤列表里没有这一笔」也抛,但话说清楚:那多半是**已经成交或
        已经撤过**,不是撤单失败。这两者在界面上必须分得开,否则人会
        反复点撤单,而那一笔其实早就成交了。
        """
        want = str(wtbh or "").strip()
        if not want:
            raise BrokerError("撤单要有委托编号,给的是空的。")

        self.session.navigate(em.REVOKE_URL)
        if em.looks_logged_out(self.session.current_url()):
            raise em.SessionExpired("打开撤单页时被弹回登录页,券商会话已失效。")

        try:
            result = self.session.execute_async(em.CANCEL_ORDER_JS, {"wtbh": want})
        except wd.WebDriverError as exc:
            raise BrokerError(
                f"撤单时浏览器出错:{exc}。**撤单请求可能已经发出去了**,"
                "去委托列表确认,不要重试。",
                submitted_unknown=True,
            ) from exc

        if not isinstance(result, Mapping):
            raise BrokerError("撤单脚本没返回对象。", submitted_unknown=True)
        if result.get("accepted"):
            logger.info("撤单已被接受:委托 %s", want)
            return

        detail = str(result.get("detail") or "").strip() or "东方财富拒绝了撤单,但没说原因。"
        if result.get("not_found"):
            raise BrokerError(detail)      # 话里已经写明"可能已成交或已撤销"
        raise BrokerError(detail)

    # -- 查询(读,可以重试) ------------------------------------------

    def account(self) -> em.AccountReport:
        """查账户和持仓。**这是读操作,重试是安全的。**"""
        self.session.navigate(em.POSITION_URL)
        result = self.session.execute_async(em.QUERY_ASSET_JS, em.ASSET_ENDPOINT)
        if not isinstance(result, Mapping):
            raise em.EastmoneyError("账户接口脚本没返回对象。")
        if not result.get("ok"):
            detail = str(result.get("detail") or "账户接口失败,但没说原因。")
            if em.looks_logged_out(str(result.get("current_url") or "")):
                raise em.SessionExpired(f"查账户时已在登录页:{detail}")
            raise em.EastmoneyError(detail)
        payload = result.get("response")
        if not isinstance(payload, Mapping):
            raise em.EastmoneyError("账户接口返回的不是对象。")
        return em.parse_asset_and_position(payload)

    def activity(self) -> dict[str, Any]:
        """查当日委托 / 成交 / 可撤。

        **三个接口各报各的成败,一个挂了不影响另外两个。** 一次报全部,
        不是撞上第一个错就停——只知道"委托查不到"而不知道"成交也查不到",
        人得跑两趟。
        """
        self.session.navigate(em.POSITION_URL)
        result = self.session.execute_async(em.QUERY_ACTIVITY_JS, em.ACTIVITY_ENDPOINTS)
        if not isinstance(result, Mapping):
            raise em.EastmoneyError("流水查询脚本没返回对象。")
        raw = result.get("result")
        if not isinstance(raw, Mapping):
            raise em.EastmoneyError("流水查询没返回结果集。")

        out: dict[str, Any] = {}
        for name in em.ACTIVITY_ENDPOINTS:
            slot = raw.get(name)
            if not isinstance(slot, Mapping) or not slot.get("ok"):
                detail = ""
                if isinstance(slot, Mapping):
                    detail = str(slot.get("detail") or "")
                # 取不到就明说取不到。**不填空列表**——空列表的意思是
                # "今天一笔都没有",那是完全不同的一件事。
                out[name] = {"取到了": False, "原因": detail or "接口没返回。"}
                continue
            payload = slot.get("response")
            rows: list[Any] = []
            if isinstance(payload, Mapping):
                status = em.status_of(payload)
                if status in em.STATUS_SESSION_EXPIRED:
                    raise em.SessionExpired(f"查{name}时券商会话已失效(Status={status})。")
                data = payload.get("Data")
                if isinstance(data, list):
                    rows = [r for r in data if isinstance(r, Mapping)]
            out[name] = {"取到了": True, "条数": len(rows), "明细": rows}
        return out


__all__ = ["BrokerError", "EastmoneyBroker", "order_ref"]
