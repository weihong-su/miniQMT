"""
测试 indicator_calculator.py 增量 MA/MACD 计算修复效果

验证场景:
  1. 增量更新1条新数据时，不再触发 "数据长度不足以计算MAxx/MACD" 警告
  2. 新行指标值（MA30 等）有效，不为 None
  3. 旧行指标记录不被重复写入（只写新行）
  4. 全量更新(force_update=True)仍然正常工作
  5. 数据库数据真实不足时（历史数据 < 30 条）警告依然正常触发
  6. 首次计算（stock_indicators 表为空）能正常完成
  7. 增量更新多条新行同样无警告
  8. 已是最新时直接返回 True 不写入
"""

import sys
import os
import sqlite3
import threading
import logging
import unittest
from datetime import datetime, timedelta

import pandas as pd

# ── 将项目根目录加入 sys.path ──────────────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402 — 确保 MA_PERIODS / MACD_* 常量可用
import indicator_calculator as ic_mod  # 只导入一次，不 reload


# ── 辅助函数 ───────────────────────────────────────────────────────────────────

def _make_trading_days(n: int, start=datetime(2025, 1, 2)):
    """生成 n 个工作日日期列表"""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _create_test_db(n_total: int, n_pre_indicator: int = 0):
    """
    创建内存 SQLite 测试数据库。

    参数:
        n_total:         stock_daily_data 总行数（日线数据）
        n_pre_indicator: 已提前写入 stock_indicators 的行数（0 表示首次计算）

    返回:
        (conn, trading_days, stock_code)
    """
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.execute("""
        CREATE TABLE stock_daily_data (
            stock_code TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER, amount REAL
        )
    """)
    conn.execute("""
        CREATE TABLE stock_indicators (
            stock_code TEXT, date TEXT,
            ma10 REAL, ma20 REAL, ma30 REAL, ma60 REAL,
            macd REAL, macd_signal REAL, macd_hist REAL
        )
    """)
    conn.commit()

    stock_code = 'TEST01.SZ'
    trading_days = _make_trading_days(n_total)

    # 写入日线数据（价格简单递增，足以通过 MyTT 计算）
    rows = []
    for i, dt in enumerate(trading_days):
        price = 10.0 + i * 0.1
        rows.append((
            stock_code, dt.strftime('%Y-%m-%d'),
            price, price * 1.02, price * 0.98, price,
            1_000_000, price * 1_000_000,
        ))
    conn.executemany("INSERT INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)", rows)

    # 写入已有指标行（模拟前 n_pre_indicator 天已计算完毕）
    if n_pre_indicator > 0:
        ind_rows = []
        for i in range(n_pre_indicator):
            dt = trading_days[i].strftime('%Y-%m-%d')
            ind_rows.append((stock_code, dt, 10.0, 9.9, 9.8, 9.6, 0.01, 0.005, 0.005))
        conn.executemany(
            "INSERT INTO stock_indicators VALUES (?,?,?,?,?,?,?,?,?)", ind_rows
        )

    conn.commit()
    return conn, trading_days, stock_code


def _make_mock_dm(conn):
    """构造模拟 DataManager，注入测试 SQLite 连接"""
    class _MockDM:
        def __init__(self, c):
            self.conn = c
            self._db_lock = threading.Lock()

        def get_history_data_from_db(self, stock_code, start_date=None, end_date=None):
            query = "SELECT * FROM stock_daily_data WHERE stock_code=?"
            params = [stock_code]
            if start_date:
                query += " AND date>=?"
                params.append(start_date)
            if end_date:
                query += " AND date<=?"
                params.append(end_date)
            query += " ORDER BY date"
            return pd.read_sql_query(query, self.conn, params=params)

    return _MockDM(conn)


def _make_calc(conn, mock_dm):
    """
    绕过 IndicatorCalculator.__init__（避免触发真实 QMT 连接），
    直接注入测试用的 data_manager 和 SQLite 连接。
    """
    calc = ic_mod.IndicatorCalculator.__new__(ic_mod.IndicatorCalculator)
    calc.data_manager = mock_dm
    calc.conn = conn
    return calc


class _WarnCapture(logging.Handler):
    """捕获指定 logger 输出的 WARNING 及以上日志消息"""
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            self.messages.append(record.getMessage())

    def ma_warnings(self):
        return [m for m in self.messages
                if '数据长度不足以计算MA' in m or '数据长度不足以计算MACD' in m]


# ── 测试类 ─────────────────────────────────────────────────────────────────────

class TestIndicatorIncrementalFix(unittest.TestCase):
    """验证增量计算修复：有足够历史数据时不产生 MA/MACD 警告"""

    # ─────────────────────────────────────────────────────────── 场景 1 ──────
    def test_1_no_warning_incremental_1_new_row(self):
        """
        场景 1（核心修复验证）:
          - 历史日线 65 条，已计算指标 64 条
          - 新增第 65 条日线后执行增量计算
          - 不应触发任何 MA/MACD 警告
          - 第 65 天的指标行应被写入，MA30 值有效（非 None）
        """
        conn, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=64)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        warn_handler = _WarnCapture()
        ic_logger = logging.getLogger('miniQMT.cal')
        ic_logger.addHandler(warn_handler)
        try:
            result = calc.calculate_all_indicators(stock_code)
        finally:
            ic_logger.removeHandler(warn_handler)

        self.assertTrue(result, "calculate_all_indicators 应返回 True")

        ma_warns = warn_handler.ma_warnings()
        self.assertEqual(len(ma_warns), 0,
                         f"不应出现 MA/MACD 不足警告，实际收到: {ma_warns}")

        # 第 65 天指标行存在且 MA30 有效
        day65 = trading_days[64].strftime('%Y-%m-%d')
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ma30 FROM stock_indicators WHERE stock_code=? AND date=?",
            (stock_code, day65)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row, f"第 65 天({day65})的指标记录应被写入")
        self.assertIsNotNone(row[0], "MA30 值不应为 None")

    # ─────────────────────────────────────────────────────────── 场景 2 ──────
    def test_2_only_new_row_written(self):
        """
        场景 2: 增量计算只写新行，旧行不重复写入
          - 已存在 64 条指标记录，新增 1 条后调用 calculate_all_indicators
          - stock_indicators 总行数应为 65（不是 65+64=129）
        """
        conn, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=64)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        calc.calculate_all_indicators(stock_code)

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stock_indicators WHERE stock_code=?", (stock_code,))
        total = cursor.fetchone()[0]
        self.assertEqual(total, 65, f"stock_indicators 应恰好有 65 行，实际 {total} 行")

    # ─────────────────────────────────────────────────────────── 场景 3 ──────
    def test_3_force_update_no_warning(self):
        """
        场景 3: force_update=True 时全量重新计算，不产生 MA 警告（65 条数据足够）
        """
        conn, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=0)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        warn_handler = _WarnCapture()
        ic_logger = logging.getLogger('miniQMT.cal')
        ic_logger.addHandler(warn_handler)
        try:
            result = calc.calculate_all_indicators(stock_code, force_update=True)
        finally:
            ic_logger.removeHandler(warn_handler)

        self.assertTrue(result)
        ma_warns = warn_handler.ma_warnings()
        self.assertEqual(len(ma_warns), 0,
                         f"force_update 时不应出现 MA 警告，实际: {ma_warns}")

        # 所有 65 行均已写入
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stock_indicators WHERE stock_code=?", (stock_code,))
        self.assertEqual(cursor.fetchone()[0], 65)

    # ─────────────────────────────────────────────────────────── 场景 4 ──────
    def test_4_warning_fires_when_truly_insufficient(self):
        """
        场景 4: 数据库历史数据真实不足（只有 5 条）时，警告依然正常触发
        验证 _calculate_ma 的保护逻辑没有被意外移除
        """
        conn, trading_days, stock_code = _create_test_db(n_total=5, n_pre_indicator=0)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        warn_handler = _WarnCapture()
        ic_logger = logging.getLogger('miniQMT.cal')
        ic_logger.addHandler(warn_handler)
        try:
            calc.calculate_all_indicators(stock_code, force_update=True)
        finally:
            ic_logger.removeHandler(warn_handler)

        # 5 条数据：MA10(需10)/MA20(需20)/MA30(需30)/MA60(需60) 均不足，应有警告
        ma_warns = warn_handler.ma_warnings()
        self.assertGreater(len(ma_warns), 0,
                           "历史数据真实不足时应仍有 MA 数据不足警告")

    # ─────────────────────────────────────────────────────────── 场景 5 ──────
    def test_5_first_time_no_warning(self):
        """
        场景 5: 首次计算（stock_indicators 表为空），65 条日线数据
          - 应成功完成，无 MA 警告，MA30 有效
        """
        conn, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=0)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        warn_handler = _WarnCapture()
        ic_logger = logging.getLogger('miniQMT.cal')
        ic_logger.addHandler(warn_handler)
        try:
            result = calc.calculate_all_indicators(stock_code)
        finally:
            ic_logger.removeHandler(warn_handler)

        self.assertTrue(result)
        ma_warns = warn_handler.ma_warnings()
        self.assertEqual(len(ma_warns), 0, f"首次计算不应有 MA 警告: {ma_warns}")

        # 验证最后一天 MA30 有效
        last_day = trading_days[-1].strftime('%Y-%m-%d')
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ma30 FROM stock_indicators WHERE stock_code=? AND date=?",
            (stock_code, last_day)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0], "首次计算最后一天 MA30 不应为 None")

    # ─────────────────────────────────────────────────────────── 场景 6 ──────
    def test_6_incremental_multiple_new_rows(self):
        """
        场景 6: 增量更新多条新行（节假日后补充 3 天数据）
          - 历史 65 条，已计算 62 条，新增 3 条
          - 不应有 MA 警告，写入后总行数为 65
        """
        conn, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=62)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        warn_handler = _WarnCapture()
        ic_logger = logging.getLogger('miniQMT.cal')
        ic_logger.addHandler(warn_handler)
        try:
            result = calc.calculate_all_indicators(stock_code)
        finally:
            ic_logger.removeHandler(warn_handler)

        self.assertTrue(result)
        ma_warns = warn_handler.ma_warnings()
        self.assertEqual(len(ma_warns), 0, f"多行增量更新不应有 MA 警告: {ma_warns}")

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stock_indicators WHERE stock_code=?", (stock_code,))
        total = cursor.fetchone()[0]
        self.assertEqual(total, 65, f"应写入 65 行指标，实际 {total} 行")

    # ─────────────────────────────────────────────────────────── 场景 7 ──────
    def test_7_already_up_to_date_returns_true(self):
        """
        场景 7: 指标已是最新（stock_indicators 最新日期 == stock_daily_data 最新日期）
          - 应直接返回 True，不写入新行，总行数不变
        """
        conn, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=65)
        mock_dm = _make_mock_dm(conn)
        calc = _make_calc(conn, mock_dm)

        result = calc.calculate_all_indicators(stock_code)

        self.assertTrue(result)
        # 行数仍为 65，不重复写入
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stock_indicators WHERE stock_code=?", (stock_code,))
        self.assertEqual(cursor.fetchone()[0], 65)

    # ─────────────────────────────────────────────────────────── 场景 8 ──────
    def test_8_ma30_value_correctness(self):
        """
        场景 8: 验证增量计算的 MA30 数值正确性
          - 第 65 天的 MA30 应等于第 36~65 天的均价（用于回归验证）
          - 与全量计算结果一致
        """
        conn_inc, trading_days, stock_code = _create_test_db(n_total=65, n_pre_indicator=64)
        mock_dm_inc = _make_mock_dm(conn_inc)
        calc_inc = _make_calc(conn_inc, mock_dm_inc)
        calc_inc.calculate_all_indicators(stock_code)  # 增量

        conn_full, _, _ = _create_test_db(n_total=65, n_pre_indicator=0)
        mock_dm_full = _make_mock_dm(conn_full)
        calc_full = _make_calc(conn_full, mock_dm_full)
        calc_full.calculate_all_indicators(stock_code, force_update=True)  # 全量

        last_day = trading_days[-1].strftime('%Y-%m-%d')

        def _get_ma30(c, sc, dt):
            cursor = c.cursor()
            cursor.execute(
                "SELECT ma30 FROM stock_indicators WHERE stock_code=? AND date=?",
                (sc, dt)
            )
            row = cursor.fetchone()
            return row[0] if row else None

        ma30_inc = _get_ma30(conn_inc, stock_code, last_day)
        ma30_full = _get_ma30(conn_full, stock_code, last_day)

        self.assertIsNotNone(ma30_inc, "增量计算 MA30 不应为 None")
        self.assertIsNotNone(ma30_full, "全量计算 MA30 不应为 None")
        self.assertAlmostEqual(ma30_inc, ma30_full, places=6,
                               msg=f"增量MA30={ma30_inc} 应等于全量MA30={ma30_full}")


# ── 入口 ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s'
    )
    unittest.main(verbosity=2)
