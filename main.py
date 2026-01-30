import re
import asyncio
import traceback
import base64
import json
from typing import Optional, List, Dict, Tuple
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

@register("astrbot_plugin_link_reader", "AstrBot_Developer", "适配 Aiocqhttp 修复异常，强化小红书页脚备案信息清洗。", "1.8.3")
class LinkReaderPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.general_config = self.config.get("general_config", {})
        self.enable_plugin = self.general_config.get("enable_plugin", True)
        self.max_length = self.general_config.get("max_content_length", 2000)
        self.timeout = self.general_config.get("request_timeout", 15)
        self.user_agent = self.general_config.get("user_agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
        self.prompt_template = self.general_config.get("prompt_template", "\n【链接解析内容】：\n{content}\n")

    def _is_music_site(self, url: str) -> bool:
        return any(domain in url for domain in ["music.163.com", "163cn.tv", "163.fm"])

    def _filter_lyrics(self, lyrics: str) -> str:
        if not lyrics: return ""
        lines = [l.strip() for l in lyrics.replace('\\n', '\n').split('\n') if l.strip()]
        filtered = []
        for line in lines:
            line = re.sub(r'\[\d+:\d+\.\d+\]', '', line).strip()
            if not line or (line.startswith('[') and line.endswith(']')): continue
            filtered.append(line)
        return '\n'.join(filtered)

    def _clean_text(self, text: str, is_xhs: bool = False) -> str:
        """深度清洗：针对小红书进行切片处理"""
        if is_xhs:
            # 策略：寻找最后一个“电话：9501-3888”标记，并切除其及之前的内容
            marker = "电话：9501-3888"
            if marker in text:
                text = text.split(marker)[-1].strip()
            
            # 进一步过滤“更多”、“关注”等紧跟在博主名字后的干扰项
            text = re.sub(r'^(更多\n|关注\n|创作中心\n|业务合作\n)+', '', text, flags=re.MULTILINE)

        blacklist = [
            "沪ICP备", "公网安备", "经营许可证", "版权所有", "©", "Copyright", "加载中",
            "医疗器械", "网信算备", "资格证书", "上海市互联网举报中心", "违法不良信息", "登录", "发现"
        ]
        lines = text.split('\n')
        cleaned = []
        for line in lines:
            line = line.strip()
            if not line or len(line) < 1 or any(kw in line for kw in blacklist): continue
            cleaned.append(line)
        
        result = '\n'.join(cleaned)
        return result[:self.max_length]

    async def _get_screenshot_and_content(self, url: str):
        if not HAS_PLAYWRIGHT: return None, None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
                context = await browser.new_context(user_agent=self.user_agent, viewport={'width': 390, 'height': 844})
                page = await context.new_page()
                await page.goto(url, wait_until='networkidle', timeout=30000)
                await asyncio.sleep(4)
                content = await page.content()
                screenshot_bytes = await page.screenshot(type='jpeg', quality=80)
                screenshot_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                await browser.close()
                return content, screenshot_base64
        except Exception as e:
            logger.error(f"[LinkReader] 浏览器组件异常: {e}")
            return None, None

    async def _fetch_url_content(self, url: str):
        domain = urlparse(url).netloc
        is_xhs = any(sp in domain for sp in ["xiaohongshu.com", "xhslink.com"])
        
        # 针对音乐网站
        if self._is_music_site(url):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url, allow_redirects=True, timeout=5) as r:
                        f_url = str(r.url)
                    id_match = re.search(r'id=(\d+)', f_url)
                    if id_match:
                        api = f"https://music.163.com/api/song/lyric?id={id_match.group(1)}&lv=-1&tv=-1"
                        async with session.get(api, headers={"Referer": "https://music.163.com/"}) as resp:
                            data = json.loads(await resp.text())
                            return self._filter_lyrics(data.get("lrc", {}).get("lyric", "")), None
            except: pass

        # 针对社交平台采用截图
        if (is_xhs or "zhihu.com" in domain) and HAS_PLAYWRIGHT:
            html, screenshot = await self._get_screenshot_and_content(url)
            if html:
                soup = BeautifulSoup(html, 'lxml')
                # 针对小红书正文 DOM 定向提取
                content_node = soup.find(class_=re.compile(r'note-content|desc'))
                if content_node:
                    text = content_node.get_text(separator='\n', strip=True)
                else:
                    text = soup.get_text(separator='\n', strip=True)
                return self._clean_text(text, is_xhs=is_xhs), screenshot

        # 常规抓取
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    soup = BeautifulSoup(await resp.text(errors='ignore'), 'lxml')
                    return self._clean_text(soup.get_text(separator='\n', strip=True)), None
        except: return "解析失败", None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_plugin: return
        urls = self.url_pattern.findall(event.message_str)
        if not urls: return
        content, screenshot_base64 = await self._fetch_url_content(urls[0])
        if content:
            req.prompt += self.prompt_template.format(content=content)
            if screenshot_base64:
                req.prompt += f"\n(附带截图: data:image/jpeg;base64,{screenshot_base64})"

    @filter.command("link_debug")
    async def link_debug(self, event: AstrMessageEvent, url: str):
        if not url: return
        yield event.plain_result(f"🔍 深度解析 v1.8.3: {url}")
        content, screenshot_base64 = await self._fetch_url_content(url)
        
        # 修复 Aiocqhttp 没有 chain() 的问题：分开发送
        if screenshot_base64:
            try:
                yield event.image_result(screenshot_base64)
            except Exception as e:
                yield event.plain_result(f"❌ 图片发送失败: {e}")
        
        yield event.plain_result(f"【清洗后的正文】:\n{content}")

    @filter.command("link_status")
    async def link_status(self, event: AstrMessageEvent):
        msg = [
            "【Link Reader 1.8.3 状态】",
            "网易云解析: ✅",
            "小红书清洗: ✅ (末尾锚点法)",
            f"截图组件: {'✅ 正常' if HAS_PLAYWRIGHT else '❌ 缺失'}"
        ]
        yield event.plain_result("\n".join(msg))
