"""pytest 全局配置：确保测试与开发者本地 .env 隔离。

miniQMT config.py 在 import 时会把项目根 .env 补进未设置的环境变量（fallback）。
测试必须可复现、不能随本地 .env（可能含真实 QMT_API_TOKEN / TUSHARE_TOKEN 等）漂移，
因此在收集测试前禁用默认路径的 .env 加载。conftest 由 pytest 在任何 import config
之前自动加载，是设置该开关的最早时机。
"""
import os

os.environ.setdefault("MINIQMT_DISABLE_DOTENV", "1")
