"""Server-side credentials for Alibaba Cloud Model Studio."""

import os


def bailian_connection() -> tuple[str, str]:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    base_url = os.environ.get("DASHSCOPE_BASE_URL", "").strip()
    if not key or key.lower().startswith(("your-", "your_")):
        raise ValueError("请在后端 .env 中配置 DASHSCOPE_API_KEY")
    if not base_url:
        raise ValueError("请在后端 .env 中配置与密钥地域匹配的 DASHSCOPE_BASE_URL")
    return key, base_url
