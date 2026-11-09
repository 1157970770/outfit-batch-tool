# -*- coding: utf-8 -*-
"""错误文案：把接口/系统异常翻成能直接展示给同事看的中文。

桌面版基于 requests 的异常树；本仓库用标准库 urllib，所以映射表保留、
异常类型换成 urllib.error + 自定义 ImageApiError。脱敏规则不变：任何
`sk-...` 或 Authorization 值都不许出现在页面和日志里。
"""
from __future__ import annotations

import binascii
import errno
import json
import re
import socket
import ssl
import urllib.error


HTTP_ERROR_REASONS = {
    400: "接口拒绝了请求，所选模型可能不在当前 API Key 分组中，或提示词、图片、尺寸参数不符合要求",
    401: "接口鉴权失败，API 密钥无效或已过期",
    403: "接口拒绝访问，当前密钥没有权限，或内容被安全策略拦截",
    404: "接口地址或所选模型不存在，请确认当前 API Key 分组已开放该模型",
    408: "接口等待请求超时",
    409: "接口当前无法处理该请求，请稍后重试",
    413: "上传的参考图片过大",
    415: "上传的图片格式不受支持",
    422: "接口无法处理当前提示词、图片或参数",
    429: "请求过于频繁，或账号额度、余额不足",
    500: "接口服务器内部异常",
    502: "接口网关异常",
    503: "接口服务暂时不可用",
    504: "接口服务器响应超时",
}

SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-\.]+")


class ImageApiError(RuntimeError):
    """接口层错误：message 已经是中文人话，status 为 HTTP 状态码（0 表示网络层）。"""

    def __init__(self, message: str, *, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = int(status or 0)
        self.detail = detail


def sanitize(text: str, limit: int = 400) -> str:
    """去掉密钥、压平空白、截断——用于回显接口原始原因。"""
    if not text:
        return ""
    cleaned = SECRET_RE.sub("[已隐藏密钥]", str(text))
    cleaned = cleaned.replace("Authorization", "[已隐藏头]")
    return " ".join(cleaned.split())[:limit]


def http_error_message(status: int, body: str = "") -> tuple[str, str]:
    """返回（给用户看的一句话，脱敏后的接口原因）。"""
    reason = HTTP_ERROR_REASONS.get(status)
    if reason is None:
        if 500 <= status <= 599:
            reason = "接口服务器发生异常"
        elif 400 <= status <= 499:
            reason = "接口拒绝了当前请求"
        else:
            reason = "接口返回了异常状态"
    detail = _error_detail(body)
    text = f"{reason}（HTTP {status}）"
    if detail:
        text += f"；接口原因：{detail}"
    return text, detail


def _error_detail(body: str) -> str:
    """只取结构化错误原因（error.message / message / detail），不回显整段响应。"""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return sanitize(body, 200)
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    message = None
    if isinstance(error, dict):
        message = error.get("message")
    elif isinstance(error, str):
        message = error
    if not isinstance(message, str):
        message = data.get("message") or data.get("detail")
    if not isinstance(message, str):
        return ""
    return sanitize(message)


def network_error_message(reason: object, timeout: int = 0) -> str:
    """urllib 的 URLError.reason 花样很多，这里按最可能的原因翻中文。"""
    if isinstance(reason, ssl.SSLError):
        return "安全连接验证失败，请检查系统时间、网络证书或代理设置"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        if timeout:
            return f"接口处理时间过长（已等待 {timeout} 秒），请稍后重试"
        return "网络请求超时，请检查网络后重试"
    if isinstance(reason, ConnectionRefusedError):
        return "接口拒绝连接，请检查接口地址或代理设置"
    text = str(reason or "").lower()
    if "timed out" in text:
        return "网络请求超时，请检查网络后重试"
    if "certificate" in text:
        return "安全连接验证失败，请检查系统时间、网络证书或代理设置"
    if "name or service not known" in text or "getaddrinfo" in text:
        return "无法解析接口域名，请检查网络或 DNS 设置"
    return f"无法连接生图接口：{sanitize(str(reason), 120)}"


def explain_error(error: BaseException) -> str:
    """把任务执行中的异常翻成中文；已经不是技术异常的（ValueError 文案）原样返回。"""
    if isinstance(error, ImageApiError):
        return str(error)

    if isinstance(error, urllib.error.HTTPError):
        message, _ = http_error_message(error.code, error.read().decode("utf-8", "replace"))
        return message

    if isinstance(error, urllib.error.URLError):
        return network_error_message(getattr(error, "reason", error))

    if isinstance(error, (socket.timeout, TimeoutError)):
        return network_error_message(error)

    if isinstance(error, ssl.SSLError):
        return network_error_message(error)

    if isinstance(error, PermissionError):
        return "没有文件读写权限，请检查数据目录权限"
    if isinstance(error, FileNotFoundError):
        return "需要的图片或目录不存在，可能已被移动或删除"
    if isinstance(error, IsADirectoryError):
        return "选择的是文件夹，不是有效的图片文件"
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) == 112 or error.errno == errno.ENOSPC:
            return "磁盘空间不足，无法写入文件"
        return "文件或系统操作失败，请检查路径、权限和磁盘空间"

    if isinstance(error, (binascii.Error, UnicodeError)):
        return "接口返回的图片数据损坏或编码不正确"

    if isinstance(error, (KeyError, TypeError, IndexError)):
        return "接口返回的数据结构不符合预期，请稍后重试或联系维护人员"

    message = str(error).strip()
    if isinstance(error, (ValueError, RuntimeError)) and message:
        return message
    return "生图过程发生未预期异常，请重试；如果持续出现请联系维护人员"
