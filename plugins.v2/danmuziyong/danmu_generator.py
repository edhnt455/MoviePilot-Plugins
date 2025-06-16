import chardet
import requests
import os
import re
import hashlib
import subprocess
import json
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from app.log import logger


@dataclass
class VideoInfo:
    file_name: str
    file_hash: str
    file_size: int
    video_duration: int
    match_mode: str = "hashAndFileName"


class DanmuAPI:
    BASE_URL = 'https://dandanapi.hankun.online/api/v1'
    HEADERS = {
        'Accept': 'application/json',
        "User-Agent": "Moviepilot/plugins 1.3.0"
    }

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

    @staticmethod
    def search_by_tmdb_id(tmdb_id: int, episode: Optional[int] = None) -> Optional[str]:
        """
        使用TMDB ID搜索弹幕
        :param tmdb_id: TMDB ID
        :param episode: 集数
        :return: 弹幕ID
        """
        try:
            url = f"{DanmuAPI.BASE_URL}/search/tmdb"
            data = {
                "tmdb_id": tmdb_id
            }
            if episode is not None:
                data["episode"] = episode
            else:
                data["episode"] = 1
            response = requests.post(url, json=data, headers=DanmuAPI.HEADERS)
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

    @staticmethod
    def get_comment_id(file_path: str, use_tmdb_id: bool = False, tmdb_id: Optional[int] = None,
                       episode: Optional[int] = None, cache_ttl: Optional[int] = None) -> Optional[str]:
        """
        获取弹幕ID
        :param file_path: 视频文件路径
        :param use_tmdb_id: 是否使用TMDB ID
        :param tmdb_id: TMDB ID
        :param episode: 集数
        :return: 弹幕ID
        """
        try:
            # 首先尝试使用文件名和文件大小匹配
            file_name = os.path.basename(file_path)
            file_size = DanmuAPI.get_file_size(file_path)
            file_hash = DanmuAPI.calculate_md5_of_first_16MB(file_path)

            video_info = VideoInfo(
                file_name=file_name,
                file_hash=file_hash,
                file_size=file_size,
                video_duration=int(DanmuAPI.get_video_duration(file_path) or 0)
            )

            # 检查当前目录下所有的 .id 文件
            video_dir = os.path.dirname(file_path)
            for file in os.listdir(video_dir):
                if file.endswith('.id'):
                    id_file = os.path.join(video_dir, file)
                    logger.info(f"找到弹幕ID文件 - {id_file}")
                    fileID = str(int(os.path.splitext(file)[0]) * 10000 + int(episode))
                    return fileID

            # 使用 match API
            url = f"{DanmuAPI.BASE_URL}/match"
            response = requests.post(url, json=video_info.__dict__, headers=DanmuAPI.HEADERS)

            if response.status_code == 200:
                result = response.json()
                if result.get("isMatched") and result.get("matches"):
                    return str(result["matches"][0]["episodeId"])

            # 如果使用TMDB ID且提供了TMDB ID，尝试使用TMDB ID匹配
            if use_tmdb_id and tmdb_id is not None:
                comment_id = DanmuAPI.search_by_tmdb_id(tmdb_id, episode)
                if comment_id:
                    return comment_id

            return None
        except Exception as e:
            logger.error(f"获取弹幕ID失败: {e}")
            return None

    @staticmethod
    def get_title_from_nfo(file_path: str) -> Optional[str]:
        nfo_file = os.path.splitext(file_path)[0] + '.nfo'
        try:
            with open(nfo_file, 'r', encoding='utf-8') as f:
                nfo_content = f.read()
                title_match = re.search(r'<title>(.*)</title>', nfo_content)
                if title_match:
                    logger.info(f'从nfo文件中获取标题 - {title_match.group(1)}')
                    return title_match.group(1)
                logger.error('未找到标题信息')
                return None
        except Exception as e:
            logger.error(f'读取nfo文件失败: {e}')
            return None

    @classmethod
    def get_comments(cls, comment_id: str) -> Optional[Dict]:
        """
        获取弹幕内容
        :param comment_id: 弹幕ID
        :return: 弹幕数据
        """
        try:
            url = f"{cls.BASE_URL}/{comment_id}?from_id=0&with_related=true&ch_convert=1"
            response = requests.get(url, headers=cls.HEADERS)
            if response.status_code == 200:
                return response.json()
            logger.error(f"获取弹幕失败: {response.text}")
            return None
        except Exception as e:
            logger.error(f"获取弹幕失败: {e}")
            return None


class DanmuConverter:
    @staticmethod
    def filter_comments(comments: List[Dict]) -> List[Dict]:
        """
        过滤弹幕，先过滤乱码，再判断数量限制
        :param comments: 弹幕列表
        :return: 过滤后的弹幕列表
        """
        max_comments = 2000

        # 按时间排序
        sorted_comments = sorted(comments, key=lambda x: float(x['p'].split(',')[0]))

        # 首先过滤乱码
        # valid_comments = []
        # for comment in sorted_comments:
        #     text = comment.get('m', '')
        #
        #     # 跳过空文本
        #     if not text:
        #         continue
        #
        #     # 检测乱码
        #     # 1. 检查是否包含过多特殊字符
        #     special_chars = sum(1 for c in text if ord(c) > 0x4E00 and ord(c) < 0x9FFF)
        #     if special_chars > len(text) * 0.5:  # 如果特殊字符超过文本长度的一半，认为是乱码
        #         continue
        #
        #     # 2. 检查是否包含过多不可打印字符
        #     non_printable = sum(1 for c in text if not c.isprintable())
        #     if non_printable > len(text) * 0.3:  # 如果不可打印字符超过文本长度的30%，认为是乱码
        #         continue
        #
        #     # 3. 检查是否包含过多emoji或特殊符号
        #     emoji_count = sum(1 for c in text if ord(c) > 0x1F600 and ord(c) < 0x1F64F)
        #     if emoji_count > len(text) * 0.3:  # 如果emoji超过文本长度的30%，认为是乱码
        #         continue
        #
        #     # 4. 检查文本长度是否合理（中文通常2-20个字符）
        #     if len(text) > 50 or len(text) < 1:  # 如果文本过长或过短，可能是乱码
        #         continue
        #
        #     # 5. 检查是否包含过多重复字符
        #     if len(set(text)) < len(text) * 0.3:  # 如果重复字符超过70%，可能是乱码
        #         continue
        #
        #     # 如果文本不是乱码，添加到结果中
        #     valid_comments.append(comment)
        #
        # logger.info(f"过滤乱码后剩余{len(valid_comments)}条弹幕")

        # 如果数量在限制内，直接返回
        # if len(valid_comments) <= max_comments:
        #     return valid_comments
        if len(sorted_comments) <= max_comments:
            return sorted_comments

        logger.info(f"弹幕数量超过{max_comments}条，开始过滤重复内容...")

        # 过滤重复内容
        unique_comments = []
        seen_texts = set()
        for comment in sorted_comments:
            text = comment.get('m', '')
            if text not in seen_texts:
                seen_texts.add(text)
                unique_comments.append(comment)

        logger.info(f"去重后剩余{len(unique_comments)}条弹幕")

        # 如果去重后仍然超过限制，随机过滤
        if len(unique_comments) > max_comments:
            # 将时间轴分成多个区间
            time_intervals = 10  # 分成10个区间
            interval_size = len(unique_comments) // time_intervals
            filtered_comments = []

            for i in range(time_intervals):
                start_idx = i * interval_size
                end_idx = (i + 1) * interval_size if i < time_intervals - 1 else len(unique_comments)
                interval_comments = unique_comments[start_idx:end_idx]

                # 计算每个区间需要保留的弹幕数量
                target_count = max(1, int(len(interval_comments) * (max_comments / len(unique_comments))))

                # 随机选择要保留的弹幕
                if target_count < len(interval_comments):
                    import random
                    filtered_comments.extend(random.sample(interval_comments, target_count))
                else:
                    filtered_comments.extend(interval_comments)

            logger.info(f"随机过滤后剩余{len(filtered_comments)}条弹幕")
            return filtered_comments

        return unique_comments

    @staticmethod
    def convert_timestamp(timestamp: float) -> str:
        timestamp = round(timestamp * 100.0)
        hour, minute = divmod(timestamp, 360000)
        minute, second = divmod(minute, 6000)
        second, centsecond = divmod(second, 100)
        return f'{int(hour)}:{int(minute):02d}:{int(second):02d}.{int(centsecond):02d}'

    @staticmethod
    def write_ass_head(f, width: int, height: int, fontface: str, fontsize: float, alpha: float, styleid: str):
        # 将透明度从0-1转换为0-255，并反转（因为ASS中0是完全不透明，255是完全透明）
        alpha_value = int((1 - alpha) * 255)
        f.write(
            f'''[Script Info]
; Script generated by Hankun 
; Super thanks to https://github.com/m13253/danmaku2ass and https://www.dandanplay.com/
Script Updated By: MoviePilot Danmu Plugin https://github.com/HankunYu/MoviePilot-Plugins
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
Aspect Ratio: {width}:{height}
Collisions: Normal
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {styleid}, {fontface}, {fontsize:.0f}, &H{alpha_value:02X}FFFFFF, &H{alpha_value:02X}FFFFFF, &H{alpha_value:02X}000000, &H{alpha_value:02X}000000, 0, 0, 0, 0, 100, 100, 0.00, 0.00, 1, {max(fontsize / 25.0, 1):.0f}, 0, 7, 0, 0, 0, 0

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
        )

    @staticmethod
    def find_non_overlapping_track(tracks: Dict[int, float], current_time: float, max_tracks: int) -> int:
        possible_track = 1
        last_time_remain = 100.0
        for track in range(1, max_tracks + 1):
            if track not in tracks or current_time >= tracks[track]:
                return track
            time_remain = float(tracks[track]) - current_time
            if time_remain > last_time_remain:
                possible_track = track
        return possible_track

    @classmethod
    def convert_comments_to_ass(cls, comments: List[Dict], output_file: str, width: int,
                                height: int, fontface: str, fontsize: float, alpha: float, duration: float):
        styleid = 'Danmu'
        # 确保所有数值都是正确的类型
        width = int(width)
        height = int(height)
        fontsize = float(fontsize)
        subtitle_area_height = 150

        # 调整最大轨道数，使弹幕更密集
        max_tracks = int((height - subtitle_area_height) / (fontsize * 0.8))
        scrolling_tracks = {}
        top_tracks = {}
        bottom_tracks = {}

        # 统计信息
        total_danmu_count = 0
        bottom_danmu_count = 0
        skipped_danmu_count = 0

        # 过滤弹幕
        comments = cls.filter_comments(comments)

        logger.info(f"{output_file} - 共匹配到{len(comments)}条弹幕。")

        with open(output_file, 'w', encoding='utf-8-sig') as f:
            cls.write_ass_head(f, width, height, fontface, fontsize, alpha, styleid)

            for comment in comments:
                try:
                    p = comment.get('p', '').split(',')
                    if len(p) < 3:
                        logger.warning(f"弹幕数据格式不正确: {comment}")
                        continue

                    timeline = float(p[0])
                    pos = int(p[1])
                    color = int(p[2])
                    text = comment.get('m', '')
                    user = str(p[3])

                    if not text:
                        continue

                    start_time = cls.convert_timestamp(timeline)
                    end_time = cls.convert_timestamp(timeline + duration)

                    gap = 1
                    text_width = len(text) * fontsize * 0.6
                    velocity = (width + text_width) / duration
                    leave_time = text_width / velocity + gap

                    color_hex = f'&H{color & 0xFFFFFF:06X}'
                    styles = ''

                    if pos == 1:  # 滚动弹幕
                        track_id = cls.find_non_overlapping_track(scrolling_tracks, timeline, max_tracks)
                        scrolling_tracks[track_id] = timeline + leave_time
                        initial_y = (track_id - 1) * fontsize + 10

                        # 检查弹幕位置是否在字幕区域内
                        if subtitle_area_height > 0 and initial_y > (height - subtitle_area_height):
                            skipped_danmu_count += 1
                            continue

                        styles = f'\\move({width}, {initial_y}, {-len(text) * fontsize}, {initial_y})'
                    elif pos == 4:  # 底部弹幕
                        bottom_danmu_count += 1
                        skipped_danmu_count += 1
                        continue
                    elif pos == 5:  # 顶部弹幕
                        track_id = cls.find_non_overlapping_track(top_tracks, timeline, max_tracks)
                        top_tracks[track_id] = timeline + duration
                        styles = f'\\an8\\pos({width / 2}, {50 + (track_id - 1) * fontsize})'
                    else:
                        styles = f'\\move(0, 0, {width}, 0)'

                    f.write(f'Dialogue: 0,{start_time},{end_time},{styleid},,0,0,0,,{{\\c{color_hex}{styles}}}{text}\n')
                    total_danmu_count += 1
                except Exception as e:
                    logger.error(f"处理弹幕数据失败: {e}, 弹幕数据: {comment}")
                    continue

            logger.info(f'弹幕生成成功 - {output_file}')
            logger.info(f'弹幕统计: 总数{total_danmu_count}, 底部弹幕{bottom_danmu_count}, 跳过{skipped_danmu_count}条')


class SubtitleProcessor:
    @staticmethod
    def get_video_streams(file_path: str) -> Dict:
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            return json.loads(result.stdout) if result.returncode == 0 else {}
        except Exception as e:
            logger.error(f"获取视频流信息失败: {e}")
            return {}

    @staticmethod
    def extract_subtitles(file_path: str, output_file: str, stream_index: int) -> bool:
        try:
            result = subprocess.run(
                ['ffmpeg', '-i', file_path, '-map', f'0:{stream_index}', '-c:s', 'ass', output_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"提取字幕失败: {e}")
            return False

    @classmethod
    def try_extract_sub(cls, file_path: str):
        streams_info = cls.get_video_streams(file_path)
        for stream in streams_info.get('streams', []):
            if stream.get('codec_type') == 'subtitle':
                stream_index = stream['index']
                base_name = os.path.splitext(file_path)[0]
                language = stream.get('tags', {}).get('language', 'unknown')

                if language not in ['zh', 'zho', 'chi', 'chs', 'cht', 'cn']:
                    continue

                output_file = f"{base_name}.{language}.ass"
                if os.path.exists(output_file):
                    os.remove(output_file)

                if cls.extract_subtitles(file_path, output_file, stream_index):
                    logger.info(f'成功提取内嵌字幕 - {output_file}')
                    break

    @staticmethod
    def find_subtitle_file(file_path: str) -> Optional[str]:
        filename = os.path.splitext(os.path.basename(file_path))[0]
        for root, _, files in os.walk(os.path.dirname(file_path)):
            for file in files:
                if (file.endswith(('.srt', '.ass', '.ssa')) and
                        'danmu' not in file and
                        file.startswith(filename)):
                    sub2 = os.path.join(root, file)
                    logger.info(f"找到字幕文件 - {sub2}")
                    return sub2
        logger.debug("没找到字幕文件")
        return None

    @staticmethod
    def combine_sub_ass(sub1: str, sub2: str) -> bool:
        if not sub1 or not sub2:
            return False

        try:
            with open(sub1, 'r', encoding='utf-8-sig') as f:
                sub1_content = f.read()

            with open(sub2, 'rb') as f:
                raw_data = f.read()
                result = chardet.detect(raw_data)
                file_encoding = result['encoding']

            with open(sub2, 'r', encoding=file_encoding) as f:
                sub2_content = f.read()

            if os.path.splitext(sub2)[1].lower() in ['.ass', '.ssa']:
                sub1ResX = re.search(r"PlayResX:\s*(\d+)", sub1_content)
                sub2ResX = re.search(r"PlayResX:\s*(\d+)", sub2_content)

                fontSizeRatio = 1
                if sub1ResX and sub2ResX:
                    fontSizeRatio = int(sub1ResX.group(1)) / int(sub2ResX.group(1)) * 0.8

                format_match = re.search(r"Format:.+", sub2_content)
                if not format_match:
                    return False

                style_lines = re.findall(r'Style:.*', sub2_content)
                for i, line in enumerate(style_lines):
                    elements = line.split(',')
                    if len(elements) >= 3:
                        elements[2] = str(int(float(elements[2]) * fontSizeRatio))
                        style_lines[i] = ','.join(elements)

                events_start = sub2_content.find('[Events]')
                if events_start == -1:
                    return False

                events_content = sub2_content[events_start + len('[Events]'):].strip()
                output = os.path.splitext(sub2)[0] + ".withDanmu.ass"

                with open(output, 'w', encoding='utf-8-sig') as f:
                    f.write(sub1_content)
                    f.write('\n[V4+ Styles]\n')
                    f.write(format_match.group())
                    f.write('\n')
                    f.write('\n'.join(style_lines))
                    f.write('\n[Events]\n')
                    f.write(events_content)

                return True

            return False

        except Exception as e:
            logger.error(f"合并字幕失败: {e}")
            return False


def danmu_generator(file_path: str, width: int = 1920, height: int = 1080,
                    fontface: str = 'Arial', fontsize: float = 50,
                    alpha: float = 0.8, duration: float = 6, onlyFromBili: bool = False,
                    use_tmdb_id: bool = False, tmdb_id: Optional[int] = None,
                    episode: Optional[int] = None, cache_ttl: Optional[int] = None) -> Optional[str]:
    try:
        comment_id = DanmuAPI.get_comment_id(file_path, use_tmdb_id, tmdb_id, episode, cache_ttl)
        if not comment_id:
            logger.info(f"未找到对应弹幕 - {file_path}")
            return None

        comments_data = DanmuAPI.get_comments(comment_id)
        if not comments_data:
            return None

        comments = sorted(comments_data["comments"], key=lambda x: float(x['p'].split(',')[0]))

        if len(comments) == 0:
            logger.info(f"弹幕数量为0，跳过生成 - {file_path}")
            return None

        # 过滤B站弹幕
        if onlyFromBili:
            comments = [comment for comment in comments if '[BiliBili]' in comment['p'].split(',')[3]]
            logger.info(f"过滤后剩余{len(comments)}条B站弹幕")

        output_file = os.path.splitext(file_path)[0] + '.danmu.ass'

        DanmuConverter.convert_comments_to_ass(
            comments, output_file,
            width=int(width),
            height=int(height),
            fontface=fontface,
            fontsize=float(fontsize),
            alpha=float(alpha),
            duration=float(duration)
        )

        sub2 = SubtitleProcessor.find_subtitle_file(file_path)
        if not sub2:
            SubtitleProcessor.try_extract_sub(file_path)
            sub2 = SubtitleProcessor.find_subtitle_file(file_path)

        if sub2:
            SubtitleProcessor.combine_sub_ass(output_file, sub2)
        else:
            logger.debug(f'未找到原生字幕，跳过合并 - {file_path}')

        return output_file

    except Exception as e:
        logger.error(f"生成弹幕失败: {e}")
        return None

