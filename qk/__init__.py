"""qk —— 郑州升达经贸管理学院教务系统抢课命令行工具。

分层结构::

    qk/__main__.py      命令行入口（python -m qk）
    qk/config.py        配置加载、环境变量覆盖、凭据脱敏
    qk/models.py        领域数据模型
    qk/net.py           会话封装：超时 / 限速 / 重试 / 并发上限
    qk/adapters/base.py 登录层与选课层的抽象接口
    qk/adapters/cas.py  统一身份认证（CAS）登录实现
    qk/adapters/jwgl.py 教务选课接口实现
    qk/grabber.py       抢课编排
    qk/mockserver.py    离线 mock 教务服务（自测用）

本包只做“用户自己账号的自动化操作”，请遵守学校教务系统的使用规定，
不要把请求间隔调到会打扰服务器的程度（默认已内置限速）。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
