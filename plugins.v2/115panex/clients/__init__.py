"""
客户端模块
包含115网盘、PanSou等客户端
"""
from .p115 import P115ClientManager
from .pansou import PanSouClient
from .hdhive import HDHiveOpenAPIClient, HDHiveOpenAPIError
from .hdhive_web import HDHiveWebClient

__all__ = [
    "P115ClientManager",
    "PanSouClient",
    "HDHiveOpenAPIClient",
    "HDHiveOpenAPIError",
    "HDHiveWebClient"
]
