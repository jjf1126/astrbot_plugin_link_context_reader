import re
import asyncio
import logging
import json
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup
from jinja2 import Template

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig

@register("astrbot_plugin_link_context_reader", "YourName", "智能链接内容读取与LLM上下文增强插件", "1.0.0", "https://github.com/YourName/astrbot_plugin_link_context_reader")
class LinkContextReader(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 编译 URL 匹配正则
        self.url_pattern = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:[/?]\S*)?')
        
        # 音乐平台域名特征
        self.music_domains = ['music.163.com', 'y.qq.com', 'kugou.com', 'kuwo.cn']
        # 社交平台域名特征
        self.social_domains = ['zhihu.com', 'weibo.com', 'weibo.cn', 'xiaohongshu.com', 'lofter.com']

    @filter.command("link_reader_status")
    async def link_reader_status(self, event: AstrMessageEvent):
        """查看当前链接解析服务的状态"""
        status = "开启" if self.config.get("enable_auto_parse", True) else "关闭"
        blacklist = self.config.get("blacklisted_domains", [])
        
        msg = (
            f"🔗 链接解析服务状态: {status}\n"
            f"🌐 当前黑名单域名数: {len(blacklist)}\n"
            f"📝 内容截断长度: {self.config.get('max_content_length', 1500)}\n"
            f"⏱️ 请求超时时间: {self.config.get('request_timeout', 10)}秒"
        )
        yield event.plain_result(msg)

    @filter.command("toggle_link_reader")
    async def toggle_link_reader(self, event: AstrMessageEvent):
        """开启或关闭链接自动解析功能"""
        current_status = self.config.get("enable_auto_parse", True)
        new_status = not current_status
        self.config["enable_auto_parse"] = new_status
        self.config.save_config() # 保存配置
        
        status_str = "已开启" if new_status else "已关闭"
        yield event.plain_result(f"🔗 链接自动解析功能{status_str}。")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        拦截 LLM 请求，检测 URL 并注入内容
        """
        # 1. 检查开关
        if not self.config.get("enable_auto_parse", True):
            return

        # 2. 提取 URL
        # 注意：这里优先检查 event.message_str，因为它包含原始用户消息
        text = event.message_str or ""
        urls = self.url_pattern.findall(text)
        
        if not urls:
            return

        # 只处理第一个 URL，避免过多请求
        target_url = urls[0]
        
        # 3. 检查黑名单
        domain = urlparse(target_url).netloc
        blacklist = self.config.get("blacklisted_domains", [])
        if any(d in domain for d in blacklist):
            logger.info(f"[LinkReader] Domain {domain} is blacklisted, skipping.")
            return

        # 4. 路由处理与内容获取
        logger.info(f"[LinkReader] Detected URL: {target_url}, start fetching...")
        try:
            parse_result = await self._fetch_and_parse(target_url)
            
            if not parse_result:
                return

            # 5. 渲染注入模板
            template_str = self.config.get("injection_template", "")
            if not template_str:
                # 默认模板
                template_str = "【系统检测到消息中包含链接，已自动读取内容】\n链接标题：{{title}}\n链接内容摘要：\n{{content}}\n\n请基于以上链接内容，回复用户的消息：\n"
            
            tmpl = Template(template_str)
            injection_text = tmpl.render(
                title=parse_result.get("title", "无标题"),
                url=target_url,
                content=parse_result.get("content", "")
            )

            # 6. 注入到req.text
            # 也可以选择追加到 System Prompt 或 context 中，这里选择追加到 req.text
            original_text = req.text or ""
            req.text = f"{injection_text}\n\n[用户原话]: {original_text}"
            
            logger.info(f"[LinkReader] Successfully injected content from {target_url}")

        except Exception as e:
            logger.error(f"[LinkReader] Error processing URL {target_url}: {e}")
            # 出错不中断流程，让 LLM 继续处理原始消息

    async def _fetch_and_parse(self, url: str) -> dict:
        """核心获取与解析逻辑"""
        timeout = self.config.get("request_timeout", 10)
        ua = self.config.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        domain = urlparse(url).netloc
        cookies = {}
        
        # 平台特定 Cookie 处理
        platform_cookies = self.config.get("platform_cookies", {})
        if "zhihu" in domain and platform_cookies.get("zhihu_cookie"):
            cookies["z_c0"] = platform_cookies["zhihu_cookie"]
        elif "weibo" in domain and platform_cookies.get("weibo_cookie"):
            cookies["SUB"] = platform_cookies["weibo_cookie"]
        elif "xiaohongshu" in domain and platform_cookies.get("xiaohongshu_cookie"):
            cookies["web_session"] = platform_cookies["xiaohongshu_cookie"]

        features = self.config.get("features_switch", {})
        
        async with aiohttp.ClientSession(cookies=cookies) as session:
            try:
                async with session.get(url, headers={"User-Agent": ua}, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.warning(f"[LinkReader] Fetch failed: {resp.status}")
                        return None
                    
                    # 针对部分编码问题，尝试自动检测，默认为 utf-8
                    html = await resp.text(errors='ignore')
                    
                    # 路由分发
                    if any(d in domain for d in self.music_domains):
                        if not features.get("search_lyrics", True): return None
                        return await self._parse_music(html, url)
                    
                    elif any(d in domain for d in self.social_domains):
                        if not features.get("parse_social_media", True): return None
                        return await self._parse_social(html, url)
                    
                    else:
                        if not features.get("parse_generic_web", True): return None
                        return await self._parse_generic(html, url)
                        
            except asyncio.TimeoutError:
                logger.warning(f"[LinkReader] Fetch timeout for {url}")
                return None
            except Exception as e:
                logger.error(f"[LinkReader] Request error: {e}")
                return None

    async def _parse_generic(self, html: str, url: str) -> dict:
        """通用网页解析"""
        soup = BeautifulSoup(html, 'lxml')
        
        # 移除干扰元素
        for tag in soup(['script', 'style', 'nav', 'footer', 'iframe', 'noscript', 'svg']):
            tag.decompose()
            
        title = soup.title.string.strip() if soup.title else "无标题"
        
        # 提取正文：优先提取 article 标签，否则提取所有 p 标签
        content = ""
        article = soup.find('article')
        if article:
            content = article.get_text(separator='\n', strip=True)
        else:
            # 简单的文本密度提取策略
            paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 10]
            content = "\n".join(paragraphs)
            
        return self._format_result(title, content)

    async def _parse_social(self, html: str, url: str) -> dict:
        """社交媒体解析 (基于 OpenGraph 协议优先)"""
        soup = BeautifulSoup(html, 'lxml')
        
        title = "社交媒体分享"
        content = ""
        
        # 尝试 OpenGraph 协议提取 (通用性强，适用于知乎、微博等渲染前的页面)
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = og_title.get("content", "").strip()
            
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            content = og_desc.get("content", "").strip()
            
        # 如果 OpenGraph 没提取到内容，尝试 fallback 到 body text
        if not content:
            # 针对知乎的特定处理 (知乎有时将内容放在 script id="js-initialData" 中，这里仅做简单文本提取)
            # 实际生产中可能需要更复杂的解析逻辑
            content = soup.get_text(separator='\n', strip=True)[:500] + "..."
            
        return self._format_result(title, content)

    async def _parse_music(self, html: str, url: str) -> dict:
        """音乐链接解析"""
        soup = BeautifulSoup(html, 'lxml')

        # 1. 提取原始标题并清洗
        raw_title = soup.title.string.strip() if soup.title else "未知音乐"
        # 移除平台后缀，仅保留 歌手 - 歌曲名 部分
        clean_title = raw_title.split('(豆瓣)')[0].split('- 网易云')[0].split('- QQ音乐')[0].strip()
        
        # 2. 尝试从 meta 标签获取更精准的关键词 (og:title 通常包含更纯净的 歌曲-歌手 信息)
        og_title = soup.find("meta", property="og:title")
        search_keyword = og_title.get("content", "") if og_title else clean_title
        
        # 3. 构造功能性内容
        content = f"🎵 识别到音乐：{search_keyword}\n"
        content += "---"
    
       # 构造精准搜索链接 (以 Google/百度 或 垂直社区为例)
        # 使用 quote 确保 URL 编码安全
        from urllib.parse import quote
        encoded_query = quote(search_keyword)
    
        content += f"\n🔍 [搜索歌词]：https://www.google.com/search?q={encoded_query}+歌词"
        content += f"\n💬 [查看评价]：https://search.douban.com/music/subject_search?search_text={encoded_query}"
        content += f"\n🎧 [平台检索]：https://music.163.com/#/search/m/?s={encoded_query}"
    
        content += "\n\n(提示：由于版权保护，详细歌词与深度乐评请点击上方链接跳转查看)"

        return self._format_result(search_keyword, content)
        

    def _format_result(self, title: str, content: str) -> dict:
        """格式化并截断结果"""
        max_len = self.config.get("max_content_length", 1500)
        
        if len(content) > max_len:
            content = content[:max_len] + f"\n...(内容过长，已截断至{max_len}字)"
            
        # 清理多余空行
        content = re.sub(r'\n\s*\n', '\n', content)
        
        return {
            "title": title,
            "content": content
        }
