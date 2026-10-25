def should_verify_ssl():
    """所有平台均校验 HTTPS 证书，使用 Requests 默认的 CA 信任链。

    证书不可信或主机名不匹配时由调用方处理请求失败，不能降级为跳过校验。
    """
    return True
