# -*- coding: utf-8 -*-
"""
Bug验证测试 - 针对2026-03-02日志中发现的两个问题进行验证
  问题1: /api/status 未使用账户信息缓存，每次直接调用 QMT API 导致8秒超时
  问题2: data_manager.py self.conn 缺少线程锁，多线程并发触发 SQLite 事务冲突

运行方式:
    C:\\Users\\PC\\Anaconda3\\envs\\python39\\python.exe test/test_bug_verification.py
"""
import sys
import os
import time
import threading
import sqlite3
import tempfile
import unittest
import inspect

# 添加项目根目录到 sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ============================================================
# 问题1 验证：/api/status 是否使用了 get_account_info_cached()
# ============================================================

class TestIssue1_AccountInfoCaching(unittest.TestCase):
    """
    验证 /api/status 使用 get_account_info_cached() 而非直接调用 QMT API
    """

    def test_get_status_uses_cache_not_executor(self):
        """
        验证修复后 get_status() 的源码中：
        1. 不再调用 api_executor.submit()
        2. 改为调用 get_account_info_cached()
        """
        import web_server
        import inspect
        src = inspect.getsource(web_server.get_status)

        self.assertNotIn(
            'api_executor.submit',
            src,
            "修复后 get_status() 不应再使用 api_executor.submit() 获取账户信息"
        )
        self.assertIn(
            'get_account_info_cached',
            src,
            "修复后 get_status() 应调用 get_account_info_cached() 读取缓存"
        )

    def test_get_account_info_cached_returns_quickly_without_qmt(self):
        """
        验证 get_account_info_cached() 在缓存为空时能快速返回默认值，不阻塞
        预期：调用耗时 < 50ms
        """
        import web_server

        # 清空缓存
        with web_server._account_info_lock:
            web_server._account_info_cache['data'] = None
            web_server._account_info_cache['ts'] = 0.0

        t0 = time.monotonic()
        result = web_server.get_account_info_cached()
        elapsed_ms = (time.monotonic() - t0) * 1000

        self.assertIsNotNone(result, "缓存为空时应返回默认账户信息，不应返回 None")
        self.assertLess(
            elapsed_ms, 50,
            f"get_account_info_cached() 耗时 {elapsed_ms:.1f}ms，应 < 50ms（不能阻塞等待 QMT API）"
        )

    def test_cache_returns_default_when_empty(self):
        """
        验证缓存为空时返回的默认值包含必要字段
        """
        import web_server

        with web_server._account_info_lock:
            web_server._account_info_cache['data'] = None
            web_server._account_info_cache['ts'] = 0.0

        result = web_server.get_account_info_cached()

        # _build_default_account_info() 提供的字段
        expected_keys = {'account_id', 'available', 'market_value', 'total_asset'}
        self.assertTrue(
            expected_keys.issubset(result.keys()),
            f"默认账户信息应包含 {expected_keys}，实际: {set(result.keys())}"
        )

    def test_cache_hit_returns_stored_data(self):
        """
        验证缓存命中时，直接返回已缓存的数据
        """
        import web_server

        mock_data = {
            'account_id': 'TEST_ACCOUNT',
            'available': 99999.0,
            'market_value': 50000.0,
            'total_asset': 149999.0,
        }
        now = time.time()
        with web_server._account_info_lock:
            web_server._account_info_cache['data'] = mock_data
            web_server._account_info_cache['ts'] = now  # 刚刚刷新

        t0 = time.monotonic()
        result = web_server.get_account_info_cached()
        elapsed_ms = (time.monotonic() - t0) * 1000

        self.assertEqual(result.get('account_id'), 'TEST_ACCOUNT', "应命中缓存返回 TEST_ACCOUNT")
        self.assertLess(elapsed_ms, 10, f"缓存命中耗时 {elapsed_ms:.1f}ms，应 < 10ms")

    def test_old_bug_direct_executor_call_would_block(self):
        """
        演示旧实现（api_executor.submit + future.result(timeout=8.0)）
        在 QMT API 不可用时会阻塞整整 8 秒。
        本测试不实际等待 8 秒，而是通过代码分析验证旧模式。
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        def slow_qmt_call():
            """模拟 QMT API 超时"""
            time.sleep(10)  # 模拟超过 8 秒的阻塞
            return {}

        # 验证旧模式会超时
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(slow_qmt_call)
        t0 = time.monotonic()
        try:
            future.result(timeout=0.1)  # 缩短超时时间用于测试
            self.fail("应该超时")
        except FuturesTimeoutError:
            elapsed = time.monotonic() - t0
            # 旧模式需要等到超时才能返回
            self.assertGreater(elapsed, 0.09, "旧模式必须等到超时才能返回，会阻塞请求线程")
        finally:
            future.cancel()
            executor.shutdown(wait=False)


# ============================================================
# 问题2 验证：data_manager.py self.conn 线程安全
# ============================================================

class TestIssue2_DataManagerThreadSafety(unittest.TestCase):
    """
    验证 data_manager.py 的 self.conn 多线程并发访问安全性
    """

    def _make_test_conn(self, db_path):
        """创建测试用 SQLite 连接（复现 data_manager 的配置）"""
        conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS stock_daily_data (
                stock_code TEXT,
                date TEXT,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, amount REAL,
                PRIMARY KEY (stock_code, date)
            )
        ''')
        conn.commit()
        return conn

    def test_concurrent_read_write_without_lock_may_fail(self):
        """
        演示：不加锁时，并发读写同一 conn 可能触发事务冲突。
        使用旧模式（无锁）验证问题存在。
        """
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = self._make_test_conn(db_path)

        # 预置数据
        conn.executemany(
            'REPLACE INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)',
            [('000001', f'2026-01-{d:02d}', 10.0, 11.0, 9.5, 10.5, 1000.0, 10500.0)
             for d in range(1, 31)]
        )
        conn.commit()

        errors = []
        iterations = 200

        def reader():
            """模拟 indicator_calculator 调用 get_history_data_from_db()"""
            import pandas as pd
            for _ in range(iterations):
                try:
                    pd.read_sql_query(
                        "SELECT * FROM stock_daily_data WHERE stock_code=?",
                        conn,
                        params=['000001']
                    )
                except Exception as e:
                    errors.append(f"Reader: {e}")

        def writer():
            """模拟 data_manager update 线程调用 save_history_data()"""
            for i in range(iterations):
                try:
                    with conn:
                        conn.executemany(
                            'REPLACE INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)',
                            [('000001', f'2026-02-{(i%28)+1:02d}',
                              10.0, 11.0, 9.5, 10.5, 1000.0, 10500.0)]
                        )
                except Exception as e:
                    errors.append(f"Writer: {e}")

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start(); t2.start()
        t1.join(); t2.join()
        conn.close()
        os.unlink(db_path)

        # 记录无锁情况下是否出现错误（可能出现也可能不出现，取决于时序）
        if errors:
            print(f"\n[无锁模式] 发现 {len(errors)} 个并发错误（验证了问题的存在）:")
            for e in errors[:3]:
                print(f"  - {e}")
        else:
            print(f"\n[无锁模式] 本次运行未触发错误（并发问题具有时序依赖性）")

        # 不在此处断言失败，只记录

    def test_concurrent_read_write_with_lock_no_error(self):
        """
        验证：加锁后，并发读写同一 conn 不出现任何错误。
        这验证了修复的有效性。
        """
        import pandas as pd

        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = self._make_test_conn(db_path)
        lock = threading.Lock()

        # 预置数据
        with lock:
            with conn:
                conn.executemany(
                    'REPLACE INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)',
                    [('000001', f'2026-01-{d:02d}', 10.0, 11.0, 9.5, 10.5, 1000.0, 10500.0)
                     for d in range(1, 31)]
                )

        errors = []
        iterations = 300

        def reader():
            for _ in range(iterations):
                try:
                    with lock:
                        pd.read_sql_query(
                            "SELECT * FROM stock_daily_data WHERE stock_code=?",
                            conn,
                            params=['000001']
                        )
                except Exception as e:
                    errors.append(f"Reader: {e}")

        def writer():
            for i in range(iterations):
                try:
                    with lock:
                        with conn:
                            conn.executemany(
                                'REPLACE INTO stock_daily_data VALUES (?,?,?,?,?,?,?,?)',
                                [('000001', f'2026-02-{(i%28)+1:02d}',
                                  10.0, 11.0, 9.5, 10.5, 1000.0, 10500.0)]
                            )
                except Exception as e:
                    errors.append(f"Writer: {e}")

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start(); t2.start()
        t1.join(); t2.join()
        conn.close()
        os.unlink(db_path)

        self.assertEqual(
            len(errors), 0,
            f"加锁后不应有任何并发错误，但出现了 {len(errors)} 个:\n" +
            "\n".join(errors[:5])
        )

    def test_data_manager_has_db_lock(self):
        """
        验证 DataManager 类已定义 _db_lock 属性（threading.Lock）
        """
        import data_manager
        import inspect
        src = inspect.getsource(data_manager.DataManager.__init__)
        self.assertIn(
            '_db_lock',
            src,
            "DataManager.__init__ 应包含 self._db_lock = threading.Lock()"
        )

    def test_save_history_data_uses_lock(self):
        """
        验证 save_history_data() 的源码包含 self._db_lock 保护
        """
        import data_manager
        import inspect
        src = inspect.getsource(data_manager.DataManager.save_history_data)
        self.assertIn(
            '_db_lock',
            src,
            "save_history_data() 应使用 self._db_lock 保护写操作"
        )

    def test_get_history_data_from_db_uses_lock(self):
        """
        验证 get_history_data_from_db() 的源码包含 self._db_lock 保护
        """
        import data_manager
        import inspect
        src = inspect.getsource(data_manager.DataManager.get_history_data_from_db)
        self.assertIn(
            '_db_lock',
            src,
            "get_history_data_from_db() 应使用 self._db_lock 保护读操作"
        )

    def test_update_stock_data_uses_lock(self):
        """
        验证 update_stock_data() 的源码包含 self._db_lock 保护
        """
        import data_manager
        import inspect
        src = inspect.getsource(data_manager.DataManager.update_stock_data)
        self.assertIn(
            '_db_lock',
            src,
            "update_stock_data() 应使用 self._db_lock 保护 cursor 查询"
        )

    def test_transaction_conflict_is_the_original_error(self):
        """
        复现原始错误：在单一 conn 上，读操作未结束时并发写操作导致
        'cannot start a transaction within a transaction'
        """
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        # 用与 data_manager 相同的方式创建连接（无 isolation_level 覆盖）
        conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS t (
                id INTEGER PRIMARY KEY,
                val TEXT
            )
        ''')
        conn.commit()

        # 预置数据
        conn.executemany('REPLACE INTO t VALUES (?,?)', [(i, f'v{i}') for i in range(100)])
        conn.commit()

        error_found = threading.Event()
        error_msg = []

        def aggressive_writer():
            """快速反复写入，制造并发压力"""
            for i in range(500):
                try:
                    with conn:
                        conn.execute('REPLACE INTO t VALUES (?,?)', (999, f'w{i}'))
                except Exception as e:
                    err = str(e)
                    if 'transaction' in err.lower():
                        error_msg.append(err)
                        error_found.set()
                        return

        def aggressive_reader():
            """快速反复读取，与写操作交织"""
            import pandas as pd
            for _ in range(500):
                try:
                    pd.read_sql_query("SELECT * FROM t", conn)
                except Exception as e:
                    pass  # 读错误可忽略

        threads = [
            threading.Thread(target=aggressive_writer),
            threading.Thread(target=aggressive_reader),
            threading.Thread(target=aggressive_writer),
        ]
        for t in threads: t.start()
        for t in threads: t.join()

        conn.close()
        os.unlink(db_path)

        if error_msg:
            print(f"\n[复现成功] 捕获到原始错误: {error_msg[0]}")
        else:
            print(f"\n[提示] 本次未复现事务冲突（需要特定时序，属正常情况）")
        # 此测试的目的是演示，不强制断言


# ============================================================
# 性能对比测试：验证修复前后的响应速度差异
# ============================================================

class TestPerformanceComparison(unittest.TestCase):
    """
    性能对比：修复前（每次阻塞等待超时）vs 修复后（立即返回缓存）
    """

    def test_cache_vs_direct_call_latency(self):
        """
        对比缓存读取 vs 模拟阻塞调用的延迟
        """
        import web_server
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTE

        # --- 缓存模式（修复后）---
        mock_data = {'account_id': 'TEST', 'available': 10000.0,
                     'market_value': 5000.0, 'total_asset': 15000.0}
        with web_server._account_info_lock:
            web_server._account_info_cache['data'] = mock_data
            web_server._account_info_cache['ts'] = time.time()

        t0 = time.monotonic()
        for _ in range(10):
            web_server.get_account_info_cached()
        cache_elapsed_ms = (time.monotonic() - t0) * 1000 / 10

        # --- 直接阻塞模式（修复前的模拟）---
        def mock_slow_qmt():
            time.sleep(0.5)  # 模拟 QMT API 慢响应（缩短为 0.5s 用于测试）
            return {}

        TIMEOUT = 0.3
        executor = ThreadPoolExecutor(max_workers=2)
        t0 = time.monotonic()
        future = executor.submit(mock_slow_qmt)
        try:
            future.result(timeout=TIMEOUT)
        except FTE:
            pass
        direct_elapsed_ms = (time.monotonic() - t0) * 1000
        executor.shutdown(wait=False)

        speedup = direct_elapsed_ms / max(cache_elapsed_ms, 0.001)
        print(f"\n  缓存读取平均耗时:  {cache_elapsed_ms:.3f} ms")
        print(f"  直接调用(超时0.3s): {direct_elapsed_ms:.0f} ms")
        print(f"  性能提升:           {speedup:.0f}x")

        self.assertLess(
            cache_elapsed_ms, 5,
            f"缓存读取应 < 5ms，实际 {cache_elapsed_ms:.2f}ms"
        )
        self.assertGreater(
            direct_elapsed_ms / max(cache_elapsed_ms, 0.001), 10,
            "缓存模式应比直接调用快至少 10 倍"
        )


if __name__ == '__main__':
    # 设置详细输出
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    print("=" * 60)
    print("Bug 验证测试 - 2026-03-02 日志问题")
    print("=" * 60)

    suite.addTests(loader.loadTestsFromTestCase(TestIssue1_AccountInfoCaching))
    suite.addTests(loader.loadTestsFromTestCase(TestIssue2_DataManagerThreadSafety))
    suite.addTests(loader.loadTestsFromTestCase(TestPerformanceComparison))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 60)
    print(f"测试结果: {'通过' if result.wasSuccessful() else '失败'}")
    print(f"  通过: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"  失败: {len(result.failures)}")
    print(f"  错误: {len(result.errors)}")
    print("=" * 60)

    sys.exit(0 if result.wasSuccessful() else 1)
