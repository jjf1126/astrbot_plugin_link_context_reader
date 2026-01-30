import re
import asyncio
import traceback
import base64
import json
from typing import Optional, List, Dict, Tuple
from urllib.parse import urlparse, quote, parse_qs

import aiohttp
from bs4 import BeautifulSoup

# 尝试导入 Playwright 截图组件
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "自动解析链接内容，支持全平台音乐短链追踪及深度歌词解析。", "1.6.0")
class LinkReaderPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 加载基础配置
        self.general_config = self.config.get("general_config", {})
        self.enable_plugin = self.general_config.get("enable_plugin", True)
        self.max_length = self.general_config.get("max_content_length", 2000)
        self.timeout = self.general_config.get("request_timeout", 15)
        self.user_agent = self.general_config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        self.prompt_template = self.general_config.get("prompt_template", "\n【以下是链接的具体内容，请参考该内容进行回答】：\n{content}\n")

        # 加载平台 Cookie
        self.platform_cookies = self.config.get("platform_cookies", {})

        # URL 匹配正则
        self.url_pattern = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.-]*\??[\w=&%\.-]*')

    def _get_headers(self, domain: str = "") -> dict:
        """根据域名获取对应的 Headers (包含 Cookie)"""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        cookie_key = None
        if "xiaohongshu" in domain: cookie_key = "xiaohongshu"
        elif "zhihu" in domain: cookie_key = "zhihu"
        elif "weibo" in domain: cookie_key = "weibo"
        elif "bilibili" in domain: cookie_key = "bilibili"
        elif "douyin" in domain: cookie_key = "douyin"
        elif "tieba.baidu" in domain: cookie_key = "tieba"
        elif "lofter" in domain: cookie_key = "lofter"

        if cookie_key:
            cookie_val = self.platform_cookies.get(cookie_key, "")
            if cookie_val:
                headers["Cookie"] = cookie_val
        return headers

    def _is_music_site(self, url: str) -> bool:
        """全平台音乐域名识别（含各类短链域名）"""
        music_domains = [
            # 网易云
            "music.163.com", "163cn.tv", "163.fm", "y.music.163.com",
            # QQ 音乐
            "y.qq.com", "c6.y.qq.com", "c.y.qq.com", "u.y.qq.com", "url.cn",
            # 酷狗
            "kugou.com", "t.kugou.com", "fanxing.kugou.com",
            # 酷我
            "kuwo.cn", "t.kuwo.cn",
            # 咪咕/B站音乐等
            "migu.cn", "b23.tv"
        ]
        return any(domain in url for domain in music_domains)

    def _contains_chinese(self, text: str) -> bool:
        """检测文本是否包含汉字"""
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False

    def _filter_lyrics(self, lyrics: str) -> str:
        """深度清洗逻辑，去除元数据和时间轴"""
        if not lyrics: return ""
        lyrics = lyrics.replace('\\n', '\n').replace('\\r', '')
        lines = lyrics.split('\n')
        filtered_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if not line or (line.startswith('[') and line.endswith(']')): continue
            
            if ((':' in line or '：' in line) and len(line) < 35) or ' - ' in line:
                if not any(kw in line for kw in ["歌词", "Lyric", "LRC", "文本"]):
                    continue
            
            if ' ' in line and self._contains_chinese(line):
                parts = [part.strip() for part in line.split(' ') if part.strip()]
                if all(len(part) < 20 for part in parts):
                    filtered_lines.extend(parts)
                    continue
            
            filtered_lines.append(line)
        
        final_lines = [l for l in filtered_lines if len(l) > 1 and not l.isdigit()]
        return '\n'.join(final_lines)

    def _clean_text(self, text: str) -> str:
        """常规网页清洗逻辑"""
        lines = text.split('\n')
        blacklist = ["沪ICP备", "公网安备", "经营许可证", "版权所有", "©", "Copyright", "下载APP", "打开APP"]
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            if not line or len(line) < 2 or any(kw in line for kw in blacklist):
                continue
            cleaned_lines.append(line)
        result = '\n'.join(cleaned_lines)
        if len(result) > self.max_length:
            result = result[:self.max_length] + "...(内容过长已截断)"
        return result

    async def _handle_music_direct_api(self, url: str) -> str:
        """音乐直连解析入口：支持全平台短链追踪"""
        try:
            async with aiohttp.ClientSession() as session:
                # 1. 全平台短链接追踪 (Redirection Tracking)
                final_url = url
                short_link_domains = [
                    "163cn.tv", "163.fm", "url.cn", "c6.y.qq.com", 
                    "u.y.qq.com", "t.kugou.com", "t.kuwo.cn", "b23.tv"
                ]
                if any(domain in url for domain in short_link_domains) or "base/fcgi-bin/u" in url:
                    headers = {"User-Agent": self.user_agent}
                    async with session.head(url, allow_redirects=True, timeout=8, headers=headers) as resp:
                        final_url = str(resp.url)
                        logger.info(f"[LinkReader] 短链重定向成功: {url} -> {final_url}")

                # --- 平台解析: 网易云 ---
                if "music.163.com" in final_url:
                    id_match = re.search(r'id=(\d+)', final_url) or re.search(r'song/(\d+)', final_url)
                    if id_match:
                        song_id = id_match.group(1)
                        api_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=-1&tv=-1"
                        headers = {"Referer": "https://music.163.com/", "Cookie": "os=pc", "User-Agent": self.user_agent}
                        async with session.get(api_url, headers=headers) as resp:
                            text = await resp.text()
                            data = json.loads(text)
                            lrc = data.get("lrc", {}).get("lyric", "")
                            tlrc = data.get("tlyric", {}).get("lyric", "")
                            if lrc:
                                res = f"【网易云解析 (ID: {song_id})】\n\n{self._filter_lyrics(lrc)}"
                                if tlrc: res += f"\n\n【翻译】\n{self._filter_lyrics(tlrc)}"
                                return res

                # --- 平台解析: QQ 音乐 ---
                elif "y.qq.com" in final_url:
                    mid_match = re.search(r'songmid=([a-zA-Z0-9]+)', final_url) or re.search(r'songDetail/([a-zA-Z0-9]+)', final_url)
                    if mid_match:
                        mid = mid_match.group(1)
                        api_url = f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={mid}&format=json&nobase64=1"
                        headers = {"Referer": "https://y.qq.com/", "User-Agent": self.user_agent}
                        async with session.get(api_url, headers=headers) as resp:
                            text = await resp.text()
                            try:
                                data = json.loads(re.sub(r'^\w+\(|\)$', '', text))
                                lrc = data.get("lyric", "")
                                if lrc: return f"【QQ音乐解析 (MID: {mid})】\n\n{self._filter_lyrics(lrc)}"
                            except: pass

                # --- 平台解析: 酷我音乐 ---
                elif "kuwo.cn" in final_url:
                    id_match = re.search(r'mid=(\d+)', final_url) or re.search(r'musicId=(\d+)', final_url)
                    if id_match:
                        mid = id_match.group(1)
                        api_url = f"http://m.kuwo.cn/newh5/singles/songinfoandlrc?musicId={mid}"
                        async with session.get(api_url) as resp:
                            text = await resp.text()
                            data = json.loads(text)
                            lrc_list = data.get("data", {}).get("lrclist", [])
                            if lrc_list:
                                lrc_text = "\n".join([i['lineLyric'] for i in lrc_list])
                                return f"【酷我音乐解析】\n\n{lrc_text}"

                # 无法直连则进入兜底搜索
                return await self._fallback_xiaojiang_search(final_url)

        except Exception as e:
            logger.error(f"[LinkReader] 音乐 API 直连异常: {e}")
            return await self._fallback_xiaojiang_search(url)

    async def _fallback_xiaojiang_search(self, url: str) -> str:
        """关键词精简兜底搜索"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": self.user_agent}, timeout=8) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    title = soup.title.string.strip() if soup.title else "未知歌曲"
            
            # 1. 基础清洗：移除平台后缀
            song_name = re.sub(r'( - 网易云音乐| - QQ音乐| - 酷狗音乐| - 酷我音乐|\|.*| - 歌曲.*| - 单曲| - 专辑| - 咪咕音乐)$', '', title).strip()
            song_name = re.sub(r'^(歌曲|单曲|分享|正在播放)：', '', song_name)
            
            # 2. 深度清洗：移除括号、周年曲等装饰性内容
            clean_name = re.sub(r'[（《\(【].*?[）》\)】]', '', song_name).strip()
            
            # 3. 歌手拆分：取第一个 '-' 前后的核心部分
            if ' - ' in clean_name:
                parts = clean_name.split(' - ')
                # 优先保留歌名，若第一部分太短（如符号）则取第二部分
                clean_name = parts[0].strip() if len(parts[0].strip()) > 1 else parts[1].strip()
            
            # 4. 安全校验：防止搜出空字符串或纯符号
            final_keyword = clean_name if len(re.sub(r'[^\w\u4e00-\u9fff]', '', clean_name)) >= 1 else song_name
            if not final_keyword: final_keyword = title[:15]

            logger.info(f"[LinkReader] 触发兜底搜索，关键词: {final_keyword}")
            content = await self._search_xiaojiang(final_keyword)
            
            if content:
                return f"【歌词解析: {final_keyword}】\n来源: 小江音乐网\n\n{content}"
            return f"识别到音乐链接，但在搜索《{final_keyword}》时未能匹配到歌词正文。"
        except Exception:
            return "音乐链接解析失败。"

    async def _search_xiaojiang(self, song_name: str) -> Optional[str]:
        """小江音乐网搜索逻辑"""
        search_url = f"https://xiaojiangclub.com/?s={quote(song_name)}"
        base_domain = "https://xiaojiangclub.com"
        headers = {"User-Agent": self.user_agent}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers=headers, timeout=10) as resp:
                    if resp.status != 200: return None
                    soup = BeautifulSoup(await resp.text(), 'lxml')
                    
                    target_link_tag = soup.find('a', class_='song-link', href=True)
                    if not target_link_tag: return None
                    
                    target_path = target_link_tag['href']
                    target_link = target_path if target_path.startswith("http") else base_domain + target_path
                    
                    async with session.get(target_link, headers=headers, timeout=10) as l_resp:
                        l_soup = BeautifulSoup(await l_resp.text(), 'lxml')
                        container = l_soup.find('div', class_='entry-content') or l_soup.find('article') or l_soup.find('div', class_='post-content')
                        if not container: container = l_soup
                        
                        for tag in container(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'button']):
                            tag.decompose()
                            
                        return self._filter_lyrics(container.get_text(separator='\n', strip=True))
        except: pass
        return None

    async def _get_screenshot_and_content(self, url: str):
        """Playwright 浏览器自动化截图"""
        if not HAS_PLAYWRIGHT: return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(user_agent=self.user_agent, viewport={'width': 1280, 'height': 800})
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=30000)
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=80)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] 截图失败: {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        """网页抓取主入口"""
        if self._is_music_site(url):
            return await self._handle_music_direct_api(url), None
        
        domain = urlparse(url).netloc
        social_platforms = ["xiaohongshu.com", "zhihu.com", "weibo.com", "bilibili.com", "douyin.com", "lofter.com"]
        if any(sp in domain for sp in social_platforms) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']): tag.decompose()
                content = soup.get_text(separator='\n', strip=True)
                return self._clean_text(content), screenshot

        headers = self._get_headers(domain)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10, ssl=False) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header']): tag.decompose()
                    return self._clean_text(soup.get_text(separator='\n', strip=True)), None
        except Exception as e:
            return f"网页解析出错: {str(e)}", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """拦截 LLM 请求注入上下文"""
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        
        target_url = urls[0]
        content, screenshot_base64 = await self._fetch_url_content(target_url)

        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带页面截图)\n图片：data:image/jpeg;base64,{screenshot_base64}"
            logger.info(f"[LinkReader] 成功注入链接内容")

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令"""
        if not url: return
        yield event.plain_result(f"🔍 正在深度解析链接: {url}...")
        content, screenshot = await self._fetch_url_content(url)
        msg = f"【解析正文内容】:\n{content}"
        yield event.plain_result(msg)

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """状态检查"""
        msg = [
            "【Link Reader 1.6.0 状态报告】",
            "全平台短链追踪: ✅ (163/QQ/Kugou/Kuwo/Bili)",
            "音乐直连 API: ✅ (网易/QQ/酷我)",
            "智能精简搜索: ✅ (XiaojiangClub)",
            f"正文最大长度: {self.max_length}"
        ]
        yield event.plain_result("\n".join(msg))
