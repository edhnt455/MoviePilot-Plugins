# MoviePilot library
from app.log import logger
from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas.types import EventType
from app.utils.system import SystemUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfo
from app.utils.http import RequestUtils
from app.helper.mediaserver import MediaServerHelper
from datetime import datetime, timedelta
import json

from typing import Any, List, Dict, Tuple, Optional
import subprocess
import os
import threading
from app.plugins.danmu import danmu_generator as generator


class Danmu(_PluginBase):
    # 插件名称
    plugin_name = "弹幕刮削"
    # 插件描述
    plugin_desc = "使用弹弹play平台生成弹幕的字幕文件，实现弹幕播放。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/HankunYu/MoviePilot-Plugins/main/icons/danmu.png"
    # 主题色
    plugin_color = "#3B5E8E"
    # 插件版本
    plugin_version = "1.1.16"
    # 插件作者
    plugin_author = "edhnt455"
    # 作者主页
    author_url = "https://github.com/edhnt455"
    # 插件配置项ID前缀
    plugin_config_prefix = "danmu_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _width = 1920
    _height = 1080
    # 搞字体太复杂 以后再说
    # _fontface = 'Arial'
    _fontsize = 50
    _alpha = 0.8
    _duration = 6
    _cron = '0 0 1 1 *'
    _path = ''
    _max_threads = 10
    _onlyFromBili = False
    _useTmdbID = True
    _convertT2S = True
    _subtitle_area_height = 150

    # Emby配置
    _mediaservers = None
    mediaserver_helper = None
    _EMBY_HOST = None
    _EMBY_USER = None
    _EMBY_APIKEY = None
    _emby_update_enabled = False

    media_chain = MediaChain()

    def init_plugin(self, config: dict = None):
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled", False)
            self._width = config.get("width", 1920)
            self._height = config.get("height", 1080)
            # self._fontface = config.get("fontface")
            self._fontsize = config.get("fontsize", 50)
            self._alpha = config.get("alpha", 0.8)
            self._duration = config.get("duration", 10)
            self._path = config.get("path", "")
            self._cron = config.get("cron", "0 0 1 1 *")
            self._onlyFromBili = config.get("onlyFromBili", False)
            self._useTmdbID = config.get("useTmdbID", True)
            self._convertT2S = config.get("convertT2S", True)
            self._subtitle_area_height = config.get("subtitle_area_height", 150)

            # Emby配置
            self._mediaservers = config.get("mediaservers", [])
            self._emby_update_enabled = config.get("emby_update_enabled", False)

        if self._enabled:
            logger.info("弹幕加载插件已启用")

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        services = []
        if self.get_state():
            if self._path and self._cron:
                services.append({
                    "id": "Danmu",
                    "name": "弹幕全局刮削服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.generate_danmu_global,
                    "kwargs": {}
                })
            if self._mediaservers and self._emby_update_enabled:
                services.append({
                    "id": "DanmuEmbyUpdate",
                    "name": "Emby观看记录弹幕更新服务",
                    "trigger": CronTrigger.from_crontab("0 0 * * *"),
                    "func": self.update_emby_watching_danmu,
                    "kwargs": {}
                })
        return services

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    # 插件配置页面
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 获取Emby服务器列表
        emby_servers = self.mediaserver_helper.get_services(type_filter="emby")
        emby_options = [{"title": name, "value": name} for name in emby_servers.keys()]

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
                                            'model': 'onlyFromBili',
                                            'label': '仅使用B站弹幕，建议关闭包含其他平台弹幕',
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
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'useTmdbID',
                                            'label': '使用TMDB ID作为预备匹配方案，当无法匹配文件hash时尝试使用TMDB ID',
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
                                            'model': 'convertT2S',
                                            'label': '繁体转中文',
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
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'emby_update_enabled',
                                            'label': '启用Emby弹幕更新,只会更新一个月内观看过的剧集中未播放的集数的弹幕',
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
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'width',
                                            'label': '宽度，默认1920',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'height',
                                            'label': '高度，默认1080',
                                            'type': 'number',
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
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'fontsize',
                                            'label': '字体大小，默认50',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'alpha',
                                            'label': '弹幕透明度，默认0.8',
                                            'type': 'number',
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
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'duration',
                                            'label': '弹幕持续时间 默认10秒',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '取消定期刮削，需要全局刮削请去 设置->服务 手动启动',
                                            'type': 'text',
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
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'subtitle_area_height',
                                            'label': '底部字幕防遮挡范围，默认150，为0不开启防遮挡',
                                            'type': 'number',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mediaservers',
                                            'label': 'Emby服务器',
                                            'items': emby_options,
                                            'multiple': True,
                                            'chips': True,
                                            'placeholder': '请选择Emby服务器',
                                            'v-show': 'emby_update_enabled'
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
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path',
                                            'label': '刮削媒体库路径，一行一个',
                                            'placeholder': '留空不启用',
                                            'rows': 2,
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
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'flat',
                                            'text': '此插件会根据情况生成两种弹幕字幕文件，均为ass格式。.danmu为刮削出来的纯弹幕，.withDanmu为原生字幕与弹幕合并后的文件。自动刮削新入库文件。如果没有外挂字幕只有内嵌字幕会自动提取内嵌字幕生成.withDanmu文件。弹幕来源为 弹弹play 提供的多站合并资源以及 https://github.com/m13253/danmaku2ass 提供的思路。第一次使用可以去 设置->服务 手动启动全局刮削。\n取消了定期全局刮削，为了降低服务器压力以及防止被ban IP。',
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "width": 1920,
            "height": 1080,
            "fontsize": 50,
            "alpha": 0.8,
            "duration": 6,
            "cron": "0 0 1 1 *",
            "path": "",
            "onlyFromBili": False,
            "useTmdbID": True,
            "convertT2S": True,
            "subtitle_area_height": 150,
            "mediaservers": [],
            "emby_update_enabled": False
        }

    def get_page(self) -> List[dict]:
        pass

    def generate_danmu(self, file_path: str) -> Optional[str]:
        """
        生成弹幕文件
        :param file_path: 视频文件路径
        :return: 生成的弹幕文件路径，如果失败则返回None
        """
        meta = MetaInfo(file_path)
        tmdb_id = None
        episode = None
        if self._useTmdbID:
            media_info = self.media_chain.recognize_media(meta=meta)
            if media_info:
                tmdb_id = media_info.tmdb_id
                episode = meta.episode.split('E')[1] if meta.episode else None

        try:
            return generator.danmu_generator(
                file_path,
                self._width,
                self._height,
                'Arial',
                self._fontsize,
                self._alpha,
                self._duration,
                self._onlyFromBili,
                self._useTmdbID,
                self._convertT2S,
                tmdb_id,
                episode,
                self._subtitle_area_height
            )
        except Exception as e:
            logger.error(f"生成弹幕失败: {e}")
            return None

    def generate_danmu_global(self):
        """
        全局刮削弹幕
        """
        if not self._path:
            logger.warning("未设置刮削路径，跳过全局刮削")
            return

        logger.info("开始全局弹幕刮削")
        threading_list = []
        paths = [path.strip() for path in self._path.split('\n') if path.strip()]

        for path in paths:
            if not os.path.exists(path):
                logger.warning(f"路径不存在: {path}")
                continue

            logger.info(f"刮削路径：{path}")
            for root, _, files in os.walk(path):
                for file in files:
                    if file.endswith(('.mp4', '.mkv')):
                        if len(threading_list) >= self._max_threads:
                            threading_list[0].join()
                            threading_list.pop(0)

                        target_file = os.path.join(root, file)
                        logger.info(f"开始生成弹幕文件：{target_file}")
                        thread = threading.Thread(
                            target=self.generate_danmu,
                            args=(target_file,)
                        )
                        thread.start()
                        threading_list.append(thread)

        for thread in threading_list:
            thread.join()

        logger.info("全局弹幕刮削完成")

    @eventmanager.register(EventType.TransferComplete)
    def generate_danmu_after_transfer(self, event):
        """
        传输完成后生成弹幕
        """
        if not self._enabled:
            return

        def __to_dict(_event):
            """
            递归将对象转换为字典
            """
            if isinstance(_event, dict):
                return {k: __to_dict(v) for k, v in _event.items()}
            elif isinstance(_event, list):
                return [__to_dict(item) for item in _event]
            elif isinstance(_event, tuple):
                return tuple(__to_dict(list(_event)))
            elif isinstance(_event, set):
                return set(__to_dict(list(_event)))
            elif hasattr(_event, 'to_dict'):
                return __to_dict(_event.to_dict())
            elif hasattr(_event, '__dict__'):
                return __to_dict(_event.__dict__)
            elif isinstance(_event, (int, float, str, bool, type(None))):
                return _event
            else:
                return str(_event)

        try:
            raw_data = __to_dict(event.event_data)
            target_file = raw_data.get("transferinfo", {}).get("file_list_new", [None])[0]

            if not target_file:
                logger.warning("未找到目标文件")
                return

            logger.info(f"开始生成弹幕文件：{target_file}")
            thread = threading.Thread(
                target=self.generate_danmu,
                args=(target_file,)
            )
            thread.start()
        except Exception as e:
            logger.error(f"处理传输完成事件失败: {e}")

    def stop_service(self):
        """
        退出插件
        """
        pass

    def get_emby_watching_series(self) -> List[Dict]:
        """
        获取Emby中正在观看的剧集信息
        """
        try:
            # 获取最近30天的观看记录
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)

            emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
            if not emby_servers:
                logger.error("未配置Emby媒体服务器")
                return []

            watching_series = []
            for emby_name, emby_server in emby_servers.items():
                self._EMBY_USER = emby_server.instance.get_user()
                self._EMBY_APIKEY = emby_server.config.config.get("apikey")
                self._EMBY_HOST = emby_server.config.config.get("host")
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

                # 获取用户观看记录
                url = f"{self._EMBY_HOST}emby/Users/{self._EMBY_USER}/Items"
                headers = {
                    'X-Emby-Token': self._EMBY_APIKEY,
                    'X-Emby-Authorization': f'MediaBrowser Client="MoviePilot", Device="MoviePilot", DeviceId="MoviePilot", Version="1.0.0"'
                }
                params = {
                    'Recursive': 'true',
                    'Fields': 'BasicSyncInfo,UserData,DatePlayed,Path,MediaPath,MediaSources',
                    'ImageTypeLimit': 1,
                    'EnableImageTypes': 'Primary',
                    'StartIndex': 0,
                    'Limit': 100,
                    'SortBy': 'DatePlayed',
                    'SortOrder': 'Descending',
                    'IncludeItemTypes': 'Episode'
                }

                response = RequestUtils(headers=headers).get_res(url, params=params)

                if response and response.status_code == 200:
                    items = response.json().get('Items', [])

                    for item in items:
                        series_name = item.get('SeriesName', '')
                        episode_name = item.get('Name', '')

                        # 检查播放进度
                        user_data = item.get('UserData', {})
                        played_percentage = user_data.get('PlayedPercentage', 0)
                        played = user_data.get('Played', False)
                        last_played = item.get('DatePlayed')

                        # 如果最后播放时间不在30天内，跳过
                        if last_played:
                            last_played_date = datetime.fromisoformat(last_played.replace('Z', '+00:00'))
                            if last_played_date < start_date:
                                continue
                        else:
                            continue

                        # 如果已标记为已播放，跳过
                        if played:
                            continue

                        series_id = item.get('SeriesId')
                        if not series_id:
                            continue

                        # 获取剧集信息
                        series_url = f"{self._EMBY_HOST}emby/Shows/{series_id}/Episodes"
                        series_response = RequestUtils(headers=headers).get_res(series_url,
                                                                                params={'UserId': self._EMBY_USER})
                        if not series_response or series_response.status_code != 200:
                            logger.error(f"获取剧集信息失败: {series_name}")
                            continue

                        series_info = series_response.json()
                        total_episodes = len(series_info.get('Items', []))
                        watched_episodes = sum(1 for ep in series_info.get('Items', [])
                                               if ep.get('UserData', {}).get('PlayCount', 0) > 0)

                        if watched_episodes < total_episodes:
                            watching_series.append({
                                'series_id': series_id,
                                'series_name': series_name,
                                'total_episodes': total_episodes,
                                'watched_episodes': watched_episodes,
                                'last_played': last_played_date if last_played else datetime.now(),
                                'emby_name': emby_name,
                                'emby_host': self._EMBY_HOST,
                                'emby_user': self._EMBY_USER,
                                'emby_apikey': self._EMBY_APIKEY
                            })

            return watching_series

        except Exception as e:
            logger.error(f"获取Emby观看记录失败: {e}", exc_info=True)
        return []

    def update_emby_watching_danmu(self):
        """
        更新Emby中正在观看的剧集的弹幕
        """
        if not self._enabled:
            logger.warning("插件未启用，跳过Emby弹幕更新")
            return

        if not self._mediaservers:
            logger.warning("未配置Emby服务器，跳过Emby弹幕更新")
            return

        if not self._path:
            logger.warning("未设置刮削路径，跳过Emby弹幕更新")
            return

        watching_series = self.get_emby_watching_series()

        if not watching_series:
            return

        # 获取所有需要监控的路径
        monitor_paths = [path.strip() for path in self._path.split('\n') if path.strip()]

        # 用于存储所有线程
        threads = []
        for series in watching_series:
            # 获取剧集所有集数信息
            url = f"{series['emby_host']}emby/Shows/{series['series_id']}/Episodes"
            headers = {
                'X-Emby-Token': series['emby_apikey'],
                'X-Emby-Authorization': f'MediaBrowser Client="MoviePilot", Device="MoviePilot", DeviceId="MoviePilot", Version="1.0.0"'
            }
            params = {
                'UserId': series['emby_user'],
                'Fields': 'Path,MediaPath,MediaSources'
            }

            response = RequestUtils(headers=headers).get_res(url, params=params)
            if not response:
                logger.error(f"获取剧集 {series['series_name']} 信息失败: 请求无响应")
                continue

            if response.status_code != 200:
                logger.error(f"获取剧集 {series['series_name']} 信息失败: HTTP {response.status_code}")
                continue

            episodes = response.json().get('Items', [])

            for episode in episodes:
                episode_name = episode.get('Name', '')
                episode_number = episode.get('IndexNumber', 0)
                episode_id = episode.get('Id', '')

                # 检查播放状态
                user_data = episode.get('UserData', {})
                play_count = user_data.get('PlayCount', 0)
                played_percentage = user_data.get('PlayedPercentage', 0)
                played = user_data.get('Played', False)

                # 跳过已观看的集数
                if play_count > 0:
                    continue

                # 获取视频文件路径
                media_path = episode.get('Path')
                if not media_path:
                    logger.warning(
                        f"未找到视频文件路径: {series['series_name']} 第 {episode_number} 集 (ID: {episode_id})")
                    continue

                if not os.path.exists(media_path):
                    logger.warning(f"视频文件不存在: {media_path}")
                    continue

                # 检查文件是否在监控路径中
                is_monitored = False
                for monitor_path in monitor_paths:
                    if os.path.abspath(media_path).startswith(os.path.abspath(monitor_path)):
                        is_monitored = True
                        break

                if not is_monitored:
                    continue

                thread = threading.Thread(
                    target=self.generate_danmu,
                    args=(media_path,)
                )
                thread.start()
                threads.append(thread)

                # 如果线程数达到最大限制，等待一个线程完成
                if len(threads) >= self._max_threads:
                    threads[0].join()
                    threads.pop(0)

        # 等待所有剩余线程完成
        if threads:
            for thread in threads:
                thread.join()

        logger.info("Emby观看记录弹幕更新完成")
