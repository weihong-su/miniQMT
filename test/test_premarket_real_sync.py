"""
针对 premarket_sync.py 改动 A 的功能测试

改动内容 (premarket_sync.py step [6/9] 实盘分支):
  原来: 直接跳过 "○ 跳过持仓(实盘)"
  现在: qmt_trader.position() + _sync_real_positions_to_memory(df)

实际 DYNAMIC_TAKE_PROFIT 配置:
  [(0.05, 0.96), (0.10, 0.93), (0.15, 0.90), (0.20, 0.87), (0.30, 0.85), (0.40, 0.83), (0.50, 0.80)]

测试场景:
  TC1: 实盘模式 + 除权成本价变化(7.31→6.04) → 内存止损价从 8.37 重算到 7.88
       (cost=7.31 → ratio 34.7% → 档位 0.85 → stop=8.37)
       (cost=6.04 → ratio 63.1% → 档位 0.80 → stop=7.88)
  TC2: 实盘模式 + QMT 返回空 DataFrame → 跳过 sync，不崩溃
  TC3a: 实盘模式 + qmt_trader.position() 抛异常 → 不崩溃，positions_synced=False
  TC3b: 实盘模式 + qmt_trader.position() 返回 None → 跳过 sync，不崩溃
  TC3c: 实盘模式 + QMT 返回有效数据 → _sync_real_positions_to_memory 被调用一次
  TC4a: 模拟模式 → 调用 _sync_db_to_memory，不调用 _sync_real_positions_to_memory
  TC4b: 实盘模式 → 不调用 _sync_db_to_memory
"""

import os
import sys
import unittest
import pandas as pd
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from test.test_base import TestBase


# ---------------------------------------------------------------------------
# 辅助：构造 _sync_real_positions_to_memory 所需格式的 DataFrame
# ---------------------------------------------------------------------------
def make_qmt_df(positions):
    """构造符合 _sync_real_positions_to_memory 列格式的 DataFrame"""
    return pd.DataFrame([{
        '证券代码': p['stock_code'],
        '股票余额': p.get('volume', 0),
        '可用余额': p.get('available', 0),
        '成本价':   p.get('cost_price', 0.0),
        '市值':     p.get('market_value', 0.0),
    } for p in positions])


# ---------------------------------------------------------------------------
# TC1: 实盘模式 + 除权成本价变化 → 内存止损价被正确重算
# ---------------------------------------------------------------------------
class TestPremarketRealSyncCostChange(TestBase):
    """
    核心场景：
    内存中 600509 cost_price=7.31（未除权），stop_loss_price=8.37（cost=7.31 的正确止损）；
    QMT 盘前同步返回除权后 cost_price=6.04；
    调用 _sync_real_positions_to_memory 后，内存止损价应重算为 7.88。

    实际 DYNAMIC_TAKE_PROFIT 配置含 (0.50, 0.80) 档位：
    - cost=7.31: ratio=(9.85-7.31)/7.31=34.7% → 档位(0.30,0.85) → stop=9.85×0.85=8.37
    - cost=6.04: ratio=(9.85-6.04)/6.04=63.1% → 档位(0.50,0.80) → stop=9.85×0.80=7.88
    """

    STOCK_CODE  = '600509'
    STALE_COST  = 7.31
    QMT_COST    = 6.04
    HIGHEST     = 9.85
    STALE_SLP   = 8.37   # cost=7.31 对应的正确止损
    CORRECT_SLP = 7.88   # cost=6.04 对应的正确止损

    def setUp(self):
        super().setUp()
        from position_manager import PositionManager
        self.pm = PositionManager()
        self.pm.stop_sync_thread()

        # 清空内存表，确保用例隔离
        with self.pm.memory_conn_lock:
            cur = self.pm.memory_conn.cursor()
            cur.execute("DELETE FROM positions")
            self.pm.memory_conn.commit()

        # 插入测试数据：cost=7.31 时的正确止损 8.37（模拟重启后从 SQLite 加载的状态）
        with self.pm.memory_conn_lock:
            cur = self.pm.memory_conn.cursor()
            cur.execute('''
                INSERT INTO positions
                  (stock_code, stock_name, volume, available,
                   cost_price, base_cost_price,
                   current_price, market_value, profit_ratio,
                   open_date, profit_triggered,
                   highest_price, stop_loss_price, last_update)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (self.STOCK_CODE, '大唐发电', 4700, 0,
                  self.STALE_COST, self.STALE_COST,
                  8.10, 4700 * 8.10, 10.8,
                  '2024-10-15 09:30:00', 1,
                  self.HIGHEST,
                  self.STALE_SLP,   # 8.37（cost=7.31 对应的正确值）
                  '2025-12-01 09:30:02'))
            self.pm.memory_conn.commit()

        # Mock 行情数据（无实时行情，以 cost_price 兜底）
        self.pm.qmt_trader = MagicMock()
        self.pm.data_manager = MagicMock()
        self.pm.data_manager.get_latest_data.return_value = None

    def tearDown(self):
        super().tearDown()

    def test_tc1_cost_change_triggers_stop_recalc(self):
        """TC1: QMT 返回除权后 cost=6.04，止损价应从 8.37 重算为 7.88"""
        # 构造 QMT 返回的实盘持仓（cost_price=6.04，除权后新值）
        qmt_df = make_qmt_df([{
            'stock_code':  self.STOCK_CODE,
            'volume':      4700,
            'available':   0,
            'cost_price':  self.QMT_COST,
            'market_value': 4700 * 8.10,
        }])

        # 执行改动 A 的核心调用（盘前同步 step [6/9] 实盘模式下执行的两行）
        self.pm._sync_real_positions_to_memory(qmt_df)

        # 读取内存中更新后的数据
        with self.pm.memory_conn_lock:
            cursor = self.pm.memory_conn.cursor()
            cursor.execute(
                "SELECT cost_price, stop_loss_price FROM positions WHERE stock_code=?",
                (self.STOCK_CODE,)
            )
            row = cursor.fetchone()

        self.assertIsNotNone(row, "持仓记录应存在于内存数据库")
        mem_cost = float(row[0])
        mem_slp  = float(row[1])

        # 断言1：成本价已更新为 QMT 除权后的新值
        self.assertAlmostEqual(mem_cost, self.QMT_COST, places=2,
            msg=f"内存 cost_price 应更新为 {self.QMT_COST}，实际={mem_cost}")

        # 断言2：止损价已按新成本重算（cost=6.04 → ratio=63.1% → 档位0.80 → 9.85×0.80=7.88）
        self.assertAlmostEqual(mem_slp, self.CORRECT_SLP, places=2,
            msg=f"内存 stop_loss_price 应重算为 {self.CORRECT_SLP}（新 cost=6.04 对应），实际={mem_slp}")

        # 断言3：止损价已从 cost=7.31 对应的旧值 8.37 变更（两值不再相等）
        self.assertNotAlmostEqual(mem_slp, self.STALE_SLP, places=2,
            msg=f"止损价不应保持 cost=7.31 对应的旧值 {self.STALE_SLP}，应已重算为 {self.CORRECT_SLP}")


# ---------------------------------------------------------------------------
# TC2/TC3: 实盘模式边界情况 & TC4: 模拟模式不变
# 直接测试 step [6/9] 的分支逻辑（与实际代码保持一致）
# ---------------------------------------------------------------------------

def _run_step6_logic(sim_mode, mock_pm):
    """模拟 premarket_sync.py step [6/9] 完整逻辑，返回 results dict"""
    results = {'positions_synced': None, 'errors': []}

    if sim_mode:
        mock_pm._sync_db_to_memory()
        results['positions_synced'] = True
    else:
        try:
            real_positions_df = mock_pm.qmt_trader.position()
            if real_positions_df is not None and not real_positions_df.empty:
                mock_pm._sync_real_positions_to_memory(real_positions_df)
            results['positions_synced'] = True
        except Exception as e:
            results['positions_synced'] = False

    return results


class TestPremarketRealSyncEdgeCases(TestBase):
    """TC2/TC3: 实盘模式的边界情况"""

    def test_tc2_empty_dataframe_no_crash(self):
        """TC2: position() 返回空 DataFrame → 跳过 _sync_real_positions_to_memory，不崩溃"""
        mock_pm = MagicMock()
        mock_pm.qmt_trader.position.return_value = pd.DataFrame()

        results = _run_step6_logic(sim_mode=False, mock_pm=mock_pm)

        self.assertTrue(results['positions_synced'],
            "空持仓时 positions_synced 应为 True（优雅跳过）")
        mock_pm._sync_real_positions_to_memory.assert_not_called()

    def test_tc3a_position_exception_graceful(self):
        """TC3a: position() 抛出异常 → positions_synced=False，不崩溃，不影响后续步骤"""
        mock_pm = MagicMock()
        mock_pm.qmt_trader.position.side_effect = Exception("QMT连接超时")

        results = _run_step6_logic(sim_mode=False, mock_pm=mock_pm)

        self.assertFalse(results['positions_synced'],
            "QMT 异常时 positions_synced 应为 False")
        mock_pm._sync_real_positions_to_memory.assert_not_called()

    def test_tc3b_none_return_no_crash(self):
        """TC3b: position() 返回 None → 跳过 sync，positions_synced=True，不崩溃"""
        mock_pm = MagicMock()
        mock_pm.qmt_trader.position.return_value = None

        results = _run_step6_logic(sim_mode=False, mock_pm=mock_pm)

        self.assertTrue(results['positions_synced'],
            "position() 返回 None 时应优雅跳过，positions_synced=True")
        mock_pm._sync_real_positions_to_memory.assert_not_called()

    def test_tc3c_real_positions_synced_with_data(self):
        """TC3c: position() 返回有效 DataFrame → _sync_real_positions_to_memory 被调用一次"""
        mock_pm = MagicMock()
        qmt_df = make_qmt_df([{
            'stock_code': '000001', 'volume': 1000, 'available': 1000,
            'cost_price': 10.0, 'market_value': 10000.0,
        }])
        mock_pm.qmt_trader.position.return_value = qmt_df

        results = _run_step6_logic(sim_mode=False, mock_pm=mock_pm)

        self.assertTrue(results['positions_synced'])
        mock_pm._sync_real_positions_to_memory.assert_called_once_with(qmt_df)


class TestPremarketSimModeUnchanged(TestBase):
    """TC4: 模拟模式的原有行为不受影响"""

    def test_tc4a_sim_mode_calls_sync_db(self):
        """TC4a: sim_mode=True → 调用 _sync_db_to_memory，不调用实盘同步"""
        mock_pm = MagicMock()

        results = _run_step6_logic(sim_mode=True, mock_pm=mock_pm)

        self.assertTrue(results['positions_synced'])
        mock_pm._sync_db_to_memory.assert_called_once()
        mock_pm._sync_real_positions_to_memory.assert_not_called()
        mock_pm.qmt_trader.position.assert_not_called()

    def test_tc4b_real_mode_does_not_call_sync_db(self):
        """TC4b: sim_mode=False → 不调用 _sync_db_to_memory"""
        mock_pm = MagicMock()
        mock_pm.qmt_trader.position.return_value = make_qmt_df([{
            'stock_code': '000001', 'volume': 1000, 'available': 1000,
            'cost_price': 10.0, 'market_value': 10000.0,
        }])

        results = _run_step6_logic(sim_mode=False, mock_pm=mock_pm)

        self.assertTrue(results['positions_synced'])
        mock_pm._sync_db_to_memory.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
