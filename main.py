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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "自动解析链接内容，支持小红书精准切片、网易云直连及网页截图发送。", "1.8.0")
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
        """识别网易云音乐相关域名"""
        music_domains = ["music.163.com", "163cn.tv", "163.fm", "y.music.163.com"]
        return any(domain in url for domain in music_domains)

    def _contains_chinese(self, text: str) -> bool:
        """检测文本是否包含汉字"""
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False

    def _filter_lyrics(self, lyrics: str) -> str:
        """深度清洗歌词，去除元数据和时间轴"""
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
                if not any(kw in line for kw in ["歌词", "Lyric", "LRC"]): continue
            if ' ' in line and self._contains_chinese(line):
                parts = [part.strip() for part in line.split(' ') if part.strip()]
                if all(len(part) < 20 for part in parts):
                    filtered_lines.extend(parts)
                    continue
            filtered_lines.append(line)
        return '\n'.join([l for l in filtered_lines if len(l) > 1 and not l.isdigit()])

    def _clean_text(self, text: str) -> str:
        """常规网页清洗逻辑"""
        lines = text.split('\n')
        blacklist = ["沪ICP备", "公网安备", "经营许可证", "版权所有", "©", "Copyright", "加载中"]
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
        """网易云音乐解析"""
        try:
            async with aiohttp.ClientSession() as session:
                final_url = url
                if any(domain in url for domain in ["163cn.tv", "163.fm"]):
                    async with session.head(url, allow_redirects=True, timeout=8) as resp:
                        final_url = str(resp.url)

                id_match = re.search(r'id=(\d+)', final_url) or re.search(r'song/(\d+)', final_url)
                if id_match:
                    song_id = id_match.group(1)
                    api_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=-1&tv=-1"
                    headers = {"Referer": "https://music.163.com/", "Cookie": "os=pc", "User-Agent": self.user_agent}
                    async with session.get(api_url, headers=headers) as resp:
                        text = await resp.text()
                        data = json.loads(text)
                        lrc = data.get("lrc", {}).get("lyric", "")
                        if lrc: return f"【网易云解析】\n\n{self._filter_lyrics(lrc)}"
                return await self._fallback_xiaojiang_search(final_url)
        except Exception as e:
            return await self._fallback_xiaojiang_search(url)

    async def _fallback_xiaojiang_search(self, url: str) -> str:
        """搜索兜底"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": self.user_agent}, timeout=8) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    title = soup.title.string.strip() if soup.title else "未知歌曲"
            song_name = re.sub(r'( - 网易云音乐|\|.*| - 歌曲.*| - 单曲| - 专辑)$', '', title).strip()
            clean_name = re.sub(r'[（《\(【].*?[）》\)】]', '', song_name).strip()
            if ' - ' in clean_name: clean_name = clean_name.split(' - ')[0].strip()
            content = await self._search_xiaojiang(clean_name if len(clean_name) > 1 else song_name)
            return f"【歌词解析: {clean_name}】\n\n{content}" if content else "未找到歌词。"
        except: return "解析失败。"

    async def _search_xiaojiang(self, song_name: str) -> Optional[str]:
        """小江音乐网搜索"""
        search_url = f"https://xiaojiangclub.com/?s={quote(song_name)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers={"User-Agent": self.user_agent}, timeout=10) as resp:
                    soup = BeautifulSoup(await resp.text(), 'lxml')
                    link = soup.find('a', class_='song-link', href=True)
                    if not link: return None
                    target = link['href'] if link['href'].startswith("http") else "https://xiaojiangclub.com" + link['href']
                    async with session.get(target, timeout=10) as l_resp:
                        l_soup = BeautifulSoup(await l_resp.text(), 'lxml')
                        container = l_soup.find('div', class_='entry-content') or l_soup.find('article')
                        for tag in container(['script', 'style']): tag.decompose()
                        return self._filter_lyrics(container.get_text(separator='\n', strip=True))
        except: return None

    async def _get_screenshot_and_content(self, url: str):
        """Playwright 截图并获取 HTML"""
        if not HAS_PLAYWRIGHT: return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                # 模拟移动端，因为小红书移动端结构相对简单
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
                    viewport={'width': 390, 'height': 844}
                )
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=30000)
                # 额外等待一点时间确保内容加载
                await asyncio.sleep(2)
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=80, full_page=False)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] 截图失败: {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        """核心抓取与切片逻辑"""
        if self._is_music_site(url):
            return await self._handle_music_direct_api(url), None
        
        domain = urlparse(url).netloc
        social_platforms = ["xiaohongshu.com", "xhslink.com", "zhihu.com", "weibo.com", "bilibili.com", "douyin.com"]
        
        # 社交平台采用截图 + 精准过滤
        if any(sp in domain for sp in social_platforms) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']): tag.decompose()
                
                raw_text = soup.get_text(separator='\n', strip=True)
                
                # --- 小红书精准切片逻辑 ---
                if "xiaohongshu" in domain or "xhslink" in domain:
                    marker = "电话：9501-3888"
                    if marker in raw_text:
                        # 只保留“电话：9501-3888”之后的内容
                        raw_text = raw_text.split(marker)[-1].strip()
                        logger.info(f"[LinkReader] 小红书噪音切片完成")
                
                return self._clean_text(raw_text), screenshot

        # 常规网页
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._get_headers(domain), timeout=10) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    for tag in soup(['script', 'style']): tag.decompose()
                    return self._clean_text(soup.get_text(separator='\n', strip=True)), None
        except: return "网页解析失败", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        content, screenshot_base64 = await self._fetch_url_content(urls[0])
        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                # 注入截图给 LLM (如果模型支持视觉)
                req.prompt += f"\n(附带页面截图参考)\n图片：data:image/jpeg;base64,{screenshot_base64}"

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令：发送清洗后的正文 + 图片"""
        if not url: return
        yield event.plain_result(f"🔍 深度解析中: {url}...")
        content, screenshot_base64 = await self._fetch_url_content(url)
        
        # 发送文本
        yield event.plain_result(f"【清洗后的有效正文】:\n{content}")
        
        # 发送图片（如果截图成功）
        if screenshot_base64:
            from astrbot.api.message_components import Image
            yield event.chain().append(Image.from_base64(screenshot_base64)).text("\n📸 以上为捕获的网页截图").build()

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        msg = [
            "【Link Reader 1.8.0 状态报告】",
            "网易云直连: ✅",
            "小红书切片: ✅ (自动切除页脚噪音)",
            f"Playwright 截图: {'✅ 已加载' if HAS_PLAYWRIGHT else '❌ 未就绪'}",
            "支持平台: 小红书/知乎/微博/B站/抖音/Lofter"
        ]
        yield event.plain_result("\n".join(msg))
