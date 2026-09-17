"""
线程健康监控与自愈模块
自动检测线程崩溃/假死,并自动重启
实现系统"无人值守"持续运行
"""
import time
import threading
from datetime import datetime

import config
from logger import get_logger

logger = get_logger("thread_monitor")


class ThreadHealthMonitor:
    """线程健康监控器"""

    def __init__(self):
        """初始化"""
        self.monitored_threads = {}
        self.health_check_interval = config.THREAD_CHECK_INTERVAL  # 线程检查间隔(秒)
        self.monitor_thread = None
        self.stop_event = threading.Event()
        # 统计信息
        self.total_restarts = 0
        self.restart_history = []

    def register_thread(self, name, thread_getter, restart_func, heartbeat_check=None):
        """注册需要监控的线程

        Args:
            name: 线程名称
            thread_getter: 获取thread对象的函数(而非直接传thread对象,因为重启后对象会变)
            restart_func: 重启函数(线程崩溃时调用)
            heartbeat_check: 心跳检查函数(可选,检测假死)
        """
        self.monitored_threads[name] = {
            'thread_getter': thread_getter,
            'restart_func': restart_func,
            'heartbeat_check': heartbeat_check,
            'restart_count': 0,
            'last_restart_time': 0,
            'last_check_alive': True  # 上次检查时是否存活
        }
        logger.info(f"✅ 已注册线程监控: {name}")

    def _monitor_loop(self):
        """监控循环"""
        logger.info("🚀 线程健康监控已启动")
        while not self.stop_event.is_set():
            try:
                if self.stop_event.wait(self.health_check_interval):
                    break
                for name, info in self.monitored_threads.items():
                    try:
                        # 获取当前thread对象
                        thread = info['thread_getter']()

                        # 检查1: 线程是否存活
                        if not thread or not thread.is_alive():
                            if info['last_check_alive']:
                                # 从存活变为停止,记录日志
                                logger.error(f"❌ 检测到 {name} 线程已停止")
                                info['last_check_alive'] = False
                            self._restart_thread(name, info, reason="线程停止")
                            continue
                        else:
                            # 线程存活
                            if not info['last_check_alive']:
                                # 从停止恢复为存活,记录日志
                                logger.info(f"✅ {name} 线程已恢复运行")
                                info['last_check_alive'] = True

                        # 检查2: 心跳检测(可选)
                        if info['heartbeat_check']:
                            try:
                                if not info['heartbeat_check']():
                                    logger.error(f"❌ 检测到 {name} 线程心跳异常")
                                    self._restart_thread(name, info, reason="心跳异常")
                            except Exception as e:
                                logger.error(f"❌ {name} 心跳检查失败: {e}")

                    except Exception as e:
                        logger.error(f"监控 {name} 时出错: {e}", exc_info=True)

            except Exception as e:
                logger.error(f"线程监控循环异常: {e}", exc_info=True)
                time.sleep(10)

        logger.info("线程健康监控已停止")

    def _restart_thread(self, name, info, reason):
        """重启线程

        Args:
            name: 线程名称
            info: 线程信息字典
            reason: 重启原因
        """
        current_time = time.time()

        # 限制重启频率(避免重启风暴)
        if current_time - info['last_restart_time'] < config.THREAD_RESTART_COOLDOWN:
            logger.warning(f"⚠ {name} 重启过于频繁(距上次{current_time - info['last_restart_time']:.1f}秒),跳过本次重启")
            return

        try:
            logger.info(f"🔄 尝试重启 {name} (原因: {reason})...")

            # 执行重启函数
            info['restart_func']()

            # 更新统计
            info['restart_count'] += 1
            info['last_restart_time'] = current_time
            self.total_restarts += 1

            # 记录历史
            self.restart_history.append({
                'timestamp': datetime.now(),
                'thread_name': name,
                'reason': reason,
                'restart_count': info['restart_count']
            })

            # 等待线程启动
            time.sleep(1)

            # 验证重启是否成功
            thread = info['thread_getter']()
            if thread and thread.is_alive():
                logger.info(f"✅ {name} 重启成功(累计重启{info['restart_count']}次)")
                info['last_check_alive'] = True
            else:
                logger.error(f"❌ {name} 重启后仍未运行")
                info['last_check_alive'] = False

        except Exception as e:
            logger.error(f"❌ {name} 重启失败: {e}", exc_info=True)

    def start(self):
        """启动监控"""
        if self.monitor_thread and self.monitor_thread.is_alive():
            logger.warning("线程监控器已在运行")
            return
        self.stop_event.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, name="ThreadHealthMonitor")
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
        logger.info("✅ 线程健康监控器已启动")

    def stop(self):
        """停止监控"""
        self.stop_event.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("线程健康监控器已停止")

    def get_status(self):
        """获取监控状态

        Returns:
            dict: 监控状态信息
        """
        thread_status = {}
        for name, info in self.monitored_threads.items():
            try:
                thread = info['thread_getter']()
                thread_status[name] = {
                    'alive': thread.is_alive() if thread else False,
                    'restart_count': info['restart_count'],
                    'last_restart_time': datetime.fromtimestamp(info['last_restart_time']).strftime('%Y-%m-%d %H:%M:%S') if info['last_restart_time'] > 0 else 'Never'
                }
            except Exception as e:
                thread_status[name] = {
                    'alive': False,
                    'error': str(e)
                }

        # 最近重启记录
        recent_restarts = [
            {
                'time': r['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                'thread': r['thread_name'],
                'reason': r['reason']
            }
            for r in self.restart_history[-10:]  # 最近10次
        ]

        return {
            'monitor_running': self.monitor_thread.is_alive() if self.monitor_thread else False,
            'total_restarts': self.total_restarts,
            'threads': thread_status,
            'recent_restarts': recent_restarts
        }


# 全局单例
_thread_monitor_instance = None
_thread_monitor_lock = threading.Lock()


def get_thread_monitor():
    """获取线程监控器的全局单例

    Returns:
        ThreadHealthMonitor: 监控器实例
    """
    global _thread_monitor_instance

    if _thread_monitor_instance is None:
        with _thread_monitor_lock:
            if _thread_monitor_instance is None:
                _thread_monitor_instance = ThreadHealthMonitor()

    return _thread_monitor_instance
