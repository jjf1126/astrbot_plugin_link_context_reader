import re
import asyncio
import traceback
from typing import Optional, List, Dict
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from playwright.async_api import async_playwright
from duckduckgo_search import DDGS
import base64

# 尝试导入 duckduckgo_search，如果未安装则降级处理
try:
    from duckduckgo_search import AsyncDDGS
    HAS_DDG = True
except ImportError:
    HAS_DDG = False

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

async def get_screenshot_and_content(url):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch() # 或者 p.firefox.launch(), p.webkit.launch()
            page = await browser.new_page()
            await page.goto(url, wait_until='networkidle') # 等待网络空闲，确保内容加载
            
            # 获取页面内容
            content = await page.content()
            
            # 获取截图，可以直接获取base64编码的图片数据
            screenshot_bytes = await page.screenshot(type='jpeg', quality=80) 
            screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            
            await browser.close()
            return content, screenshot_base64
    except Exception as e:
        print(f"Error during screenshot or content fetching: {e}")
        return None, None

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "一个强大的LLM上下文增强插件，自动解析链接内容。", "1.0.0", "https://github.com/your-repo/astrbot_plugin_link_reader")
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

        # 简单的域名匹配逻辑，映射配置中的 key
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
        """判断是否为音乐网站"""
        music_domains = ["music.163.com", "y.qq.com", "kugou.com", "kuwo.cn", "spotify.com", "163cn.tv"]
        return any(domain in url for domain in music_domains)

    def _clean_text(self, text: str) -> str:
        """清洗提取的文本"""
        # 移除多余空白字符
        text = re.sub(r'\s+', ' ', text).strip()
        # 截断
        if len(text) > self.max_length:
            text = text[:self.max_length] + "...(内容过长已截断)"
        return text

    async def _fetch_url_content(self, url: str) -> str:
        """抓取并解析 URL 内容的核心逻辑"""
        domain = urlparse(url).netloc
        
        # 1. 音乐链接特殊处理
        if self._is_music_site(url) and self.enable_music_search and HAS_DDG:
            return await self._handle_music_smart_search(url)
        
        # 2. 常规抓取 (包含社交媒体的 Cookie 处理)
        headers = self._get_headers(domain)
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=self.timeout, ssl=False) as response:
                    if response.status != 200:
                        return f"链接访问失败，状态码: {response.status}"
                    
                    # 获取编码
                    content_type = response.headers.get('Content-Type', '').lower()
                    charset = 'utf-8'
                    if 'charset=' in content_type:
                        charset = content_type.split('charset=')[-1]
                    
                    try:
                        html = await response.text(encoding=charset, errors='ignore')
                    except Exception:
                        html = await response.text(errors='ignore')

                    # 解析 HTML
                    soup = BeautifulSoup(html, 'lxml')
                    
                    # 移除无用标签
                    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'iframe', 'noscript']):
                        tag.decompose()

                    # 针对特定平台的简单提取优化 (示例)
                    content = ""
                    if "zhihu.com" in domain:
                        # 尝试提取知乎正文
                        main_content = soup.find('div', class_='RichContent-inner')
                        if main_content:
                            content = main_content.get_text(separator='\n', strip=True)
                    elif "xiaohongshu.com" in domain:
                        # 尝试提取小红书描述
                        desc = soup.find('div', class_='desc') or soup.find('div', id='detail-desc')
                        if desc:
                            content = desc.get_text(separator='\n', strip=True)
                    
                    # 通用提取：如果特定提取失败或未定义
                    if not content:
                        # 优先提取 body
                        body = soup.find('body')
                        if body:
                            content = body.get_text(separator='\n', strip=True)
                        else:
                            content = soup.get_text(separator='\n', strip=True)

                    return self._clean_text(content)

        except asyncio.TimeoutError:
            return "抓取超时，无法获取内容。"
        except Exception as e:
            logger.error(f"[LinkReader] 抓取错误: {e}")
            return f"解析链接时发生错误: {str(e)}"

    async def _handle_music_smart_search(self, url: str) -> str:
        """处理音乐链接：不爬取，而是通过 DDG 搜索歌词和评价"""
        try:
            # 第一步：简单尝试获取网页 Title 作为关键词
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
                keyword = url # 降级使用 URL

            # 清理 Title 中的杂项 (如 " - 网易云音乐")
            keyword = re.sub(r'( - .*| \| .*)$', '', keyword)
            logger.info(f"[LinkReader] 识别到音乐链接，提取关键词: {keyword}，开始搜索增强...")

            # 第二步：使用 DuckDuckGo 搜索
            search_query = f"{keyword} 歌词"
            results_text = []
            
            async with AsyncDDGS() as ddgs:
                async for r in ddgs.text(search_query, max_results=3):
                    results_text.append(f"来源: {r['title']}\n摘要: {r['body']}")
            
            if results_text:
                return f"【音乐链接智能解析】\n识别歌曲: {keyword}\n\n网络搜索结果:\n" + "\n---\n".join(results_text)
            else:
                return f"识别到音乐链接: {keyword}，但未搜索到相关详细信息。"

        except Exception as e:
            logger.warning(f"[LinkReader] 音乐智能解析失败: {e}")
            return "音乐链接解析失败，请尝试直接询问。"

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """核心钩子：在 LLM 请求前拦截并注入链接内容"""
        if not self.enable_plugin:
            return

        # 获取用户文本
        text = event.message_str
        if not text:
            return

        # 查找链接
        urls = self.url_pattern.findall(text)
        if not urls:
            return
        
        target_url = urls[0] # 暂时只处理第一个链接，避免过长
        logger.info(f"[LinkReader] 检测到链接，开始解析: {target_url}")

        # 发送处理中的提示（可选，这里不发送以保持无感，或根据需要发送）
        # await event.send(event.plain_result("正在阅读链接内容...")) 

        # 抓取内容
        content = await self._fetch_url_content(target_url)
        # 使用异步函数获取截图
        html_content, screenshot_base64 = await get_screenshot_and_content(original_url)

        if content:
            # 注入 Prompt
            injection = self.prompt_template.format(content=content)

            if screenshot_base64:
                req.prompt = f"{req.prompt}\n{injection_text}\n图片：data:image/jpeg;base64,{screenshot_base64}"
            else:
                
            # 这里选择追加到用户的 input_text 后面
            # 也可以选择修改 system_prompt，取决于具体效果
            # req.system_prompt += injection # 方式A
                req.prompt += injection    # 方式B：更符合直觉，像用户贴了内容进去
            
            logger.info(f"[LinkReader] 已将链接内容注入上下文 (长度: {len(content)})")
        else:
            logger.warning("[LinkReader] 未能提取到有效内容。")

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        """调试指令：直接返回抓取到的内容"""
        if not url:
            yield event.plain_result("请提供 URL，例如: /link_debug https://www.example.com")
            return
            
        yield event.plain_result(f"正在抓取: {url} ...")
        
        try:
            content = await self._fetch_url_content(url)
            yield event.plain_result(f"【抓取结果】(长度 {len(content)}):\n\n{content}")
        except Exception as e:
            yield event.plain_result(f"抓取发生异常: {e}")

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        """状态检查指令"""
        status_msg = ["【Link Reader 插件状态】"]
        status_msg.append(f"插件启用: {self.enable_plugin}")
        status_msg.append(f"音乐搜索增强: {self.enable_music_search} (依赖库: {'已安装' if HAS_DDG else '未安装'})")
        status_msg.append(f"最大截断长度: {self.max_length}")
        
        status_msg.append("\n【平台 Cookie 配置】")
        platforms = ["xiaohongshu", "zhihu", "weibo", "bilibili", "douyin", "tieba", "lofter"]
        for p in platforms:
            cookie = self.platform_cookies.get(p, "")
            state = "✅ 已配置" if cookie else "❌ 未配置 (使用游客模式)"
            status_msg.append(f"- {p}: {state}")
            
        yield event.plain_result("\n".join(status_msg))
