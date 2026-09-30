"""日志配置。

时间戳用 runner 的本地时区 (GitHub Actions 上是 UTC), 与 GitHub 日志每行自带的
UTC 前缀一致, 便于对照。

注: logging.config.dictConfig 不认识 formatter 的 `converter` 键, 写了也不会生效
(2026-09 在 TZ=UTC 下实测, 打印的仍是 UTC)。真要改时区得显式赋值
`logging.Formatter.converter`。
"""
import logging.config


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "level": "INFO",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}


def init_logger():
    """初始化日志"""
    logging.config.dictConfig(LOGGING_CONFIG)
    return logging.getLogger()
