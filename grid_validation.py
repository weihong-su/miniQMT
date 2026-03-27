"""
网格交易参数校验模块

使用marshmallow进行API参数校验
"""

from marshmallow import Schema, fields, validate, ValidationError, validates_schema
from logger import get_logger

logger = get_logger(__name__)


class GridConfigSchema(Schema):
    """网格交易配置参数校验"""

    stock_code = fields.Str(
        required=True,
        validate=validate.Regexp(
            r'^\d{6}\.(SZ|SH)$',
            error='股票代码格式错误，应为6位数字+.SZ或.SH'
        )
    )

    center_price = fields.Float(
        required=False,
        validate=validate.Range(min=0.01, error='中间价必须大于0.01')
    )

    price_interval = fields.Float(
        validate=validate.Range(min=0.01, max=0.20, error='网格价格间隔必须在0.01-0.20之间（1%-20%）')
    )

    position_ratio = fields.Float(
        validate=validate.Range(min=0.01, max=1.0, error='每档交易比例必须在0.01-1.0之间（1%-100%）')
    )

    callback_ratio = fields.Float(
        validate=validate.Range(min=0.001, max=0.10, error='回调触发比例必须在0.001-0.10之间（0.1%-10%）')
    )

    max_investment = fields.Float(
        required=True,
        # VAL-3修复：严格要求 > 0。允许 max_investment=0 会创建永远无法买入的无效会话，
        # 而代码层面以 <= 0 作为无效配置标志，校验层与实现层需保持一致。
        validate=validate.Range(min=0.01, error='最大追加投入必须大于0（至少0.01元）')
    )

    max_deviation = fields.Float(
        validate=validate.Range(min=0.05, max=0.50, error='最大偏离度必须在0.05-0.50之间（5%-50%）')
    )

    target_profit = fields.Float(
        validate=validate.Range(min=0.01, max=1.0, error='目标盈利必须在0.01-1.0之间（1%-100%）')
    )

    stop_loss = fields.Float(
        # C-4修复：max 从 0 改为 -0.001，禁止 stop_loss=0（即"亏损0%就止损"）。
        # stop_loss=0 意味着首次买入后只要浮亏就触发退出，与止损的本意（容忍一定回撤）矛盾。
        # 新 max=-0.001 要求止损比例至少为 -0.1%，确保"止损"语义上确实在容忍一定亏损。
        validate=validate.Range(min=-0.50, max=-0.001, error='止损比例必须在-0.50到-0.001之间（-50%到-0.1%）')
    )

    duration_days = fields.Int(
        validate=validate.Range(min=1, max=365, error='运行时长必须在1-365天之间')
    )

    @validates_schema
    def validate_cross_fields(self, data, **kwargs):
        """跨字段校验入口"""
        self._validate_profit_and_loss(data)
        self._validate_callback_vs_interval(data)
        self._validate_investment_feasibility(data)

    def _validate_profit_and_loss(self, data):
        """验证目标盈利和止损的合理性

        VAL-2修复：精确化豁免逻辑。
        原逻辑：任意一个字段处于边界值即豁免，导致 target_profit=0.01 + stop_loss=-0.45
        这种极不合理的组合也能通过校验。
        新逻辑：仅当两者同时处于各自的极端边界（target=0.01 AND stop=-0.50）时才豁免，
        因为这是唯一一个满足各自字段约束但不满足跨字段约束的合法极端组合。

        C-3修复：使用容差比较替代精确浮点相等（==），防止 JSON 反序列化后
        浮点精度漂移（如 0.010000000000000002）导致豁免失效。
        """
        if 'target_profit' in data and 'stop_loss' in data:
            # C-3修复: 用容差 1e-9 替代精确浮点相等，防止 JSON 解析精度漂移
            _FLOAT_EPS = 1e-9
            both_at_extreme_boundary = (
                abs(data['target_profit'] - 0.01) < _FLOAT_EPS and
                abs(data['stop_loss'] - (-0.50)) < _FLOAT_EPS
            )

            if not both_at_extreme_boundary and data['target_profit'] < abs(data['stop_loss']):
                raise ValidationError('目标盈利应大于或等于止损幅度', 'target_profit')

    def _validate_callback_vs_interval(self, data):
        """C-1修复：校验回调比例必须小于网格价格间隔

        语义约束：callback_ratio 是价格从峰/谷回落的触发阈值，price_interval 是网格档位间距。
        若 callback_ratio >= price_interval，回调信号在价格尚未回到上一档时就触发，
        导致在错误方向上执行交易（如价格仍在下轨以下时就触发买入回调卖出）。
        """
        if 'callback_ratio' in data and 'price_interval' in data:
            if data['callback_ratio'] >= data['price_interval']:
                raise ValidationError(
                    f'回调比例({data["callback_ratio"]})必须小于网格价格间隔({data["price_interval"]})，'
                    '否则信号触发方向与网格逻辑矛盾',
                    'callback_ratio'
                )

    def _validate_investment_feasibility(self, data):
        """A-2修复：校验 max_investment × position_ratio 可行性

        买入金额 = max_investment × position_ratio，需至少 100 元才能生成最低 100 股的订单。
        若乘积 < 100，则每次买入都会因金额不足被拒绝，会话永远无法执行任何交易。
        """
        if 'max_investment' in data and 'position_ratio' in data:
            min_trade_amount = data['max_investment'] * data['position_ratio']
            if min_trade_amount < 100:
                raise ValidationError(
                    f'最大投入({data["max_investment"]})×每档比例({data["position_ratio"]})'
                    f'={min_trade_amount:.2f}元 < 100元最低买入额，'
                    '组合无法执行任何交易，请增大max_investment或position_ratio',
                    'max_investment'
                )


class GridTemplateSchema(Schema):
    """网格配置模板参数校验"""

    template_name = fields.Str(
        required=True,
        validate=validate.Length(min=1, max=50, error='模板名称长度必须在1-50字符之间')
    )

    price_interval = fields.Float(
        validate=validate.Range(min=0.01, max=0.20, error='网格价格间隔必须在0.01-0.20之间')
    )

    position_ratio = fields.Float(
        validate=validate.Range(min=0.01, max=1.0, error='每档交易比例必须在0.01-1.0之间')
    )

    callback_ratio = fields.Float(
        validate=validate.Range(min=0.001, max=0.10, error='回调触发比例必须在0.001-0.10之间')
    )

    max_deviation = fields.Float(
        validate=validate.Range(min=0.05, max=0.50, error='最大偏离度必须在0.05-0.50之间')
    )

    target_profit = fields.Float(
        validate=validate.Range(min=0.01, max=1.0, error='目标盈利必须在0.01-1.0之间')
    )

    stop_loss = fields.Float(
        # C-4修复：同 GridConfigSchema，禁止 stop_loss=0
        validate=validate.Range(min=-0.50, max=-0.001, error='止损比例必须在-0.50到-0.001之间')
    )

    duration_days = fields.Int(
        validate=validate.Range(min=1, max=365, error='运行时长必须在1-365天之间')
    )

    max_investment_ratio = fields.Float(
        validate=validate.Range(min=0.1, max=1.0, error='最大投入比例必须在0.1-1.0之间（10%-100%）')
    )

    description = fields.Str(
        validate=validate.Length(max=200, error='描述长度不能超过200字符')
    )

    is_default = fields.Bool()


def validate_request(schema_class, data):
    """
    通用请求参数校验函数

    Args:
        schema_class: marshmallow Schema类
        data: 要校验的数据字典

    Returns:
        tuple: (is_valid, result_or_errors)
            - 如果有效: (True, validated_data)
            - 如果无效: (False, error_messages)
    """
    schema = schema_class()

    try:
        validated_data = schema.load(data)
        return True, validated_data
    except ValidationError as e:
        logger.warning(f"参数校验失败: {e.messages}")
        return False, e.messages


def validate_grid_config(data):
    """
    校验网格交易配置参数

    Args:
        data: 配置数据字典

    Returns:
        tuple: (is_valid, result_or_errors)
    """
    return validate_request(GridConfigSchema, data)


def validate_grid_template(data):
    """
    校验网格配置模板参数

    Args:
        data: 模板数据字典

    Returns:
        tuple: (is_valid, result_or_errors)
    """
    return validate_request(GridTemplateSchema, data)


def validate_grid_config_simple(user_config: dict) -> dict:
    """
    网格配置参数校验(简化版,供Web API使用)

    Args:
        user_config: 用户配置字典,参数已转换为小数格式(非百分比)

    Returns:
        {
            'valid': True/False,
            'errors': [...]  # 错误列表
        }

    注意: 此函数返回格式与validate_grid_config不同,更符合Web API使用习惯
    """
    # 调用原有的marshmallow校验
    is_valid, result_or_errors = validate_grid_config(user_config)

    if is_valid:
        return {'valid': True, 'errors': []}
    else:
        # 将marshmallow的错误格式转换为简单的字符串列表
        errors = []
        if isinstance(result_or_errors, dict):
            for field, messages in result_or_errors.items():
                if isinstance(messages, list):
                    errors.extend([f"{field}: {msg}" for msg in messages])
                else:
                    errors.append(f"{field}: {messages}")
        else:
            errors = [str(result_or_errors)]

        return {'valid': False, 'errors': errors}
