"""
miniqmt_autobuy 独立进程入口与调度。

由 miniqmt.bat 菜单 [j] 启动。调度循环每 30s tick，支持两种触发模式:
  daily    — 命中 cfg.daily_times 的时刻 (当日去重)
  interval — 距上次触发 >= cfg.interval_minutes (仅交易时段)
  both     — 两者都启用

单轮流程: 拉候选池 → 大盘指数门禁 → 防重过滤 → 洗牌后惰性条件检查(记决策日志) → HTTP下单(复用web买入API) → 记买入历史。
下单后止盈止损由主程序 position_manager 自动接管。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import threading
from datetime import datetime

import config
from .config import DEFAULT_CFG_PATH, PROJECT_ROOT, get_autobuy_logger, load_config
from .pool import normalize_code, read_candidates
from .store import AutoBuyStore
from .client import WebClient
from .filter import BuyConditionFilter, MarketIndexFilter

logger = get_autobuy_logger("autobuy")

STATUS_FILE = os.path.join(PROJECT_ROOT, "data", ".autobuy_status.json")
TICK_SECONDS = 30


class AutoBuyApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.store = AutoBuyStore()
        self.client = WebClient(cfg)

        # data_manager 用于取行情/历史/标的明细 (独立进程内自取，下单才走 HTTP)
        from data_manager import get_data_manager
        self.dm = get_data_manager()
        self.filter = BuyConditionFilter(cfg, self.dm)
        self.market_filter = MarketIndexFilter(self.dm)

        # 调度状态
        self._fired_daily = set()        # 当日已触发的 (h, m)
        self._fired_daily_date = None
        self._last_interval_run = datetime.now()  # 启动后等一个间隔再触发 interval

    # ------------------------------------------------------------------
    # 单轮执行
    # ------------------------------------------------------------------
    def run_once(self, trigger: str) -> None:
        run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"===== 触发自动买入 [{trigger}] {run_time} =====")

        codes = read_candidates(self.cfg)
        status = {
            "last_run": run_time, "trigger": trigger,
            "candidates": len(codes), "checked": 0, "passed": 0, "bought": [],
        }
        if not codes:
            logger.info("候选池为空，结束本轮")
            self._write_status(status)
            return

        market_ok, market_reason = self.market_filter.check()
        status["market_filter"] = market_reason
        if not market_ok:
            logger.info(f"大盘指数门禁未通过，本轮不自动买入: {market_reason}")
            self._write_status(status)
            return
        logger.info(f"大盘指数门禁通过: {market_reason}")

        # 防重过滤前置: 先剔除已持仓/窗口内已买，避免对不可买标的做昂贵的条件检查
        eligible = self._dedup_filter(codes)
        if not eligible:
            logger.info("候选池经防重过滤后无可买标的，结束本轮")
            self._write_status(status)
            return

        # 洗牌 + 惰性条件检查: 收集到 max_buys_per_run 只通过即停。
        # 对均匀洗牌后的列表取"前 k 个通过项"，等价于在全部通过标的中均匀随机选 k 只，
        # 且无需检查整个候选池(可能数百只)。
        random.shuffle(eligible)
        need = self.cfg.max_buys_per_run
        chosen = []
        checked = 0
        for code in eligible:
            if len(chosen) >= need:
                break
            checked += 1
            try:
                ok, reason = self.filter.check(code)
            except Exception as e:
                ok, reason = False, {"code": code, "failed": [f"检查异常: {e}"]}
                logger.error(f"{code} 条件检查异常: {e}")
            self.store.record_decision(run_time, code, ok, reason)
            if ok:
                chosen.append(code)
                logger.info(f"  ✓ {code} 通过条件检查")
            else:
                logger.debug(f"  ✗ {code} 未通过: {reason.get('failed')}")
        status["checked"] = checked
        status["passed"] = len(chosen)
        logger.info(
            f"惰性检查: 合格标的 {len(eligible)} 只，检查 {checked} 只，"
            f"命中 {len(chosen)}/{need} 只: {chosen}"
        )
        if not chosen:
            logger.info("检查完毕无标的通过条件，结束本轮")
            self._write_status(status)
            return

        # 下单 (复用 web 买入 API)
        for code in chosen:
            success, http_status, result = self.client.buy(code)
            self.store.record_buy(code, trigger, success, http_status, result, amount=None)
            if success:
                status["bought"].append(code)
                logger.info(f"  下单成功: {code} (后续止盈止损交由主程序)")
            else:
                logger.warning(f"  下单失败: {code} -> {result}")

        self._write_status(status)

    def _dedup_filter(self, codes: list) -> list:
        """过滤掉已持仓 / 防重窗口内已买过的股票。"""
        cfg = self.cfg
        held = None
        if cfg.dedup_by_position:
            held = self.client.get_held_codes()
            if held is None:
                # 持仓查询失败：安全优先，跳过本轮买入，避免重复买入
                logger.warning("持仓查询失败，为避免重复买入，本轮不下单")
                return []
        recent = self.store.recently_bought_codes(cfg.dedup_window_days)

        eligible = []
        for code in codes:
            key = normalize_code(code)
            if held is not None and key in held:
                logger.info(f"  防重跳过 {code}: 已持仓")
                continue
            if key in recent:
                logger.info(f"  防重跳过 {code}: {cfg.dedup_window_days}日内已买过")
                continue
            eligible.append(code)
        return eligible

    # ------------------------------------------------------------------
    # 调度循环
    # ------------------------------------------------------------------
    def _should_skip_non_trade(self) -> bool:
        if self.cfg.only_trade_time and not config.is_trade_time():
            return True
        return False

    def _tick(self) -> None:
        now = datetime.now()
        mode = self.cfg.mode

        # 重置当日 daily 去重
        if self._fired_daily_date != now.date():
            self._fired_daily.clear()
            self._fired_daily_date = now.date()

        # daily 触发
        if mode in ("daily", "both"):
            for (h, m) in self.cfg.daily_times:
                if now.hour == h and now.minute == m and (h, m) not in self._fired_daily:
                    self._fired_daily.add((h, m))
                    if self._should_skip_non_trade():
                        logger.info(f"daily {h:02d}:{m:02d} 命中但非交易时段，跳过")
                    else:
                        self._safe_run(f"daily-{h:02d}:{m:02d}")

        # interval 触发
        if mode in ("interval", "both"):
            elapsed = (now - self._last_interval_run).total_seconds()
            if elapsed >= self.cfg.interval_minutes * 60:
                self._last_interval_run = now
                if self._should_skip_non_trade():
                    logger.debug("interval 命中但非交易时段，跳过")
                else:
                    self._safe_run(f"interval-{self.cfg.interval_minutes}m")

    def _safe_run(self, trigger: str) -> None:
        try:
            self.run_once(trigger)
        except Exception as e:
            logger.error(f"本轮执行异常 [{trigger}]: {e}", exc_info=True)

    def run_loop(self) -> None:
        logger.info(
            f"自动买入调度启动: mode={self.cfg.mode} "
            f"daily={self.cfg.daily_times} interval={self.cfg.interval_minutes}min "
            f"only_trade_time={self.cfg.only_trade_time}"
        )
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"调度 tick 异常: {e}", exc_info=True)
            self.stop_event.wait(TICK_SECONDS)
        logger.info("调度循环已退出")

    def shutdown(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------
    def _write_status(self, status: dict) -> None:
        status["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATUS_FILE)
        except OSError as e:
            logger.debug(f"写状态文件失败: {e}")

    def close(self) -> None:
        self.store.close()
        try:
            if os.path.exists(STATUS_FILE):
                os.remove(STATUS_FILE)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="miniQMT 自动买入服务")
    parser.add_argument("--config", default=DEFAULT_CFG_PATH, help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="立即执行一轮后退出(用于测试/手动触发)")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"加载配置失败: {e}")
        return 1

    app = AutoBuyApp(cfg)

    def _handle_signal(signum, _frame):
        logger.info(f"收到信号 {signum}，准备退出...")
        app.shutdown()

    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    try:
        if args.once:
            app.run_once("manual-once")
        else:
            app.run_loop()
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，退出")
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
