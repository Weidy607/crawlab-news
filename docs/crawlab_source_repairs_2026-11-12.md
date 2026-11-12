# 三个新闻来源修复与线上验证：2026-11-12

BVP、Crunchbase、Thoma Bravo 已修复并上传至 http://47.245.111.142:18803/，每条任务 33 个文件，共 99 个文件 MD5 校验通过。默认执行命令、36 小时时间窗口、环境配置和定时均未修改。

## 实际结果

| 来源 | 试跑范围 | Crawlab 结果 | MySQL 核对 | 日志 |
|---|---|---:|---|---|
| BVP | 默认窗口，最多 2 条样本 | 2 | 2/2 条已找到，中英文 AI 摘要字段长度与 Crawlab 一致 | [查看](http://47.245.111.142:18803/#/tasks/6af55ccfc85e5314c2634d0d/logs) |
| Crunchbase | 最近 36 小时，最多 2 条 | 2 | 2/2 条已找到，中英文 AI 摘要字段长度与 Crawlab 一致 | [查看](http://47.245.111.142:18803/#/tasks/6af55cd0c85e5314c2634d0e/logs) |
| Thoma Bravo | 默认最近 36 小时 | 0 | 数据库连接正常；没有近期文章需要写入 | [查看](http://47.245.111.142:18803/#/tasks/6af55cd0c85e5314c2634d0f/logs) |
| Thoma Bravo | 临时扩大到最近 30 天，最多 2 条 | 2 | 2/2 条已找到，中英文 AI 摘要字段长度与 Crawlab 一致 | [查看](http://47.245.111.142:18803/#/tasks/6af55cd0c85e5314c2634d10/logs) |

本次 6 条样本均实际查询到了 MySQL 记录，crawl_time 为本轮运行时间；中英文 AI 摘要长度与 Crawlab Data 输出逐条一致。计数为本轮处理量，不等同于数据库新增行数（已有 URL 会更新）。

Thoma Bravo 默认窗口的日志明确显示 `candidates=30 filtered_by_time=30 queued=30 hours=36`，因此 0 条是日期筛选结果。扩大至 30 天后，2026-10-15 和 2026-10-29 的两篇文章抓取、AI 丰富化、Crawlab 保存及 MySQL 写入均通过。没有把默认范围改成 30 天。

BVP 的一篇样本在站点中没有发布日期，沿用已有规则保留空日期，没有填成今天。

## 修改内容

- **BVP**：列表入口改为 `/atlas?type=news`，解析公开 Next.js 数据中的新闻路径、发布日期和作者；开启本来源重定向，兼容无 HTML 文章链接的新页面。
- **Thoma Bravo**：移除失效 `/podcasts` 与旧 `/insights` 列表入口，使用当前 `/behind-the-deal`；修正正文容器，并记录候选数、时间过滤数和请求数；支持 `max_articles` 限制验证规模。
- **Crunchbase**：使用其官方全文 RSS `https://feeds.feedburner.com/CrunchbaseNews`，保留原文链接、作者、发布时间和全文；直连站点 feed 作为备用。去掉关闭钩子中发起 RSS 抓取的流程，正文正常进入统一 AI、MySQL 与 Crawlab pipeline；没有使用 RSS 简介冒充 AI 摘要。
- **数据库**：移除 Crunchbase 根据容器本机 IP 自动强制改用公网数据库地址的逻辑，统一使用已有 `AI_NEWS_MYSQL` 配置。

RSS 当前提供最近 10 篇文章；扩大时间窗口不会自动覆盖订阅源之外的全部历史文章。

## 验证

- 21 项单元测试通过，覆盖新版 BVP 数据、Thoma Bravo 路径和日期筛选、Crunchbase RSS 正文和备用入口、数据库地址以及上传校验。
- 真实网站 HTML/RSS 样本解析通过；`scrapy list` 能发现全部现有来源。
- 三条线上任务文件均校验成功，四次采集试跑全部结束，未见这三条来源的数据库连接或 AI 参数错误。
- [只读数据库核对任务日志](http://47.245.111.142:18803/#/tasks/6af55d60c85e5314c2634d18/logs)。

## 本轮样本

### BVP

- [Dreeze：为智能体时代开创“应用复兴”之路](https://www.bvp.com/news/dreeze-pioneering-application-revival-for-the-agentic-age)：中文 AI 摘要 191 字符，英文 AI 摘要 772 字符。
- [使用 Twin1 构建企业级 AI 的受治理上下文层](https://www.bvp.com/news/building-the-governed-context-layer-for-enterprise-ai-with-twin1)：中文 AI 摘要 245 字符，英文 AI 摘要 889 字符。

### Crunchbase

- [Anthropic 寻求创纪录的885亿美元融资，或与公开上市有关](https://news.crunchbase.com/ai/anthropic-record-ipo-ant-public-market-debut-spcx/)：中文 AI 摘要 223 字符，英文 AI 摘要 796 字符。
- [独家：Nubank 前高管为哥伦比亚 Palomma 融资 1400 万美元，发力租金支付与房东融资领域](https://news.crunchbase.com/fintech/nubank-alum-colombia-startup-palomma-ai-rental-services/)：中文 AI 摘要 251 字符，英文 AI 摘要 851 字符。

### Thoma Bravo

- [Thoma Bravo 连续第四年入选《Inc.》“创始人友好型投资者”榜单](https://www.thomabravo.com/press-releases/thoma-bravo-earns-spot-on-inc.-s-founder-friendly-investors-list-for-fourth-consecutive-year)：中文 AI 摘要 181 字符，英文 AI 摘要 647 字符。
- [Proofpoint 将以 63 亿美元收购 Varonis](https://www.thomabravo.com/press-releases/proofpoint-to-acquire-varonis-in-a-6.3-billion-transaction)：中文 AI 摘要 310 字符，英文 AI 摘要 987 字符。

## 后续使用

日常执行仍使用 `scrapy crawl bvp`、`scrapy crawl crunchbase`、`scrapy crawl thomabravo`。Thoma Bravo 有时多天没有新文章，正常运行也可能输出 0 条。

需要手工重现 Thoma Bravo 的历史样本验证时，在 news 项目目录运行：

```bash
scrapy crawl thomabravo -a hours=720 -a max_articles=2
```
