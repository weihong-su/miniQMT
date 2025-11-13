"""
配置管理模块 - 实现Web配置项的数据库持久化
"""
import sqlite3
import json
import threading
from datetime import datetime
from logger import get_logger
import config

logger = get_logger("config_manager")

class ConfigManager:
    """配置管理器，负责配置的持久化和加载"""

    def __init__(self, db_path=None):
        """
        初始化配置管理器

        参数:
            db_path: 数据库路径，默认使用config.DB_PATH
        """
        self.db_path = db_path or config.DB_PATH
        self.lock = threading.Lock()
        self._init_db()
        logger.info("配置管理器初始化完成")

    def _init_db(self):
        """初始化数据库表结构"""
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            # 创建配置表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_config (
                    config_key TEXT PRIMARY KEY,
                    config_value TEXT NOT NULL,
                    config_type TEXT NOT NULL,
                    description TEXT,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 创建配置历史表（用于审计）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_key TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    changed_by TEXT DEFAULT 'web_interface'
                )
            ''')

            conn.commit()
            conn.close()
            logger.info("配置数据库表初始化完成")
        except Exception as e:
            logger.error(f"初始化配置数据库失败: {str(e)}")
            raise

    def save_config(self, config_key, config_value, config_type=None, description=None):
        """
        保存单个配置项到数据库

        参数:
            config_key: 配置键名
            config_value: 配置值
            config_type: 配置类型（int, float, bool, str）
            description: 配置描述
        """
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()

                # 获取旧值用于历史记录
                cursor.execute("SELECT config_value FROM system_config WHERE config_key = ?", (config_key,))
                result = cursor.fetchone()
                old_value = result[0] if result else None

                # 自动推断类型
                if config_type is None:
                    if isinstance(config_value, bool):
                        config_type = 'bool'
                    elif isinstance(config_value, int):
                        config_type = 'int'
                    elif isinstance(config_value, float):
                        config_type = 'float'
                    else:
                        config_type = 'str'

                # 将值转换为字符串存储
                value_str = json.dumps(config_value)

                # 插入或更新配置
                cursor.execute('''
                    INSERT OR REPLACE INTO system_config
                    (config_key, config_value, config_type, description, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                ''', (config_key, value_str, config_type, description, datetime.now()))

                # 记录配置变更历史
                if old_value != value_str:
                    cursor.execute('''
                        INSERT INTO config_history (config_key, old_value, new_value)
                        VALUES (?, ?, ?)
                    ''', (config_key, old_value, value_str))

                conn.commit()
                conn.close()

                logger.debug(f"配置已保存: {config_key} = {config_value}")
                return True
            except Exception as e:
                logger.error(f"保存配置失败 {config_key}: {str(e)}")
                return False

    def load_config(self, config_key, default_value=None):
        """
        从数据库加载单个配置项

        参数:
            config_key: 配置键名
            default_value: 默认值

        返回:
            配置值，如果不存在则返回默认值
        """
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT config_value, config_type FROM system_config
                WHERE config_key = ?
            ''', (config_key,))

            result = cursor.fetchone()
            conn.close()

            if result:
                value_str, config_type = result
                # 反序列化值
                value = json.loads(value_str)

                # 类型转换（JSON可能改变类型）
                if config_type == 'bool':
                    return bool(value)
                elif config_type == 'int':
                    return int(value)
                elif config_type == 'float':
                    return float(value)
                else:
                    return value
            else:
                return default_value
        except Exception as e:
            logger.error(f"加载配置失败 {config_key}: {str(e)}")
            return default_value

    def load_all_configs(self):
        """
        加载所有配置项

        返回:
            dict: 配置字典 {config_key: config_value}
        """
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            cursor.execute('SELECT config_key, config_value, config_type FROM system_config')
            results = cursor.fetchall()
            conn.close()

            configs = {}
            for config_key, value_str, config_type in results:
                try:
                    value = json.loads(value_str)

                    # 类型转换
                    if config_type == 'bool':
                        configs[config_key] = bool(value)
                    elif config_type == 'int':
                        configs[config_key] = int(value)
                    elif config_type == 'float':
                        configs[config_key] = float(value)
                    else:
                        configs[config_key] = value
                except Exception as e:
                    logger.warning(f"解析配置项 {config_key} 失败: {str(e)}")
                    continue

            logger.info(f"成功加载 {len(configs)} 个配置项")
            return configs
        except Exception as e:
            logger.error(f"加载所有配置失败: {str(e)}")
            return {}

    def save_batch_configs(self, configs_dict):
        """
        批量保存配置项

        参数:
            configs_dict: 配置字典 {config_key: config_value}
        """
        success_count = 0
        fail_count = 0

        for config_key, config_value in configs_dict.items():
            if self.save_config(config_key, config_value):
                success_count += 1
            else:
                fail_count += 1

        logger.info(f"批量保存配置完成: 成功 {success_count}, 失败 {fail_count}")
        return success_count, fail_count

    def delete_config(self, config_key):
        """
        删除配置项

        参数:
            config_key: 配置键名
        """
        with self.lock:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cursor = conn.cursor()

                cursor.execute('DELETE FROM system_config WHERE config_key = ?', (config_key,))
                conn.commit()
                conn.close()

                logger.info(f"配置已删除: {config_key}")
                return True
            except Exception as e:
                logger.error(f"删除配置失败 {config_key}: {str(e)}")
                return False

    def get_config_history(self, config_key=None, limit=50):
        """
        获取配置变更历史

        参数:
            config_key: 配置键名，None表示获取所有配置的历史
            limit: 返回记录数量限制

        返回:
            list: 历史记录列表
        """
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            if config_key:
                cursor.execute('''
                    SELECT config_key, old_value, new_value, changed_at, changed_by
                    FROM config_history
                    WHERE config_key = ?
                    ORDER BY changed_at DESC
                    LIMIT ?
                ''', (config_key, limit))
            else:
                cursor.execute('''
                    SELECT config_key, old_value, new_value, changed_at, changed_by
                    FROM config_history
                    ORDER BY changed_at DESC
                    LIMIT ?
                ''', (limit,))

            results = cursor.fetchall()
            conn.close()

            history = []
            for row in results:
                history.append({
                    'config_key': row[0],
                    'old_value': row[1],
                    'new_value': row[2],
                    'changed_at': row[3],
                    'changed_by': row[4]
                })

            return history
        except Exception as e:
            logger.error(f"获取配置历史失败: {str(e)}")
            return []

    def apply_configs_to_runtime(self):
        """
        将数据库中的配置应用到运行时config模块

        返回:
            int: 成功应用的配置数量
        """
        configs = self.load_all_configs()
        applied_count = 0

        # 配置项映射关系（数据库键名 -> config模块属性名）
        # 注意：ENABLE_AUTO_TRADING 和 ENABLE_SIMULATION_MODE 不持久化
        # 理由：为了安全，这两个关键开关每次启动需手动确认
        config_mapping = {
            'POSITION_UNIT': 'POSITION_UNIT',
            'INITIAL_TAKE_PROFIT_RATIO': 'INITIAL_TAKE_PROFIT_RATIO',
            'ENABLE_DYNAMIC_STOP_PROFIT': 'ENABLE_DYNAMIC_STOP_PROFIT',
            'INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE': 'INITIAL_TAKE_PROFIT_RATIO_PERCENTAGE',
            'STOP_LOSS_RATIO': 'STOP_LOSS_RATIO',
            'MAX_POSITION_VALUE': 'MAX_POSITION_VALUE',
            'MAX_TOTAL_POSITION_RATIO': 'MAX_TOTAL_POSITION_RATIO',
            'ENABLE_ALLOW_BUY': 'ENABLE_ALLOW_BUY',
            'ENABLE_ALLOW_SELL': 'ENABLE_ALLOW_SELL',
            # 'ENABLE_AUTO_TRADING': 'ENABLE_AUTO_TRADING',  # 不持久化
            # 'ENABLE_SIMULATION_MODE': 'ENABLE_SIMULATION_MODE',  # 不持久化
            'ENABLE_STOP_LOSS_BUY': 'ENABLE_STOP_LOSS_BUY',
            'WEB_SERVER_PORT': 'WEB_SERVER_PORT',
            'BUY_GRID_LEVEL_1': 'BUY_GRID_LEVELS'  # 特殊处理
        }

        for db_key, config_attr in config_mapping.items():
            if db_key in configs:
                try:
                    value = configs[db_key]

                    # 特殊处理网格配置
                    if db_key == 'BUY_GRID_LEVEL_1':
                        # 更新第二个网格级别
                        if hasattr(config, 'BUY_GRID_LEVELS') and len(config.BUY_GRID_LEVELS) > 1:
                            config.BUY_GRID_LEVELS[1] = value
                            logger.info(f"应用配置: BUY_GRID_LEVELS[1] = {value}")
                            applied_count += 1
                    else:
                        # 正常属性设置
                        setattr(config, config_attr, value)
                        logger.info(f"应用配置: {config_attr} = {value}")
                        applied_count += 1
                except Exception as e:
                    logger.error(f"应用配置 {db_key} 失败: {str(e)}")

        logger.info(f"成功应用 {applied_count} 个配置项到运行时")
        return applied_count


# 全局单例
_config_manager_instance = None

def get_config_manager():
    """获取配置管理器单例"""
    global _config_manager_instance
    if _config_manager_instance is None:
        _config_manager_instance = ConfigManager()
    return _config_manager_instance
