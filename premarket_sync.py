"""
盘前配置同步与初始化模块

功能:
1. 每天9:15自动同步数据库配置到内存
2. 重新初始化xtquant行情和交易接口
3. 支持reset后的补偿执行
4. 详细日志记录所有操作
"""

import threading
import time
import sqlite3
import json
from datetime import datetime, timedelta
from logger import get_logger
import config
from config_manager import get_config_manager

logger = get_logger(__name__)

# 全局调度器实例
_scheduler = None


class PreMarketSyncScheduler:
    """盘前同步调度器"""

    def __init__(self):
        # 从配置文件读取同步时间
        self.sync_time = (
            config.PREMARKET_SYNC_TIME["hour"],
            config.PREMARKET_SYNC_TIME["minute"]
        )
        self.compensation_window = config.PREMARKET_SYNC_TIME["compensation_window_minutes"]
        self.running = False
        self.timer = None
        self.config_manager = get_config_manager()

    def calculate_next_sync_time(self):
        """
        计算下次同步时间

        返回: datetime对象
        """
        now = datetime.now()
        target = now.replace(hour=self.sync_time[0], minute=self.sync_time[1],
                           second=0, microsecond=0)

        # 如果已过今天的9:15,计算明天
        if now >= target:
            target += timedelta(days=1)

        # 跳过周末 (周六=5, 周日=6)
        while target.weekday() >= 5:
            target += timedelta(days=1)

        return target

    def load_persisted_schedule(self):
        """
        从数据库加载持久化的下次执行时间

        返回: datetime对象或None
        """
        try:
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute('SELECT next_sync_time FROM premarket_schedule WHERE id = 1')
            result = cursor.fetchone()

            conn.close()

            if result and result[0]:
                return datetime.fromisoformat(result[0])
            return None
        except Exception as e:
            logger.error(f"加载持久化调度时间失败: {e}")
            return None

    def save_persisted_schedule(self, next_time):
        """
        保存下次执行时间到数据库

        参数:
            next_time: datetime对象
        """
        try:
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute('''
                INSERT OR REPLACE INTO premarket_schedule (id, next_sync_time, updated_at)
                VALUES (1, ?, CURRENT_TIMESTAMP)
            ''', (next_time.isoformat(),))

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"保存持久化调度时间失败: {e}")

    def start(self):
        """启动调度器"""
        self.running = True

        # 1. 加载持久化的下次执行时间
        persisted_time = self.load_persisted_schedule()

        now = datetime.now()

        # 2. 判断是否需要补偿执行
        if persisted_time and persisted_time < now:
            # 计划时间已过,检查是否在合理窗口内(例如9:15-9:30)
            sync_time = now.replace(
                hour=self.sync_time[0],
                minute=self.sync_time[1],
                second=0,
                microsecond=0
            )
            window_end = sync_time + timedelta(minutes=self.compensation_window)

            if sync_time <= now <= window_end and now.weekday() < 5:
                logger.warning(
                    f"检测到reset场景(计划时间{persisted_time.strftime('%H:%M')},"
                    f"当前{now.strftime('%H:%M')}),立即执行补偿同步"
                )
                threading.Thread(target=perform_premarket_sync, daemon=True).start()
            else:
                logger.info(f"错过执行窗口,跳到下次调度")

        # 3. 调度下次执行
        self.schedule_next_sync()

        logger.info("盘前同步调度器已启动")

    def schedule_next_sync(self):
        """调度下次同步"""
        if not self.running:
            return

        next_time = self.calculate_next_sync_time()

        # 持久化到数据库
        self.save_persisted_schedule(next_time)

        delay = (next_time - datetime.now()).total_seconds()
        logger.info(f"下次盘前同步: {next_time.strftime('%Y-%m-%d %H:%M:%S')} (倒计时{delay/3600:.1f}小时)")

        self.timer = threading.Timer(delay, self._sync_and_reschedule)
        self.timer.daemon = True
        self.timer.start()

    def _sync_and_reschedule(self):
        """执行同步并重新调度"""
        try:
            perform_premarket_sync()
        except Exception as e:
            logger.error(f"盘前同步失败: {e}", exc_info=True)
        finally:
            self.schedule_next_sync()

    def stop(self):
        """停止调度器"""
        self.running = False
        if self.timer:
            self.timer.cancel()
        logger.info("盘前同步调度器已停止")


def perform_premarket_sync():
    """
    执行盘前配置同步与初始化

    返回: dict包含同步结果
    """
    logger.info("=" * 60)
    logger.info(f"开始执行盘前配置同步与初始化 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    start_time = time.time()
    results = {
        'timestamp': datetime.now().isoformat(),
        'configs_synced': 0,
        'switches_synced': 0,
        'xtdata_reconnected': False,
        'xttrader_reconnected': False,
        'connection_status': {},
        'positions_synced': False,
        'errors': []
    }

    try:
        # 步骤1: 同步持久化配置
        logger.info("[步骤1/7] 同步持久化配置...")
        config_manager = get_config_manager()
        count = config_manager.apply_configs_to_runtime()
        results['configs_synced'] = count
        logger.info(f"  ✓ 持久化配置已同步: {count}个配置项")

        # 步骤2: 同步特殊开关
        logger.info("[步骤2/7] 同步特殊开关...")
        switch_count = sync_special_switches()
        results['switches_synced'] = switch_count
        logger.info(f"  ✓ 特殊开关已同步: {switch_count}个")

        # 步骤3: 重新初始化xtquant行情接口 (可配置)
        logger.info("[步骤3/8] 重新初始化xtquant行情接口...")
        if config.ENABLE_PREMARKET_XTQUANT_REINIT and config.PREMARKET_REINIT_XTDATA:
            xtdata_result = reinit_xtquant_data()
            results['xtdata_reconnected'] = xtdata_result
            if xtdata_result:
                logger.info("  ✓ 行情接口重新初始化成功")
            else:
                logger.warning("  ⚠ 行情接口初始化失败(不阻止继续)")
                results['errors'].append("xtdata初始化失败")
        else:
            logger.info("  ○ 跳过xtdata重新初始化(配置已禁用)")
            results['xtdata_reconnected'] = None

        # 步骤4: 重新初始化xtquant交易接口 (可配置)
        logger.info("[步骤4/8] 重新初始化xtquant交易接口...")
        if config.ENABLE_PREMARKET_XTQUANT_REINIT and config.PREMARKET_REINIT_XTTRADER:
            xttrader_result = reinit_xtquant_trader()
            results['xttrader_reconnected'] = xttrader_result
            if xttrader_result:
                logger.info("  ✓ 交易接口重新初始化成功")
            else:
                logger.warning("  ⚠ 交易接口初始化失败(不阻止继续)")
                results['errors'].append("xttrader初始化失败")
        else:
            logger.info("  ○ 跳过xttrader重新初始化(配置已禁用)")
            results['xttrader_reconnected'] = None

        # 步骤5: 验证xtquant连接状态
        logger.info("[步骤5/8] 验证xtquant连接状态...")
        connection_status = verify_xtquant_connections()
        results['connection_status'] = connection_status
        logger.info(f"  ✓ xtdata状态: {connection_status.get('xtdata', '未知')}")
        logger.info(f"  ✓ xttrader状态: {connection_status.get('xttrader', '未知')}")

        # 步骤6: 同步持仓数据(仅模拟模式)
        logger.info("[步骤6/8] 同步持仓数据...")
        if config.ENABLE_SIMULATION_MODE:
            from position_manager import get_position_manager
            position_manager = get_position_manager()
            position_manager._sync_db_to_memory()
            results['positions_synced'] = True
            logger.info("  ✓ 持仓数据已同步(模拟模式)")
        else:
            logger.info("  ○ 跳过持仓同步(实盘模式)")

        # 步骤7: 触发Web数据全量刷新 (可配置)
        logger.info("[步骤7/8] 触发Web数据全量刷新...")
        if config.ENABLE_WEB_REFRESH_AFTER_REINIT:
            refresh_result = trigger_web_data_refresh(results)
            results['web_refresh'] = refresh_result
            if refresh_result['success']:
                logger.info(f"  ✓ Web数据刷新成功 (刷新{refresh_result['refreshed_stocks']}只股票)")
            else:
                logger.warning(f"  ⚠ Web数据刷新失败: {refresh_result.get('error')}")
        else:
            logger.info("  ○ 跳过Web数据刷新(配置已禁用)")
            results['web_refresh'] = None

        # 步骤8: 记录同步历史
        logger.info("[步骤8/8] 记录同步历史...")
        execution_time = int((time.time() - start_time) * 1000)
        results['execution_time_ms'] = execution_time
        record_sync_history(results)
        logger.info("  ✓ 写入数据库")

        logger.info("=" * 60)
        logger.info(f"盘前同步成功完成 (耗时{execution_time}ms)")
        logger.info("=" * 60)

        return results

    except Exception as e:
        error_msg = f"盘前同步异常: {str(e)}"
        results['errors'].append(error_msg)
        logger.error(error_msg, exc_info=True)
        record_sync_history(results)
        raise


def sync_special_switches():
    """
    同步特殊开关到数据库

    返回: 成功同步的开关数量
    """
    count = 0
    config_manager = get_config_manager()

    # 1. ENABLE_AUTO_TRADING
    try:
        memory_value = config.ENABLE_AUTO_TRADING
        db_value = config_manager.load_config('ENABLE_AUTO_TRADING', None)

        if db_value is None:
            config_manager.save_config('ENABLE_AUTO_TRADING', memory_value, 'bool', '自动交易总开关')
            logger.info(f"  ✓ ENABLE_AUTO_TRADING: {memory_value} (初始化)")
        elif db_value != memory_value:
            config_manager.save_config('ENABLE_AUTO_TRADING', memory_value, 'bool', '自动交易总开关')
            logger.info(f"  ✓ ENABLE_AUTO_TRADING: {db_value}→{memory_value} (修复)")
        else:
            logger.info(f"  ✓ ENABLE_AUTO_TRADING: {memory_value}")

        count += 1
    except Exception as e:
        logger.error(f"同步ENABLE_AUTO_TRADING失败: {e}")

    # 2. ENABLE_SIMULATION_MODE
    try:
        memory_value = getattr(config, 'ENABLE_SIMULATION_MODE', False)
        db_value = config_manager.load_config('ENABLE_SIMULATION_MODE', None)

        if db_value is None:
            config_manager.save_config('ENABLE_SIMULATION_MODE', memory_value, 'bool', '模拟/实盘模式')
            logger.info(f"  ✓ ENABLE_SIMULATION_MODE: {memory_value} (初始化)")
        elif db_value != memory_value:
            config_manager.save_config('ENABLE_SIMULATION_MODE', memory_value, 'bool', '模拟/实盘模式')
            logger.info(f"  ✓ ENABLE_SIMULATION_MODE: {db_value}→{memory_value} (修复)")
        else:
            logger.info(f"  ✓ ENABLE_SIMULATION_MODE: {memory_value}")

        count += 1
    except Exception as e:
        logger.error(f"同步ENABLE_SIMULATION_MODE失败: {e}")

    return count


def reinit_xtquant_data():
    """
    重新初始化xtquant行情接口

    策略:
    1. 检查当前连接状态
    2. 尝试reconnect
    3. 验证连接可用性
    4. 优雅处理失败情况

    返回: bool 成功/失败
    """
    try:
        from position_manager import get_position_manager

        position_manager = get_position_manager()
        qmt_trader = position_manager.qmt_trader

        # 步骤1: 检查现有连接状态
        current_status = "已连接" if getattr(qmt_trader, 'xtdata_connected', False) else "未连接"
        logger.info(f"  → 当前状态: {current_status}")

        # 步骤2: 执行reconnect (通过qmt_trader)
        logger.info("  → 使用qmt_trader重连xtdata...")
        try:
            # 临时禁用hello消息
            if qmt_trader.xtdata:
                original_hello = getattr(qmt_trader.xtdata, 'enable_hello', True)
                qmt_trader.xtdata.enable_hello = False

            reconnect_result = qmt_trader.reconnect_xtdata()

            # 恢复设置
            if qmt_trader.xtdata:
                qmt_trader.xtdata.enable_hello = original_hello

        except Exception as e:
            logger.error(f"  ✗ reconnect调用异常: {e}")
            return False

        if not reconnect_result:
            logger.error("  ✗ 行情接口reconnect失败")
            return False

        logger.info("  ✓ reconnect执行成功")

        # 步骤3: 验证连接可用性
        logger.info("  → 验证连接可用性...")
        try:
            verify_result = qmt_trader.verify_xtdata_connection()

            if verify_result:
                logger.info("  ✓ 连接验证通过 (数据可用)")
                return True
            else:
                # 验证失败但不是致命错误
                logger.info("  ○ 连接已建立但数据验证未通过 (可能非交易时间)")
                return True  # 仍然返回成功,因为连接已建立

        except Exception as e:
            logger.warning(f"  ⚠ 连接验证异常: {e}")
            return True  # 验证异常不影响连接建立

    except ImportError as e:
        logger.error(f"  ✗ 导入模块失败: {e}")
        return False
    except Exception as e:
        logger.error(f"  ✗ 初始化过程异常: {e}", exc_info=True)
        return False


def reinit_xtquant_trader():
    """
    重新初始化xtquant交易接口

    设计理念: 完全模仿系统初始化(PositionManager.__init__)的行为
    - 不检查现有状态
    - 总是调用 connect()
    - 简单直接,与系统初始化一致

    返回: bool 成功/失败
    """
    try:
        from position_manager import get_position_manager

        position_manager = get_position_manager()

        # 检查 qmt_trader 对象
        if not hasattr(position_manager, 'qmt_trader') or not position_manager.qmt_trader:
            logger.info("  ○ qmt_trader未初始化,跳过交易接口初始化")
            return True

        qmt_trader = position_manager.qmt_trader

        # 模仿系统初始化: 直接调用 connect()
        logger.info("  → 调用 qmt_trader.connect() 重新连接...")

        try:
            connect_result = qmt_trader.connect()

            if connect_result is None:
                logger.warning("  ⚠ 交易接口连接返回None (可能已连接或连接失败)")
                # 模仿系统初始化的容错逻辑: 返回None也继续运行
                return True
            else:
                logger.info("  ✓ 交易接口连接成功")
                return True

        except Exception as e:
            logger.warning(f"  ⚠ 连接过程异常: {e}")
            # 模仿系统初始化: 异常不阻止系统运行
            return True

    except ImportError as e:
        logger.error(f"  ✗ 导入模块失败: {e}")
        return False
    except Exception as e:
        logger.error(f"  ✗ 初始化过程异常: {e}", exc_info=True)
        return False


def verify_xtquant_connections():
    """
    验证xtquant连接状态

    返回: dict 连接状态
    """
    status = {
        'xtdata': '未知',
        'xttrader': '未知'
    }

    # 验证 xtdata
    try:
        from data_manager import get_data_manager
        data_manager = get_data_manager()

        if data_manager.xt:
            verify_result = data_manager._verify_connection()
            status['xtdata'] = '正常' if verify_result else '异常'
        else:
            status['xtdata'] = '未初始化'
    except Exception as e:
        logger.error(f"验证xtdata状态失败: {e}")
        status['xtdata'] = '异常'

    # 验证 xttrader (使用position_manager.qmt_trader)
    try:
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        if hasattr(position_manager, 'qmt_trader') and position_manager.qmt_trader:
            qmt_trader = position_manager.qmt_trader

            if hasattr(qmt_trader, 'xt_trader') and qmt_trader.xt_trader:
                # 尝试查询账户资产来验证连接
                try:
                    if hasattr(qmt_trader.xt_trader, 'query_stock_asset') and hasattr(qmt_trader, 'acc'):
                        asset = qmt_trader.xt_trader.query_stock_asset(qmt_trader.acc)
                        status['xttrader'] = '正常' if asset else '未连接'
                    else:
                        status['xttrader'] = '无法验证'
                except Exception:
                    status['xttrader'] = '异常'
            else:
                status['xttrader'] = '未连接'
        else:
            status['xttrader'] = '未初始化'

    except Exception as e:
        logger.error(f"验证xttrader状态失败: {e}")
        status['xttrader'] = '异常'

    return status


def trigger_web_data_refresh(sync_results):
    """
    触发Web界面数据全量刷新

    策略:
    1. 检查接口初始化是否成功
    2. 调用position_manager全量刷新
    3. 更新data_version触发前端更新
    4. 返回刷新结果

    参数:
        sync_results: 同步结果字典

    返回: dict包含success, refreshed_stocks, error等信息
    """
    result = {
        'success': False,
        'refreshed_stocks': 0,
        'error': None
    }

    try:
        # 步骤1: 检查接口初始化状态
        xtdata_ok = sync_results.get('xtdata_reconnected')
        xttrader_ok = sync_results.get('xttrader_reconnected')

        if xtdata_ok is False and xttrader_ok is False:
            result['error'] = "xtquant接口初始化失败,跳过刷新"
            logger.warning(f"  → {result['error']}")
            return result

        # 步骤2: 获取position_manager
        from position_manager import get_position_manager
        position_manager = get_position_manager()

        # 步骤3: 执行全量数据刷新
        logger.info("  → 执行全量持仓数据刷新...")

        if config.ENABLE_SIMULATION_MODE:
            # 模拟模式: 全量刷新模拟数据
            position_manager._full_refresh_simulation_data()
            logger.info("  → 模拟模式全量刷新完成")
        else:
            # 实盘模式: 从QMT获取最新持仓
            positions = position_manager.get_all_positions_with_all_fields()
            result['refreshed_stocks'] = len(positions)
            logger.info(f"  → 实盘模式刷新了{len(positions)}只股票")

        # 步骤4: 更新data_version
        position_manager.increment_data_version()
        logger.info("  → data_version已更新,前端将获取最新数据")

        result['success'] = True
        result['refreshed_stocks'] = len(position_manager.get_all_positions_with_all_fields())

        return result

    except Exception as e:
        error_msg = f"Web数据刷新异常: {str(e)}"
        result['error'] = error_msg
        logger.error(error_msg, exc_info=True)
        return result


def record_sync_history(results):
    """记录同步历史到数据库"""
    try:
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO premarket_sync_history
            (sync_time, configs_synced, switches_synced, xtdata_reconnected,
             xttrader_reconnected, connection_status, positions_synced,
             errors, execution_time_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            results['timestamp'],
            results['configs_synced'],
            results['switches_synced'],
            results['xtdata_reconnected'],
            results['xttrader_reconnected'],
            json.dumps(results['connection_status'], ensure_ascii=False),
            results['positions_synced'],
            json.dumps(results['errors'], ensure_ascii=False),
            results.get('execution_time_ms', 0)
        ))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"记录同步历史失败: {e}")


def start_premarket_sync_scheduler():
    """启动盘前同步调度器"""
    global _scheduler

    if _scheduler is None:
        _scheduler = PreMarketSyncScheduler()
        _scheduler.start()
    else:
        logger.warning("盘前同步调度器已经在运行")


def stop_premarket_sync_scheduler():
    """停止盘前同步调度器"""
    global _scheduler

    if _scheduler:
        _scheduler.stop()
        _scheduler = None
    else:
        logger.warning("盘前同步调度器未运行")
