import re
import asyncio
import traceback
import base64
import json
from typing import Optional, List, Dict
from urllib.parse import urlparse, quote

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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "自动解析链接内容，支持社交平台截图及多源音乐API歌词搜索。", "1.2.0")
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

        # 加载音乐配置
        self.music_config = self.config.get("music_feature", {})
        self.enable_music_search = self.music_config.get("enable_search", True)

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
        """判断是否为音乐网站 (参考 lyricnext 兼容性)"""
        music_domains = ["music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "163cn.tv", "url.cn"]
        return any(domain in url for domain in music_domains)

    def _filter_lyrics(self, lyrics: str) -> str:
        """参考 lyricnext 的过滤逻辑，去除元数据和时间轴"""
        lines = lyrics.split('\n')
        filtered_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            # 去除时间标签 [00:00.00]
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if not line or line.startswith('['): continue
            
            # 过滤掉信息行（作词、作曲等）
            if (':' in line or '：' in line or ' - ' in line or 
                ('(' in line and ')' in line) or re.match(r'^[A-Za-z\s:]+$', line)):
                continue
            filtered_lines.append(line)
        return '\n'.join(filtered_lines)

    async def _search_netease(self, song_name: str) -> Optional[str]:
        """异步实现网易云歌词搜索"""
        try:
            async with aiohttp.ClientSession() as session:
                search_url = f"https://music.163.com/api/search/get?s={quote(song_name)}&type=1&limit=5"
                async with session.get(search_url, headers={"User-Agent": self.user_agent}) as resp:
                    data = await resp.json()
                    if 'result' in data and 'songs' in data['result'] and data['result']['songs']:
                        song_id = data['result']['songs'][0]['id']
                        lyric_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
                        async with session.get(lyric_url) as l_resp:
                            l_data = await l_resp.json()
                            return l_data.get('lrc', {}).get('lyric')
        except: return None

    async def _search_qq(self, song_name: str) -> Optional[str]:
        """异步实现QQ音乐歌词搜索"""
        try:
            async with aiohttp.ClientSession() as session:
                search_data = {"req_0": {"method": "DoSearchForQQMusicDesktop","module": "music.search.SearchCgiService","param": {"query": song_name,"page_num": 1,"num_per_page": 5,"search_type": 0}}}
                search_url = f"https://u.y.qq.com/cgi-bin/musicu.fcg?data={quote(json.dumps(search_data))}"
                async with session.get(search_url) as resp:
                    data = await resp.json()
                    song_mid = data['req_0']['data']['body']['song']['list'][0]['mid']
                    lyric_url = f"https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid={song_mid}&format=json&nobase64=1"
                    headers = {"Referer": "https://y.qq.com/", "User-Agent": self.user_agent}
                    async with session.get(lyric_url, headers=headers) as l_resp:
                        l_data = await l_resp.json()
                        if 'lyric' in l_data:
                            return base64.b64decode(l_data['lyric']).decode('utf-8')
        except: return None

    async def _handle_music_smart_search(self, url: str) -> str:
        """核心音乐解析：仅根据曲名搜索歌词"""
        try:
            headers = {"User-Agent": self.user_agent}
            keyword = ""
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=5, ssl=False) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors='ignore')
                        soup = BeautifulSoup(html, 'lxml')
                        if soup.title: keyword = soup.title.string.strip()
            
            if not keyword: keyword = url
            # 提取曲名逻辑
            song_name = re.sub(r'( - 网易云音乐| - QQ音乐| - 酷狗音乐| - 酷我音乐|\|.*)$', '', keyword).strip()
            song_name = re.sub(r' - .*$', '', song_name).strip() # 只要曲名
            
            logger.info(f"[LinkReader] 正在从 API 检索歌词: {song_name}")
            
            # 多源检索
            raw_lyric = await self._search_netease(song_name) or await self._search_qq(song_name)
            
            if raw_lyric:
                clean_lyric = self._filter_lyrics(raw_lyric)
                return f"【音乐解析结果】\n识别歌曲: {song_name}\n\n歌词内容:\n{clean_lyric[:1500]}"
            else:
                return f"识别到歌曲《{song_name}》，但未能检索到有效的纯净歌词。"
        except Exception as e:
            return f"音乐解析异常: {str(e)}"

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
        """抓取逻辑入口"""
        if self._is_music_site(url):
            return await self._handle_music_smart_search(url), None
        
        social_platforms = ["xiaohongshu.com", "zhihu.com", "weibo.com", "bilibili.com", "douyin.com", "lofter.com"]
        if any(sp in url for sp in social_platforms) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'noscript']): tag.decompose()
                body = soup.find('body')
                content = body.get_text(separator='\n', strip=True) if body else soup.get_text(separator='\n', strip=True)
                return content[:2000], screenshot

        # 常规网页抓取
        headers = self._get_headers(urlparse(url).netloc)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10, ssl=False) as resp:
                    html = await resp.text(errors='ignore')
                    soup = BeautifulSoup(html, 'lxml')
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'noscript']): tag.decompose()
                    body = soup.find('body')
                    content = body.get_text(separator='\n', strip=True) if body else soup.get_text(separator='\n', strip=True)
                    return content[:2000], None
        except Exception as e:
            return f"解析出错: {str(e)}", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        
        target_url = urls[0]
        content, screenshot_base64 = await self._fetch_url_content(target_url)

        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带页面截图参考)\n图片：data:image/jpeg;base64,{screenshot_base64}"

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令：直接输出抓取内容"""
        if not url: return
        yield event.plain_result(f"正在进行深度解析: {url}...")
        content, _ = await self._fetch_url_content(url)
        yield event.plain_result(f"【抓取预览】:\n{content}")

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """完整状态检查指令"""
        status_msg = ["【Link Reader 插件详尽状态】"]
        status_msg.append(f"插件总开关: {'✅ 开启' if self.enable_plugin else '❌ 关闭'}")
        status_msg.append(f"音乐解析增强: {'✅ 已启用 (网易云/QQ音乐 API)' if self.enable_music_search else '❌ 已禁用'}")
        status_msg.append(f"截图功能支持: {'✅ 已就绪 (Playwright)' if HAS_PLAYWRIGHT else '❌ 未就绪 (缺失 Playwright)'}")
        status_msg.append(f"最大内容长度: {self.max_length} 字符")
        
        status_msg.append("\n【社交平台 Cookie 状态】")
        platforms = ["xiaohongshu", "zhihu", "weibo", "bilibili", "douyin", "tieba", "lofter"]
        for p in platforms:
            cookie = self.platform_cookies.get(p, "")
            state = "✅ 已配置" if cookie else "❌ 未配置 (游客访问)"
            status_msg.append(f"- {p}: {state}")
            
        yield event.plain_result("\n".join(status_msg))
