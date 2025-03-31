from abc import ABC, abstractmethod
import hashlib
import subprocess
import re
import os
import requests
from typing import Optional, Dict, List
from dataclasses import dataclass
from app.log import logger

@dataclass
class VideoInfo:
    file_name: str
    file_hash: str
    file_size: int
    video_duration: int
    match_mode: str = "hashAndFileName"

class BaseDanmuAPI(ABC):
    """弹幕API基类"""
    
    @abstractmethod
    def search_by_tmdb_id(self, tmdb_id: int, episode: Optional[int] = None) -> Optional[str]:
        """使用TMDB ID搜索弹幕"""
        pass

    @abstractmethod
    def get_comment_id(self, file_path: str, use_tmdb_id: bool = False, 
                      tmdb_id: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        """获取弹幕ID"""
        pass

    @abstractmethod
    def get_comments(self, comment_id: str) -> Optional[Dict]:
        """获取弹幕内容"""
        pass

    @staticmethod
    def calculate_md5_of_first_16MB(file_path: str) -> str:
        md5 = hashlib.md5()
        size_16MB = 16 * 1024 * 1024
        try:
            with open(file_path, 'rb') as f:
                data = f.read(size_16MB)
                md5.update(data)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"计算MD5失败: {e}")
            return ""

    @staticmethod
    def get_video_duration(file_path: str) -> Optional[float]:
        try:
            process = subprocess.Popen(
                ['ffmpeg', '-i', file_path],
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE
            )
            _, stderr = process.communicate()
            
            stderr = stderr.decode('utf-8', errors='ignore')
            duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", stderr)
            
            if duration_match:
                hours, minutes, seconds = map(float, duration_match.groups())
                return hours * 3600 + minutes * 60 + seconds
            return None
        except Exception as e:
            logger.error(f"获取视频时长失败: {e}")
            return None

    @staticmethod
    def get_file_size(file_path: str) -> int:
        try:
            return os.path.getsize(file_path)
        except Exception as e:
            logger.error(f"获取文件大小失败: {e}")
            return 0

class DanDanAPI(BaseDanmuAPI):
    """弹弹play API实现"""
    
    BASE_URL = 'https://dandanapi.hankun.online/api/v1'
    HEADERS = {
        'Accept': 'application/json',
        "User-Agent": "Moviepilot/plugins 1.1.0"
    }

    def search_by_tmdb_id(self, tmdb_id: int, episode: Optional[int] = None) -> Optional[str]:
        try:
            url = f"{self.BASE_URL}/search/tmdb"
            data = {
                "tmdb_id": tmdb_id,
                "episode": episode if episode is not None else 1
            }
            response = requests.post(url, json=data, headers=self.HEADERS)
            if response.status_code == 200:
                result = response.json()
                if result.get("success") and not result.get("hasMore"):
                    animes = result.get("animes", [])
                    if animes and len(animes) > 0:
                        episodes = animes[0].get("episodes", [])
                        if episodes and len(episodes) > 0:
                            return str(episodes[0].get("episodeId"))
            return None
        except Exception as e:
            logger.error(f"使用TMDB ID搜索弹幕失败: {e}")
            return None

    def get_comment_id(self, file_path: str, use_tmdb_id: bool = False, 
                      tmdb_id: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        try:
            file_name = os.path.basename(file_path)
            file_size = self.get_file_size(file_path)
            file_hash = self.calculate_md5_of_first_16MB(file_path)
            
            video_info = VideoInfo(
                file_name=file_name,
                file_hash=file_hash,
                file_size=file_size,
                video_duration=int(self.get_video_duration(file_path) or 0)
            )
            
            url = f"{self.BASE_URL}/match"
            response = requests.post(url, json=video_info.__dict__, headers=self.HEADERS)
            
            if response.status_code == 200:
                result = response.json()
                if result.get("isMatched") and result.get("matches"):
                    return str(result["matches"][0]["episodeId"])
            
            if use_tmdb_id and tmdb_id is not None:
                return self.search_by_tmdb_id(tmdb_id, episode)
            
            return None
        except Exception as e:
            logger.error(f"获取弹幕ID失败: {e}")
            return None

    def get_comments(self, comment_id: str) -> Optional[Dict]:
        try:
            url = f"{self.BASE_URL}/{comment_id}?from_id=0&with_related=true&ch_convert=0"
            response = requests.get(url, headers=self.HEADERS)
            if response.status_code == 200:
                return response.json()
            logger.error(f"获取弹幕失败: {response.text}")
            return None
        except Exception as e:
            logger.error(f"获取弹幕失败: {e}")
            return None

# 默认使用弹弹play API
default_api = DanDanAPI()

