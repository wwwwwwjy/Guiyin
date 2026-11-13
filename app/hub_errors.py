"""Public actionable errors. Never return upstream URLs, cookies or tokens."""
import re
import sqlite3
import ssl
import urllib.error


def transport_error(exc):
    """Diagnose connections without publishing upstream URLs or credentials."""
    code = getattr(exc, 'code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
    if isinstance(code, int):
        return 'HTTP ' + str(code)
    reason = getattr(exc, 'reason', None)
    return type(exc).__name__ + (' / ' + type(reason).__name__ if isinstance(reason, BaseException) else '')


class UpstreamFailure(RuntimeError):
    def __init__(self, stage, cause):
        self.stage, self.cause = stage, cause
        super().__init__(stage + '失败（' + transport_error(cause) + '）')


class SourcesExhausted(RuntimeError):
    def __init__(self,attempts):
        self.attempts=attempts
        super().__init__('已尝试的音源均未完成下载')


def describe(exc):
    if isinstance(exc,SourcesExhausted):
        return 'sources',f'已依次尝试 {len(exc.attempts)} 个音源，均未完成下载；展开“音源记录”查看各源原因',any(x.get('retryable') for x in exc.attempts)
    if isinstance(exc, UpstreamFailure):
        category, detail, retry = describe(exc.cause)
        return category, str(exc) + '；' + detail, retry
    text = str(exc)
    low = text.lower()
    code = getattr(exc, 'code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
    if isinstance(exc, sqlite3.Error):
        return 'index', '音乐索引读取失败（'+type(exc).__name__+'），请检查飞牛曲库挂载和数据库版本', False
    if 'certificate' in low or isinstance(exc, ssl.SSLError) or isinstance(getattr(exc, 'reason', None), ssl.SSLError):
        return 'tls', '音乐平台证书校验失败，请检查 NAS 时间、网络代理和证书链', True
    if code == 429 or any(s in low for s in ('限流','频繁','too many requests')):
        return 'rate_limit', '音乐平台请求过于频繁，已延后重试', True
    if code in (401,403) or any(s in text for s in ('会员','版权','未授权','权限','试听片段','可下载地址','扫码登录','重新登录','登录失效','账号未登录')):
        return 'access', '当前账号未获完整音频授权或登录已失效；请重新扫码/确认会员权限，也可试听或选择其他版本', False
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)) or any(s in low for s in ('timeout','timed out','连接','network','connection','urlerror')):
        return 'network', '连接音乐平台超时或网络不可达，稍后自动重试；持续失败请检查 NAS 网络', True
    if isinstance(exc, (ValueError, RuntimeError)) and not re.search(r'https?://|cookie|token|password|secret', text, re.I):
        return 'request', text[:220], False
    return 'internal', '处理失败（'+type(exc).__name__+'）；请在任务详情查看错误类别', False
