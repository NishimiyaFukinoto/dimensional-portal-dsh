"""次元门 DSH 版：本地执行层（daemon）+ MCP 工具层。

AstrBot 版把请求从 QQ 收进来；DSH 版的「大脑」就是 DSH 会话本身，
所以这里没有聊天平台，只有两件事：
    dp.service  —— 一台手机、一个队列、一套安全策略
    tools/      —— dp_daemon.py（常驻）与 dp_mcp.py（给 DSH 的工具）
"""

__all__ = ["config", "service"]
