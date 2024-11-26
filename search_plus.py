import os
import json
import requests
from datetime import datetime
import pytz
from azure.cognitiveservices.search.websearch import WebSearchClient
from azure.cognitiveservices.search.websearch.models import SafeSearch
from msrest.authentication import CognitiveServicesCredentials
import google.generativeai as genai
from common.log import logger
from plugins import register, Plugin, Event
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from plugins.event import EventContext, EventAction
from config import conf

@register(
    name="SearchPlus",
    desc="A plugin that combines Bing search with Gemini model for comprehensive search results",
    version="1.0",
    author="田羊羽",
    desire_priority=800
)
class SearchPlus(Plugin):
    def __init__(self):
        super().__init__()
        try:
            # 加载配置
            self.config = self.load_config()
            if not self.config:
                logger.warn("[SearchPlus] No config found, using default config")
                self.config = {
                    "bing_subscription_key": "",
                    "search_count": 5,
                    "prefix": ["搜索", "search"],
                    "prompts": {
                        "default": """请对以下搜索结果进行分析和总结：

当前北京时间：{current_time}
搜索结果：
{search_results}

要求：
1. 重点关注信息的时效性，标注信息的发布时间
2. 如果是实时数据（如股票价格），请明确标注数据的时间点
3. 如果发现信息可能过时，请在总结中提醒用户
4. 按照时间顺序组织信息，最新的信息放在前面
5. 如果不同来源的数据有冲突，请标注出来并说明可能的原因
6. 将所有时间都转换为北京时间显示

总结：""",
                        "news": """请对以下新闻搜索结果进行分析和总结：

当前北京时间：{current_time}
新闻搜索结果：
{search_results}

要求：
1. 重点关注新闻的时效性，标注新闻的发布时间
2. 如果是实时新闻，请明确标注新闻的时间点
3. 如果发现新闻可能过时，请在总结中提醒用户
4. 按照时间顺序组织新闻，最新的新闻放在前面
5. 如果不同来源的新闻有冲突，请标注出来并说明可能的原因
6. 将所有时间都转换为北京时间显示

总结：""",
                    },
                    "max_tokens": 800
                }
            
            # 初始化Bing搜索配置
            self.bing_subscription_key = self.config.get("bing_subscription_key")
            self.search_url = "https://api.bing.microsoft.com/v7.0/search"
            if not self.bing_subscription_key:
                logger.warn("[SearchPlus] No Bing subscription key found")

            # 初始化Gemini模型
            self.model = None
            api_key = conf().get("gemini_api_key")
            if api_key:
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel('gemini-pro')
                logger.info("[SearchPlus] Gemini model initialized")
            else:
                logger.warn("[SearchPlus] No Gemini API key found")

            # 初始化时区
            self.beijing_tz = pytz.timezone('Asia/Shanghai')
            logger.info("[SearchPlus] Timezone initialized")

            # 注册事件处理器
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            logger.info("[SearchPlus] Plugin initialized successfully")

        except Exception as e:
            logger.error(f"[SearchPlus] Init failed: {e}")

    def get_beijing_time(self):
        """获取当前北京时间"""
        return datetime.now(self.beijing_tz).strftime("%Y-%m-%d %H:%M:%S")

    def format_utc_to_beijing(self, utc_time_str):
        """将UTC时间转换为北京时间"""
        try:
            # 处理包含毫秒的时间格式
            if '.0000000Z' in utc_time_str:
                utc_time_str = utc_time_str.replace('.0000000Z', 'Z')
            # 解析UTC时间字符串
            utc_time = datetime.strptime(utc_time_str, "%Y-%m-%dT%H:%M:%SZ")
            utc_time = pytz.utc.localize(utc_time)
            # 转换为北京时间
            beijing_time = utc_time.astimezone(self.beijing_tz)
            return beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.error(f"[SearchPlus] Error converting time: {e}")
            return utc_time_str

    def get_help_text(self, **kwargs):
        help_text = "🔍 搜索插件使用说明：\n\n"
        help_text += "1. 基本搜索：\n"
        help_text += "   发送：'搜索 <关键词>' 或 'search <keywords>'\n"
        help_text += "   示例：'搜索 今天上证指数收盘价'\n\n"
        
        help_text += "2. 指定搜索类型：\n"
        help_text += "   发送：'搜索 <类型>#<关键词>'\n"
        help_text += "   支持的类型：\n"
        for prompt_type in self.config["prompts"].keys():
            help_text += f"   - {prompt_type}\n"
        help_text += "\n   示例：\n"
        help_text += "   - '搜索 news#最新科技新闻'\n"
        help_text += "   - '搜索 tech#人工智能发展'\n"
        help_text += "   - '搜索 finance#股市分析'\n\n"
        
        help_text += "3. 注意事项：\n"
        help_text += "   - 所有时间都会自动转换为北京时间显示\n"
        help_text += "   - 搜索结果不限制时间范围\n"
        help_text += "   - 如果不指定类型，将使用默认的搜索模式\n"
        
        return help_text

    def on_handle_context(self, e_context: EventContext):
        content = e_context['context'].content
        logger.info(f"[SearchPlus] Event handler called")
        
        if not content:
            return
            
        logger.info(f"[SearchPlus] Received content: {content}")
        
        # 检查是否包含搜索前缀
        prefix_found = False
        for prefix in self.config["prefix"]:
            if content.startswith(prefix):
                content = content[len(prefix):].strip()
                prefix_found = True
                break
                
        if not prefix_found:
            return

        try:
            logger.info(f"[SearchPlus] Starting search for: {content}")
            
            # 获取当前北京时间
            current_time = self.get_beijing_time()
            logger.info(f"[SearchPlus] Current Beijing time: {current_time}")
            
            # 调用Bing搜索API
            headers = {"Ocp-Apim-Subscription-Key": self.bing_subscription_key}
            params = {
                "q": content,
                "count": self.config["search_count"],
                "textDecorations": True,
                "textFormat": "HTML",
                "mkt": "zh-CN",
            }
            response = requests.get(self.search_url, headers=headers, params=params)
            response.raise_for_status()
            search_results = response.json()

            # 处理搜索结果
            results_text = ""
            
            # 先处理新闻结果
            if "news" in search_results and "value" in search_results["news"]:
                results_text += "最新新闻：\n\n"
                for idx, news in enumerate(search_results["news"]["value"][:3], 1):
                    date_published = news.get("datePublished", "")
                    if date_published:
                        date_published = self.format_utc_to_beijing(date_published)
                    
                    results_text += f"{idx}. {news['name']}\n"
                    results_text += f"时间：{date_published}\n"
                    results_text += f"来源：{news.get('provider', [{}])[0].get('name', '未知来源')}\n"
                    results_text += f"详情：{news['description']}\n\n"

            # 处理网页结果
            if "webPages" in search_results and "value" in search_results["webPages"]:
                if results_text:
                    results_text += "相关网页：\n\n"
                for idx, page in enumerate(search_results["webPages"]["value"][:3], 1):
                    date_published = page.get("dateLastCrawled", "")
                    if date_published:
                        date_published = self.format_utc_to_beijing(date_published)
                    
                    results_text += f"{idx}. {page['name']}\n"
                    if date_published:
                        results_text += f"更新时间：{date_published}\n"
                    results_text += f"详情：{page['snippet']}\n\n"

            if not results_text:
                reply = Reply(ReplyType.TEXT, "抱歉，没有找到相关的搜索结果。")
                e_context['reply'] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            # 使用默认提示词模板，强调提取具体数据
            prompt = """
请从以下搜索结果中提取股票指数相关的具体数据：

搜索结果：
{search_results}

当前时间：{current_time}

请按以下格式提取信息：
1. 股票指数名称和数值（如：上证指数 3000点）
2. 涨跌幅
3. 成交量/成交额（如有）
4. 数据时间
5. 数据来源

注意事项：
- 优先提取最新的数据
- 必须包含具体的数字
- 如果数据不是今天的，请特别说明
- 如果找不到具体数据，请直接说明"未找到具体的股票数据"
""".format(
                current_time=current_time,
                search_results=results_text
            )
            
            # 使用Gemini生成摘要
            response = self.model.generate_content(prompt)
            if response.text:
                reply = Reply(ReplyType.TEXT, response.text)
            else:
                reply = Reply(ReplyType.TEXT, "抱歉，生成摘要时出现错误。")
            
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS

        except Exception as e:
            logger.error(f"[SearchPlus] Error: {e}")
            reply = Reply(ReplyType.ERROR, f"搜索出错：{e}")
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS

    def load_config(self):
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"[SearchPlus] Error loading config: {e}")
        return None
