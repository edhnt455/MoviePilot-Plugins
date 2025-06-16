import datetime
import os
import re
import shutil
import threading
import traceback
import subprocess
import json
from pathlib import Path
from typing import Dict, Any, Optional
import xml.etree.ElementTree as ET

import pytz
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.schemas import NotificationType, TransferInfo, TransferDirectoryConf
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

def get_video_info_from_nfo(file_path: Path) -> Optional[Dict[str, Any]]:
    """
    从NFO文件获取视频信息
    """
    try:
        # 构建NFO文件路径
        nfo_path = file_path.with_suffix('.nfo')
        if not nfo_path.exists():
            return None
            
        # 解析NFO文件
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        
        # 获取视频信息
        video_info = {
            'duration': None,
            'width': None,
            'height': None
        }
        
        # 尝试从runtime获取时长
        runtime = root.find('runtime')
        if runtime is not None and runtime.text:
            video_info['duration'] = float(runtime.text) * 60  # 转换为秒
            
        # 从fileinfo获取详细信息
        fileinfo = root.find('fileinfo/streamdetails/video')
        if fileinfo is not None:
            width = fileinfo.find('width')
            height = fileinfo.find('height')
            duration = fileinfo.find('durationinseconds')
            
            if width is not None and width.text:
                video_info['width'] = int(width.text)
            if height is not None and height.text:
                video_info['height'] = int(height.text)
            if duration is not None and duration.text:
                video_info['duration'] = float(duration.text)
                
        return video_info
    except Exception as e:
        logger.error(f"从NFO文件获取视频信息失败：{str(e)}")
        return None

def get_video_info(file_path: Path) -> Dict[str, Any]:
    """
    获取视频信息，优先从NFO文件读取，如果没有则使用ffmpeg获取
    """
    # 首先尝试从NFO文件获取信息
    video_info = get_video_info_from_nfo(file_path)
    if video_info and all(v is not None for v in video_info.values()):
        return video_info
        
    # 如果NFO文件不存在或信息不完整，使用ffmpeg获取
    try:
        logger.info(f"NFO文件不存在,使用ffmpeg获取视频信息：{file_path}")
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"获取视频信息失败：{file_path}")
            return None
            
        info = json.loads(result.stdout)
        video_info = {
            'duration': None,
            'width': None,
            'height': None
        }
        
        # 获取视频流信息
        for stream in info.get('streams', []):
            if stream.get('codec_type') == 'video':
                video_info['width'] = int(stream.get('width', 0))
                video_info['height'] = int(stream.get('height', 0))
                break
                
        # 获取时长
        format_info = info.get('format', {})
        if 'duration' in format_info:
            video_info['duration'] = float(format_info['duration'])
            
        return video_info
    except Exception as e:
        logger.error(f"获取视频信息出错：{str(e)}")
        return None

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class VideoTransitionImpl:
    """
    目录监控实现类
    """

    def __init__(self, config: dict = None, systemconfig=None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        self.mediaChain = MediaChain()
        self.storagechain = StorageChain()
        self.filetransfer = FileManagerModule()
        self.systemconfig = systemconfig
        self.chain = TransferChain()

        # 初始化配置
        self._dirconf = {}
        self._transferconf = {}
        self._overwrite_mode = {}
        self._medias = {}
        self._event = threading.Event()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._scrape = config.get("scrape")
            self._category = config.get("category")
            self._refresh = config.get("refresh")
            self._mode = config.get("mode")
            self._transfer_type = config.get("transfer_type")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._interval = config.get("interval") or 10
            self._min_size = config.get("min_size") or 100
            self._size = config.get("size") or 0
            self._softlink = config.get("softlink")
            self._strm = config.get("strm")
            self._min_duration = config.get("min_duration") or 15
            self._max_duration = config.get("max_duration") or 120
            self._min_resolution = config.get("min_resolution") or "1920x1080"

    def init_monitor(self):
        """
        初始化监控
        """
        # 读取目录配置
        monitor_dirs = self._monitor_dirs.split("\n")
        if not monitor_dirs:
            return []

        observers = []
        for mon_path in monitor_dirs:
            if not mon_path:
                continue

            # 自定义覆盖方式
            _overwrite_mode = 'never'
            if mon_path.count("@") == 1:
                _overwrite_mode = mon_path.split("@")[1]
                mon_path = mon_path.split("@")[0]

            # 自定义转移方式
            _transfer_type = self._transfer_type
            if mon_path.count("#") == 1:
                _transfer_type = mon_path.split("#")[1]
                mon_path = mon_path.split("#")[0]

            # 存储目的目录
            if SystemUtils.is_windows():
                if mon_path.count(":") > 1:
                    paths = [mon_path.split(":")[0] + ":" + mon_path.split(":")[1],
                             mon_path.split(":")[2] + ":" + mon_path.split(":")[3]]
                else:
                    paths = [mon_path]
            else:
                paths = mon_path.split(":")

            # 目的目录
            target_path = None
            if len(paths) > 1:
                mon_path = paths[0]
                target_path = Path(paths[1])
                self._dirconf[mon_path] = target_path
            else:
                self._dirconf[mon_path] = None

            # 转移方式
            self._transferconf[mon_path] = _transfer_type
            self._overwrite_mode[mon_path] = _overwrite_mode

            # 启用目录监控
            if self._enabled:
                # 检查媒体库目录是不是下载目录的子目录
                try:
                    if target_path and target_path.is_relative_to(Path(mon_path)):
                        logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控")
                        continue
                except Exception as e:
                    logger.debug(str(e))
                    pass

                try:
                    if self._mode == "compatibility":
                        # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                        observer = PollingObserver(timeout=10)
                    else:
                        # 内部处理系统操作类型选择最优解
                        observer = Observer(timeout=10)
                    observers.append(observer)
                    observer.schedule(FileMonitorHandler(mon_path, self), path=mon_path, recursive=True)
                    observer.daemon = True
                    observer.start()
                    logger.info(f"{mon_path} 的按需转移视频服务启动")
                except Exception as e:
                    err_msg = str(e)
                    if "inotify" in err_msg and "reached" in err_msg:
                        logger.warn(
                            f"按需转移视频服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                            + """
                                 echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                 echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                 sudo sysctl -p
                                 """)
                    else:
                        logger.error(f"{mon_path} 启动按需转移视频服务失败：{err_msg}")
        return observers

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化，此功能已禁用
        """
        pass

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始转移符合条件的视频文件 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 获取目标目录
            target: Path = self._dirconf.get(mon_path)
            if not target:
                logger.error(f"未配置监控目录 {mon_path} 的目的目录")
                continue
                
            # 确保目标目录存在
            target.mkdir(parents=True, exist_ok=True)
            
            # 遍历目录下所有文件
            for root, dirs, files in os.walk(mon_path):
                for file in files:
                    file_path = Path(os.path.join(root, file))

                    # 新增：命中过滤关键字不处理
                    if self._exclude_keywords:
                        for keyword in self._exclude_keywords.split("\n"):
                            if keyword and re.findall(keyword, str(file_path)):
                                try:
                                    file_path.unlink()
                                    logger.debug(f"删除命中过滤关键字的文件：{file_path}")
                                    # 删除文件后立即检查并删除空目录
                                    self.__delete_empty_dirs(file_path.parent, mon_path)
                                except Exception as e:
                                    logger.error(f"删除文件失败：{file_path} - {str(e)}")
                                continue  # 跳过该文件处理

                    # 跳过.nfo文件，等待处理对应的视频文件时再处理
                    if file_path.suffix.lower() == '.nfo':
                        continue

                    # 检查文件是否存在
                    if not file_path.exists():
                        continue

                    # 检查文件扩展名
                    if file_path.suffix.lower() not in ['.mp4', '.avi', '.mkv']:
                        try:
                            file_path.unlink()
                            logger.debug(f"删除非视频文件：{file_path}")
                            # 删除文件后立即检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        except Exception as e:
                            logger.error(f"删除文件失败：{file_path} - {str(e)}")
                        continue

                    # 检查视频信息
                    if not self.__check_video_info(file_path):
                        try:
                            # 删除视频文件
                            file_path.unlink()
                            logger.debug(f"删除不符合要求的视频：{file_path}")

                            # 尝试删除对应的.nfo文件
                            nfo_path = file_path.with_suffix('.nfo')
                            if nfo_path.exists():
                                try:
                                    nfo_path.unlink()
                                    logger.debug(f"删除对应的NFO文件：{nfo_path}")
                                except Exception as e:
                                    logger.error(f"删除NFO文件失败：{nfo_path} - {str(e)}")

                            # 删除文件后立即检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        except Exception as e:
                            logger.error(f"删除文件失败：{file_path} - {str(e)}")
                        continue

                    # 移动文件到目标目录
                    try:
                        target_file = target / file_path.name
                        if self._transfer_type == "move":
                            shutil.move(str(file_path), str(target_file))
                            logger.info(f"移动文件：{file_path} -> {target_file}")

                            # 移动对应的.nfo文件
                            nfo_path = file_path.with_suffix('.nfo')
                            if nfo_path.exists():
                                try:
                                    target_nfo = target / nfo_path.name
                                    shutil.move(str(nfo_path), str(target_nfo))
                                except Exception as e:
                                    logger.error(f"移动NFO文件失败：{nfo_path} - {str(e)}")

                            # 移动文件后立即检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        else:
                            shutil.copy2(str(file_path), str(target_file))
                            logger.info(f"复制文件：{file_path} -> {target_file}")

                            # 复制对应的.nfo文件
                            nfo_path = file_path.with_suffix('.nfo')
                            if nfo_path.exists():
                                try:
                                    target_nfo = target / nfo_path.name
                                    shutil.copy2(str(nfo_path), str(target_nfo))
                                except Exception as e:
                                    logger.error(f"复制NFO文件失败：{nfo_path} - {str(e)}")
                    except Exception as e:
                        logger.error(f"移动/复制文件失败：{file_path} - {str(e)}")
                        continue

        logger.info("视频转移完成！")

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias or not self._medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._medias.keys()):
            media_list = self._medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._interval) \
                    or mediainfo.type == MediaType.MOVIE:
                # 发送通知
                if self._notify:
                    # 汇总处理文件总大小
                    total_size = 0
                    file_count = 0

                    # 剧集汇总
                    episodes = []
                    for file in media_files:
                        transferinfo = file.get("transferinfo")
                        total_size += transferinfo.total_size
                        file_count += 1

                        file_meta = file.get("file_meta")
                        if file_meta and file_meta.begin_episode:
                            episodes.append(file_meta.begin_episode)

                    transferinfo.total_size = total_size
                    # 汇总处理文件数量
                    transferinfo.file_count = file_count

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    season_episode = None
                    # 处理文件多，说明是剧集，显示季入库消息
                    if mediainfo.type == MediaType.TV:
                        # 季集文本
                        season_episode = f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                    # 发送消息
                    self.transferchian.send_transfer_message(meta=file_meta,
                                                             mediainfo=mediainfo,
                                                             transferinfo=transferinfo,
                                                             season_episode=season_episode)
                # 发送完消息，移出key
                del self._medias[medis_title_year_season]
                continue

    def __handle_file(self, event_path: str, mon_path: str):
        """
        同步一个文件
        """
        file_path = Path(event_path)
        try:
            if not file_path.exists():
                return
            # 全程加锁
            with lock:
                # 回收站及隐藏的文件不处理
                if event_path.find('/@Recycle/') != -1 \
                        or event_path.find('/#recycle/') != -1 \
                        or event_path.find('/.') != -1 \
                        or event_path.find('/@eaDir') != -1:
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 命中过滤关键字不处理
                if self._exclude_keywords:
                    for keyword in self._exclude_keywords.split("\n"):
                        if keyword and re.findall(keyword, event_path):
                            try:
                                file_path.unlink()
                                # 删除文件后检查并删除空目录
                                self.__delete_empty_dirs(file_path.parent, mon_path)
                                if self._notify:
                                    self.post_message(
                                        mtype=NotificationType.Manual,
                                        title="文件已删除",
                                        text=f"文件 {file_path.name} 命中过滤关键字 {keyword}"
                                    )
                            except Exception as e:
                                logger.error(f"删除文件失败：{event_path} - {str(e)}")
                            return

                # 整理屏蔽词不处理
                transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(r"%s" % keyword, event_path, re.IGNORECASE):
                            return

                # 检查文件大小
                try:
                    file_size_bytes = os.path.getsize(file_path)
                    file_size_mb = file_size_bytes / (1024 * 1024)
                except Exception as e:
                    logger.error(f"获取文件大小失败: {file_path} - {str(e)}")
                    return

                if file_size_mb < self._min_size:
                    try:
                        file_path.unlink()
                        # 删除文件后检查并删除空目录
                        self.__delete_empty_dirs(file_path.parent, mon_path)
                        if self._notify:
                            self.post_message(
                                mtype=NotificationType.Manual,
                                title="文件已删除",
                                text=f"文件 {file_path.name} 大小 {file_size_mb:.2f}MB 小于最小限制 {self._min_size}MB"
                            )
                    except Exception as e:
                        logger.error(f"删除文件失败：{event_path} - {str(e)}")
                    return

                # 检查视频信息
                check_result = self.__check_video_info(file_path)
                if check_result is False:
                    # 如果是移动模式，直接删除不符合要求的视频
                    if self._transfer_type == "move":
                        try:
                            file_path.unlink()
                            # 删除视频后检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        except Exception as e:
                            logger.error(f"删除视频文件失败：{event_path} - {str(e)}")
                    return

                # 不是媒体文件不处理
                if file_path.suffix not in settings.RMT_MEDIAEXT:
                    logger.debug(f"{event_path} 不是媒体文件")
                    # 如果是移动模式，直接删除非媒体文件
                    if self._transfer_type == "move":
                        try:
                            file_path.unlink()
                            # 删除文件后立即检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        except Exception as e:
                            logger.error(f"删除非媒体文件失败：{event_path} - {str(e)}")
                    return

                # 判断文件大小
                if self._size and float(self._size) > 0 and file_path.stat().st_size < float(self._size) * 1024 ** 3:
                    # 如果是移动模式，直接删除小文件
                    if self._transfer_type == "move":
                        try:
                            file_path.unlink()
                            # 删除文件后立即检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        except Exception as e:
                            logger.error(f"删除小文件失败：{event_path} - {str(e)}")
                    return

                # 查询转移目的目录
                target: Path = self._dirconf.get(mon_path)
                # 查询转移方式
                transfer_type = self._transferconf.get(mon_path)

                # 查找这个文件项
                file_item = self.storagechain.get_file_item(storage="local", path=file_path)
                if not file_item:
                    logger.warn(f"{event_path.name} 未找到对应的文件")
                    return

                # 创建目标目录配置
                target_dir = TransferDirectoryConf()
                target_dir.library_path = target
                target_dir.transfer_type = transfer_type
                target_dir.scraping = False  # 禁用刮削
                target_dir.renaming = False  # 禁用重命名
                target_dir.notify = self._notify
                target_dir.overwrite_mode = self._overwrite_mode.get(mon_path) or 'never'
                target_dir.library_storage = "local"
                target_dir.library_category_folder = False  # 禁用二级分类

                if not target_dir.library_path:
                    logger.error(f"未配置监控目录 {mon_path} 的目的目录")
                    return

                # 创建基本的元数据信息
                file_meta = MetaInfoPath(file_path)
                mediainfo = MediaInfo()
                mediainfo.type = MediaType.UNKNOWN
                mediainfo.title = file_path.stem

                # 转移文件
                transferinfo: TransferInfo = self.chain.transfer(fileitem=file_item,
                                                                 meta=file_meta,
                                                                 mediainfo=mediainfo,
                                                                 target_directory=target_dir)

                if not transferinfo:
                    logger.error("文件转移模块运行失败")
                    return

                if not transferinfo.success:
                    # 转移失败
                    logger.warn(f"{file_path.name} 转移失败：{transferinfo.message}")
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{file_path.name} 转移失败！",
                            text=f"原因：{transferinfo.message or '未知'}"
                        )
                    return

                # 发送通知
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.Manual,
                        title=f"{file_path.name} 转移成功！",
                        text=f"已转移到：{transferinfo.target_path}"
                    )

                # 移动模式删除空目录
                if transfer_type == "move":
                    # 从文件所在目录开始向上遍历删除空目录
                    current_dir = file_path.parent
                    mon_path_obj = Path(mon_path)

                    while current_dir != mon_path_obj and current_dir.is_relative_to(mon_path_obj):
                        try:
                            # 检查目录是否为空
                            dir_contents = list(current_dir.iterdir())
                            if not dir_contents:
                                try:
                                    shutil.rmtree(current_dir, ignore_errors=True)
                                    logger.debug(f"成功删除空目录：{current_dir}")
                                except Exception as e:
                                    logger.error(f"删除目录失败：{current_dir} - {str(e)}")
                                # 继续检查父目录
                                current_dir = current_dir.parent
                            else:
                                # 如果目录不为空则记录内容并停止
                                logger.debug(f"目录不为空，停止检查：{current_dir}")
                                break
                        except Exception as e:
                            logger.error(f"检查目录失败：{current_dir} - {str(e)}")
                            break

                    logger.debug("空目录检查删除完成")

        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def __check_video_info(self, file_path: Path) -> bool:
        """
        检查视频信息
        """
        try:
            # 使用ffmpeg获取视频信息
            video_info = get_video_info(file_path)
            if not video_info:
                logger.error(f"无法获取视频信息：{file_path}")
                return True

            duration = video_info.get('duration')
            width = video_info.get('width')
            height = video_info.get('height')

            logger.debug(f"获取视频信息：{file_path} - {width}x{height} - {duration / 60:.1f}分钟")

            # 检查时长
            if duration < float(self._min_duration) * 60 or duration > float(self._max_duration) * 60:
                logger.info(
                    f"视频时长不符合要求：{file_path} - {duration / 60:.1f}分钟 (要求：{self._min_duration}-{self._max_duration}分钟)")
                return False

            # 检查分辨率
            min_width, min_height = map(int, self._min_resolution.split('x'))
            if width < height:
                logger.info(f"竖屏视频：{file_path} - {width}x{height}")
                return False
            if width * height < min_width * min_height:
                logger.info(f"分辨率不足：{file_path} - {width}x{height} (要求：{self._min_resolution})")
                return False

            logger.debug(f"视频信息检查通过：{file_path}")
            return True

        except Exception as e:
            logger.error(f"检查视频信息失败：{file_path} - {str(e)}")
            logger.error(f"错误详情：{traceback.format_exc()}")
            return False

    def __delete_empty_dirs(self, start_dir: Path, mon_path: str):
        """
        递归删除空目录
        当目录下没有视频文件时也视为空目录
        """
        try:
            current_dir = start_dir
            mon_path_obj = Path(mon_path)
            logger.debug(f"开始检查目录：{current_dir}")

            while current_dir != mon_path_obj and current_dir.is_relative_to(mon_path_obj):
                try:
                    # 检查目录内容
                    dir_contents = list(current_dir.iterdir())
                    logger.debug(f"目录 {current_dir} 内容：{[item.name for item in dir_contents]}")
                    
                    # 检查是否包含视频文件
                    has_video = False
                    for item in dir_contents:
                        if item.is_file() and item.suffix.lower() in ['.mp4', '.avi', '.mkv']:
                            has_video = True
                            logger.debug(f"目录 {current_dir} 包含视频文件：{item.name}")
                            break
                    
                    # 如果目录为空或没有视频文件，则删除
                    if not dir_contents or not has_video:
                        try:
                            shutil.rmtree(current_dir, ignore_errors=True)
                            logger.debug(f"删除空目录或无视频目录：{current_dir}")
                        except Exception as e:
                            logger.error(f"删除目录失败：{current_dir} - {str(e)}")
                        # 继续检查父目录
                        current_dir = current_dir.parent
                        logger.debug(f"继续检查父目录：{current_dir}")
                    else:
                        # 如果目录不为空且包含视频文件，则停止检查
                        logger.debug(f"目录包含视频文件，停止检查：{current_dir}")
                        break
                except Exception as e:
                    logger.error(f"检查目录失败：{current_dir} - {str(e)}")
                    break
        except Exception as e:
            logger.error(f"删除空目录过程中发生错误：{str(e)}") 