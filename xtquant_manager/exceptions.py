"""
XtQuantManager 自定义异常类
"""


class XtQuantManagerError(Exception):
    """基类"""


class XtQuantTimeoutError(XtQuantManagerError):
    """API 调用超时"""


class XtQuantCallError(XtQuantManagerError):
    """API 调用失败（非超时）"""


class XtQuantConnectionError(XtQuantManagerError):
    """连接失败"""


class AccountNotFoundError(XtQuantManagerError):
    """账号不存在"""


class AccountAlreadyExistsError(XtQuantManagerError):
    """账号已注册"""
