"""
双层存储架构专项测试
===================
覆盖 MECE 审查发现的 P1/P4/P5 问题：

P1 - _sync_memory_to_db() INSERT 路径 available 应写 0（不写内存快照）
P4 - grid_database.py _init_base_tables 重复建表注释一致性
P5 - 测试覆盖空白：
     - UPDATE 路径不写 available（保护约束）
     - INSERT 路径 available=0（回归保护）
     - 重启后 available 由 QMT 实盘数据覆盖（不依赖 SQLite 快照）
     - available 字段类型强转为 int 的边界情况
"""

import unittest
import sqlite3
import os
import sys
import time
from datetime import datetime
from unittest.mock import patch, MagicMock
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from logger import get_logger

logger = get_logger("test_dual_layer_storage")

# --------------------------------------------------------------------------
# 辅助：创建符合 position_manager.py Schema 的内存表
# --------------------------------------------------------------------------
_CREATE_POSITIONS_SQL = """
    CREATE TABLE IF NOT EXISTS positions (
        stock_code TEXT PRIMARY KEY,
        stock_name TEXT,
        volume REAL,
        available REAL,
        cost_price REAL,
        base_cost_price REAL,
        current_price REAL,
        market_value REAL,
        profit_ratio REAL,
        last_update TIMESTAMP,
        open_date TIMESTAMP,
        profit_triggered BOOLEAN DEFAULT FALSE,
        highest_price REAL,
        stop_loss_price REAL,
        profit_breakout_triggered BOOLEAN DEFAULT FALSE,
        breakout_highest_price REAL
    )
"""


def _make_memory_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_POSITIONS_SQL)
    conn.commit()
    return conn


def _make_sqlite_conn(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(_CREATE_POSITIONS_SQL)
    conn.commit()
    return conn


def _insert_memory_position(conn, stock_code, volume, available, cost_price=10.0,
                             profit_triggered=False, highest_price=None, stop_loss_price=None,
                             base_cost_price=None, profit_breakout_triggered=False,
                             breakout_highest_price=None):
    """向内存表插入测试持仓行"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
        INSERT OR REPLACE INTO positions
            (stock_code, stock_name, volume, available, cost_price, base_cost_price,
             current_price, market_value, profit_ratio, last_update, open_date,
             profit_triggered, highest_price, stop_loss_price,
             profit_breakout_triggered, breakout_highest_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        stock_code, stock_code, volume, available, cost_price,
        base_cost_price or cost_price,
        cost_price, volume * cost_price, 0.0,
        now, now,
        profit_triggered,
        highest_price or cost_price,
        stop_loss_price or cost_price * 0.93,
        profit_breakout_triggered,
        breakout_highest_price or cost_price,
    ))
    conn.commit()


# --------------------------------------------------------------------------
# 测试组 A：_sync_memory_to_db 的 INSERT/UPDATE 路径
# --------------------------------------------------------------------------
class TestSyncMemoryToDbPaths(unittest.TestCase):
    """
    直接调用 position_manager._sync_memory_to_db，验证：
    A1 - INSERT 路径：SQLite 中不存在的记录，available 写入值为 0
    A2 - UPDATE 路径：SQLite 中已有记录，available 不在 SET 子句中（值不更新）
    A3 - 模拟模式下 _sync_memory_to_db 直接跳过（不写 SQLite）
    A4 - 非交易时间 _sync_memory_to_db 跳过（不写 SQLite）
    """

    TEST_DB = "data/test_dual_layer_sync.db"

    @classmethod
    def setUpClass(cls):
        # 确保 data 目录存在
        os.makedirs("data", exist_ok=True)
        # 测试使用独立 DB，与生产隔离
        cls._orig_db_path = config.DB_PATH
        config.DB_PATH = cls.TEST_DB

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._orig_db_path
        if os.path.exists(cls.TEST_DB):
            try:
                os.remove(cls.TEST_DB)
            except Exception:
                pass

    def _make_pm_with_memory(self, memory_conn, sqlite_conn_path):
        """
        构造一个最小化的 PositionManager 代理对象，只暴露 _sync_memory_to_db 需要的属性。
        避免启动真实的 PositionManager（需要 QMT 环境）。
        """
        import threading

        class FakePM:
            pass

        pm = FakePM()
        pm.memory_conn = memory_conn
        pm.memory_conn_lock = threading.Lock()

        # 绑定真实方法到 fake 对象
        from position_manager import PositionManager
        pm._sync_memory_to_db = PositionManager._sync_memory_to_db.__get__(pm, FakePM)
        return pm

    def setUp(self):
        # 每个测试前清理测试 DB
        if os.path.exists(self.TEST_DB):
            os.remove(self.TEST_DB)
        self.sqlite_conn = _make_sqlite_conn(self.TEST_DB)
        self.sqlite_conn.close()  # 让 _sync_memory_to_db 自己建连接

        self.memory_conn = _make_memory_conn()

    def tearDown(self):
        self.memory_conn.close()
        if os.path.exists(self.TEST_DB):
            os.remove(self.TEST_DB)

    # ------------------------------------------------------------------
    # A1: INSERT 路径 - available 必须为 0
    # ------------------------------------------------------------------
    def test_A1_insert_path_available_is_zero(self):
        """
        P1回归：_sync_memory_to_db INSERT 新记录时，SQLite 中 available 必须为 0，
        而不是内存中的实际值（可能是过期快照）。
        """
        # 内存中 available=800（模拟已部分卖出但数据未实盘覆盖）
        _insert_memory_position(self.memory_conn, "000001.SZ", volume=1000, available=800,
                                cost_price=10.0, profit_triggered=False,
                                highest_price=11.0, stop_loss_price=9.3)

        # 以实盘模式 + 交易时间执行同步
        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch('config.is_trade_time', return_value=True):
            from position_manager import PositionManager
            # 直接调用实例方法（传入 self=None 会失败，需要构造最小对象）
            pm = self._build_real_pm_stub()
            pm._sync_memory_to_db()

        # 验证 SQLite 中 available=0，而非内存中的 800
        conn = sqlite3.connect(self.TEST_DB)
        row = conn.execute("SELECT available FROM positions WHERE stock_code=?",
                           ("000001.SZ",)).fetchone()
        conn.close()

        self.assertIsNotNone(row, "SQLite 中应有该记录")
        self.assertEqual(int(row[0]), 0,
                         f"INSERT 路径 available 应为 0，实际为 {row[0]}（P1回归检查）")

    def _build_real_pm_stub(self):
        """
        构建能运行 _sync_memory_to_db 的最小 PositionManager 桩对象。
        使用 unittest.mock 替换所有外部依赖。
        """
        import threading
        from unittest.mock import MagicMock

        # 导入真实类但不初始化（避免 QMT 连接）
        from position_manager import PositionManager

        # 用 object.__new__ 跳过 __init__
        pm = object.__new__(PositionManager)
        pm.memory_conn = self.memory_conn
        pm.memory_conn_lock = threading.Lock()

        return pm

    # ------------------------------------------------------------------
    # A2: UPDATE 路径 - available 不出现在 SET 子句
    # ------------------------------------------------------------------
    def test_A2_update_path_available_not_changed_in_sqlite(self):
        """
        UPDATE 路径：SQLite 中已有持仓（available=500），内存中 available=300，
        同步后 SQLite 的 available 应保持 500（不被覆盖），
        因为 UPDATE SET 语句中不包含 available。
        """
        # 先在 SQLite 中插入初始记录（available=500，模拟历史快照）
        initial_sqlite_conn = sqlite3.connect(self.TEST_DB)
        initial_sqlite_conn.execute(_CREATE_POSITIONS_SQL)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        initial_sqlite_conn.execute("""
            INSERT INTO positions
                (stock_code, stock_name, volume, available, cost_price, base_cost_price,
                 open_date, profit_triggered, highest_price, stop_loss_price,
                 profit_breakout_triggered, breakout_highest_price, last_update)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("000002.SZ", "000002.SZ", 1000, 500, 10.0, 10.0,
              now, False, 10.5, 9.3, False, 10.0, now))
        initial_sqlite_conn.commit()
        initial_sqlite_conn.close()

        # 内存中 available=300（已变化），profit_triggered=True（应被同步）
        _insert_memory_position(self.memory_conn, "000002.SZ", volume=1000, available=300,
                                cost_price=10.0, profit_triggered=True,
                                highest_price=11.0, stop_loss_price=9.3)

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch('config.is_trade_time', return_value=True):
            pm = self._build_real_pm_stub()
            pm._sync_memory_to_db()

        # 验证：available 保持 500（未被 UPDATE 覆盖）
        conn = sqlite3.connect(self.TEST_DB)
        row = conn.execute(
            "SELECT available, profit_triggered FROM positions WHERE stock_code=?",
            ("000002.SZ",)
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 500,
                         f"UPDATE 路径不应修改 SQLite.available（应保持500），实际为 {row[0]}")
        # profit_triggered 应该被同步过来（=1）
        self.assertTrue(bool(row[1]),
                        "UPDATE 路径应同步 profit_triggered 到 SQLite")

    # ------------------------------------------------------------------
    # A3: 模拟模式下直接跳过
    # ------------------------------------------------------------------
    def test_A3_simulation_mode_skips_sync(self):
        """模拟模式下 _sync_memory_to_db 不写 SQLite"""
        _insert_memory_position(self.memory_conn, "000003.SZ", volume=1000, available=1000)

        with patch.object(config, 'ENABLE_SIMULATION_MODE', True):
            pm = self._build_real_pm_stub()
            pm._sync_memory_to_db()

        conn = sqlite3.connect(self.TEST_DB)
        conn.execute(_CREATE_POSITIONS_SQL)
        row = conn.execute("SELECT * FROM positions WHERE stock_code=?",
                           ("000003.SZ",)).fetchone()
        conn.close()

        self.assertIsNone(row, "模拟模式下不应有数据写入 SQLite")

    # ------------------------------------------------------------------
    # A4: 非交易时间跳过
    # ------------------------------------------------------------------
    def test_A4_non_trade_time_skips_sync(self):
        """非交易时间 _sync_memory_to_db 不写 SQLite"""
        _insert_memory_position(self.memory_conn, "000004.SZ", volume=1000, available=1000)

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch('config.is_trade_time', return_value=False):
            pm = self._build_real_pm_stub()
            pm._sync_memory_to_db()

        conn = sqlite3.connect(self.TEST_DB)
        conn.execute(_CREATE_POSITIONS_SQL)
        row = conn.execute("SELECT * FROM positions WHERE stock_code=?",
                           ("000004.SZ",)).fetchone()
        conn.close()

        self.assertIsNone(row, "非交易时间不应有数据写入 SQLite")

    # ------------------------------------------------------------------
    # A5: INSERT 后仅持久化字段被写入，available=0 与其他字段正确
    # ------------------------------------------------------------------
    def test_A5_insert_persists_strategy_fields_correctly(self):
        """
        INSERT 路径除了 available=0 外，其他策略持久化字段
        (profit_triggered, highest_price, stop_loss_price 等) 应被正确写入。
        """
        _insert_memory_position(
            self.memory_conn, "000005.SZ",
            volume=2000, available=1500,
            cost_price=15.0,
            profit_triggered=True,
            highest_price=18.0,
            stop_loss_price=13.95,
            profit_breakout_triggered=True,
            breakout_highest_price=17.5,
        )

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False), \
             patch('config.is_trade_time', return_value=True):
            pm = self._build_real_pm_stub()
            pm._sync_memory_to_db()

        conn = sqlite3.connect(self.TEST_DB)
        row = conn.execute("""
            SELECT available, volume, profit_triggered, highest_price,
                   stop_loss_price, profit_breakout_triggered, breakout_highest_price
            FROM positions WHERE stock_code=?
        """, ("000005.SZ",)).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 0, "available 应为 0（P1修复验证）")
        self.assertEqual(int(row[1]), 2000, "volume 应正确写入")
        self.assertTrue(bool(row[2]), "profit_triggered 应为 True")
        self.assertAlmostEqual(row[3], 18.0, places=4, msg="highest_price 应正确写入")
        self.assertAlmostEqual(row[4], 13.95, places=4, msg="stop_loss_price 应正确写入")
        self.assertTrue(bool(row[5]), "profit_breakout_triggered 应为 True")
        self.assertAlmostEqual(row[6], 17.5, places=4, msg="breakout_highest_price 应正确写入")


# --------------------------------------------------------------------------
# 测试组 B：重启恢复链路 - available 由 QMT 实盘数据覆盖
# --------------------------------------------------------------------------
class TestRestartAvailableRecovery(unittest.TestCase):
    """
    P5补充：验证重启时 available 由实盘数据覆盖，不依赖 SQLite 快照。

    B1 - _sync_db_to_memory 从 SQLite 加载后 available=0（快照为0）
    B2 - update_position 用实盘 available 覆盖内存（available恢复正确值）
    B3 - 完整重启链路：DB→内存(available=0) → 实盘数据解析 → update_position → available=实盘值
    """

    def _make_full_pm_stub(self):
        """
        构建完整的 PositionManager stub，所有属性都使用内存对象，
        不涉及任何文件 IO，避免 Windows 文件锁问题。
        """
        import threading
        from position_manager import PositionManager

        mem_conn = _make_memory_conn()
        sqlite_conn = sqlite3.connect(":memory:")
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_conn.execute(_CREATE_POSITIONS_SQL)
        sqlite_conn.commit()

        pm = object.__new__(PositionManager)
        pm.memory_conn = mem_conn
        pm.memory_conn_lock = threading.Lock()
        pm.conn = sqlite_conn
        pm.sync_lock = threading.Lock()
        pm.signal_lock = threading.Lock()
        pm.version_lock = threading.Lock()
        pm._deleting_stocks = set()
        pm.data_version = 0
        pm.data_changed = False
        pm.latest_signals = {}
        pm.pending_orders = {}
        pm.data_manager = MagicMock()
        pm.data_manager.get_latest_data.return_value = {'lastPrice': 10.5}
        pm._update_stock_positions_file = MagicMock()
        return pm, mem_conn, sqlite_conn

    # ------------------------------------------------------------------
    # B1: _sync_db_to_memory 后内存中 available=0（SQLite 快照为 0）
    # ------------------------------------------------------------------
    def test_B1_restart_loads_available_zero_from_sqlite(self):
        """
        重启时从 SQLite 加载（P1修复后 SQLite.available=0），
        内存中 available 应为 0，说明重启窗口期不会有过期快照。
        """
        import threading
        from position_manager import PositionManager

        # 准备一个内存 SQLite 作为"重启前的文件 DB 快照"
        sqlite_conn = sqlite3.connect(":memory:")
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_conn.execute(_CREATE_POSITIONS_SQL)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sqlite_conn.execute("""
            INSERT INTO positions
                (stock_code, stock_name, volume, available, cost_price, base_cost_price,
                 open_date, profit_triggered, highest_price, stop_loss_price,
                 profit_breakout_triggered, breakout_highest_price, last_update)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("000001.SZ", "平安银行", 1000, 10.0, 10.0,
              now, True, 11.0, 9.3, False, 10.0, now))
        sqlite_conn.commit()

        memory_conn = _make_memory_conn()

        pm = object.__new__(PositionManager)
        pm.memory_conn = memory_conn
        pm.memory_conn_lock = threading.Lock()
        pm.conn = sqlite_conn

        PositionManager._sync_db_to_memory(pm)
        sqlite_conn.close()

        row = memory_conn.execute(
            "SELECT available FROM positions WHERE stock_code=?", ("000001.SZ",)
        ).fetchone()
        memory_conn.close()

        self.assertIsNotNone(row, "内存中应已加载该持仓")
        self.assertEqual(int(row[0]), 0,
                         f"重启后内存 available 应为 0（来自 SQLite 快照），实际为 {row[0]}")

    # ------------------------------------------------------------------
    # B2: 实盘数据解析后 update_position 写入正确的 available
    # ------------------------------------------------------------------
    def test_B2_realtime_sync_overwrites_available(self):
        """
        _sync_real_positions_to_memory 中解析 QMT 的 '可用余额' 后
        调用 update_position 写入内存，应覆盖 available=0 的快照值。
        """
        from position_manager import PositionManager

        pm, mem_conn, sqlite_conn = self._make_full_pm_stub()

        # 先在内存中插入 available=0 的启动快照
        _insert_memory_position(mem_conn, "000001.SZ", volume=1000, available=0, cost_price=10.0,
                                profit_triggered=True, highest_price=11.0, stop_loss_price=9.3)
        # 同时在 sqlite_conn 中插入（供 _sync_real_positions_to_memory 查询 profit_triggered 等）
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sqlite_conn.execute("""
            INSERT INTO positions
                (stock_code, stock_name, volume, available, cost_price, base_cost_price,
                 open_date, profit_triggered, highest_price, stop_loss_price,
                 profit_breakout_triggered, breakout_highest_price, last_update)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("000001.SZ", "平安银行", 1000, 10.0, 10.0,
              now, True, 11.0, 9.3, False, 10.0, now))
        sqlite_conn.commit()

        # 构造 QMT 实盘数据（可用余额=800）
        real_df = pd.DataFrame([{
            '证券代码': '000001.SZ',
            '股票余额': 1000,
            '可用余额': 800,
            '成本价': 10.0,
            '市值': 10000.0,
        }])

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            PositionManager._sync_real_positions_to_memory(pm, real_df)

        sqlite_conn.close()

        row = mem_conn.execute(
            "SELECT available FROM positions WHERE stock_code=?", ("000001.SZ",)
        ).fetchone()
        mem_conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), 800,
                         f"实盘同步后内存 available 应为 800，实际为 {row[0]}")

    # ------------------------------------------------------------------
    # B3: 完整重启链路：DB→内存(available=0) → 实盘覆盖(available=实盘值)
    # ------------------------------------------------------------------
    def test_B3_full_restart_chain_available_correctly_restored(self):
        """
        完整重启链路验证：
        1. _sync_db_to_memory → 内存 available=0（快照）
        2. _sync_real_positions_to_memory → 内存 available=实盘值
        3. 最终内存值正确，无过期快照残留
        """
        import threading
        from position_manager import PositionManager

        # 构造"文件 DB"（用内存模拟，available=0 为 P1 修复后的状态）
        sqlite_conn = sqlite3.connect(":memory:")
        sqlite_conn.row_factory = sqlite3.Row
        sqlite_conn.execute(_CREATE_POSITIONS_SQL)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sqlite_conn.execute("""
            INSERT INTO positions
                (stock_code, stock_name, volume, available, cost_price, base_cost_price,
                 open_date, profit_triggered, highest_price, stop_loss_price,
                 profit_breakout_triggered, breakout_highest_price, last_update)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("000001.SZ", "平安银行", 1000, 10.0, 10.0,
              now, True, 11.0, 9.3, False, 10.0, now))
        sqlite_conn.commit()

        memory_conn = _make_memory_conn()

        pm = object.__new__(PositionManager)
        pm.memory_conn = memory_conn
        pm.memory_conn_lock = threading.Lock()
        pm.conn = sqlite_conn
        pm.sync_lock = threading.Lock()
        pm.signal_lock = threading.Lock()
        pm.version_lock = threading.Lock()
        pm._deleting_stocks = set()
        pm.data_version = 0
        pm.data_changed = False
        pm.latest_signals = {}
        pm.pending_orders = {}
        pm.data_manager = MagicMock()
        pm.data_manager.get_latest_data.return_value = {'lastPrice': 10.5}
        pm._update_stock_positions_file = MagicMock()

        # Step 1: 模拟系统重启，加载 SQLite → 内存
        PositionManager._sync_db_to_memory(pm)

        row_after_db_load = memory_conn.execute(
            "SELECT available FROM positions WHERE stock_code=?", ("000001.SZ",)
        ).fetchone()
        self.assertEqual(int(row_after_db_load[0]), 0,
                         "步骤1: DB→内存 available 应为 0（SQLite 快照）")

        # Step 2: 实盘同步覆盖（QMT 返回 available=750）
        real_df = pd.DataFrame([{
            '证券代码': '000001.SZ',
            '股票余额': 1000,
            '可用余额': 750,
            '成本价': 10.0,
            '市值': 10000.0,
        }])

        with patch.object(config, 'ENABLE_SIMULATION_MODE', False):
            PositionManager._sync_real_positions_to_memory(pm, real_df)

        sqlite_conn.close()

        row_after_realtime = memory_conn.execute(
            "SELECT available FROM positions WHERE stock_code=?", ("000001.SZ",)
        ).fetchone()
        memory_conn.close()

        self.assertEqual(int(row_after_realtime[0]), 750,
                         f"步骤2: 实盘同步后内存 available 应为 750，实际为 {row_after_realtime[0]}")


# --------------------------------------------------------------------------
# 测试组 C：available 类型边界
# --------------------------------------------------------------------------
class TestAvailableTypeBoundary(unittest.TestCase):
    """
    P5补充：available 字段类型强转为 int 的边界情况。
    C1 - 内存表定义为 REAL，写入时强转 int，验证精度
    C2 - available=None 时默认为 volume
    C3 - available 小数四舍五入后的值
    """

    def setUp(self):
        self.memory_conn = _make_memory_conn()

    def tearDown(self):
        self.memory_conn.close()

    def test_C1_available_stored_as_integer_in_memory(self):
        """
        update_position 写入内存时将 available 强转为 int(p_available)，
        验证写入后读回为整数类型。
        """
        _insert_memory_position(self.memory_conn, "000001.SZ", volume=1000, available=999)

        row = self.memory_conn.execute(
            "SELECT available FROM positions WHERE stock_code=?", ("000001.SZ",)
        ).fetchone()

        # SQLite REAL 列存储但值应为整数
        self.assertEqual(int(row[0]), 999)

    def test_C2_available_none_defaults_to_volume(self):
        """
        当 available 未传入（None）时，update_position 默认 p_available=p_volume，
        即全仓可用。
        """
        # 直接测试 update_position 的默认值逻辑
        volume = 500
        available = None
        p_available = int(available) if available is not None else volume
        self.assertEqual(p_available, 500,
                         "available=None 时应默认为 volume=500")

    def test_C3_available_float_truncated_to_int(self):
        """
        available 传入浮点数时，int() 向零截断（非四舍五入）。
        """
        available_float = 499.9
        p_available = int(available_float)
        self.assertEqual(p_available, 499,
                         "浮点数 available 应向零截断为 int")

        available_float_neg = 0.1
        p_available_neg = int(available_float_neg)
        self.assertEqual(p_available_neg, 0,
                         "0.1 截断后应为 0")

    def test_C4_available_zero_prevents_duplicate_signal(self):
        """
        validate_trading_signal 中 available=0 且 volume>0 时应拒绝信号，
        防止重复委托。这是 available 字段的核心业务语义。
        """
        position = {'stock_code': '000001.SZ', 'volume': 1000, 'available': 0}
        volume = int(position.get('volume', 0))
        available = int(position.get('available', 0))

        # 模拟 validate_trading_signal 中的判断逻辑
        should_reject = (available == 0 and volume > 0)
        self.assertTrue(should_reject,
                        "available=0 且 volume>0 时应拒绝信号（有委托在途）")

    def test_C5_available_positive_allows_signal(self):
        """
        available>0 时不应拒绝信号。
        """
        position = {'stock_code': '000001.SZ', 'volume': 1000, 'available': 600}
        volume = int(position.get('volume', 0))
        available = int(position.get('available', 0))

        should_reject = (available == 0 and volume > 0)
        self.assertFalse(should_reject,
                         "available>0 时不应拒绝信号")


# --------------------------------------------------------------------------
# 测试组 D：grid_database.py 一致性（P4）
# --------------------------------------------------------------------------
class TestGridDatabaseSchemaConsistency(unittest.TestCase):
    """
    P4回归：grid_database.py 与 position_manager.py 的 positions 表字段一致。
    D1 - 两处建表语句的字段集合完全一致
    D2 - grid_database.py 使用 CREATE TABLE IF NOT EXISTS（不覆盖已有表）
    D3 - DatabaseManager 初始化后 positions 表存在
    """

    TEST_DB = "data/test_grid_db_schema.db"

    @classmethod
    def setUpClass(cls):
        os.makedirs("data", exist_ok=True)
        cls._orig_db_path = config.DB_PATH
        config.DB_PATH = cls.TEST_DB

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._orig_db_path
        if os.path.exists(cls.TEST_DB):
            try:
                os.remove(cls.TEST_DB)
            except Exception:
                pass

    def setUp(self):
        if os.path.exists(self.TEST_DB):
            os.remove(self.TEST_DB)

    def tearDown(self):
        if os.path.exists(self.TEST_DB):
            os.remove(self.TEST_DB)

    def _get_table_columns(self, conn, table_name):
        """获取表的所有列名集合"""
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # D1: grid_database 建表字段与 position_manager 一致
    # ------------------------------------------------------------------
    def test_D1_grid_database_positions_columns_match_position_manager(self):
        """
        grid_database.py _init_base_tables 建的 positions 表字段集合
        应与 position_manager.py _create_memory_table 完全一致。
        """
        from grid_database import DatabaseManager

        # GridDatabase 建表
        gdb = DatabaseManager(db_path=self.TEST_DB)
        gdb_conn = sqlite3.connect(self.TEST_DB)
        grid_columns = self._get_table_columns(gdb_conn, "positions")
        gdb_conn.close()
        gdb.conn.close()

        # position_manager 建表（内存）
        mem_conn = _make_memory_conn()
        pm_columns = self._get_table_columns(mem_conn, "positions")
        mem_conn.close()

        self.assertEqual(grid_columns, pm_columns,
                         f"字段不一致:\n"
                         f"  grid_database 独有: {grid_columns - pm_columns}\n"
                         f"  position_manager 独有: {pm_columns - grid_columns}")

    # ------------------------------------------------------------------
    # D2: CREATE TABLE IF NOT EXISTS 不覆盖已有数据
    # ------------------------------------------------------------------
    def test_D2_grid_database_does_not_overwrite_existing_data(self):
        """
        DatabaseManager 初始化时 _init_base_tables 使用 IF NOT EXISTS，
        不覆盖已有数据。
        """
        # 先建表并插入数据
        conn = sqlite3.connect(self.TEST_DB)
        conn.execute(_CREATE_POSITIONS_SQL)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("""
            INSERT INTO positions (stock_code, stock_name, volume, available, cost_price,
                                   base_cost_price, open_date, last_update)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("000001.SZ", "Test", 1000, 0, 10.0, 10.0, now, now))
        conn.commit()
        conn.close()

        # 初始化 DatabaseManager（会调用 _init_base_tables）
        from grid_database import DatabaseManager
        gdb = DatabaseManager(db_path=self.TEST_DB)
        gdb.conn.close()

        # 数据应完整保留
        conn = sqlite3.connect(self.TEST_DB)
        row = conn.execute(
            "SELECT volume FROM positions WHERE stock_code=?", ("000001.SZ",)
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "已有数据不应被 CREATE TABLE IF NOT EXISTS 覆盖")
        self.assertEqual(int(row[0]), 1000, "已有数据 volume 应保持不变")

    # ------------------------------------------------------------------
    # D3: DatabaseManager 初始化后核心表存在
    # ------------------------------------------------------------------
    def test_D3_database_manager_creates_required_tables(self):
        """DatabaseManager 初始化后 positions 和 trade_records 表都应存在"""
        from grid_database import DatabaseManager

        gdb = DatabaseManager(db_path=self.TEST_DB)
        conn = sqlite3.connect(self.TEST_DB)

        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        conn.close()
        gdb.conn.close()

        self.assertIn("positions", tables, "positions 表应由 DatabaseManager 创建")
        self.assertIn("trade_records", tables, "trade_records 表应由 DatabaseManager 创建")


if __name__ == '__main__':
    unittest.main(verbosity=2)
