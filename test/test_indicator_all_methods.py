"""
IndicatorCalculator 全方法覆盖测试
覆盖所有 9 个公开/私有方法，揭示并验证已知 bug。

方法列表:
  1. calculate_all_indicators   - 核心入口（增量/全量/首次/已最新）
  2. _calculate_ma              - MA 计算（足够/不足/边界）
  3. _calculate_macd            - MACD 计算（足够/不足/异常）
  4. _save_indicators           - 写入数据库（去重/回滚）
  5. get_latest_indicators      - 查最新指标行
  6. get_indicators_history     - 查历史指标行
  7. check_buy_signal           - 买入信号（金叉+多头）
  8. check_sell_signal          - 卖出信号（死叉+空头）
  9. update_all_stock_indicators- 批量更新所有股票

已知 Bug（本测试会先重现，再验证修复）:
  BUG-01: force_update=True 重复调用 → 重复插入行
"""

import sys
import os
import sqlite3
import threading
import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import numpy as np
import pandas as pd

# ── 将项目根目录加入 sys.path ──────────────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
import indicator_calculator as ic_mod


# ══════════════════════════════════════════════════════════════════════════════
# 共用测试基础设施
# ══════════════════════════════════════════════════════════════════════════════

def _make_trading_days(n: int, start=datetime(2025, 1, 2)):
    """生成 n 个工作日日期（跳过周六、周日）"""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _create_db(n_total: int, n_pre_indicator: int = 0,
               stock_code: str = 'TEST01.SZ',
               price_start: float = 10.0,
               price_step: float = 0.1):
    """
    创建内存 SQLite 测试数据库。
    返回: (conn, trading_days, stock_code)
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

    trading_days = _make_trading_days(n_total)
    rows = []
    for i, dt in enumerate(trading_days):
        price = price_start + i * price_step
        rows.append((
            stock_code, dt.strftime('%Y-%m-%d'),
            price, price * 1.02, price * 0.98, price,
            1_000_000, price * 1_000_000,
        ))
    conn.executemany("INSERT INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)", rows)

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


def _make_calc(conn):
    """绕过 __init__，直接注入依赖"""
    mock_dm = _make_mock_dm(conn)
    calc = ic_mod.IndicatorCalculator.__new__(ic_mod.IndicatorCalculator)
    calc.data_manager = mock_dm
    calc.conn = conn
    return calc


def _row_count(conn, stock_code, table='stock_indicators'):
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE stock_code=?", (stock_code,))
    return cursor.fetchone()[0]


def _get_value(conn, stock_code, date_str, col, table='stock_indicators'):
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {col} FROM {table} WHERE stock_code=? AND date=?",
        (stock_code, date_str)
    )
    row = cursor.fetchone()
    return row[0] if row else None


# ══════════════════════════════════════════════════════════════════════════════
# 1. calculate_all_indicators
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateAllIndicators(unittest.TestCase):
    """覆盖 calculate_all_indicators 的各种路径"""

    # ── 1.1 首次计算：空 stock_indicators 表 ────────────────────────────────
    def test_1_1_first_time_calculation(self):
        """首次计算，65条数据，返回True，全部写入"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)

        result = calc.calculate_all_indicators(sc)

        self.assertTrue(result)
        self.assertEqual(_row_count(conn, sc), 65)

    # ── 1.2 增量更新：+1 条新行 ────────────────────────────────────────────
    def test_1_2_incremental_one_new_row(self):
        """已有64条指标，新增1条，应写1行，总计65行"""
        conn, days, sc = _create_db(65, 64)
        calc = _make_calc(conn)

        result = calc.calculate_all_indicators(sc)

        self.assertTrue(result)
        self.assertEqual(_row_count(conn, sc), 65)

    # ── 1.3 增量更新：+多条新行 ───────────────────────────────────────────
    def test_1_3_incremental_multiple_new_rows(self):
        """已有60条指标，新增5条，总计65行"""
        conn, days, sc = _create_db(65, 60)
        calc = _make_calc(conn)

        result = calc.calculate_all_indicators(sc)

        self.assertTrue(result)
        self.assertEqual(_row_count(conn, sc), 65)

    # ── 1.4 已是最新：直接返回True，行数不变 ─────────────────────────────
    def test_1_4_already_up_to_date(self):
        """指标已是最新，返回True，不写入新行"""
        conn, days, sc = _create_db(65, 65)
        calc = _make_calc(conn)

        result = calc.calculate_all_indicators(sc)

        self.assertTrue(result)
        self.assertEqual(_row_count(conn, sc), 65)  # 不变

    # ── 1.5 force_update=True 全量重算 ───────────────────────────────────
    def test_1_5_force_update_full_recalc(self):
        """force_update=True，写入全部65行"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)

        result = calc.calculate_all_indicators(sc, force_update=True)

        self.assertTrue(result)
        self.assertEqual(_row_count(conn, sc), 65)

    # ── 1.6 无历史数据时返回False ─────────────────────────────────────────
    def test_1_6_no_history_data_returns_false(self):
        """stock_daily_data 为空，返回 False"""
        conn, days, sc = _create_db(0, 0)
        calc = _make_calc(conn)

        result = calc.calculate_all_indicators(sc)

        self.assertFalse(result)

    def test_1_6_1_no_history_warning_is_throttled(self):
        """无历史数据时只首次 warning，节流期内后续降级为 debug"""
        conn, days, sc = _create_db(0, 0)
        calc = _make_calc(conn)

        with patch("config.INDICATOR_EMPTY_DATA_LOG_INTERVAL_SECONDS", 300), \
             self.assertLogs("miniQMT.ic", level="DEBUG") as cm:
            self.assertFalse(calc.calculate_all_indicators(sc))
            self.assertFalse(calc.calculate_all_indicators(sc))

        warning_logs = [m for m in cm.output if "WARNING" in m and f"没有 {sc} 的历史数据" in m]
        debug_logs = [m for m in cm.output if "DEBUG" in m and "重复告警已降噪" in m]
        self.assertEqual(len(warning_logs), 1)
        self.assertEqual(len(debug_logs), 1)

    def test_1_6_2_empty_signal_warning_is_throttled(self):
        """无指标数据时买卖信号告警分别限频"""
        conn, days, sc = _create_db(0, 0)
        calc = _make_calc(conn)

        with patch("config.INDICATOR_EMPTY_DATA_LOG_INTERVAL_SECONDS", 300), \
             self.assertLogs("miniQMT.ic", level="DEBUG") as cm:
            self.assertFalse(calc.check_buy_signal(sc))
            self.assertFalse(calc.check_buy_signal(sc))
            self.assertFalse(calc.check_sell_signal(sc))
            self.assertFalse(calc.check_sell_signal(sc))

        buy_warnings = [m for m in cm.output if "WARNING" in m and "检查买入信号" in m]
        sell_warnings = [m for m in cm.output if "WARNING" in m and "检查卖出信号" in m]
        debug_logs = [m for m in cm.output if "DEBUG" in m and "重复告警已降噪" in m]
        self.assertEqual(len(buy_warnings), 1)
        self.assertEqual(len(sell_warnings), 1)
        self.assertEqual(len(debug_logs), 2)

    # ── 1.7 BUG-01 重现：force_update 重复调用产生重复行 ─────────────────
    def test_1_7_bug01_force_update_duplicate_rows(self):
        """
        BUG-01: force_update=True 调用两次时，由于 _save_indicators 使用
        if_exists='append' 且无 UNIQUE 约束，导致重复插入。
        此测试先重现 bug（期望失败表示 bug 尚未修复）。
        修复后，第二次调用不应产生重复行（总行数仍为65）。
        """
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)

        calc.calculate_all_indicators(sc, force_update=True)  # 第1次: 65行
        calc.calculate_all_indicators(sc, force_update=True)  # 第2次

        total = _row_count(conn, sc)
        # 修复后应为 65，如果仍为 130 说明 bug 未修复
        self.assertEqual(total, 65,
                         f"BUG-01: force_update 重复调用导致重复插入，"
                         f"期望65行，实际{total}行")

    # ── 1.8 增量更新的 MA30 值与全量一致 ─────────────────────────────────
    def test_1_8_incremental_ma30_equals_full(self):
        """增量计算的最后一天 MA30 应与全量计算一致（EWM 路径依赖）"""
        # 增量：已有64条，计算第65天
        conn_inc, days, sc = _create_db(65, 64)
        calc_inc = _make_calc(conn_inc)
        calc_inc.calculate_all_indicators(sc)

        # 全量
        conn_full, _, _ = _create_db(65, 0)
        calc_full = _make_calc(conn_full)
        calc_full.calculate_all_indicators(sc, force_update=True)

        last = days[-1].strftime('%Y-%m-%d')
        ma30_inc = _get_value(conn_inc, sc, last, 'ma30')
        ma30_full = _get_value(conn_full, sc, last, 'ma30')

        self.assertIsNotNone(ma30_inc)
        self.assertIsNotNone(ma30_full)
        self.assertAlmostEqual(ma30_inc, ma30_full, places=6)


# ══════════════════════════════════════════════════════════════════════════════
# 2. _calculate_ma
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateMa(unittest.TestCase):
    """覆盖 _calculate_ma 的各种输入场景"""

    def _make_calc_with_df(self, n):
        """创建带有 n 行价格数据的 df"""
        conn, days, sc = _create_db(n, 0)
        calc = _make_calc(conn)
        df = pd.read_sql_query(
            "SELECT * FROM stock_daily_data WHERE stock_code=? ORDER BY date",
            conn, params=(sc,)
        )
        return calc, df

    def test_2_1_sufficient_data(self):
        """足够数据时，MA30 返回有效 Series，长度正确"""
        calc, df = self._make_calc_with_df(65)
        result = calc._calculate_ma(df, 30)

        self.assertEqual(len(result), 65)
        # 最后一个值应为有效数值
        last_val = result.iloc[-1]
        self.assertIsNotNone(last_val)
        self.assertFalse(np.isnan(float(last_val)))

    def test_2_2_insufficient_data_returns_none_series(self):
        """数据不足时，返回全 None Series（触发警告）"""
        calc, df = self._make_calc_with_df(5)  # 5 < 10
        result = calc._calculate_ma(df, 10)

        self.assertEqual(len(result), 5)
        # 全为 None
        self.assertTrue(all(v is None for v in result))

    def test_2_3_exact_boundary(self):
        """恰好等于 period 条数据，应能计算（最后一个值有效）"""
        calc, df = self._make_calc_with_df(10)
        result = calc._calculate_ma(df, 10)

        self.assertEqual(len(result), 10)
        last_val = result.iloc[-1]
        self.assertIsNotNone(last_val)

    def test_2_4_period_one(self):
        """period=1，每行就是当行收盘价"""
        calc, df = self._make_calc_with_df(10)
        result = calc._calculate_ma(df, 1)
        closes = df['close'].values.tolist()

        for i, (got, expected) in enumerate(zip(result, closes)):
            self.assertAlmostEqual(float(got), expected, places=5,
                                   msg=f"第{i}行 MA1 期望{expected}，实际{got}")

    def test_2_5_all_periods_in_config(self):
        """config.MA_PERIODS 中所有周期均可计算（100条数据）"""
        calc, df = self._make_calc_with_df(100)
        for period in config.MA_PERIODS:
            with self.subTest(period=period):
                result = calc._calculate_ma(df, period)
                self.assertEqual(len(result), 100)
                self.assertIsNotNone(result.iloc[-1])


# ══════════════════════════════════════════════════════════════════════════════
# 3. _calculate_macd
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateMacd(unittest.TestCase):
    """覆盖 _calculate_macd 的各种输入场景"""

    def _make_calc_with_df(self, n):
        conn, days, sc = _create_db(n, 0)
        calc = _make_calc(conn)
        df = pd.read_sql_query(
            "SELECT * FROM stock_daily_data WHERE stock_code=? ORDER BY date",
            conn, params=(sc,)
        )
        return calc, df

    def test_3_1_sufficient_data(self):
        """足够数据时，返回三列均有效"""
        calc, df = self._make_calc_with_df(65)
        result = calc._calculate_macd(df)

        self.assertIn('macd', result.columns)
        self.assertIn('macd_signal', result.columns)
        self.assertIn('macd_hist', result.columns)
        self.assertEqual(len(result), 65)
        # 最后一行不为 None
        last = result.iloc[-1]
        for col in ['macd', 'macd_signal', 'macd_hist']:
            self.assertIsNotNone(last[col], f"{col} 不应为 None")

    def test_3_2_insufficient_data_returns_none_df(self):
        """数据不足时，三列全为 None"""
        min_p = max(config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL) + 10
        calc, df = self._make_calc_with_df(min_p - 5)  # 不足

        result = calc._calculate_macd(df)

        self.assertIn('macd', result.columns)
        for col in ['macd', 'macd_signal', 'macd_hist']:
            self.assertTrue(all(v is None for v in result[col]),
                             f"{col} 应全为 None")

    def test_3_3_macd_columns_mapping(self):
        """
        验证列名映射正确:
          macd        = DIF (EMA12 - EMA26)
          macd_signal = DEA (EMA(DIF,9))
          macd_hist   = (DIF - DEA) * 2
        """
        calc, df = self._make_calc_with_df(100)
        result = calc._calculate_macd(df)

        close = df['close'].values.astype(float)

        # 参考计算
        dif_raw = (pd.Series(close).ewm(span=config.MACD_FAST, adjust=False).mean()
                   - pd.Series(close).ewm(span=config.MACD_SLOW, adjust=False).mean()).values
        dea_raw = pd.Series(dif_raw).ewm(span=config.MACD_SIGNAL, adjust=False).mean().values
        expected_dif = np.round(dif_raw, 3)
        expected_dea = np.round(dea_raw, 3)
        expected_hist = np.round((dif_raw - dea_raw) * 2, 3)

        for i in range(-5, 0):
            self.assertAlmostEqual(result['macd'].iloc[i], expected_dif[i], places=5,
                                   msg=f"第{i}行 macd 不一致")
            self.assertAlmostEqual(result['macd_signal'].iloc[i], expected_dea[i], places=5,
                                   msg=f"第{i}行 macd_signal 不一致")
            self.assertAlmostEqual(result['macd_hist'].iloc[i], expected_hist[i], places=5,
                                   msg=f"第{i}行 macd_hist 不一致")

    def test_3_4_macd_length_matches_input(self):
        """MACD 结果长度与输入 df 长度一致"""
        for n in [40, 65, 100, 200]:
            with self.subTest(n=n):
                calc, df = self._make_calc_with_df(n)
                result = calc._calculate_macd(df)
                self.assertEqual(len(result), n)


# ══════════════════════════════════════════════════════════════════════════════
# 4. _save_indicators
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveIndicators(unittest.TestCase):
    """覆盖 _save_indicators 的写入/去重/回滚场景"""

    def _make_indicator_df(self, sc, days, n):
        rows = []
        for i in range(n):
            dt = days[i].strftime('%Y-%m-%d')
            rows.append({
                'stock_code': sc,
                'date': dt,
                'ma10': 10.0 + i,
                'ma20': 9.9 + i,
                'ma30': 9.8 + i,
                'ma60': 9.6 + i,
                'macd': 0.01,
                'macd_signal': 0.005,
                'macd_hist': 0.005,
            })
        return pd.DataFrame(rows)

    def test_4_1_normal_write(self):
        """正常写入 10 行"""
        conn, days, sc = _create_db(10, 0)
        calc = _make_calc(conn)

        df = self._make_indicator_df(sc, days, 10)
        calc._save_indicators(df)

        self.assertEqual(_row_count(conn, sc), 10)

    def test_4_2_nan_values_handled(self):
        """含 NaN 的 DataFrame，写入后读回应为 None，不报错"""
        conn, days, sc = _create_db(5, 0)
        calc = _make_calc(conn)

        df = self._make_indicator_df(sc, days, 5)
        df.loc[2, 'ma30'] = np.nan

        calc._save_indicators(df)

        val = _get_value(conn, sc, days[2].strftime('%Y-%m-%d'), 'ma30')
        self.assertIsNone(val)

    def test_4_3_write_does_not_overwrite_existing(self):
        """
        _save_indicators 使用 append 模式，再次写同日期会产生重复行。
        这里验证行为（bug 重现），供修复参考。
        """
        conn, days, sc = _create_db(5, 0)
        calc = _make_calc(conn)

        df = self._make_indicator_df(sc, days, 5)
        calc._save_indicators(df)
        calc._save_indicators(df)  # 再写一次

        # 修复前：10行（重复）；修复后：5行（去重）
        total = _row_count(conn, sc)
        # 修复后此断言应通过
        self.assertEqual(total, 5,
                         f"_save_indicators 应幂等，期望5行，实际{total}行")


# ══════════════════════════════════════════════════════════════════════════════
# 5. get_latest_indicators
# ══════════════════════════════════════════════════════════════════════════════

class TestGetLatestIndicators(unittest.TestCase):

    def test_5_1_returns_latest_row(self):
        """返回最新日期的指标行"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        result = calc.get_latest_indicators(sc)

        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['stock_code'], sc)
        self.assertEqual(result['date'], days[-1].strftime('%Y-%m-%d'))

    def test_5_2_no_data_returns_none(self):
        """无任何指标行时返回 None"""
        conn, days, sc = _create_db(10, 0)
        calc = _make_calc(conn)

        result = calc.get_latest_indicators(sc)

        self.assertIsNone(result)

    def test_5_3_contains_all_indicator_columns(self):
        """返回字典包含所有指标列"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        result = calc.get_latest_indicators(sc)

        expected_cols = {'stock_code', 'date', 'ma10', 'ma20', 'ma30', 'ma60',
                         'macd', 'macd_signal', 'macd_hist'}
        self.assertTrue(expected_cols.issubset(set(result.keys())),
                        f"缺少列: {expected_cols - set(result.keys())}")

    def test_5_4_ma30_is_valid_not_none(self):
        """65条数据后 MA30 不为 None"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        result = calc.get_latest_indicators(sc)

        self.assertIsNotNone(result['ma30'])


# ══════════════════════════════════════════════════════════════════════════════
# 6. get_indicators_history
# ══════════════════════════════════════════════════════════════════════════════

class TestGetIndicatorsHistory(unittest.TestCase):

    def test_6_1_returns_correct_number_of_rows(self):
        """请求60天，返回最多60行（且已按日期升序）"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        result = calc.get_indicators_history(sc, days=60)

        self.assertLessEqual(len(result), 60)
        self.assertGreater(len(result), 0)

    def test_6_2_sorted_ascending_by_date(self):
        """返回结果按日期升序排列"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        result = calc.get_indicators_history(sc, days=30)
        dates = result['date'].tolist()

        self.assertEqual(dates, sorted(dates))

    def test_6_3_no_data_returns_empty_df(self):
        """无指标数据时返回空 DataFrame"""
        conn, days, sc = _create_db(10, 0)
        calc = _make_calc(conn)

        result = calc.get_indicators_history(sc)

        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_6_4_days_1_returns_one_row(self):
        """days=1 仅返回最新1行"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        result = calc.get_indicators_history(sc, days=1)

        self.assertEqual(len(result), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 7. check_buy_signal
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckBuySignal(unittest.TestCase):
    """
    买入信号条件: MACD 金叉(prev_hist<0 且 curr_hist>0) + 均线多头(MA10>MA20>MA30>MA60)
    这里直接注入 stock_indicators 行来精确控制信号。
    """

    def _make_calc_with_indicators(self, indicator_rows):
        """
        创建带有预设指标行的计算器。
        indicator_rows: list of dicts，每个包含 date, ma10/20/30/60, macd/signal/hist
        """
        sc = 'TEST01.SZ'
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
        for row in indicator_rows:
            conn.execute(
                "INSERT INTO stock_indicators VALUES (?,?,?,?,?,?,?,?,?)",
                (sc, row['date'],
                 row['ma10'], row['ma20'], row['ma30'], row['ma60'],
                 row['macd'], row['macd_signal'], row['macd_hist'])
            )
        conn.commit()

        calc = _make_calc(conn)
        return calc, sc

    def _make_row(self, date, ma10, ma20, ma30, ma60, hist, macd=0.1, signal=0.05):
        return {
            'date': date,
            'ma10': ma10, 'ma20': ma20, 'ma30': ma30, 'ma60': ma60,
            'macd': macd, 'macd_signal': signal, 'macd_hist': hist
        }

    def test_7_1_buy_signal_true(self):
        """MACD 金叉 + 均线多头 → True"""
        rows = [
            self._make_row('2025-01-02', 12, 11, 10, 9, -0.1),  # prev: hist<0
            self._make_row('2025-01-03', 12, 11, 10, 9, +0.1),  # curr: hist>0 + MA多头
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertTrue(calc.check_buy_signal(sc))

    def test_7_2_no_golden_cross(self):
        """均线多头但无金叉 → False"""
        rows = [
            self._make_row('2025-01-02', 12, 11, 10, 9, 0.1),  # prev: hist>0
            self._make_row('2025-01-03', 12, 11, 10, 9, 0.2),  # curr: hist>0（无金叉）
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_buy_signal(sc))

    def test_7_3_no_ma_alignment(self):
        """MACD 金叉但无多头排列 → False"""
        rows = [
            self._make_row('2025-01-02', 9, 11, 10, 12, -0.1),  # prev
            self._make_row('2025-01-03', 9, 11, 10, 12, +0.1),  # curr: MA10<MA20 不满足
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_buy_signal(sc))

    def test_7_4_none_macd_hist_returns_false(self):
        """macd_hist 为 None → False（不崩溃）"""
        rows = [
            self._make_row('2025-01-02', 12, 11, 10, 9, -0.1),
            {'date': '2025-01-03', 'ma10': 12, 'ma20': 11, 'ma30': 10, 'ma60': 9,
             'macd': None, 'macd_signal': None, 'macd_hist': None},
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_buy_signal(sc))

    def test_7_5_none_ma_values_returns_false(self):
        """MA 值为 None → False（不崩溃）"""
        rows = [
            self._make_row('2025-01-02', 12, 11, 10, 9, -0.1),
            {'date': '2025-01-03', 'ma10': None, 'ma20': 11, 'ma30': 10, 'ma60': 9,
             'macd': 0.1, 'macd_signal': 0.05, 'macd_hist': 0.05},
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_buy_signal(sc))

    def test_7_6_insufficient_rows_returns_false(self):
        """只有1行指标，无法判断金叉 → False"""
        rows = [
            self._make_row('2025-01-02', 12, 11, 10, 9, 0.1),
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_buy_signal(sc))

    def test_7_7_no_indicators_returns_false(self):
        """无指标数据 → False（不崩溃）"""
        conn, days, sc = _create_db(5, 0)
        calc = _make_calc(conn)
        self.assertFalse(calc.check_buy_signal(sc))


# ══════════════════════════════════════════════════════════════════════════════
# 8. check_sell_signal
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckSellSignal(unittest.TestCase):
    """
    卖出信号条件: MACD 死叉(prev_hist>0 且 curr_hist<0) + 均线空头(MA10<MA20<MA30<MA60)
    """

    def _make_calc_with_indicators(self, indicator_rows):
        sc = 'TEST01.SZ'
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
        for row in indicator_rows:
            conn.execute(
                "INSERT INTO stock_indicators VALUES (?,?,?,?,?,?,?,?,?)",
                (sc, row['date'],
                 row['ma10'], row['ma20'], row['ma30'], row['ma60'],
                 row['macd'], row['macd_signal'], row['macd_hist'])
            )
        conn.commit()
        calc = _make_calc(conn)
        return calc, sc

    def _make_row(self, date, ma10, ma20, ma30, ma60, hist):
        return {
            'date': date,
            'ma10': ma10, 'ma20': ma20, 'ma30': ma30, 'ma60': ma60,
            'macd': -0.1, 'macd_signal': -0.05, 'macd_hist': hist
        }

    def test_8_1_sell_signal_true(self):
        """MACD 死叉 + 均线空头 → True"""
        rows = [
            self._make_row('2025-01-02', 9, 10, 11, 12, +0.1),  # prev: hist>0
            self._make_row('2025-01-03', 9, 10, 11, 12, -0.1),  # curr: hist<0 + MA空头
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertTrue(calc.check_sell_signal(sc))

    def test_8_2_no_death_cross(self):
        """均线空头但无死叉 → False"""
        rows = [
            self._make_row('2025-01-02', 9, 10, 11, 12, -0.1),  # prev: hist<0
            self._make_row('2025-01-03', 9, 10, 11, 12, -0.2),  # curr: hist<0（无死叉）
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_sell_signal(sc))

    def test_8_3_no_ma_alignment(self):
        """MACD 死叉但无空头排列 → False"""
        rows = [
            self._make_row('2025-01-02', 12, 10, 11, 9, +0.1),  # prev
            self._make_row('2025-01-03', 12, 10, 11, 9, -0.1),  # MA不满足空头
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_sell_signal(sc))

    def test_8_4_none_macd_hist_returns_false(self):
        """macd_hist 为 None → False（不崩溃）"""
        rows = [
            self._make_row('2025-01-02', 9, 10, 11, 12, +0.1),
            {'date': '2025-01-03', 'ma10': 9, 'ma20': 10, 'ma30': 11, 'ma60': 12,
             'macd': None, 'macd_signal': None, 'macd_hist': None},
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_sell_signal(sc))

    def test_8_5_none_ma_values_returns_false(self):
        """MA 值为 None → False（不崩溃）"""
        rows = [
            self._make_row('2025-01-02', 9, 10, 11, 12, +0.1),
            {'date': '2025-01-03', 'ma10': None, 'ma20': 10, 'ma30': 11, 'ma60': 12,
             'macd': -0.1, 'macd_signal': -0.05, 'macd_hist': -0.05},
        ]
        calc, sc = self._make_calc_with_indicators(rows)
        self.assertFalse(calc.check_sell_signal(sc))

    def test_8_6_no_indicators_returns_false(self):
        """无指标数据 → False（不崩溃）"""
        conn, days, sc = _create_db(5, 0)
        calc = _make_calc(conn)
        self.assertFalse(calc.check_sell_signal(sc))

    def test_8_7_both_signals_not_simultaneously_true(self):
        """同一指标行不能同时触发买入和卖出（互斥）"""
        # 买入场景
        buy_rows = [
            {'date': '2025-01-02', 'ma10': 12, 'ma20': 11, 'ma30': 10, 'ma60': 9,
             'macd': 0.1, 'macd_signal': 0.05, 'macd_hist': -0.1},
            {'date': '2025-01-03', 'ma10': 12, 'ma20': 11, 'ma30': 10, 'ma60': 9,
             'macd': 0.1, 'macd_signal': 0.05, 'macd_hist': 0.1},
        ]
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
        sc = 'TEST01.SZ'
        for row in buy_rows:
            conn.execute(
                "INSERT INTO stock_indicators VALUES (?,?,?,?,?,?,?,?,?)",
                (sc, row['date'], row['ma10'], row['ma20'], row['ma30'], row['ma60'],
                 row['macd'], row['macd_signal'], row['macd_hist'])
            )
        conn.commit()
        calc = _make_calc(conn)

        # 买入信号为 True 时，卖出信号应为 False
        buy = calc.check_buy_signal(sc)
        sell = calc.check_sell_signal(sc)
        self.assertFalse(buy and sell, "买入和卖出信号不应同时为 True")


# ══════════════════════════════════════════════════════════════════════════════
# 9. update_all_stock_indicators
# ══════════════════════════════════════════════════════════════════════════════

class TestUpdateAllStockIndicators(unittest.TestCase):
    """
    update_all_stock_indicators 遍历 config.STOCK_POOL 调用 calculate_all_indicators。
    使用 patch config.STOCK_POOL 并注入测试数据库。
    """

    def test_9_1_updates_multiple_stocks(self):
        """多个股票均被更新"""
        sc1, sc2 = 'TEST01.SZ', 'TEST02.SZ'

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

        days = _make_trading_days(65)
        for sc in [sc1, sc2]:
            for i, dt in enumerate(days):
                p = 10.0 + i * 0.1
                conn.execute(
                    "INSERT INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)",
                    (sc, dt.strftime('%Y-%m-%d'), p, p*1.02, p*0.98, p, 1000000, p*1000000)
                )
        conn.commit()

        calc = _make_calc(conn)

        # 临时替换 config.STOCK_POOL
        original_pool = config.STOCK_POOL
        try:
            config.STOCK_POOL = [sc1, sc2]
            calc.update_all_stock_indicators(force_update=True)
        finally:
            config.STOCK_POOL = original_pool

        self.assertEqual(_row_count(conn, sc1), 65)
        self.assertEqual(_row_count(conn, sc2), 65)

    def test_9_2_empty_stock_pool(self):
        """空股票池：不崩溃，不写入任何数据"""
        conn, days, sc = _create_db(65, 0)
        calc = _make_calc(conn)

        original_pool = config.STOCK_POOL
        try:
            config.STOCK_POOL = []
            calc.update_all_stock_indicators()
        finally:
            config.STOCK_POOL = original_pool

        self.assertEqual(_row_count(conn, sc), 0)

    def test_9_3_partial_failure_continues(self):
        """某个股票无数据时，其他股票仍正常更新"""
        sc_good = 'GOOD01.SZ'
        sc_bad = 'NODATA.SZ'  # 无历史数据

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

        days = _make_trading_days(65)
        for i, dt in enumerate(days):
            p = 10.0 + i * 0.1
            conn.execute(
                "INSERT INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)",
                (sc_good, dt.strftime('%Y-%m-%d'), p, p*1.02, p*0.98, p, 1000000, p*1000000)
            )
        conn.commit()

        calc = _make_calc(conn)

        original_pool = config.STOCK_POOL
        try:
            config.STOCK_POOL = [sc_bad, sc_good]  # sc_bad 先执行，应失败但不中断
            calc.update_all_stock_indicators(force_update=True)
        finally:
            config.STOCK_POOL = original_pool

        # sc_good 应已写入
        self.assertEqual(_row_count(conn, sc_good), 65)
        # sc_bad 无数据，应为 0
        self.assertEqual(_row_count(conn, sc_bad), 0)


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s'
    )
    unittest.main(verbosity=2)
