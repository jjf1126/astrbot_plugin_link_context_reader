import re
import asyncio
import traceback
import base64
from typing import Optional, List, Dict
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

# 尝试导入可选依赖
try:
    from duckduckgo_search import DDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "一个强大的LLM上下文增强插件，自动解析链接内容并支持社交平台截图。", "1.1.0", "https://github.com/your-repo/astrbot_plugin_link_reader")
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
                logger.debug(f"[LinkReader] 使用配置的 Cookie 访问: {domain}")
        
        return headers

    def _is_music_site(self, url: str) -> bool:
        """判断是否为音乐网站（包含短链接域名识别）"""
        music_domains = [
            "music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", 
            "spotify.com", "163cn.tv", "url.cn"
        ]
        return any(domain in url for domain in music_domains)

    def _clean_text(self, text: str) -> str:
        """清洗提取的文本"""
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > self.max_length:
            text = text[:self.max_length] + "...(内容过长已截断)"
        return text

    async def _get_screenshot_and_content(self, url: str):
        """使用 Playwright 获取页面内容和截图"""
        if not HAS_PLAYWRIGHT:
            return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=self.user_agent,
                    viewport={'width': 1280, 'height': 800}
                )
                page = await context.new_page()
                # 针对社交平台增加超时时间
                await page.goto(url, wait_until='networkidle', timeout=30000) 
                
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=80, full_page=False)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] Playwright 抓取/截图失败: {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        """抓取并解析内容的主逻辑"""
        domain = urlparse(url).netloc
        
        # 1. 音乐链接：直接走搜索增强，不截图
        if self._is_music_site(url) and self.enable_music_search and HAS_DDG:
            music_text = await self._handle_music_smart_search(url)
            return music_text, None
        
        # 2. 社交平台：使用 Playwright 抓取正文 + 截图
        social_platforms = ["xiaohongshu.com", "zhihu.com", "weibo.com", "bilibili.com", "douyin.com", "lofter.com"]
        if any(sp in url for sp in social_platforms) and HAS_PLAYWRIGHT:
            logger.info(f"[LinkReader] 识别为社交平台，启动浏览器模拟: {url}")
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'noscript']):
                    tag.decompose()
                
                content = ""
                if "zhihu.com" in domain:
                    main_content = soup.find('div', class_='RichContent-inner')
                    if main_content: content = main_content.get_text(separator='\n', strip=True)
                elif "xiaohongshu.com" in domain:
                    desc = soup.find('div', class_='desc') or soup.find('div', id='detail-desc')
                    if desc: content = desc.get_text(separator='\n', strip=True)
                
                if not content:
                    body = soup.find('body')
                    content = body.get_text(separator='\n', strip=True) if body else soup.get_text(separator='\n', strip=True)
                
                return self._clean_text(content), screenshot

        # 3. 常规网页：aiohttp 抓取内容，不截图
        headers = self._get_headers(domain)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=self.timeout, ssl=False) as response:
                    if response.status != 200:
                        return f"链接访问失败，状态码: {response.status}", None
                    
                    html = await response.text(errors='ignore')
                    soup = BeautifulSoup(html, 'lxml')
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'noscript']):
                        tag.decompose()
                    
                    body = soup.find('body')
                    content = body.get_text(separator='\n', strip=True) if body else soup.get_text(separator='\n', strip=True)
                    return self._clean_text(content), None
        except Exception as e:
            logger.error(f"[LinkReader] 常规抓取错误: {e}")
            return f"解析链接时发生错误: {str(e)}", None

    async def _handle_music_smart_search(self, url: str) -> str:
        """处理音乐链接：通过搜索获取具体歌词"""
        try:
            headers = {"User-Agent": self.user_agent}
            keyword = ""
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=5, ssl=False) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors='ignore')
                        soup = BeautifulSoup(html, 'lxml')
                        if soup.title:
                            keyword = soup.title.string.strip()
            
            if not keyword:
                keyword = url

            # 优化关键词提取：剔除平台后缀及曲名外的多余信息
            keyword = re.sub(r'( - .*| \| .*)$', '', keyword)
            logger.info(f"[LinkReader] 识别到音乐链接，提取关键词: {keyword}，开始搜索增强...")

            # 第二步：使用 DuckDuckGo 搜索
            search_query = f"{keyword}"
            results_text = []
            
            # 适配最新版 duckduckgo_search API
            if HAS_DDG:
                with DDGS() as ddgs:
                    results = ddgs.text(search_query, max_results=3)
                    for r in results:
                        results_text.append(f"来源: {r['title']}\n摘要: {r['body']}")
            
            if results_text:
                return f"【音乐链接智能解析】\n识别歌曲: {keyword}\n\n网络搜索歌词结果:\n" + "\n---\n".join(results_text)
            else:
                return f"识别到音乐链接: {keyword}，但未在指定的搜索网站中找到具体歌词。"

        except Exception as e:
            logger.warning(f"[LinkReader] 音乐智能解析失败: {e}")
            return f"音乐链接解析失败: {str(e)}"

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """核心钩子：拦截请求并注入链接内容及图片"""
        if not self.enable_plugin:
            return

        text = event.message_str
        if not text:
            return

        urls = self.url_pattern.findall(text)
        if not urls:
            return
        
        target_url = urls[0]
        logger.info(f"[LinkReader] 检测到链接，开始解析: {target_url}")

        content, screenshot_base64 = await self._fetch_url_content(target_url)

        if content:
            injection = self.prompt_template.format(content=content)
            # 注入文本到 prompt 属性
            req.prompt += injection
            
            # 如果有截图，以 Base64 格式注入 prompt 实现多模态参考
            if screenshot_base64:
                req.prompt += f"\n(该链接页面的截图已自动抓取，请参考图片中的文字或视觉内容进行回答。)\n图片：data:image/jpeg;base64,{screenshot_base64}"
            
            logger.info(f"[LinkReader] 已将链接内容注入 Prompt (截图: {'有' if screenshot_base64 else '无'})")
        else:
            logger.warning("[LinkReader] 未能提取到有效内容。")

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试模式：返回解析结果"""
        if not url:
            yield event.plain_result("用法: /link_debug [URL]")
            return
            
        yield event.plain_result(f"正在分析链接: {url} ...")
        content, screenshot = await self._fetch_url_content(url)
        
        msg = f"【抓取文本 (长度 {len(content)})】:\n\n{content}"
        if screenshot:
            msg += "\n\n(截图获取成功)"
        
        yield event.plain_result(msg)

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """查看插件各依赖状态"""
        status = ["【Link Reader 插件状态】"]
        status.append(f"插件总开关: {'✅ 开启' if self.enable_plugin else '❌ 关闭'}")
        status.append(f"音乐搜索支持 (DDG): {'✅ 已安装' if HAS_DDG else '❌ 未安装'}")
        status.append(f"截图功能支持 (Playwright): {'✅ 已启用' if HAS_PLAYWRIGHT else '❌ 未安装'}")
        status.append(f"内容截断阈值: {self.max_length}")
        
        status.append("\n【Cookie 配置状态】")
        platforms = ["xiaohongshu", "zhihu", "weibo", "bilibili", "douyin", "tieba", "lofter"]
        for p in platforms:
            has_cookie = "✅" if self.platform_cookies.get(p) else "❌"
            status.append(f"- {p}: {has_cookie}")
            
        yield event.plain_result("\n".join(status))
