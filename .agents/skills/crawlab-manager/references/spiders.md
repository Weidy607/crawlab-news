# 新增来源与修复抓取

## 实现选择

1. 查看一份真实列表/API/feed 和文章页，再确定选择器、发布时间和正文规则。网站名称明确时自行查官网；无法访问时报告访问限制，不宣称猜测的接口已经可用。
2. 提供稳定 API 的来源参考 `techstartups.py`、`techcrunch_api.py`；RSS 来源参考 `techcrunch_venture.py`；一般 HTML 来源优先复用 `DirectNewsMixin`。参考文件需读当前内容，不把旧站规则直接套用到新站。
3. 唯一 `name` 使用简短小写下划线；沿用同类来源常见 `hours=36` 默认窗口，尊重用户指定范围。显示名称和 Scrapy 名称可以不同，运行命令用实际 `name`。
4. 只需配置即可抓取的来源，可在 `direct_news_sources.py` 添加 `(DirectNewsMixin, BaseSpider)` 子类，配置 `name`、`source_name`、`home_url`、`allowed_domains`、`list_urls`、`article_pattern`、`content_selectors`；先确认其默认时区、链接标准化和中间件适合该网站。
5. 特殊 API 或复杂正文用独立文件，继承公共 `BaseSpider`。保留兼容入口；不得误把 `name = "base"` 的公共类注册成可部署来源。

## 数据契约

新新闻来源输出沿用现有 dict 字段，优先以 `TechCrunchMySQLPipeline._prepare_row` 和真实来源输出为准：

```python
{
    "标题(中文)": "",
    "标题(英文)": "",
    "链接": article_url,
    "网站": source_name,
    "发布日期": publication_date,
    "作者": author,
    "标签": "",
    "内容摘要(中文)": "",
    "内容摘要(英文)": "",
    "AI摘要(中文)": "",
    "AI摘要(英文)": "",
    "完整内容(英文)": cleaned_body,
    "match_type": "",
}
```

`完整内容(英文)` 是兼容字段名，现有中文来源也用它承载正文；不要为新增中文源更改数据库结构。原文按实际语言放置，其他语言交给公共 pipeline。不要把页面 description、截断正文或图片说明当 AI 摘要。AI 结果为空时保留可识别的空结果/现有降级行为，不伪造成功。

正常新闻任务在 `custom_settings` 中接入：

```python
"ITEM_PIPELINES": {
    "news.pipelines.TechCrunchMySQLPipeline": 100,
    "crawlab.scrapy.pipelines.CrawlabPipeline": 200,
}
```

顺序保证 Crawlab Data 与 MySQL 使用丰富化后的数据。不要直接继承全局旧 Mongo pipeline 而漏掉此覆盖。

## 抽取与运行约定

- 解析真实发布日期并使用明确时区比较；英文 UTC/GMT、中国来源 UTC+8 分别处理。缺日期时不要统一伪填今天，遵循来源和用户的数据要求。
- 列表分页、详情数量、超时和重试保持有界；规范化 URL 时检查查询参数是否参与文章标识。去掉导航、广告、图片署名和重复正文。
- 保留按 URL 去重及已有数据库去重行为。为日志提供候选数、时间过滤数、正文输出数和失败原因。
- 默认复用节流与代理约定；只在站点确有需要时采用现有 requests 中间件，处理解压后的响应头和编码。不要全局替换其他来源的网络行为。
- 数据库与 AI 配置走既有环境变量和公共实现；不要把密码、密钥或平台配置复制进新文件、技能或日志。
- 共享 AI 修改先查当前模型及错误参数；保留已修复的默认采样处理。模型名/部署别名不能凭空推断，不能只改默认温度却漏掉显式调用参数。

## 验证与交付

- 使用项目可用解释器；环境缺依赖时先查现有 venv。必要依赖可放隔离临时环境，不为验证任意升级生产环境。
- 静态检查新增及相关 Python 文件；依赖可用时执行 `scrapy list`，确认命名唯一、所有来源仍可导入。静态 AST 清单不代表 Scrapy 动态发现已验证。
- 解析验证优先通过真实 HTML/JSON 样本构造 `HtmlResponse`/`TextResponse`，调用解析回调并断言标题、URL、正文、日期、语言字段；不要默认运行会写生产库或调用 AI 的完整 crawl。
- 需要请求层烟测时使用有限页数/条数，并关闭 MySQL、Crawlab 持久化 pipeline；核对回调和关闭钩子是否有额外网络、入库或 AI 副作用。未经同一任务授权不执行邮件发送或历史回填写库。
- 测试规模与变更匹配：复杂解析和共享逻辑增加有价值的回归验证；低影响配置不堆形式化测试。临时输出放工作区外，不清理用户已有测试。
- 如维护了 `NEWS_SPIDERS` 或任务清单，同步登记新来源并去除本次涉及的失效名称；不要将 `email_report`、`base` 当普通新闻来源批量运行。
- 交付命令在 `news/` 执行：`scrapy crawl <name> -a hours=36`。说明实际验证范围和是否已上传，不把代码完成与线上验证混为一谈。
