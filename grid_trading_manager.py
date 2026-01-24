"""
网格交易管理器模块

提供网格交易的核心功能:
- GridSession: 网格会话数据模型
- PriceTracker: 价格追踪状态机
- GridTradingManager: 网格交易管理器
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import threading
import time
import json

import config
from logger import get_logger

logger = get_logger(__name__)


@dataclass
class GridSession:
    """网格交易会话"""
    id: Optional[int] = None
    stock_code: str = ""
    status: str = "active"

    # 价格配置
    center_price: float = 0.0
    current_center_price: float = 0.0
    price_interval: float = 0.05

    # 交易配置
    position_ratio: float = 0.25
    callback_ratio: float = 0.005

    # 资金配置
    max_investment: float = 0.0
    current_investment: float = 0.0

    # 退出配置
    max_deviation: float = 0.15
    target_profit: float = 0.10
    stop_loss: float = -0.10
    end_time: Optional[datetime] = None

    # 统计数据
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    total_buy_amount: float = 0.0
    total_sell_amount: float = 0.0

    # 时间戳
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    stop_reason: Optional[str] = None

    def get_profit_ratio(self) -> float:
        """计算网格盈亏率"""
        if self.total_buy_amount == 0:
            return 0.0
        return (self.total_sell_amount - self.total_buy_amount) / self.total_buy_amount

    def get_deviation_ratio(self) -> float:
        """计算当前偏离度"""
        if self.center_price == 0 or self.current_center_price == 0:
            return 0.0
        return abs(self.current_center_price - self.center_price) / self.center_price

    def get_grid_levels(self) -> dict:
        """生成当前网格档位"""
        center = self.current_center_price or self.center_price
        return {
            'lower': center * (1 - self.price_interval),
            'center': center,
            'upper': center * (1 + self.price_interval)
        }


@dataclass
class PriceTracker:
    """价格追踪器,用于检测回调"""
    session_id: int
    last_price: float = 0.0
    peak_price: float = 0.0
    valley_price: float = 0.0
    direction: Optional[str] = None
    crossed_level: Optional[float] = None
    waiting_callback: bool = False

    def update_price(self, new_price: float):
        """更新价格并追踪峰谷值"""
        self.last_price = new_price

        if self.waiting_callback:
            if self.direction == 'rising' and new_price > self.peak_price:
                self.peak_price = new_price
            elif self.direction == 'falling' and new_price < self.valley_price:
                self.valley_price = new_price

    def check_callback(self, callback_ratio: float) -> Optional[str]:
        """检查是否触发回调,返回信号类型"""
        if not self.waiting_callback:
            return None

        if self.direction == 'rising':
            ratio = (self.peak_price - self.last_price) / self.peak_price
            if ratio >= callback_ratio:
                return 'SELL'

        elif self.direction == 'falling':
            ratio = (self.last_price - self.valley_price) / self.valley_price
            if ratio >= callback_ratio:
                return 'BUY'

        return None

    def reset(self, price: float):
        """重置追踪器"""
        self.last_price = price
        self.peak_price = price
        self.valley_price = price
        self.direction = None
        self.crossed_level = None
        self.waiting_callback = False
