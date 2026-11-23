"""webGPT 原创人族 Bot；导入模块不会启动游戏。"""

__all__ = ["CompetitiveBot"]


def __getattr__(name: str):
    if name == "CompetitiveBot":
        from .bot import CompetitiveBot

        return CompetitiveBot
    raise AttributeError(name)
