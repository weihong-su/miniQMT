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
        validate=validate.Range(min=0, error='最大追加投入必须大于等于0')
    )

    max_deviation = fields.Float(
        validate=validate.Range(min=0.05, max=0.50, error='最大偏离度必须在0.05-0.50之间（5%-50%）')
    )

    target_profit = fields.Float(
        validate=validate.Range(min=0.01, max=1.0, error='目标盈利必须在0.01-1.0之间（1%-100%）')
    )

    stop_loss = fields.Float(
        validate=validate.Range(min=-0.50, max=0, error='止损比例必须在-0.50-0之间（-50%-0%）')
    )

    duration_days = fields.Int(
        validate=validate.Range(min=1, max=365, error='运行时长必须在1-365天之间')
    )

    @validates_schema
    def validate_profit_and_loss(self, data, **kwargs):
        """验证目标盈利和止损的合理性"""
        if 'target_profit' in data and 'stop_loss' in data:
            if data['target_profit'] < abs(data['stop_loss']):
                raise ValidationError('目标盈利应大于或等于止损幅度', 'target_profit')


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
        validate=validate.Range(min=-0.50, max=0, error='止损比例必须在-0.50-0之间')
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

    # DEBUG: 校验前的详细日志
    logger.info(f"[DEBUG validate_request] 开始校验...")
    logger.info(f"[DEBUG validate_request] Schema类: {schema_class.__name__}")
    logger.info(f"[DEBUG validate_request] 输入data keys: {list(data.keys())}")
    logger.info(f"[DEBUG validate_request] 输入data内容: {data}")
    logger.info(f"[DEBUG validate_request] max_investment值: {data.get('max_investment')} (type: {type(data.get('max_investment'))})")
    logger.info(f"[DEBUG validate_request] max_investment是否为None: {data.get('max_investment') is None}")

    try:
        validated_data = schema.load(data)
        logger.info(f"[DEBUG validate_request] 校验通过！validated_data: {validated_data}")
        return True, validated_data
    except ValidationError as e:
        logger.error(f"[DEBUG validate_request] 校验失败！错误消息: {e.messages}")
        logger.error(f"[DEBUG validate_request] ValidationError详情: {str(e)}")
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
