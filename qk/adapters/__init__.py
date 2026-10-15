"""适配器包：把“平台差异”关在这里，CLI 与抢课编排只依赖抽象接口。"""

from .base import (
    AdapterError,
    AuthError,
    CaptchaRequiredError,
    CourseAdapter,
    CourseNotFoundError,
    LoginAdapter,
    PlatformUnsupportedError,
    SessionExpiredError,
    SelectionError,
)

__all__ = [
    "AdapterError",
    "AuthError",
    "CaptchaRequiredError",
    "CourseAdapter",
    "CourseNotFoundError",
    "LoginAdapter",
    "PlatformUnsupportedError",
    "SelectionError",
    "SessionExpiredError",
]
