import requests
import zlib
import xml.etree.ElementTree as ET
import time
import re
import json
from typing import List, Dict, Optional
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote


@dataclass
class IqiyiComment:
    content: str
    time: float
    type: int
    color: str
    uid: str
    nickname: str


@dataclass
class IqiyiEpisode:
    tv_id: str
    name: str
    order: int
    duration: str
    play_url: str


@dataclass
class IqiyiVideoInfo:
    tv_id: str
    album_id: str
    video_name: str
    channel_name: str
    duration: int
    video_count: int
    video_url: str
    episode_list: List[IqiyiEpisode]


@dataclass
class IqiyiSearchResult:
    title: str
    link: str
    site_id: str
    video_doc_type: int
    channel: str
    score: float


class IqiyiDanmuAPI:
    def __init__(self):
        self.http_user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36 Edg/115.0.1901.183"
        self.mobile_user_agent = "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36 Edg/130.0.0.0"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.http_user_agent})
        self.reg_video_info = re.compile(r'"videoInfo":\s*({[^}]+})')
        self.reg_album_info = re.compile(r'"albumInfo":\s*({[^}]+})')

    def _limit_request_frequency(self):
        """限制请求频率"""
        time.sleep(0.1)  # 每次请求间隔100ms

    def search_video(self, keyword: str) -> List[IqiyiSearchResult]:
        """搜索视频

        Args:
            keyword: 搜索关键词

        Returns:
            搜索结果列表
        """
        if not keyword:
            return []

        self._limit_request_frequency()
        keyword = quote(keyword)
        url = f"https://search.video.iqiyi.com/o?if=html5&key={keyword}&pageNum=1&pageSize=20"

        response = self.session.get(url)
        response.raise_for_status()

        result = []
        search_data = response.json()

        if search_data and "data" in search_data and "docinfos" in search_data["data"]:
            for doc in search_data["data"]["docinfos"]:
                if doc["score"] > 0.7:
                    album_info = doc["albumDocInfo"]
                    if (album_info["albumLink"] and
                            "iqiyi.com" in album_info["albumLink"] and
                            album_info["siteId"] == "iqiyi" and
                            album_info["videoDocType"] == 1 and
                            "原创" not in album_info["channel"] and
                            "教育" not in album_info["channel"]):
                        result.append(IqiyiSearchResult(
                            title=album_info["albumTitle"],
                            link=album_info["albumLink"],
                            site_id=album_info["siteId"],
                            video_doc_type=album_info["videoDocType"],
                            channel=album_info["channel"],
                            score=doc["score"]
                        ))

        return result

    def get_video_info(self, video_id: str) -> Optional[IqiyiVideoInfo]:
        """获取视频信息

        Args:
            video_id: 视频ID

        Returns:
            视频信息
        """
        if not video_id:
            return None

        video_id = "2g5a5i86730"
        self._limit_request_frequency()
        url = f"https://m.iqiyi.com/v_{video_id}.html"

        headers = {"User-Agent": self.mobile_user_agent}
        response = self.session.get(url, headers=headers)
        response.raise_for_status()

        body = response.text
        album_match = self.reg_album_info.search(body)
        video_match = self.reg_video_info.search(body)

        if not album_match or not video_match:
            print("未找到匹配的JSON数据")
            return None

        try:
            album_info = json.loads(album_match.group(1))
            video_info = json.loads(video_match.group(1))
        except json.JSONDecodeError as e:
            print(f"JSON解析错误: {e}")
            print(f"album_info匹配内容: {album_match.group(1)}")
            print(f"video_info匹配内容: {video_match.group(1)}")
            return None

        if not video_info:
            return None

        # 根据频道类型获取剧集信息
        episode_list = []
        if video_info["channelName"] == "综艺":
            episode_list = self._get_zongyi_episodes(str(video_info["aid"]))
        elif video_info["channelName"] == "电影":
            duration = video_info["duration"]
            episode_list = [IqiyiEpisode(
                tv_id=video_info["tvid"],
                name=video_info["videoName"],
                order=1,
                duration=f"{duration // 3600:02d}:{(duration % 3600) // 60:02d}:{duration % 60:02d}",
                play_url=video_info["videoUrl"]
            )]
        else:
            episode_list = self._get_episodes(str(video_info["aid"]), video_info.get("videoCount", 0) or album_info.get("videoCount", 0))

        return IqiyiVideoInfo(
            tv_id=video_info["tvid"],
            album_id=str(video_info["aid"]),
            video_name=video_info["videoName"],
            channel_name=video_info["channelName"],
            duration=video_info["duration"],
            video_count=album_info.get("videoCount", 0),
            video_url=video_info["videoUrl"],
            episode_list=episode_list
        )

    def _get_episodes(self, album_id: str, size: int) -> List[IqiyiEpisode]:
        """获取电视剧剧集列表"""
        if not album_id:
            return []

        size = 12
        self._limit_request_frequency()
        url = f"https://pcw-api.iqiyi.com/albums/album/avlistinfo?aid={album_id}&page=1&size={size}"

        response = self.session.get(url)
        response.raise_for_status()

        result = response.json()
        if not result or "data" not in result or "epsodelist" not in result["data"]:
            return []

        episodes = []
        for i, episode in enumerate(result["data"]["epsodelist"], 1):
            episodes.append(IqiyiEpisode(
                tv_id=episode["tvId"],
                name=episode["name"],
                order=i,
                duration=episode["duration"],
                play_url=episode["playUrl"]
            ))

        return episodes

    def _get_zongyi_episodes(self, album_id: str) -> List[IqiyiEpisode]:
        """获取综艺剧集列表"""
        if not album_id:
            return []

        self._limit_request_frequency()
        url = f"https://pcw-api.iqiyi.com/album/album/baseinfo/{album_id}"

        response = self.session.get(url)
        response.raise_for_status()

        result = response.json()
        if not result or "data" not in result:
            return []

        data = result["data"]
        if not data.get("firstVideo") or not data.get("latestVideo"):
            return []

        start_date = datetime.fromtimestamp(data["firstVideo"]["publishTime"] / 1000)
        end_date = datetime.fromtimestamp(data["latestVideo"]["publishTime"] / 1000)

        # 超过一年的太大直接不处理
        if (end_date - start_date).days > 365:
            return []

        episodes = []
        current_date = start_date

        while current_date.month <= end_date.month:
            year = current_date.year
            month = current_date.strftime("%m")

            self._limit_request_frequency()
            url = f"https://pub.m.iqiyi.com/h5/main/videoList/source/month/?sourceId={album_id}&year={year}&month={month}"
            response = self.session.get(url)
            response.raise_for_status()

            month_result = response.json()
            if not month_result or "data" not in month_result or "videos" not in month_result["data"]:
                break

            videos = month_result["data"]["videos"]
            for video in videos:
                if "精编版" not in video["shortTitle"] and "会员版" not in video["shortTitle"]:
                    episodes.append(IqiyiEpisode(
                        tv_id=video["id"],
                        name=video["shortTitle"],
                        order=len(episodes) + 1,
                        duration=video["duration"],
                        play_url=video["playUrl"]
                    ))

            current_date = (current_date.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)

        return episodes

    def get_danmu_content(self, tv_id: str) -> List[IqiyiComment]:
        """获取视频弹幕内容

        Args:
            tv_id: 视频ID

        Returns:
            弹幕列表
        """
        if not tv_id:
            return []

        danmu_list = []
        mat = 1

        while mat < 1000:
            try:
                comments = self._get_danmu_content_by_mat(tv_id, mat)
                # 每段有300秒弹幕，为避免弹幕太大，从中间隔抽取最大60秒200条弹幕
                danmu_list.extend(comments[:1000])
            except Exception as e:
                print(f"获取弹幕失败: {e}")
                break

            mat += 1
            self._limit_request_frequency()

        return danmu_list

    def _get_danmu_content_by_mat(self, tv_id: str, mat: int) -> List[IqiyiComment]:
        """获取指定时间段的弹幕内容

        Args:
            tv_id: 视频ID
            mat: 视频分钟数(从1开始)

        Returns:
            弹幕列表
        """
        if not tv_id:
            return []

        # 确保tv_id是字符串类型
        tv_id = str(tv_id)
        s1 = tv_id[-4:-2]
        s2 = tv_id[-2:]
        url = f"http://cmts.iqiyi.com/bullet/{s1}/{s2}/{tv_id}_300_{mat}.z"

        response = self.session.get(url)
        response.raise_for_status()

        # 解压zlib数据
        decompressed_data = zlib.decompress(response.content)

        # 解析XML
        root = ET.fromstring(decompressed_data)
        comments = []

        for entry in root.findall(".//entry"):
            for bullet in entry.findall("bulletInfo"):
                comment = IqiyiComment(
                    content=bullet.find("content").text,
                    time=float(bullet.find("timePoint").text),
                    type=int(bullet.find("type").text),
                    color=bullet.find("color").text,
                    uid=bullet.find("uid").text,
                    nickname=bullet.find("nickname").text
                )
                comments.append(comment)

        return comments


def main():
    # 使用示例
    api = IqiyiDanmuAPI()

    # 搜索视频
    keyword = "沉默的真相"
    search_results = api.search_video(keyword)
    print(f"\n搜索结果:")
    for result in search_results:
        print(f"标题: {result.title}, 频道: {result.channel}, 链接: {result.link}")

    # 获取视频信息
    video_info = None
    if search_results:
        video_id = search_results[0].link.split("_")[-1].split(".")[0]
        video_info = api.get_video_info(video_id)
        if video_info:
            print(f"\n视频信息:")
            print(f"标题: {video_info.video_name}")
            print(f"频道: {video_info.channel_name}")
            print(f"集数: {video_info.video_count}")
            print(f"\n剧集列表:")
            for episode in video_info.episode_list:
                print(f"第{episode.order}集: {episode.name}")

    # 获取弹幕
    if video_info and video_info.episode_list:
        danmu_list = api.get_danmu_content(video_info.episode_list[0].tv_id)
        print(f"\n弹幕列表(前5条):")
        for comment in danmu_list[:5]:
            print(f"时间: {comment.time}秒, 用户: {comment.nickname}, 内容: {comment.content}")


if __name__ == "__main__":
    main()
