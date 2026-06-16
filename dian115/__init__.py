from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
import random
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.scheduler import Scheduler
from app.schemas import NotificationType


class Dian115Signin(_PluginBase):
    # 插件名称
    plugin_name = "癫影签到"
    # 插件描述
    plugin_desc = "癫影站点自动执行每日签到。"
    # 插件图标 (已转为raw直链以供页面渲染)
    plugin_icon = "https://raw.githubusercontent.com/jiumian8/MoviePilot-Plugins/main/icons/dian115.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "jiumian"
    # 作者主页
    author_url = "https://github.com/jiumian8/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "dian115signin_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: Optional[str] = None
    _portal_token: str = ""
    _use_proxy: bool = False
    _history_count: int = 30
    _random_time_range: str = ""
    _retry_count: int = 0
    _retry_interval: int = 5
    _connect_timeout: int = 10
    _read_timeout: int = 30

    _base_url: str = "https://m.dian115.com"
    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()

    @staticmethod
    def _to_bool(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "true"
        return bool(val)

    @staticmethod
    def _to_int(val: Any, default: int = 0) -> int:
        try:
            return int(val)
        except Exception:
            return default

    def init_plugin(self, config: Optional[dict] = None) -> None:
        try:
            self.stop_service()

            if self.plugin_icon and str(self.plugin_icon).startswith(("http://", "https://")):
                parsed_icon = urlparse(str(self.plugin_icon))
                icon_domain = f"{parsed_icon.scheme}://{parsed_icon.netloc}" if parsed_icon.scheme and parsed_icon.netloc else None
                if icon_domain and icon_domain not in settings.SECURITY_IMAGE_DOMAINS:
                    settings.SECURITY_IMAGE_DOMAINS.append(icon_domain)

            self._enabled = False
            self._notify = True
            self._onlyonce = False
            self._cron = "0 10 * * *"
            self._portal_token = ""
            self._use_proxy = False
            self._history_count = 30
            self._random_time_range = ""
            self._retry_count = 0
            self._retry_interval = 5
            self._connect_timeout = 10
            self._read_timeout = 30

            if config:
                self._enabled = self._to_bool(config.get("enabled", False))
                self._notify = self._to_bool(config.get("notify", True))
                self._onlyonce = self._to_bool(config.get("onlyonce", False))
                self._cron = config.get("cron") or "0 10 * * *"
                self._portal_token = (config.get("portal_token") or "").strip()
                self._use_proxy = self._to_bool(config.get("use_proxy", False))
                self._history_count = self._to_int(config.get("history_count", 30), 30)
                self._random_time_range = (config.get("random_time_range") or "").strip()
                self._retry_count = self._to_int(config.get("retry_count", 0), 0)
                self._retry_interval = self._to_int(config.get("retry_interval", 5), 5)
                self._connect_timeout = self._to_int(config.get("connect_timeout", 10), 10)
                self._read_timeout = self._to_int(config.get("read_timeout", 30), 30)

            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"{self.plugin_name}: 立即执行一次签到任务")
                self._scheduler.add_job(
                    func=self._signin,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="癫影签到"
                )
                self._onlyonce = False
                self.update_config(self._get_config())

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if not self._enabled:
                logger.info(f"{self.plugin_name}: 插件未启用")
                return

            if self._enabled and self._cron:
                logger.info(f"{self.plugin_name}: 已配置 CRON '{self._cron}'，任务将通过公共服务注册")
        except Exception as err:
            logger.error(f"{self.plugin_name}: 初始化失败 - {err}")
            self._enabled = False

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        services = []

        if self._enabled and self._cron:
            services.append({
                "id": "dian115signin",
                "name": "癫影签到 - 定时任务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._schedule_signin_with_random_delay,
                "kwargs": {},
            })

        pending = self.get_data("pending_task")
        if pending and isinstance(pending, dict):
            run_time_ts = pending.get("run_time_ts")
            if run_time_ts:
                run_date = datetime.fromtimestamp(run_time_ts)
                task_type = pending.get("type", "unknown")

                if run_date > datetime.now():
                    task_id = f"dian115signin_pending_{task_type}"
                    task_name = f"癫影签到 - {'随机延迟' if task_type == 'random_delay' else '重试'}"
                    services.append({
                        "id": task_id,
                        "name": task_name,
                        "trigger": "date",
                        "func": self._execute_delayed_signin,
                        "kwargs": {"run_date": run_date},
                    })
                    logger.info(
                        f"{self.plugin_name}: 通过 get_service() 注册 {task_type} 恢复任务 "
                        f"({run_date.strftime('%Y-%m-%d %H:%M:%S')})"
                    )
                else:
                    logger.info(f"{self.plugin_name}: pending 任务时间已过期 ({task_type})，跳过注册并清理")
                    self._clear_pending_task()

        return services

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        version = getattr(settings, "VERSION_FLAG", "v1")
        cron_field_component = "VCronField" if version == "v2" else "VTextField"
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VCard",
                        "props": {
                            "variant": "flat",
                            "class": "mb-6",
                            "color": "surface"
                        },
                        "content": [
                            {
                                "component": "VCardItem",
                                "props": {"class": "px-6 pb-0"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "d-flex align-center text-h6"},
                                        "content": [
                                            {
                                                "component": "VIcon",
                                                "props": {
                                                    "style": "color: #16b1ff;",
                                                    "class": "mr-3",
                                                    "size": "default"
                                                },
                                                "text": "mdi-calendar-check"
                                            },
                                            {
                                                "component": "span",
                                                "text": "基本设置"
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                "component": "VDivider",
                                "props": {"class": "mx-4 my-2"}
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "px-6 pb-6"},
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "enabled",
                                                            "label": "启用插件",
                                                            "color": "primary",
                                                            "hide-details": True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "use_proxy",
                                                            "label": "启用代理",
                                                            "color": "success",
                                                            "hide-details": True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "notify",
                                                            "label": "开启通知",
                                                            "color": "info",
                                                            "hide-details": True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "onlyonce",
                                                            "label": "立即执行一次",
                                                            "color": "warning",
                                                            "hide-details": True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "portal_token",
                                                            "label": "Portal Token (Cookie)",
                                                            "placeholder": "请输入抓包获取的 portal_token",
                                                            "autocomplete": "off",
                                                            "name": "dian115-signin-token",
                                                            "prepend-inner-icon": "mdi-cookie",
                                                            "persistent-hint": True,
                                                            "hint": "填入请求 Header 中 Cookie 里的 portal_token 值 即 portal_token=xxxxx 只要xxxx里面的内容 不要=和=之前的"
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 4},
                                                "content": [
                                                    {
                                                        "component": cron_field_component,
                                                        "props": {
                                                            "model": "cron",
                                                            "label": "Cron 表达式",
                                                            "placeholder": "0 10 * * *",
                                                            "prepend-inner-icon": "mdi-clock-outline",
                                                            "persistent-hint": True,
                                                            "hint": "默认每天 10:00 执行签到"
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 4},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "history_count",
                                                            "label": "历史保留条数",
                                                            "type": "number",
                                                            "min": 1,
                                                            "step": 1,
                                                            "active": True,
                                                            "persistent-hint": True,
                                                            "hint": "默认保留最近 30 条签到记录",
                                                            "placeholder": "默认保留30条",
                                                            "prepend-inner-icon": "mdi-counter"
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 4},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "random_time_range",
                                                            "label": "随机时间范围(分钟)",
                                                            "placeholder": "例如: 0-30",
                                                            "prepend-inner-icon": "mdi-timer-outline",
                                                            "persistent-hint": True,
                                                            "hint": "定时任务将在该范围内随机延迟执行，留空则不随机"
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "retry_count",
                                                            "label": "失败重试次数",
                                                            "type": "number",
                                                            "min": 0,
                                                            "step": 1,
                                                            "active": True,
                                                            "persistent-hint": True,
                                                            "hint": "签到失败后额外重试次数，默认不重试",
                                                            "placeholder": "默认0次",
                                                            "prepend-inner-icon": "mdi-refresh"
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "retry_interval",
                                                            "label": "重试间隔(分钟)",
                                                            "type": "number",
                                                            "min": 1,
                                                            "step": 1,
                                                            "active": True,
                                                            "persistent-hint": True,
                                                            "hint": "每次失败重试之间的等待时间",
                                                            "placeholder": "默认5分钟",
                                                            "prepend-inner-icon": "mdi-timer-refresh-outline"
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "connect_timeout",
                                                            "label": "连接超时(秒)",
                                                            "type": "number",
                                                            "min": 1,
                                                            "step": 1,
                                                            "active": True,
                                                            "persistent-hint": True,
                                                            "hint": "建立TCP连接的超时时间",
                                                            "placeholder": "默认10秒",
                                                            "prepend-inner-icon": "mdi-lan-connect"
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "sm": 3},
                                                "content": [
                                                    {
                                                        "component": "VTextField",
                                                        "props": {
                                                            "model": "read_timeout",
                                                            "label": "读取超时(秒)",
                                                            "type": "number",
                                                            "min": 1,
                                                            "step": 1,
                                                            "active": True,
                                                            "persistent-hint": True,
                                                            "hint": "等待服务器返回响应的超时时间",
                                                            "placeholder": "默认30秒",
                                                            "prepend-inner-icon": "mdi-clock-outline"
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCard",
                        "props": {
                            "variant": "flat",
                            "class": "mb-6",
                            "color": "surface"
                        },
                        "content": [
                            {
                                "component": "VCardItem",
                                "props": {"class": "px-6 pb-0"},
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "props": {"class": "d-flex align-center text-h6 mb-0"},
                                        "content": [
                                            {
                                                "component": "VIcon",
                                                "props": {
                                                    "style": "color: #16b1ff;",
                                                    "class": "mr-3",
                                                    "size": "default"
                                                },
                                                "text": "mdi-information"
                                            },
                                            {
                                                "component": "span",
                                                "text": "使用说明"
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                "component": "VDivider",
                                "props": {"class": "mx-4 my-2"}
                            },
                            {
                                "component": "VCardText",
                                "props": {"class": "px-6 py-0"},
                                "content": [
                                    {
                                        "component": "VList",
                                        "props": {
                                            "lines": "two",
                                            "density": "comfortable"
                                        },
                                        "content": [
                                            {
                                                "component": "VListItem",
                                                "props": {"lines": "two"},
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "d-flex align-items-start"},
                                                        "content": [
                                                            {
                                                                "component": "VIcon",
                                                                "props": {"color": "primary", "class": "mt-1 mr-2"},
                                                                "text": "mdi-cookie"
                                                            },
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "text-subtitle-1 font-weight-regular mb-1"},
                                                                "text": "凭证获取方式"
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 ml-8"},
                                                        "text": "在浏览器中登录癫影，通过开发者工具抓取请求头 Cookie 中的 portal_token 值填入。"
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VListItem",
                                                "props": {"lines": "two"},
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "d-flex align-items-start"},
                                                        "content": [
                                                            {
                                                                "component": "VIcon",
                                                                "props": {"color": "warning", "class": "mt-1 mr-2"},
                                                                "text": "mdi-run-fast"
                                                            },
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "text-subtitle-1 font-weight-regular mb-1"},
                                                                "text": "立即执行一次"
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 ml-8"},
                                                        "text": "保存配置时勾选后会立刻执行一次签到，完成后自动取消勾选。"
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "VListItem",
                                                "props": {"lines": "two"},
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "d-flex align-items-start"},
                                                        "content": [
                                                            {
                                                                "component": "VIcon",
                                                                "props": {"color": "error", "class": "mt-1 mr-2"},
                                                                "text": "mdi-history"
                                                            },
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "text-subtitle-1 font-weight-regular mb-1"},
                                                                "text": "历史记录"
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 ml-8"},
                                                        "text": "每次执行结果都会写入插件历史，并在详情页中展示最近记录。"
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], self._get_config()

    def get_page(self) -> List[dict]:
        latest = self.get_data("latest_result") or {}
        history = self.get_data("history") or []
        user_info = self.get_data("user_info") or {}

        configured = bool(self._portal_token)
        status_text = "已启用" if self._enabled else "未启用"
        
        # 从 API 缓存中读取用户名和VIP状态
        username = user_info.get("nickname", "未获取 (请先成功执行一次)")
        is_vip = user_info.get("vip", False)
        
        # 优先从 api 获取最新数据，其次取历史记录中的
        current_points = user_info.get("points", latest.get("points", "--"))
        checkin_days = user_info.get("consecutive_signin", latest.get("checkin_days", "--"))

        status_color = "success" if latest.get("success") else ("warning" if latest else "info")
        action_map = {
            "signed": "签到成功",
            "already_signed": "今日已签到",
            "failed": "执行失败",
            "config_required": "待配置",
        }
        action_text = action_map.get(latest.get("action"), "暂无状态")

        history_rows = []
        for item in history[:10]:
            success = item.get("success")
            action = item.get("action")
            action_text_row = action_map.get(action, action or "--")
            action_color = "success" if success else ("warning" if action == "already_signed" else "error")
            action_icon = "mdi-check-circle" if success else ("mdi-alert-circle" if action == "already_signed" else "mdi-close-circle")
            history_rows.append({
                "component": "tr",
                "props": {
                    "class": "text-sm"
                },
                "content": [
                    {
                        "component": "td",
                        "props": {"class": "text-center text-high-emphasis"},
                        "content": [
                            {"component": "VIcon", "props": {"color": "info", "size": "x-small", "class": "mr-1"}, "text": "mdi-clock-time-four-outline"},
                            {"component": "span", "text": item.get("timestamp") or "--"}
                        ]
                    },
                    {
                        "component": "td",
                        "props": {"class": "text-center text-high-emphasis"},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"color": action_color, "size": "small", "variant": "tonal"},
                                "content": [
                                    {"component": "VIcon", "props": {"size": "small", "start": True}, "text": action_icon},
                                    {"component": "span", "text": action_text_row}
                                ]
                            }
                        ]
                    },
                    {
                        "component": "td",
                        "props": {"class": "text-center text-high-emphasis"},
                        "content": [
                            {"component": "VIcon", "props": {"color": "info", "size": "x-small", "class": "mr-1"}, "text": "mdi-counter"},
                            {"component": "span", "text": f"{item.get('checkin_days', '-') or '-'}天"}
                        ]
                    },
                    {
                        "component": "td",
                        "props": {"class": "text-center text-high-emphasis"},
                        "content": [
                            {"component": "VIcon", "props": {"color": "warning", "size": "x-small", "class": "mr-1"}, "text": "mdi-star-circle-outline"},
                            {"component": "span", "text": str(item.get("points_awarded", "-"))}
                        ]
                    },
                    {
                        "component": "td",
                        "props": {"class": "text-center text-high-emphasis"},
                        "content": [
                            {
                                "component": "VChip",
                                "props": {"color": "warning" if item.get("is_retry_task") else "default", "size": "small", "variant": "tonal"},
                                "text": "是" if item.get("is_retry_task") else "否"
                            }
                        ]
                    },
                    {
                        "component": "td",
                        "props": {"class": "text-center text-high-emphasis"},
                        "content": [
                            {"component": "VIcon", "props": {"color": "primary", "size": "x-small", "class": "mr-1"}, "text": "mdi-text-box-outline"},
                            {"component": "span", "text": item.get("message") or "--"}
                        ]
                    },
                ]
            })

        if not history_rows:
            history_rows.append({
                "component": "tr",
                "content": [
                    {
                        "component": "td",
                        "props": {"colspan": 6, "class": "text-center text-medium-emphasis"},
                        "text": "暂无签到历史"
                    }
                ]
            })

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "flat", "class": "mb-6 h-100", "color": "surface"},
                                "content": [
                                    {
                                        "component": "VCardItem",
                                        "props": {"class": "px-6 pb-0"},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "props": {"class": "d-flex align-center text-h6"},
                                                "content": [
                                                    {"component": "VIcon", "props": {"class": "mr-3", "style": "color: #2196F3;", "size": "default"}, "text": "mdi-movie-check-outline"},
                                                    {"component": "span", "text": "设置状态"}
                                                ]
                                            }
                                        ]
                                    },
                                    {"component": "VDivider", "props": {"class": "mx-4 my-2"}},
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "px-6 pb-6"},
                                        "content": [
                                            {
                                                "component": "VRow",
                                                "content": [
                                                    {
                                                        "component": "VCol",
                                                        "props": {"cols": 12, "md": 3},
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "d-flex flex-column justify-space-between", "style": "min-height: 64px;"},
                                                                "content": [
                                                                    {"component": "div", "props": {"class": "text-subtitle-2 text-medium-emphasis"}, "text": "插件状态"},
                                                                    {"component": "VChip", "props": {"color": "success" if self._enabled else "grey", "class": "mt-2 align-self-start"}, "text": status_text}
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {"cols": 12, "md": 3},
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "d-flex flex-column justify-space-between", "style": "min-height: 64px;"},
                                                                "content": [
                                                                    {"component": "div", "props": {"class": "text-subtitle-2 text-medium-emphasis"}, "text": "账号配置"},
                                                                    {"component": "VChip", "props": {"color": "success" if configured else "warning", "class": "mt-2 align-self-start"}, "text": "已配置" if configured else "未配置"}
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {"cols": 12, "md": 3},
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "d-flex flex-column justify-space-between", "style": "min-height: 64px;"},
                                                                "content": [
                                                                    {"component": "div", "props": {"class": "text-subtitle-2 text-medium-emphasis"}, "text": "调度周期"},
                                                                    {"component": "div", "props": {"class": "text-body-1 mt-2"}, "text": self._cron or "--"}
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "VCol",
                                                        "props": {"cols": 12, "md": 3},
                                                        "content": [
                                                            {
                                                                "component": "div",
                                                                "props": {"class": "d-flex flex-column justify-space-between", "style": "min-height: 64px;"},
                                                                "content": [
                                                                    {"component": "div", "props": {"class": "text-subtitle-2 text-medium-emphasis"}, "text": "最近状态"},
                                                                    {"component": "VChip", "props": {"color": status_color, "class": "mt-2 align-self-start"}, "text": action_text}
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "flat", "class": "mb-6 h-100", "color": "surface"},
                                "content": [
                                    {
                                        "component": "VCardItem",
                                        "props": {"class": "px-6 pb-0"},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "props": {"class": "d-flex align-center text-h6"},
                                                "content": [
                                                    {"component": "VIcon", "props": {"class": "mr-3", "style": "color: #4CAF50;", "size": "default"}, "text": "mdi-account-circle-outline"},
                                                    {"component": "span", "text": "账号信息"}
                                                ]
                                            }
                                        ]
                                    },
                                    {"component": "VDivider", "props": {"class": "mx-4 my-2"}},
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "px-6 pb-6"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {"class": "d-flex flex-wrap align-center justify-space-between mb-3 ga-2"},
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-h6 font-weight-bold"},
                                                        "text": username
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "d-flex flex-wrap ga-2"},
                                                        "content": [
                                                            {
                                                                "component": "VChip",
                                                                "props": {"color": "amber-darken-2" if is_vip else "default", "variant": "tonal", "class": "ma-1"},
                                                                "text": "👑 VIP" if is_vip else "普通用户"
                                                            },
                                                            {
                                                                "component": "VChip",
                                                                "props": {"color": "success", "variant": "tonal", "class": "ma-1"},
                                                                "text": f"当前余额：{current_points}"
                                                            },
                                                            {
                                                                "component": "VChip",
                                                                "props": {"color": "info", "variant": "tonal", "class": "ma-1"},
                                                                "text": f"连续签到：{checkin_days}天"
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "flat", "class": "mb-4 elevation-2", "color": "surface", "style": "border-radius: 16px;"},
                                "content": [
                                    {
                                        "component": "VCardItem",
                                        "props": {"class": "pa-6"},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "props": {"class": "d-flex align-center text-h6"},
                                                "content": [
                                                    {"component": "VIcon", "props": {"class": "mr-3", "style": "color: #9C27B0;", "size": "default"}, "text": "mdi-table-clock"},
                                                    {"component": "span", "text": "最近签到历史"}
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "pa-6"},
                                        "content": [
                                            {
                                                "component": "VTable",
                                                "props": {"hover": True, "density": "comfortable", "class": "rounded-lg"},
                                                "content": [
                                                    {
                                                        "component": "thead",
                                                        "content": [
                                                            {
                                                                "component": "tr",
                                                                "content": [
                                                                    {
                                                                        "component": "th",
                                                                        "props": {"class": "text-center text-body-1 font-weight-bold"},
                                                                        "content": [
                                                                            {"component": "VIcon", "props": {"color": "info", "size": "small", "class": "mr-1"}, "text": "mdi-clock-time-four-outline"},
                                                                            {"component": "span", "text": "签到时间"}
                                                                        ]
                                                                    },
                                                                    {
                                                                        "component": "th",
                                                                        "props": {"class": "text-center text-body-1 font-weight-bold"},
                                                                        "content": [
                                                                            {"component": "VIcon", "props": {"color": "success", "size": "small", "class": "mr-1"}, "text": "mdi-check-circle"},
                                                                            {"component": "span", "text": "签到状态"}
                                                                        ]
                                                                    },
                                                                    {
                                                                        "component": "th",
                                                                        "props": {"class": "text-center text-body-1 font-weight-bold"},
                                                                        "content": [
                                                                            {"component": "VIcon", "props": {"color": "info", "size": "small", "class": "mr-1"}, "text": "mdi-counter"},
                                                                            {"component": "span", "text": "签到天数"}
                                                                        ]
                                                                    },
                                                                    {
                                                                        "component": "th",
                                                                        "props": {"class": "text-center text-body-1 font-weight-bold"},
                                                                        "content": [
                                                                            {"component": "VIcon", "props": {"color": "warning", "size": "small", "class": "mr-1"}, "text": "mdi-star-circle-outline"},
                                                                            {"component": "span", "text": "奖励积分"}
                                                                        ]
                                                                    },
                                                                    {
                                                                        "component": "th",
                                                                        "props": {"class": "text-center text-body-1 font-weight-bold"},
                                                                        "content": [
                                                                            {"component": "VIcon", "props": {"color": "warning", "size": "small", "class": "mr-1"}, "text": "mdi-refresh-auto"},
                                                                            {"component": "span", "text": "重试任务"}
                                                                        ]
                                                                    },
                                                                    {
                                                                        "component": "th",
                                                                        "props": {"class": "text-center text-body-1 font-weight-bold"},
                                                                        "content": [
                                                                            {"component": "VIcon", "props": {"color": "primary", "size": "small", "class": "mr-1"}, "text": "mdi-text-box-outline"},
                                                                            {"component": "span", "text": "结果说明"}
                                                                        ]
                                                                    },
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                    {
                                                        "component": "tbody",
                                                        "content": history_rows
                                                    }
                                                ]
                                            },
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-caption text-grey mt-2",
                                                    "style": "background: #f5f5f7; border-radius: 8px; padding: 6px 12px; display: inline-block;"
                                                },
                                                "content": [
                                                    {"component": "VIcon", "props": {"size": "x-small", "class": "mr-1"}, "text": "mdi-format-list-bulleted"},
                                                    {"component": "span", "text": f"共显示 {len(history[:10])} 条签到记录"}
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def _get_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron or "",
            "portal_token": self._portal_token,
            "use_proxy": self._use_proxy,
            "history_count": self._history_count,
            "random_time_range": self._random_time_range,
            "retry_count": self._retry_count,
            "retry_interval": self._retry_interval,
            "connect_timeout": self._connect_timeout,
            "read_timeout": self._read_timeout,
        }

    def _save_config(self, config: dict) -> Dict[str, Any]:
        new_config = {
            "enabled": self._to_bool(config.get("enabled", False)),
            "notify": self._to_bool(config.get("notify", False)),
            "onlyonce": self._to_bool(config.get("onlyonce", False)),
            "cron": config.get("cron") or "0 10 * * *",
            "portal_token": (config.get("portal_token") or "").strip(),
            "use_proxy": self._to_bool(config.get("use_proxy", False)),
            "history_count": self._to_int(config.get("history_count", 30), 30),
            "random_time_range": (config.get("random_time_range") or "").strip(),
            "retry_count": self._to_int(config.get("retry_count", 0), 0),
            "retry_interval": self._to_int(config.get("retry_interval", 5), 5),
        }
        self.update_config(new_config)
        self.init_plugin(new_config)
        return {"success": True, "message": "配置保存成功", "data": self._get_config()}

    def _parse_random_time_range(self) -> Tuple[int, int]:
        raw_value = (self._random_time_range or "").strip()
        if not raw_value:
            return 0, 0

        try:
            if "-" in raw_value:
                start_text, end_text = raw_value.split("-", 1)
                start_min = max(0, int(start_text.strip() or 0))
                end_min = max(0, int(end_text.strip() or 0))
            else:
                start_min = 0
                end_min = max(0, int(raw_value))

            if end_min < start_min:
                start_min, end_min = end_min, start_min
            return start_min, end_min
        except Exception:
            logger.warning(f"{self.plugin_name}: 随机时间范围格式无效，已忽略 - {raw_value}")
            return 0, 0

    def _save_pending_task(self, task_type: str, run_time: datetime, **extra) -> None:
        self.save_data("pending_task", {
            "type": task_type,
            "run_time_ts": run_time.timestamp(),
            "run_time_str": run_time.strftime("%Y-%m-%d %H:%M:%S"),
            **extra,
        })
        logger.info(f"{self.plugin_name}: 已保存 pending {task_type} 任务，执行时间: {run_time.strftime('%Y-%m-%d %H:%M:%S')}")

    def _clear_pending_task(self) -> None:
        self.save_data("pending_task", None)
        logger.debug(f"{self.plugin_name}: 已清理 pending 任务数据")

    def _schedule_signin_with_random_delay(self) -> None:
        start_min, end_min = self._parse_random_time_range()
        delay_minutes = random.randint(start_min, end_min) if end_min > 0 else 0

        if delay_minutes <= 0:
            logger.info(f"{self.plugin_name}: 未设置随机延迟，立即执行签到任务")
            self._clear_pending_task()
            self._signin()
            return

        tz = pytz.timezone(settings.TZ)
        run_time = datetime.now(tz=tz) + timedelta(minutes=delay_minutes)
        logger.info(f"{self.plugin_name}: 定时任务触发，已安排在 {delay_minutes} 分钟后执行签到")

        self._save_pending_task("random_delay", run_time)
        self.reregister_plugin()

    def _schedule_retry_signin(self, retry_index: int) -> Optional[str]:
        if retry_index > self._retry_count:
            self._clear_pending_task()
            return None

        retry_interval = max(self._retry_interval, 1)
        tz = pytz.timezone(settings.TZ)
        run_time = datetime.now(tz=tz) + timedelta(minutes=retry_interval)

        self._save_pending_task("retry", run_time, retry_index=retry_index)
        self.reregister_plugin()

        return run_time.strftime("%Y-%m-%d %H:%M:%S")

    def reregister_plugin(self) -> None:
        logger.info(f"{self.plugin_name}: 重新注册插件任务")
        Scheduler().update_plugin_job(self.__class__.__name__)

    def _execute_delayed_signin(self) -> Dict[str, Any]:
        pending = self.get_data("pending_task") or {}
        retry_index = pending.get("retry_index", 0) if isinstance(pending, dict) else 0
        self._clear_pending_task()
        logger.info(f"{self.plugin_name}: 通过 get_service() 执行{'重试' if retry_index > 0 else '延迟'}签到任务 (retry_index={retry_index})")
        return self._signin(retry_index=retry_index)

    def _get_status(self) -> Dict[str, Any]:
        latest = self.get_data("latest_result") or {}
        history = self.get_data("history") or []
        return {
            "enabled": self._enabled,
            "cron": self._cron,
            "notify": self._notify,
            "use_proxy": self._use_proxy,
            "configured": bool(self._portal_token),
            "latest_result": latest,
            "history_count": len(history),
        }

    def _get_history_api(self) -> Dict[str, Any]:
        return {"success": True, "data": self.get_data("history") or []}

    def _run_once(self) -> Dict[str, Any]:
        result = self._signin()
        return {"success": result.get("success", False), "data": result, "message": result.get("message", "")}

    def stop_service(self):
        try:
            Scheduler().remove_plugin_job(self.__class__.__name__.lower())
        except Exception as err:
            logger.debug(f"{self.plugin_name}: 停止服务时忽略错误 - {err}")

        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.debug(f"{self.plugin_name}: 停止内部调度器时忽略错误 - {err}")

    def _record_history(self, record: Dict[str, Any]) -> None:
        history = self.get_data("history") or []
        history.append(record)
        history = sorted(history, key=lambda x: x.get("timestamp") or "", reverse=True)
        if len(history) > self._history_count:
            history = history[:self._history_count]
        self.save_data("history", history)
        self.save_data("latest_result", record)

    def _notify_result(self, title: str, text: str) -> None:
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text,
            )

    def _fetch_user_profile(self, proxies) -> dict:
        """调用 /api/portal/me 接口获取用户信息"""
        url = f"{self._base_url}/api/portal/me"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
            'Origin': self._base_url,
            'Referer': f"{self._base_url}/",
        }
        cookies = {'portal_token': self._portal_token}
        
        response = requests.get(
            url, 
            headers=headers, 
            cookies=cookies, 
            proxies=proxies, 
            timeout=(self._connect_timeout, self._read_timeout)
        )
        response.raise_for_status()
        data = response.json()
        
        if data.get("code") == "ok":
            return data.get("user", {})
        return {}

    def _signin(self, retry_index: int = 0) -> Dict[str, Any]:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self._portal_token:
            result = {
                "success": False,
                "timestamp": timestamp,
                "message": "未配置 portal_token (Cookie)",
                "action": "config_required",
            }
            self._record_history(result)
            return result

        try:
            proxies = None
            if self._use_proxy:
                proxies = getattr(settings, "PROXY", None)

            # 1. 签到前先请求 /api/portal/me 提取用户信息 (Nickname, VIP, Points 等)
            try:
                user_info = self._fetch_user_profile(proxies)
                if user_info:
                    self.save_data("user_info", user_info)
                else:
                    logger.warning(f"{self.plugin_name}: 未能提取到有效的用户信息，可能 Cookie 已过期")
            except Exception as e:
                logger.error(f"{self.plugin_name}: 获取用户信息失败 - {e}")
                user_info = {}

            nickname = user_info.get("nickname", "未知用户")
            is_vip = user_info.get("vip", False)
            vip_str = "👑 VIP" if is_vip else "普通用户"

            # 2. 准备发送真正签到的 POST 请求
            url = f"{self._base_url}/api/portal/signin"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
                'Origin': self._base_url,
                'Referer': f"{self._base_url}/me/signin",
            }
            cookies = {'portal_token': self._portal_token}
            payload = {"mode": "normal"}

            response = requests.post(
                url, 
                headers=headers, 
                cookies=cookies, 
                json=payload, 
                proxies=proxies,
                timeout=(self._connect_timeout, self._read_timeout)
            )
            
            try:
                res_data = response.json()
            except ValueError:
                res_data = {}

            # 签到成功处理 (200 OK)
            if response.status_code == 200 and res_data.get("code") == "ok":
                award = res_data.get("award", 0)
                new_balance = res_data.get("new_balance", user_info.get("points", 0))
                streak = res_data.get("streak", user_info.get("consecutive_signin", 0))

                result = {
                    "success": True,
                    "timestamp": timestamp,
                    "message": f"签到成功，获得 {award} 积分",
                    "action": "signed",
                    "points_awarded": award,
                    "points": new_balance,
                    "checkin_days": streak,
                }
                self._record_history(result)
                self._clear_pending_task()
                self._notify_result(
                    title="【🎬癫影】签到成功 🟢",
                    text=(
                        f"━━━━━━━━━━━━━━\n"
                        f"✨ 状态：✅已签到\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"📊 数据统计\n"
                        f"👤 用户：{nickname} ({vip_str})\n"
                        f"🎁 奖励积分：{award}\n"
                        f"⭐ 当前余额：{new_balance}\n"
                        f"📆 连续签到：{streak} 天\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🕐 时间：{timestamp}"
                    ),
                )
                return result

            # 重复签到处理 (409 Conflict)
            elif response.status_code == 409:
                # 409 时服务器有时不返回积分，这里优先提取 user_info 里获取的数据兜底
                new_balance = res_data.get("new_balance", user_info.get("points", "未知"))
                streak = user_info.get("consecutive_signin", "未知")

                result = {
                    "success": True,
                    "timestamp": timestamp,
                    "message": "今日已签到过，请勿重复操作",
                    "action": "already_signed",
                    "points": new_balance,
                    "checkin_days": streak,
                }
                self._record_history(result)
                self._clear_pending_task()
                self._notify_result(
                    title="【🎬癫影】签到状态 🟡",
                    text=(
                        f"━━━━━━━━━━━━━━\n"
                        f"✨ 状态：ℹ️今日已签到\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"📊 数据统计\n"
                        f"👤 用户：{nickname} ({vip_str})\n"
                        f"⭐ 当前余额：{new_balance}\n"
                        f"📆 连续签到：{streak} 天\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🕐 时间：{timestamp}"
                    ),
                )
                return result

            # 其他未知错误状态
            else:
                error_msg = res_data.get("message") or f"HTTP 状态码: {response.status_code}"
                raise ValueError(error_msg)

        except Exception as err:
            logger.error(f"{self.plugin_name}: 执行签到失败 - {err}")
            next_retry_time = None
            if retry_index < self._retry_count:
                next_retry_time = self._schedule_retry_signin(retry_index + 1)
            else:
                self._clear_pending_task()

            # 确保获取不到的时候给个占位符，防止报错
            nickname = locals().get("nickname", "未知用户")
            vip_str = locals().get("vip_str", "未知状态")

            result = {
                "success": False,
                "timestamp": timestamp,
                "message": str(err),
                "action": "failed",
                "retry_index": retry_index,
                "next_retry_time": next_retry_time,
                "is_retry_task": retry_index > 0,
            }
            self._record_history(result)
            self._notify_result(
                title="【🎬癫影】签到异常 🔴",
                text=(
                    f"━━━━━━━━━━━━━━\n"
                    f"✨ 状态：❌签到失败\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"👤 用户：{nickname} ({vip_str})\n"
                    f"💬 失败原因：{err}\n"
                    f"🔁 当前重试：{retry_index}/{self._retry_count}\n"
                    f"⏰ 下次重试：{next_retry_time or '无'}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🕐 时间：{timestamp}"
                ),
            )
            return result
