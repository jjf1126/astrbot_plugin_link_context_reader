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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "自动解析链接内容，网易云直连解析 + 社交平台截图解析。", "1.7.1")
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
        """仅识别网易云音乐相关域名"""
        music_domains = ["music.163.com", "163cn.tv", "163.fm", "y.music.163.com"]
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
                if not any(kw in line for kw in ["歌词", "Lyric", "LRC"]):
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
        """网页正文清洗"""
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
        """网易云音乐直连解析"""
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
                        tlrc = data.get("tlyric", {}).get("lyric", "")
                        if lrc:
                            res = f"【网易云解析 (ID: {song_id})】\n\n{self._filter_lyrics(lrc)}"
                            if tlrc: res += f"\n\n【翻译】\n{self._filter_lyrics(tlrc)}"
                            return res

                return await self._fallback_xiaojiang_search(final_url)

        except Exception as e:
            logger.error(f"[LinkReader] 网易云 API 异常: {e}")
            return await self._fallback_xiaojiang_search(url)

    async def _fallback_xiaojiang_search(self, url: str) -> str:
        """通用歌词搜索兜底"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": self.user_agent}, timeout=8) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    title = soup.title.string.strip() if soup.title else "未知歌曲"
            
            song_name = re.sub(r'( - 网易云音乐|\|.*| - 歌曲.*| - 单曲| - 专辑)$', '', title).strip()
            clean_name = re.sub(r'[（《\(【].*?[）》\)】]', '', song_name).strip()
            
            if ' - ' in clean_name:
                parts = clean_name.split(' - ')
                clean_name = parts[0].strip() if len(parts[0].strip()) > 1 else parts[1].strip()
            
            final_keyword = clean_name if len(re.sub(r'[^\w\u4e00-\u9fff]', '', clean_name)) >= 1 else song_name

            content = await self._search_xiaojiang(final_keyword)
            if content:
                return f"【歌词解析: {final_keyword}】\n来源: 小江音乐网\n\n{content}"
            return f"识别到音乐链接，但在搜索《{final_keyword}》时未能匹配到歌词。"
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
                    
                    target_link = target_link_tag['href'] if target_link_tag['href'].startswith("http") else base_domain + target_link_tag['href']
                    
                    async with session.get(target_link, headers=headers, timeout=10) as l_resp:
                        l_soup = BeautifulSoup(await l_resp.text(), 'lxml')
                        container = l_soup.find('div', class_='entry-content') or l_soup.find('article')
                        if not container: container = l_soup
                        for tag in container(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']): tag.decompose()
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
        """主入口：区分网易云、社交平台、常规网页"""
        if self._is_music_site(url):
            return await self._handle_music_direct_api(url), None
        
        domain = urlparse(url).netloc
        social_platforms = ["xiaohongshu.com", "zhihu.com", "weibo.com", "bilibili.com", "douyin.com", "lofter.com"]
        
        # 社交平台截图解析
        if any(sp in domain for sp in social_platforms) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe']): tag.decompose()
                if "xiaohongshu.com" in url:
                    content_div = soup.find(class_=re.compile(r'desc|note-content|text'))
                    content = content_div.get_text(separator='\n', strip=True) if content_div else soup.get_text(separator='\n', strip=True)
                else:
                    content = soup.get_text(separator='\n', strip=True)
                return self._clean_text(content), screenshot

        # 常规网页抓取
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
        """注入上下文"""
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        
        target_url = urls[0]
        content, screenshot_base64 = await self._fetch_url_content(target_url)
        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带页面截图)\n图片：data:image/jpeg;base64,{screenshot_base64}"

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令"""
        if not url: return
        yield event.plain_result(f"🔍 正在解析链接: {url}...")
        content, screenshot = await self._fetch_url_content(url)
        msg = f"【解析正文】:\n{content}"
        yield event.plain_result(msg)

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """状态检查 - 重新加入社交平台显示"""
        msg = [
            "【Link Reader 1.7.1 状态报告】",
            "网易云解析: ✅ (ID直连 + 搜索兜底)",
            "社交平台截图: ✅ (小红书/知乎/微博/B站/抖音/Lofter)",
            "智能搜索源: xiaojiangclub.com ✅",
            f"截图引擎 (Playwright): {'✅ 已加载' if HAS_PLAYWRIGHT else '❌ 未就绪'}",
            f"内容截断长度: {self.max_length} 字"
        ]
        yield event.plain_result("\n".join(msg))
