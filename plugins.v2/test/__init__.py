from typing import Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, DownloadHistory
from app.schemas.types import EventType
from librouteros import connect

class Test(_PluginBase):
    # 插件名称
    plugin_name = "测试"
    # 插件描述
    plugin_desc = "测试。"
    # 插件图标
    plugin_icon = "clean.png"
    # 插件版本
    plugin_version = "0.1"
    # 插件作者
    plugin_author = "edhnt455"
    # 作者主页
    author_url = "https://github.com/edhnt455"
    # 插件配置项ID前缀
    plugin_config_prefix = "test_"
    # 加载顺序
    plugin_order = 1

    # 私有属性
    _enabled: bool = False
    # 路由器地址
    _address: str = None

    def init_plugin(self, config: dict = None):
        if not config:
            return

        self._enabled = config.get("enabled")
        self._address = config.get("address")

        # 停止现有任务
        self.stop_service()

            # 加载模块
        if self._enabled:
            self.start_job()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.value
            })

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                            'hint': '开启后插件将处于激活状态',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                            'hint': '插件将立即运行一次',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                            'hint': '是否在特定事件发生时发送通知',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VAutocomplete',
                                        'props': {
                                            'multiple': False,
                                            'model': 'msg_type',
                                            'label': '消息类型',
                                            'placeholder': '自定义消息发送类型',
                                            'items': MsgTypeOptions,
                                            'hint': '选择消息的类型',
                                            'persistent-hint': True,
                                            'active': True,
                                        }
                                    }
                                ]
                            },
                            # {
                            #     'component': 'VCol',
                            #     'props': {
                            #         'cols': 12,
                            #         'md': 4
                            #     },
                            #     'content': [
                            #         {
                            #             'component': 'VSwitch',
                            #             'props': {
                            #                 'model': 'del_dns',
                            #                 'label': '立刻清除DNS',
                            #                 'hint': '终止运行并清除符合当前hosts的DNS记录',
                            #                 'persistent-hint': True
                            #             }
                            #         }
                            #     ]
                            # },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'cron_enabled',
                                            'label': '启用定时器',
                                            'hint': '开启后执行周期才会生效',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式',
                                            'hint': '使用cron表达式指定执行周期，如 0 8 * * *',
                                            'persistent-hint': True
                                        },
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'address',
                                            'label': '路由器地址',
                                            'placeholder': '192.168.*.* or http(s)://example.com:443',
                                            'hint': '请输入路由器的地址',
                                            'persistent-hint': True,
                                            'clearable': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'timeout',
                                            'label': '超时时间',
                                            'placeholder': '请求超时时间，单位秒',
                                            'hint': 'API请求时的超时时间',
                                            'persistent-hint': True,
                                            'type': 'number',
                                            'min': 1,
                                            'suffix': '秒',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'ttl',
                                            'label': 'TTL',
                                            'placeholder': 'DNS记录的TTL时间',
                                            'hint': 'DNS记录的TTL，最小120',
                                            'persistent-hint': True,
                                            'type': 'number',
                                            'min': 120,
                                            'suffix': '秒',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'username',
                                            'label': '管理员',
                                            'placeholder': 'RouterOS的管理员用户，如：admin',
                                            'hint': '请输入管理员账号',
                                            'persistent-hint': True,
                                            'clearable': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'password',
                                            'label': '密码',
                                            'placeholder': 'RouterOS的管理员用户的密码',
                                            'hint': '请输入管理员账号密码',
                                            'persistent-hint': True,
                                            'clearable': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'ipv4',
                                            'label': 'IPv4',
                                            'hint': '同步IPv4地址的Hosts',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'ipv6',
                                            'label': 'IPv6',
                                            'hint': '同步IPv6地址的Hosts',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'match_subdomain',
                                            'label': '子域名通配',
                                            'hint': '写入的DNS记录将同步匹配子域名',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'ignore',
                                            'label': '忽略的IP或域名',
                                            'hint': '请使用|进行分割，如：10.10.10.1|wiki.movie-pilot.org',
                                            'persistent-hint': True,
                                            'clearable': True,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'style': 'white-space: pre-line;',
                                            'text':
                                                '使用提示：\n'
                                                '1、可以配合自定义Hosts以及Cloudflare IP优选插件，实现RouterOS路由Cloudflare优选；\n'
                                                '2、插件版本：v1.1以后，仅启用插件，只会注册工作流支持，插件并不会自动注册定时任务，需要插件自身支持定时任务的，请启用定时器；\n'
                                                '3、v2.4.8+版本后，可通过关闭定时器，将定时任务完全交由工作流统一管理，实现无缝联动运行；\n'
                                                '4、工作流与插件内置的定时执行周期，互相独立，不会互相影响，可同时使用（建议二选一即可）。\n'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "del_dns": False,
            "cron_enabled": True,
            "cron": "0 6 * * *",
            "notify": True,
            "msg_type": "Plugin",
            "address": None,
            "timeout": 10,
            "ttl": 86400,
            "username": None,
            "password": None,
            "ipv4": True,
            "ipv6": True,
            "match_subdomain": False,
            "ignore": None,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def start_job(self) -> bool:
        try:
            conn = connect(
                host='192.168.50.254',
                username='admin',  # 替换为你的用户名
                password='Hjl7946138520',  # 替换为你的密码
                port=8728
            )
            # 执行命令示例
            dns_entries = conn('/ip/dns/static/print')
            logger.info(list(dns_entries))
        except Exception as e:
            logger.info(f"连接失败: {e}")