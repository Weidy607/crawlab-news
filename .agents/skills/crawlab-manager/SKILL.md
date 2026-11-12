---
name: crawlab-manager
description: 维护 crawlab-news 独立工程，新增或修复新闻爬虫，管理共享 AI 入库逻辑及 Crawlab 上传任务。用户提到这个工程、新增新闻来源、爬虫报错、整理任务或同步代码时使用；不用于无关项目的通用爬虫开发。
---

# Crawlab 工程维护

用中文直接完成用户要求，沿用本工程约定，让用户只需提供网站名称或 URL、报错或简短维护目标。

本工程只维护 `news/`，保留外层 Scrapy 项目目录与内层同名 Python 包，不再增加 `projects/` 分类层。交易所 MNA 采集放在独立工程，不在这里新增或恢复该项目；news 内的并购邮件报告仍属于本工程。

MNA 工程位于同级 `../crawlab-mna/`；只有任务明确涉及交易所采集时才进入该工程处理。

## 工作方式

- 从现有代码、任务映射和会话推断默认选择；命名、复用哪种基类、文件位置、分页和字段接入等常规决定自行完成，不要求用户提供技术方案。
- 信息不足时先做不依赖该信息的工作。仅对无法从工程或网站发现、且会改变结果的信息提问，例如同名网站无法区分、上传目标环境不明确或现有任务 ID 无法查到。
- 沿用本会话已授权的动作，不重复确认。新增/修复通常包含本地实现和必要验证；用户要求上传、更新线上或部署时，包含相应发布。单纯要求开发不等同于发布、设置定时或发送邮件。
- 完成后简短说明改了什么、验证结果和运行命令；部署任务补充实际成功/失败的条目。遇到环境缺失说明准确限制，不把静态检查说成实际抓取成功。

## 定位与事实来源

优先从当前目录向上找同时包含 `docker-compose.yml` 和 `news/scrapy.cfg` 的根目录；也可识别当前仓库内的 `crawlab-news/`。本机已知位置为 `E:/01_战略投资/09_Src/csid/crawlab-news`，只作为查找线索，不覆盖用户指定路径。

首先读取适用的 `AGENTS.md` 和相关差异。可运行本技能的只读脚本快速盘点：

```powershell
python "<skill-dir>/scripts/inventory.py" --root "<crawlab-root>"
```

`--json` 输出结构化清单。脚本仅做静态发现和检查，不导入项目、不联网、不执行任务；遇到动态命名或继承特殊逻辑时再用实际 `scrapy list` 核对。

关键文件：

| 路径 | 作用 |
|---|---|
| `news/` | 上传的完整项目根，`scrapy.cfg` 位于此处 |
| `news/news/spiders/` | 多来源爬虫 |
| `news/news/spiders/baseSpider.py` | 公共 `BaseSpider`，兼容旧 `start_requests()` 与新版异步入口 |
| `news/news/spiders/direct_news_sources.py` | `DirectNewsMixin`、共享正文抽取及多个独立来源 |
| `news/news/pipelines.py` | `TechCrunchMySQLPipeline`，清洗、AI 丰富化、MySQL 入库、同步输出字段 |
| `news/azure_ai.py` | 翻译、摘要、标签公共入口 |
| `news/Makefile`、`.crawlab_tasks.map` | 上传命令和已知远端任务映射 |
| `scripts/update_news.py` | 标准库 API 更新入口，默认仅预览，`--check-remote` 核对目标，`--execute` 上传并校验文件 |
| `crawlab.server.json` | 非敏感服务器地址和 news 项目 ID；认证仍读取本机 CLI 配置 |
| 根 `Dockerfile.crawlab`、`docker-compose.yml` | 平台镜像、启动与持久化配置 |

以代码和实际命令为准；`docs/` 可能保留历史记录。名称和 ID 每次读取，不复制本技能中的过期清单。不要重建已移除的 `news/news/Makefile`、旧 `atlproc`、`companydb` 等工具。

## 新增或修复爬虫

按 [references/spiders.md](references/spiders.md) 实施：先验证站点实际结构，选择 API、RSS 或列表页方案，复用现有基类与字段，接入现有 pipeline，完成有限验证与命令登记。不要复制整套项目或为每个来源重复写 AI、数据库连接。

## 项目维护与上传

按 [references/operations.md](references/operations.md) 处理任务映射、按来源上传、公共代码同步、运行环境与清理。Crawlab 中的 `news` 分组不代表各条任务共享代码；更新工具按任务 ID 上传完整项目。

新增或修改公共代码时，识别受影响来源；只有用户授权同步时才上传。在根目录运行 `python scripts/update_news.py` 预览，增加 `--execute` 执行；`--names a16z bvp` 限定任务。`make plan-update-all` 和 `make update-all` 是 news 目录下的对应入口。清理前核对配置字符串和导入链，保留 `.crawlab/` 持久化内容与用户仍使用的独立项目。
