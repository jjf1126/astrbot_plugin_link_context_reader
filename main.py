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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "自动解析链接内容，支持多平台音乐 ID 直连及 xiaojiangclub.com 定向搜索。", "1.5.0")
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
        """判断是否为音乐网站"""
        music_domains = ["music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "163cn.tv", "url.cn", "163.fm"]
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
            # 去除时间标签 [00:00.00]
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            # 去除 [id:xxx] 等标签
            if not line or (line.startswith('[') and line.endswith(']')): continue
            
            # 过滤掉常见的作词作曲信息行
            if ((':' in line or '：' in line) and len(line) < 30) or ' - ' in line:
                if not any(kw in line for kw in ["歌词", "Lyric", "LRC"]):
                    continue
            
            # 汉字歌词空格拆分逻辑
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
        """音乐直连解析入口"""
        try:
            async with aiohttp.ClientSession() as session:
                # 1. 短链接跳转处理
                final_url = url
                if any(domain in url for domain in ["163cn.tv", "url.cn", "163.fm"]):
                    async with session.head(url, allow_redirects=True, timeout=5) as resp:
                        final_url = str(resp.url)

                # --- 平台适配: 网易云 ---
                if "music.163.com" in final_url:
                    id_match = re.search(r'id=(\d+)', final_url) or re.search(r'song/(\d+)', final_url)
                    if id_match:
                        song_id = id_match.group(1)
                        # 使用用户指定的新版 API
                        api_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=-1&tv=-1"
                        headers = {"Referer": "https://music.163.com/", "Cookie": "os=pc", "User-Agent": self.user_agent}
                        async with session.get(api_url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                lrc = data.get("lrc", {}).get("lyric", "")
                                tlrc = data.get("tlyric", {}).get("lyric", "")
                                if lrc:
                                    res = f"【网易云解析】\n\n{self._filter_lyrics(lrc)}"
                                    if tlrc: res += f"\n\n【翻译】\n{self._filter_lyrics(tlrc)}"
                                    return res

                # --- 平台适配: QQ 音乐 ---
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
                                if lrc: return f"【QQ音乐解析】\n\n{self._filter_lyrics(lrc)}"
                            except: pass

                # --- 平台适配: 酷我音乐 ---
                elif "kuwo.cn" in final_url:
                    id_match = re.search(r'mid=(\d+)', final_url) or re.search(r'musicId=(\d+)', final_url)
                    if id_match:
                        mid = id_match.group(1)
                        api_url = f"http://m.kuwo.cn/newh5/singles/songinfoandlrc?musicId={mid}"
                        async with session.get(api_url) as resp:
                            data = await resp.json()
                            lrc_list = data.get("data", {}).get("lrclist", [])
                            if lrc_list:
                                lrc_text = "\n".join([i['lineLyric'] for i in lrc_list])
                                return f"【酷我音乐解析】\n\n{lrc_text}"

                # --- 平台适配: 酷狗音乐 ---
                elif "kugou.com" in final_url:
                    hash_match = re.search(r'hash=([a-fA-F0-9]{32})', final_url.lower())
                    if hash_match:
                        f_hash = hash_match.group(1)
                        api_url = f"http://krcs.kugou.com/search?ver=1&man=yes&client=mobi&hash={f_hash}"
                        async with session.get(api_url) as resp:
                            # 酷狗接口较为严格，若无结果将跳转兜底搜索
                            pass

                # 以上直连均失败，触发 xiaojiangclub 兜底搜索
                return await self._fallback_xiaojiang_search(final_url)

        except Exception as e:
            logger.error(f"[LinkReader] 音乐 API 解析异常: {e}")
            return await self._fallback_xiaojiang_search(url)

    async def _fallback_xiaojiang_search(self, url: str) -> str:
        """兜底逻辑：获取标题并在 xiaojiangclub.com 搜索第一个结果"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": self.user_agent}, timeout=5) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    title = soup.title.string.strip() if soup.title else "未知歌曲"
            
            # 清理标题得到纯净歌名
            song_name = re.sub(r'( - 网易云音乐| - QQ音乐| - 酷狗音乐| - 酷我音乐|\|.*| - 歌曲.*)$', '', title).strip()
            song_name = re.sub(r'^歌曲：', '', song_name)
            
            logger.info(f"[LinkReader] 正在 xiaojiangclub.com 搜索: {song_name}")
            content = await self._search_xiaojiang(song_name)
            
            if content:
                return f"【歌词解析: {song_name}】\n来源: 小江音乐网\n\n{content}"
            return f"识别到歌曲《{song_name}》，但未能获取歌词正文。"
        except Exception:
            return "音乐链接解析失败。"

    async def _search_xiaojiang(self, song_name: str) -> Optional[str]:
        """xiaojiangclub.com 搜索第一个链接获取歌词"""
        search_url = f"https://xiaojiangclub.com/?s={quote(song_name)}"
        headers = {"User-Agent": self.user_agent}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url, headers=headers, timeout=10) as resp:
                    if resp.status != 200: return None
                    soup = BeautifulSoup(await resp.text(), 'lxml')
                    
                    # 查找第一个搜索结果链接 (通常在 h2.entry-title 或 article a 中)
                    first_result = soup.find('h2', class_='entry-title') or soup.find('article')
                    if not first_result:
                        return None
                    
                    target_link_tag = first_result.find('a', href=True)
                    if not target_link_tag: return None
                    target_link = target_link_tag['href']
                    
                    # 访问详情页
                    logger.info(f"[LinkReader] 正在访问搜索结果: {target_link}")
                    async with session.get(target_link, headers=headers, timeout=10) as l_resp:
                        l_soup = BeautifulSoup(await l_resp.text(), 'lxml')
                        
                        # 提取歌词容器 (entry-content 是 WordPress 常用的正文类名)
                        content_container = l_soup.find('div', class_='entry-content') or l_soup.find('article')
                        if not content_container: content_container = l_soup
                        
                        # 清洗无关元素
                        for tag in content_container(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'button']):
                            tag.decompose()
                            
                        raw_text = content_container.get_text(separator='\n', strip=True)
                        return self._filter_lyrics(raw_text)
        except Exception as e:
            logger.error(f"[LinkReader] Xiaojiang 搜索失败: {e}")
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
        """拦截 LLM 请求注入上下文"""
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        
        target_url = urls[0]
        content, screenshot_base64 = await self._fetch_url_content(target_url)

        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带页面截图参考)\n图片：data:image/jpeg;base64,{screenshot_base64}"
            logger.info(f"[LinkReader] 内容已成功注入 Prompt")

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令"""
        if not url: return
        yield event.plain_result(f"🔍 正在进行多模式深度解析: {url}...")
        content, screenshot = await self._fetch_url_content(url)
        msg = f"【解析正文内容】:\n{content}"
        if screenshot: msg += "\n\n(已成功捕获视觉截图)"
        yield event.plain_result(msg)

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """插件状态检查"""
        status_msg = ["【Link Reader 插件状态报告】"]
        status_msg.append(f"插件运行: {'✅ 正常' if self.enable_plugin else '❌ 已禁用'}")
        status_msg.append(f"直连 API 支持: 网易云/QQ/酷我/酷狗")
        status_msg.append(f"智能兜底源: xiaojiangclub.com")
        status_msg.append(f"Playwright 截图: {'✅ 已加载' if HAS_PLAYWRIGHT else '❌ 未就绪'}")
        status_msg.append(f"正文最大截断: {self.max_length} 字")
        yield event.plain_result("\n".join(status_msg))
