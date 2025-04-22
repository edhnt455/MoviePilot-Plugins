import datetime
import re
import shutil
import threading
import traceback
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
import xml.etree.ElementTree as ET

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
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
from app.plugins import _PluginBase
from app.schemas import NotificationType, TransferInfo, TransferDirectoryConf
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

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


class CloudLinkMonitor(_PluginBase):
    # 插件名称
    plugin_name = "目录实时监控"
    # 插件描述
    plugin_desc = "监控目录文件变化，自动转移媒体文件。"
    # 插件图标
    plugin_icon = "Linkease_A.png"
    # 插件版本
    plugin_version = "2.6.0"
    # 插件作者
    plugin_author = "edhnt455"
    # 作者主页
    author_url = "https://github.com/edhnt455"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudlinkmonitor_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    transferhis = None
    downloadhis = None
    transferchian = None
    tmdbchain = None
    storagechain = None
    _observer = []
    _enabled = False
    _notify = False
    _onlyonce = False
    _scrape = False
    _category = False
    _refresh = False
    _softlink = False
    _strm = False
    _cron = None
    filetransfer = None
    mediaChain = None
    _size = 0
    # 模式 compatibility/fast
    _mode = "compatibility"
    # 转移方式
    _transfer_type = "softlink"
    _monitor_dirs = ""
    _exclude_keywords = ""
    _interval: int = 10
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    _overwrite_mode: Dict[str, Optional[str]] = {}
    _medias = {}
    # 退出事件
    _event = threading.Event()
    _min_duration = 15
    _max_duration = 120
    _min_resolution = "1920x1080"
    _min_size = 100

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        self.mediaChain = MediaChain()
        self.storagechain = StorageChain()
        self.filetransfer = FileManagerModule()
        # 清空配置
        self._dirconf = {}
        self._transferconf = {}
        self._overwrite_mode = {}

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

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

            # 读取目录配置
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path in monitor_dirs:
                # 格式源目录:目的目录
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
                            self.systemmessage.put(f"{target_path} 是下载目录 {mon_path} 的子目录，无法监控")
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
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(mon_path, self), path=mon_path, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{mon_path} 的云盘实时监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"云盘实时监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     sudo sysctl -p
                                     """)
                        else:
                            logger.error(f"{mon_path} 启动目云盘实时监控失败：{err_msg}")
                        self.systemmessage.put(f"{mon_path} 启动云盘实时监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("云盘实时监控服务启动，立即运行一次")
                self._scheduler.add_job(name="云盘实时监控",
                                        func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "mode": self._mode,
            "transfer_type": self._transfer_type,
            "monitor_dirs": self._monitor_dirs,
            "exclude_keywords": self._exclude_keywords,
            "interval": self._interval,
            "softlink": self._softlink,
            "strm": self._strm,
            "scrape": self._scrape,
            "category": self._category,
            "size": self._size,
            "refresh": self._refresh,
            "min_size": self._min_size,
            "min_duration": self._min_duration,
            "max_duration": self._max_duration,
            "min_resolution": self._min_resolution
        })

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程全量同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloud_link_sync":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始同步云盘实时监控目录 ...",
                              userid=event.event_data.get("user"))
        self.sync_all()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘实时监控目录同步完成！", userid=event.event_data.get("user"))

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步云盘实时监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            logger.info(f"开始处理监控目录 {mon_path} ...")
            list_files = SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT)
            logger.info(f"监控目录 {mon_path} 共发现 {len(list_files)} 个文件")
            # 遍历目录下所有文件
            for file_path in list_files:
                logger.info(f"开始处理文件 {file_path} ...")
                self.__handle_file(event_path=str(file_path), mon_path=mon_path)
        logger.info("全量同步云盘实时监控目录完成！")

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug("文件%s：%s" % (text, event_path))
            self.__handle_file(event_path=event_path, mon_path=mon_path)

    def __parse_nfo_file(self, file_path: Path) -> tuple:
        """
        解析nfo文件获取视频信息
        :param file_path: 视频文件路径
        :return: (duration, width, height) 如果解析失败返回None
        """
        nfo_path = file_path.with_suffix('.nfo')
        if not nfo_path.exists():
            logger.info(f"未找到nfo文件：{nfo_path}")
            return None

        try:
            logger.info(f"开始解析nfo文件：{nfo_path}")
            tree = ET.parse(nfo_path)
            root = tree.getroot()
            
            # 获取时长（秒）
            duration = None
            runtime = root.find('.//runtime')
            if runtime is not None and runtime.text:
                try:
                    # 尝试将分钟转换为秒
                    duration = float(runtime.text) * 60
                    logger.info(f"从nfo文件获取时长：{duration/60:.1f}分钟")
                except ValueError:
                    logger.warn(f"nfo文件时长格式错误：{runtime.text}")
                    pass
            else:
                logger.warn(f"nfo文件未找到runtime节点或内容为空")

            # 获取分辨率
            width = None
            height = None
            streamdetails = root.find('.//streamdetails')
            if streamdetails is not None:
                video = streamdetails.find('.//video')
                if video is not None:
                    width_elem = video.find('width')
                    height_elem = video.find('height')
                    if width_elem is not None and width_elem.text:
                        width = int(width_elem.text)
                        logger.info(f"从nfo文件获取宽度：{width}")
                    if height_elem is not None and height_elem.text:
                        height = int(height_elem.text)
                        logger.info(f"从nfo文件获取高度：{height}")
                else:
                    logger.warn("nfo文件未找到video节点")
            else:
                logger.warn("nfo文件未找到streamdetails节点")

            if duration is not None and width is not None and height is not None:
                logger.info(f"成功从nfo文件获取完整信息：{width}x{height} - {duration/60:.1f}分钟")
                return duration, width, height
            else:
                logger.warn(f"nfo文件信息不完整：duration={duration}, width={width}, height={height}")
                return None
        except Exception as e:
            logger.error(f"解析nfo文件失败：{nfo_path} - {str(e)}")
            logger.error(f"错误详情：{traceback.format_exc()}")
            return None

    def __check_video_info(self, file_path: Path) -> bool:
        """
        检查视频信息
        :param file_path: 视频文件路径
        :return: 是否满足要求
        """
        try:
            # 先尝试从nfo文件获取信息
            logger.info(f"开始检查视频信息：{file_path}")
            video_info = self.__parse_nfo_file(file_path)
            if video_info:
                duration, width, height = video_info
                
                logger.info(f"从nfo文件获取视频信息：{file_path} - {width}x{height} - {duration/60:.1f}分钟")
                
                # 检查时长
                if duration < float(self._min_duration) * 60 or duration > float(self._max_duration) * 60:
                    logger.info(f"视频时长不符合要求：{file_path} - {duration/60:.1f}分钟 (要求：{self._min_duration}-{self._max_duration}分钟)")
                    return False

                # 检查分辨率
                min_width, min_height = map(int, self._min_resolution.split('x'))
                if width < height:
                    logger.info(f"竖屏视频：{file_path} - {width}x{height}")
                    return False
                if width * height < min_width * min_height:
                    logger.info(f"分辨率不足：{file_path} - {width}x{height} (要求：{self._min_resolution})")
                    return False

                logger.info(f"视频信息检查通过：{file_path}")
                return True
            else:
                logger.info(f"未找到nfo文件或解析失败：{file_path}，将尝试其他方式获取信息")
                # 这里可以添加其他获取视频信息的方式
                return True  # 如果无法获取信息，返回True以保留文件
                
        except Exception as e:
            logger.error(f"检查视频信息失败：{file_path} - {str(e)}")
            logger.error(f"错误详情：{traceback.format_exc()}")
            return True  # 返回True以保留文件

    def __handle_file(self, event_path: str, mon_path: str):
        """
        同步一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
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
                            logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                            return

                # 整理屏蔽词不处理
                transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(r"%s" % keyword, event_path, re.IGNORECASE):
                            logger.info(f"{event_path} 命中整理屏蔽词 {keyword}，不处理")
                            return

                # 检查文件大小
                file_size_mb = file_path.stat().st_size / (1024 * 1024)
                # nfo 文件不检查大小
                if file_path.suffix == '.nfo':
                    logger.debug(f"{event_path} 是 nfo 文件，跳过大小检查")
                elif file_size_mb < self._min_size:
                    logger.info(f"{event_path} 文件大小 {file_size_mb:.2f}MB 小于最小限制 {self._min_size}MB，将被删除")
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
                            logger.info(f"移动模式，删除不符合要求的视频：{event_path}")
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
                            logger.info(f"移动模式，删除非媒体文件：{event_path}")
                            file_path.unlink()
                            # 删除文件后立即检查并删除空目录
                            self.__delete_empty_dirs(file_path.parent, mon_path)
                        except Exception as e:
                            logger.error(f"删除非媒体文件失败：{event_path} - {str(e)}")
                    return

                # 判断文件大小
                if self._size and float(self._size) > 0 and file_path.stat().st_size < float(self._size) * 1024 ** 3:
                    logger.info(f"{file_path} 文件大小小于监控文件大小，不处理")
                    # 如果是移动模式，直接删除小文件
                    if self._transfer_type == "move":
                        try:
                            logger.info(f"移动模式，删除小文件：{event_path}")
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
                target_dir.scraping = self._scrape  # 启用刮削
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

                # 先进行刮削
                if self._scrape:
                    self.mediaChain.scrape_metadata(fileitem=file_item,
                                                    meta=file_meta,
                                                    mediainfo=mediainfo)

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

                # 处理对应的 .nfo 文件
                nfo_path = file_path.with_suffix('.nfo')
                if nfo_path.exists():
                    try:
                        if transferinfo.success:
                            # 如果视频文件转移成功，也转移 .nfo 文件
                            target_nfo_path = Path(transferinfo.target_path).with_suffix('.nfo')
                            if transfer_type == "move":
                                # 移动模式
                                shutil.move(str(nfo_path), str(target_nfo_path))
                                logger.info(f"移动 .nfo 文件：{nfo_path} -> {target_nfo_path}")
                            else:
                                # 其他模式（复制、硬链接等）
                                shutil.copy2(str(nfo_path), str(target_nfo_path))
                                logger.info(f"复制 .nfo 文件：{nfo_path} -> {target_nfo_path}")
                        else:
                            # 如果视频文件转移失败，删除 .nfo 文件
                            nfo_path.unlink()
                            logger.info(f"删除 .nfo 文件：{nfo_path}")
                    except Exception as e:
                        logger.error(f"处理 .nfo 文件失败：{nfo_path} - {str(e)}")

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
                    logger.info(f"开始检查并删除空目录，从 {current_dir} 开始")
                    
                    while current_dir != mon_path_obj and current_dir.is_relative_to(mon_path_obj):
                        try:
                            # 检查目录是否为空
                            dir_contents = list(current_dir.iterdir())
                            if not dir_contents:
                                logger.info(f"目录为空，准备删除：{current_dir}")
                                try:
                                    shutil.rmtree(current_dir, ignore_errors=True)
                                    logger.info(f"成功删除空目录：{current_dir}")
                                except Exception as e:
                                    logger.error(f"删除目录失败：{current_dir} - {str(e)}")
                                # 继续检查父目录
                                current_dir = current_dir.parent
                                logger.info(f"继续检查父目录：{current_dir}")
                            else:
                                # 如果目录不为空则记录内容并停止
                                logger.info(f"目录不为空，停止检查：{current_dir}")
                                break
                        except Exception as e:
                            logger.error(f"检查目录失败：{current_dir} - {str(e)}")
                            break
                    
                    logger.info("空目录检查删除完成")

        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

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

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cloud_link_sync",
            "event": EventType.PluginAction,
            "desc": "云盘实时监控同步",
            "category": "",
            "data": {
                "action": "cloud_link_sync"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/cloud_link_sync",
            "endpoint": self.sync,
            "methods": ["GET"],
            "summary": "云盘实时监控同步",
            "description": "云盘实时监控同步",
        }]

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
        if self._enabled and self._cron:
            return [{
                "id": "CloudLinkMonitor",
                "name": "云盘实时监控全量同步服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_all,
                "kwargs": {}
            }]
        return []

    def sync(self) -> schemas.Response:
        """
        API调用目录同步
        """
        self.sync_all()
        return schemas.Response(success=True)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '监控模式',
                                            'items': [
                                                {'title': '兼容模式', 'value': 'compatibility'},
                                                {'title': '性能模式', 'value': 'fast'}
                                            ]
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'},
                                                {'title': 'Rclone复制', 'value': 'rclone_copy'},
                                                {'title': 'Rclone移动', 'value': 'rclone_move'}
                                            ]
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '消息延迟',
                                            'placeholder': '10'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_size',
                                            'label': '最小文件大小(MB)',
                                            'placeholder': '100'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_duration',
                                            'label': '最小视频时长(分钟)',
                                            'placeholder': '15'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_duration',
                                            'label': '最大视频时长(分钟)',
                                            'placeholder': '120'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_resolution',
                                            'label': '最小分辨率(如:1920x1080)',
                                            'placeholder': '1920x1080'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_dirs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '每一行一个目录，支持以下几种配置方式，转移方式支持 move、copy、link、softlink、rclone_copy、rclone_move：\n'
                                                           '监控目录:转移目的目录\n'
                                                           '监控目录:转移目的目录#转移方式\n'
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
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '消息延迟默认10s，如网络较慢可酌情调大。'
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
            "notify": False,
            "onlyonce": False,
            "mode": "fast",
            "transfer_type": "move",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "interval": 10,
            "min_size": 100,
            "min_duration": 15,
            "max_duration": 120,
            "min_resolution": "1920x1080"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None

    def __delete_empty_dirs(self, start_dir: Path, mon_path: str):
        """
        递归删除空目录
        :param start_dir: 开始检查的目录
        :param mon_path: 监控目录
        """
        try:
            current_dir = start_dir
            mon_path_obj = Path(mon_path)
            
            while current_dir != mon_path_obj and current_dir.is_relative_to(mon_path_obj):
                try:
                    # 检查目录是否为空
                    dir_contents = list(current_dir.iterdir())
                    if not dir_contents:
                        try:
                            shutil.rmtree(current_dir, ignore_errors=True)
                            logger.info(f"删除空目录：{current_dir}")
                        except Exception as e:
                            logger.error(f"删除目录失败：{current_dir} - {str(e)}")
                        # 继续检查父目录
                        current_dir = current_dir.parent
                    else:
                        break
                except Exception as e:
                    logger.error(f"检查目录失败：{current_dir} - {str(e)}")
                    break
        except Exception as e:
            logger.error(f"删除空目录过程中发生错误：{str(e)}")

