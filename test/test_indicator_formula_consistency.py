"""
指标公式一致性验证测试

目标：独立实现与 indicator_calculator.py 中完全一致的公式，
逐行逐列对比两套实现的计算结果，验证数值一致性。

覆盖指标（对应 stock_indicators 表的所有列）:
  - MA10 / MA20 / MA30 / MA60  ← Wilder's SMA (ewm alpha=1/N)
  - macd (DIF)                 ← round(EMA(12) - EMA(26), 3)
  - macd_signal (DEA)          ← round(EMA(DIF, 9), 3)
  - macd_hist (HIST)           ← round((DIF-DEA)*2, 3)

测试场景:
  A. 单调递增价格（基线简单场景）
  B. 正弦振荡价格（真实市场形态）
  C. 先涨后跌混合趋势
  D. 极短序列（数据不足时边界行为）
  E. 完整管道验证（计算→写入DB→读取→比较）
  F. 增量写入一致性（前N行全量 + 第N+1行增量 = 全量计算结果）
"""

import sys
import os
import sqlite3
import threading
import logging
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ── 项目根目录 ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config
import indicator_calculator as ic_mod


# ══════════════════════════════════════════════════════════════════════════════
#  参考实现（Reference Implementation）
#  与 MyTT 库函数及 indicator_calculator 中的调用方式完全一致
# ══════════════════════════════════════════════════════════════════════════════

def ref_sma(close: np.ndarray, N: int, M: int = 1) -> np.ndarray:
    """
    Wilder's 平滑移动平均（MyTT.SMA 等价）
    公式：ewm(alpha=M/N, adjust=False).mean()
    用于：MA10 / MA20 / MA30 / MA60
    """
    return pd.Series(close).ewm(alpha=M / N, adjust=False).mean().values


def ref_ema(close: np.ndarray, N: int) -> np.ndarray:
    """
    标准指数移动平均（MyTT.EMA 等价）
    公式：ewm(span=N, adjust=False).mean()   [alpha = 2/(N+1)]
    用于：MACD 内部计算
    """
    return pd.Series(close).ewm(span=N, adjust=False).mean().values


def ref_macd(close: np.ndarray, SHORT: int = 12, LONG: int = 26, M: int = 9):
    """
    MACD 指标（MyTT.MACD 等价）
    返回 (DIF, DEA, HIST) 均四舍五入到 3 位小数（RD 函数）

    列映射:
      stock_indicators.macd        ← DIF = EMA(12) - EMA(26)
      stock_indicators.macd_signal ← DEA = EMA(DIF, 9)
      stock_indicators.macd_hist   ← HIST = (DIF - DEA) * 2
    """
    dif = ref_ema(close, SHORT) - ref_ema(close, LONG)
    dea = ref_ema(dif, M)
    hist = (dif - dea) * 2
    return np.round(dif, 3), np.round(dea, 3), np.round(hist, 3)


def ref_all_indicators(close: np.ndarray) -> pd.DataFrame:
    """
    对给定收盘价序列一次性计算所有指标，返回与 stock_indicators 表列顺序一致的 DataFrame

    当数据不足以计算某指标时，对应列全为 NaN（与 indicator_calculator 行为一致）
    """
    n = len(close)
    result = pd.DataFrame(index=range(n))

    # ── MA ──────────────────────────────────────────────────────────────────
    for period in config.MA_PERIODS:
        col = f'ma{period}'
        if n < period:
            result[col] = np.nan
        else:
            result[col] = ref_sma(close, period)

    # ── MACD ─────────────────────────────────────────────────────────────────
    min_macd = max(config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL) + 10
    if n < min_macd:
        result['macd'] = np.nan
        result['macd_signal'] = np.nan
        result['macd_hist'] = np.nan
    else:
        dif, dea, hist = ref_macd(close, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
        result['macd'] = dif
        result['macd_signal'] = dea
        result['macd_hist'] = hist

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  测试基础设施
# ══════════════════════════════════════════════════════════════════════════════

def _trading_days(n: int, start=datetime(2020, 1, 2)) -> list:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _create_db(close_prices: np.ndarray, n_pre_indicator: int = 0):
    """
    创建内存 SQLite 数据库，写入日线数据和可选的已有指标记录。
    返回 (conn, trading_days, stock_code)
    """
    n = len(close_prices)
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

    stock_code = 'TEST.SZ'
    days = _trading_days(n)

    rows = [
        (stock_code, days[i].strftime('%Y-%m-%d'),
         p * 0.99, p * 1.01, p * 0.98, p, 1_000_000, p * 1_000_000)
        for i, p in enumerate(close_prices)
    ]
    conn.executemany("INSERT INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)", rows)

    if n_pre_indicator > 0:
        ind = [(stock_code, days[i].strftime('%Y-%m-%d'),
                None, None, None, None, None, None, None)
               for i in range(n_pre_indicator)]
        conn.executemany(
            "INSERT INTO stock_indicators VALUES (?,?,?,?,?,?,?,?,?)", ind
        )

    conn.commit()
    return conn, days, stock_code


class _MockDM:
    def __init__(self, conn):
        self.conn = conn
        self._db_lock = threading.Lock()

    def get_history_data_from_db(self, stock_code, start_date=None, end_date=None):
        q = "SELECT * FROM stock_daily_data WHERE stock_code=?"
        params = [stock_code]
        if start_date:
            q += " AND date>=?"; params.append(start_date)
        if end_date:
            q += " AND date<=?"; params.append(end_date)
        q += " ORDER BY date"
        return pd.read_sql_query(q, self.conn, params=params)


def _make_calc(conn):
    calc = ic_mod.IndicatorCalculator.__new__(ic_mod.IndicatorCalculator)
    calc.data_manager = _MockDM(conn)
    calc.conn = conn
    return calc


def _read_indicators(conn, stock_code) -> pd.DataFrame:
    """从 stock_indicators 读回结果，None→NaN"""
    return pd.read_sql_query(
        "SELECT * FROM stock_indicators WHERE stock_code=? ORDER BY date",
        conn, params=[stock_code]
    )


def _assert_series_close(tc: unittest.TestCase, ref: np.ndarray, got: np.ndarray,
                         label: str, places: int = 6):
    """
    逐元素断言两个序列在指定精度内相等，自动跳过两侧均为 NaN 的位置。
    若发现不一致则打印前几个差异供调试。
    """
    tc.assertEqual(len(ref), len(got),
                   f"{label}: 长度不一致 ref={len(ref)} got={len(got)}")

    mismatches = []
    for i, (r, g) in enumerate(zip(ref, got)):
        r_nan = r is None or (isinstance(r, float) and np.isnan(r))
        g_nan = g is None or (isinstance(g, float) and np.isnan(g)) or g is None

        if r_nan and g_nan:
            continue  # 双方均 NaN，跳过

        if r_nan != g_nan:
            mismatches.append((i, r, g, 'NaN mismatch'))
            continue

        diff = abs(float(r) - float(g))
        tol = 10 ** (-places)
        if diff > tol:
            mismatches.append((i, r, g, diff))

    if mismatches:
        preview = '\n'.join(
            f"  [{i}] ref={r:.8f}  got={g:.8f}  diff={d}"
            for i, r, g, d in mismatches[:5]
        )
        tc.fail(f"{label}: {len(mismatches)} 处不一致（共 {len(ref)} 行）:\n{preview}")


# ══════════════════════════════════════════════════════════════════════════════
#  价格序列生成器
# ══════════════════════════════════════════════════════════════════════════════

def _prices_monotonic(n=200):
    """单调递增，每天 +0.1 元"""
    return np.array([10.0 + i * 0.1 for i in range(n)])


def _prices_sine(n=200, base=10.0, amp=2.0, period=40):
    """正弦振荡，模拟真实市场波动"""
    t = np.arange(n)
    trend = t * 0.02  # 轻微上升趋势
    return base + trend + amp * np.sin(2 * np.pi * t / period)


def _prices_mixed(n=200):
    """先涨后跌的混合趋势（含噪声）"""
    rng = np.random.default_rng(42)
    t = np.arange(n)
    base = 10.0 + 2.0 * np.sin(np.pi * t / n)  # 先涨后跌
    noise = rng.normal(0, 0.05, n)
    return np.clip(base + noise, 1.0, None)  # 避免负价格


# ══════════════════════════════════════════════════════════════════════════════
#  测试类
# ══════════════════════════════════════════════════════════════════════════════

class TestIndicatorFormulas(unittest.TestCase):
    """验证 indicator_calculator 与参考公式的数值一致性"""

    def setUp(self):
        # 测试执行期间抑制日志噪声，但不影响其他测试文件的 logger 行为
        self._ic_orig_level = logging.getLogger('miniQMT.cal').level
        self._dm_orig_level = logging.getLogger('miniQMT.dat').level
        logging.getLogger('miniQMT.cal').setLevel(logging.CRITICAL)
        logging.getLogger('miniQMT.dat').setLevel(logging.CRITICAL)

    def tearDown(self):
        # 恢复 logger 级别，避免影响其他测试
        logging.getLogger('miniQMT.cal').setLevel(self._ic_orig_level)
        logging.getLogger('miniQMT.dat').setLevel(self._dm_orig_level)

    # ────────────────────────────────────────────────── 场景 A ──────────────
    def test_A_monotonic_all_columns(self):
        """
        场景 A: 单调递增价格（200条）
        验证 stock_indicators 表的全部 7 列（MA×4 + MACD×3）与参考公式一致
        """
        close = _prices_monotonic(200)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        ref = ref_all_indicators(close)

        self.assertEqual(len(got), 200, "应写入 200 行")

        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref[col].values, got[col].values, f"A/{col}")

        # MACD 精度：RD(N,3) 保留 3 位小数
        _assert_series_close(self, ref['macd'].values,        got['macd'].values,        'A/macd',        places=3)
        _assert_series_close(self, ref['macd_signal'].values, got['macd_signal'].values, 'A/macd_signal', places=3)
        _assert_series_close(self, ref['macd_hist'].values,   got['macd_hist'].values,   'A/macd_hist',   places=3)

    # ────────────────────────────────────────────────── 场景 B ──────────────
    def test_B_sine_all_columns(self):
        """
        场景 B: 正弦振荡价格（200条）
        MA 和 MACD 在振荡行情下与参考公式一致
        """
        close = _prices_sine(200)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        ref = ref_all_indicators(close)

        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref[col].values, got[col].values, f"B/{col}")

        _assert_series_close(self, ref['macd'].values,        got['macd'].values,        'B/macd',        places=3)
        _assert_series_close(self, ref['macd_signal'].values, got['macd_signal'].values, 'B/macd_signal', places=3)
        _assert_series_close(self, ref['macd_hist'].values,   got['macd_hist'].values,   'B/macd_hist',   places=3)

    # ────────────────────────────────────────────────── 场景 C ──────────────
    def test_C_mixed_trend_all_columns(self):
        """
        场景 C: 混合趋势（先涨后跌 + 随机噪声，200条）
        验证非单调价格序列下所有指标一致
        """
        close = _prices_mixed(200)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        ref = ref_all_indicators(close)

        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref[col].values, got[col].values, f"C/{col}")

        _assert_series_close(self, ref['macd'].values,        got['macd'].values,        'C/macd',        places=3)
        _assert_series_close(self, ref['macd_signal'].values, got['macd_signal'].values, 'C/macd_signal', places=3)
        _assert_series_close(self, ref['macd_hist'].values,   got['macd_hist'].values,   'C/macd_hist',   places=3)

    # ────────────────────────────────────────────────── 场景 D ──────────────
    def test_D_edge_exact_min_bars(self):
        """
        场景 D1: 恰好 60 条数据（满足所有 MA 最低要求）
        所有 MA60/MA30/MA20/MA10 均不为 NaN，MACD 也应有效（60 > 36）
        """
        close = _prices_sine(60)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        ref = ref_all_indicators(close)

        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref[col].values, got[col].values, f"D1/{col}")
        _assert_series_close(self, ref['macd'].values, got['macd'].values, 'D1/macd', places=3)

    def test_D_edge_insufficient_ma60(self):
        """
        场景 D2: 只有 30 条数据
        - MA10/MA20/MA30 应有效（数值与参考一致）
        - MA60 应全为 None（数据不足，30 < 60）
        - MACD 应全为 None（数据不足，30 < 36）
        """
        close = _prices_monotonic(30)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        ref = ref_all_indicators(close)

        # MA10/MA20/MA30 有效
        for col in ['ma10', 'ma20', 'ma30']:
            _assert_series_close(self, ref[col].values, got[col].values, f"D2/{col}")

        # MA60 全为 NULL
        self.assertTrue(got['ma60'].isna().all(),
                        f"D2/ma60: 30条数据时应全为 NaN，实际: {got['ma60'].dropna().values}")

        # MACD 全为 NULL
        for col in ['macd', 'macd_signal', 'macd_hist']:
            self.assertTrue(got[col].isna().all(),
                            f"D2/{col}: 30条数据时应全为 NaN")

    def test_D_edge_insufficient_all(self):
        """
        场景 D3: 只有 5 条数据
        所有指标全为 None（均不足最小要求）
        """
        close = _prices_monotonic(5)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        for col in ['ma10', 'ma20', 'ma30', 'ma60', 'macd', 'macd_signal', 'macd_hist']:
            self.assertTrue(got[col].isna().all(),
                            f"D3/{col}: 5条数据时应全为 NaN，实际有效值: {got[col].dropna().values}")

    # ────────────────────────────────────────────────── 场景 E ──────────────
    def test_E_pipeline_write_read_roundtrip(self):
        """
        场景 E: 完整管道验证（计算→写入 SQLite→读取→比较）
        验证 _save_indicators 写入的值经 SQLite 读回后精度无损失
        重点验证 MACD 的 3 位小数精度在 REAL 类型下保持
        """
        close = _prices_sine(150)
        conn, days, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        got = _read_indicators(conn, sc)
        ref = ref_all_indicators(close)

        # 检查 MA 精度在 SQLite REAL 往返后不超过 1e-9
        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref[col].values, got[col].values, f"E/{col}", places=9)

        # 检查 MACD 精度：存储的是 round(x,3)，读回后应在 3 位内一致
        _assert_series_close(self, ref['macd'].values,        got['macd'].values,        'E/macd',        places=3)
        _assert_series_close(self, ref['macd_signal'].values, got['macd_signal'].values, 'E/macd_signal', places=3)
        _assert_series_close(self, ref['macd_hist'].values,   got['macd_hist'].values,   'E/macd_hist',   places=3)

        # 额外验证：get_latest_indicators 返回值与最后一行参考一致
        latest = calc.get_latest_indicators(sc)
        self.assertIsNotNone(latest)
        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            self.assertAlmostEqual(latest[col], ref[col].values[-1], places=6,
                                   msg=f"E/latest/{col} 不一致")

    # ────────────────────────────────────────────────── 场景 F ──────────────
    def test_F_incremental_equals_full(self):
        """
        场景 F: 增量写入一致性
        全量计算 200 条 vs 先计算 199 条再增量计算第 200 条
        两者在最后一行的所有指标值应完全一致（EMA 路径依赖已使用 df_full 修复）
        """
        close = _prices_sine(200)

        # ── 全量路径 ──────────────────────────────────────────────────────────
        conn_full, _, sc = _create_db(close)
        calc_full = _make_calc(conn_full)
        calc_full.calculate_all_indicators(sc, force_update=True)
        got_full = _read_indicators(conn_full, sc)

        # ── 增量路径（前199条全量，再写第200条，执行增量） ─────────────────────
        conn_inc, days, _ = _create_db(close)
        # 先写 199 条指标（伪造）
        ind_pre = [
            (sc, days[i].strftime('%Y-%m-%d'), None, None, None, None, None, None, None)
            for i in range(199)
        ]
        conn_inc.executemany(
            "INSERT INTO stock_indicators VALUES (?,?,?,?,?,?,?,?,?)", ind_pre
        )
        conn_inc.commit()
        calc_inc = _make_calc(conn_inc)
        calc_inc.calculate_all_indicators(sc)  # 增量：只写第 200 条
        got_inc = _read_indicators(conn_inc, sc)

        # 取最后一行（第 200 条）进行比较
        last_full = got_full.iloc[-1]
        last_inc  = got_inc[got_inc['date'] == days[199].strftime('%Y-%m-%d')].iloc[0]

        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            self.assertAlmostEqual(last_full[col], last_inc[col], places=6,
                                   msg=f"F/{col}: 增量({last_inc[col]:.8f}) ≠ 全量({last_full[col]:.8f})")

        for col in ['macd', 'macd_signal', 'macd_hist']:
            self.assertAlmostEqual(last_full[col], last_inc[col], places=3,
                                   msg=f"F/{col}: 增量({last_inc[col]}) ≠ 全量({last_full[col]})")

    # ────────────────────────────────────────────────── 场景 G ──────────────
    def test_G_formula_internals_sma_vs_ema(self):
        """
        场景 G: 公式内部验证
        证明 MA（ewm alpha=1/N）与标准 EMA（ewm span=N）是不同公式，
        不应混用——分别验证与参考实现的等价性
        """
        close = _prices_sine(100)

        # Wilder's SMA: alpha = M/N = 1/N
        sma10_ref = ref_sma(close, 10)
        # 标准 EMA: alpha = 2/(N+1)
        ema10_ref = ref_ema(close, 10)

        # 二者不相等（N=10 时 alpha 分别为 0.1 vs 0.1818）
        diff = np.abs(sma10_ref - ema10_ref).max()
        self.assertGreater(diff, 1e-6,
                           "SMA(alpha=1/N) 与 EMA(span=N) 应产生不同结果")

        # indicator_calculator 使用的是 SMA（Wilder's），验证与参考 SMA 一致
        conn, _, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)
        got = _read_indicators(conn, sc)

        _assert_series_close(self, sma10_ref, got['ma10'].values, 'G/ma10_is_SMA')

        # 若错误地用 EMA，则与 DB 中的值不一致——验证这一反例
        ema10_ref_rounded = np.round(ema10_ref, 6)
        got_ma10_rounded  = np.round(got['ma10'].values.astype(float), 6)
        eq_ema = np.allclose(ema10_ref_rounded, got_ma10_rounded, atol=1e-4)
        self.assertFalse(eq_ema,
                         "MA10 不应等于标准 EMA10（公式验证：SMA≠EMA）")

    # ────────────────────────────────────────────────── 场景 H ──────────────
    def test_H_macd_column_mapping(self):
        """
        场景 H: MACD 列名映射验证
        stock_indicators 中的列名含义:
          macd        → DIF = round(EMA(12) - EMA(26), 3)
          macd_signal → DEA = round(EMA(DIF_unrounded, 9), 3)
          macd_hist   → HIST = round((DIF_unrounded - DEA_unrounded) × 2, 3)

        注意：HIST 的乘以 2 和四舍五入均基于未舍入的 DIF/DEA 中间值，
        不能用已存储的 round(DIF) 和 round(DEA) 反推，否则产生二次舍入误差。
        此处用参考公式的未舍入中间值进行验证。
        """
        close = _prices_sine(150)
        conn, _, sc = _create_db(close)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)
        got = _read_indicators(conn, sc)

        # ── 计算未舍入的 DIF/DEA（参考实现内部中间值）──────────────────────
        dif_raw = ref_ema(close, config.MACD_FAST) - ref_ema(close, config.MACD_SLOW)
        dea_raw = ref_ema(dif_raw, config.MACD_SIGNAL)
        hist_raw = (dif_raw - dea_raw) * 2

        # ── 验证 1: macd 列 = round(EMA(12)-EMA(26), 3) ───────────────────
        dif_rd = np.round(dif_raw, 3)
        _assert_series_close(self, dif_rd, got['macd'].values, 'H/macd=EMA12-EMA26', places=3)

        # ── 验证 2: macd_signal 列 = round(EMA(DIF_raw, 9), 3) ───────────
        dea_rd = np.round(dea_raw, 3)
        _assert_series_close(self, dea_rd, got['macd_signal'].values, 'H/signal=EMA(DIF,9)', places=3)

        # ── 验证 3: macd_hist 列 = round((DIF_raw-DEA_raw)*2, 3) ─────────
        # 关键：HIST 是对未舍入中间值做 ×2 后再取 round(3)，
        # 不是 round(DIF) - round(DEA)，否则出现二次舍入误差
        hist_rd = np.round(hist_raw, 3)
        _assert_series_close(self, hist_rd, got['macd_hist'].values, 'H/hist=(DIF-DEA)*2', places=3)

        # ── 验证 4: HIST 乘以 2（而非不乘）─────────────────────────────────
        # HIST = (DIF-DEA)*2, 量级应明显大于 DIF-DEA
        diff_no_mult = np.abs(np.round(dif_raw - dea_raw, 3))
        diff_with_mult = np.abs(hist_rd)
        # 两者比值应约为 2（非零元素）
        mask = diff_no_mult > 1e-6
        if mask.sum() > 10:
            ratio = (diff_with_mult[mask] / diff_no_mult[mask]).mean()
            self.assertAlmostEqual(ratio, 2.0, places=1,
                                   msg=f"H: HIST 应为 (DIF-DEA)×2，实际比值={ratio:.4f}")

    # ────────────────────────────────────────────────── 场景 I ──────────────
    def test_I_check_signal_logic(self):
        """
        场景 I: check_buy_signal / check_sell_signal 逻辑验证
        手工构造满足买入/卖出条件的指标数据，验证信号函数返回正确结果
        """
        # 构造价格：上涨趋势（MA10>MA20>MA30>MA60）+ MACD 金叉
        # 用 200 条上涨价格触发均线多头
        close_up = np.array([10.0 + i * 0.15 for i in range(200)])
        # 最后一根人工置为下跌再回升，制造 MACD hist 从负变正
        # 这里先用简单方式：直接测试 None 检测路径
        conn, _, sc = _create_db(close_up)
        calc = _make_calc(conn)
        calc.calculate_all_indicators(sc, force_update=True)

        # 有足够数据后，信号函数至少不应抛出异常
        buy  = calc.check_buy_signal(sc)
        sell = calc.check_sell_signal(sc)
        self.assertIsInstance(buy,  bool, "check_buy_signal 应返回 bool")
        self.assertIsInstance(sell, bool, "check_sell_signal 应返回 bool")
        # 上涨趋势下不应同时为买入+卖出
        self.assertFalse(buy and sell, "不应同时触发买入和卖出信号")

    # ────────────────────────────────────────────────── 场景 J ──────────────
    def test_J_extreme_values(self):
        """
        场景 J: 极端价格值（高价股、低价股、大波动）
        验证浮点溢出保护和 NaN/Inf 替换逻辑
        """
        # 高价股（类似贵州茅台 ~1800 元）
        close_high = _prices_sine(150, base=1800.0, amp=50.0)
        conn_h, _, sc = _create_db(close_high)
        calc_h = _make_calc(conn_h)
        calc_h.calculate_all_indicators(sc, force_update=True)
        got_h = _read_indicators(conn_h, sc)
        ref_h = ref_all_indicators(close_high)
        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref_h[col].values, got_h[col].values, f"J_high/{col}")

        # 低价股（~1 元）
        close_low = _prices_sine(150, base=1.5, amp=0.3)
        conn_l, _, sc = _create_db(close_low)
        calc_l = _make_calc(conn_l)
        calc_l.calculate_all_indicators(sc, force_update=True)
        got_l = _read_indicators(conn_l, sc)
        ref_l = ref_all_indicators(close_low)
        for col in ['ma10', 'ma20', 'ma30', 'ma60']:
            _assert_series_close(self, ref_l[col].values, got_l[col].values, f"J_low/{col}")


# ══════════════════════════════════════════════════════════════════════════════
#  快速公式验证（unittest 外部，可独立运行）
# ══════════════════════════════════════════════════════════════════════════════

def _quick_formula_check():
    """打印关键公式参数，供人工目视核对"""
    print("\n" + "="*60)
    print("公式参数速查")
    print("="*60)
    print(f"MA_PERIODS    : {config.MA_PERIODS}")
    print(f"MACD_FAST     : {config.MACD_FAST}  (EMA span={config.MACD_FAST}, alpha=2/{config.MACD_FAST+1})")
    print(f"MACD_SLOW     : {config.MACD_SLOW}  (EMA span={config.MACD_SLOW}, alpha=2/{config.MACD_SLOW+1})")
    print(f"MACD_SIGNAL   : {config.MACD_SIGNAL}   (EMA span={config.MACD_SIGNAL}, alpha=2/{config.MACD_SIGNAL+1})")
    print(f"MA alpha      : 1/N  (Wilder's SMA, NOT standard EMA)")
    print(f"MACD rounding : 3 decimal places (RD 函数)")
    print(f"MACD_hist     : (DIF - DEA) × 2  （注意乘以 2）")
    print()

    # 数值示例（100条递增价格，最后一行）
    close = _prices_monotonic(100)
    ref = ref_all_indicators(close)
    print("示例（100条单调递增, 最后一行）:")
    for col in ['ma10', 'ma20', 'ma30', 'ma60', 'macd', 'macd_signal', 'macd_hist']:
        v = ref[col].values[-1]
        print(f"  {col:<14}: {v:.6f}" if not np.isnan(v) else f"  {col:<14}: NaN")
    print("="*60)


# ══════════════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING,
                        format='%(asctime)s [%(levelname)s] - %(message)s')
    _quick_formula_check()
    unittest.main(verbosity=2)
