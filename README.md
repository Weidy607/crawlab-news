# Crawlab 新闻采集工程（crawlab-news）

独立仓库：[Weidy607/crawlab-news](https://github.com/Weidy607/crawlab-news)。后续代码、问题和版本在此仓库管理。

本工程只维护新闻爬虫、共享 AI 处理及邮件报告。交易所 MNA 采集已移到同级 `../crawlab-mna/` 独立工程。

## 目录

- `news/`：可上传到 Crawlab 的完整 Scrapy 工程；`news/news/spiders/` 存放各来源爬虫。
- `scripts/update_news.py`：按任务映射上传代码，并核对远端文件 MD5。
- `.agents/skills/crawlab-manager/`：工程维护技能，包含新增来源和部署约定。
- `tests/`、`docs/`：测试、使用说明及线上验证记录。
- `Dockerfile.crawlab`、`docker-compose.yml`：平台镜像与启动配置，见 [BUILD.md](BUILD.md)。

## 本地使用

```powershell
git clone https://github.com/Weidy607/crawlab-news.git
Set-Location crawlab-news
python -m pip install -r news/requirements.txt
Copy-Item news/.env.example.json news/.env.local.json
# 编辑 .env.local.json 中的数据库、AI 等配置后运行
Set-Location news
python -m scrapy list
python -m scrapy crawl bvp
```

`news/.env.local.json` 只供本地使用，自动加载且不覆盖已有环境变量。真实密码、API key、平台认证和 `.crawlab/` 持久化数据不会提交 Git；示例文件不含凭据。

`news/.env.example.json` 是字段模板，不参与运行时加载，也不会自动同步线上配置。实际使用的是 `.env.local.json` 的值或已存在的环境变量（环境变量优先）。新闻数据库使用 `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DB`、`MYSQL_TABLE`；`AI_NEWS_MYSQL_PASSWORD` 不是当前新闻库读取的变量。线上 `MYSQL_HOST=172.17.0.1` 属于节点内网地址，复制到本机后仍需可达的网络路径；本机无法直连时，应在 Crawlab 节点上执行读取。AI 和发件配置分别使用 `OPENAI_*`、`SMTP_*`。

线上 Crawlab 任务通过环境变量设置 `MYSQL_*`、`OPENAI_*`（或 `AZURE_OPENAI_*`）；需要邮件时配置 `SMTP_*`，需要代理时配置 `KUAIDAILI_PROXY_URL` 等。更新工具不会上传本地配置，首次部署此版本前应在任务或节点环境中配置所需凭据。GitHub 代码发布不修改现有线上任务。

## 两份独立日报

原日报继续使用 `news/send_email_report.py`（或 `scrapy crawl email_report`），保留原来的来源白名单和邮件主题。

新增 `news/send_all_sources_email_report.py`，中文邮件主题为“全球投研日报”，不附加日期；指定 `--subject-suffix TEST` 时主题为“全球投研日报 - TEST”。取消来源白名单，按新闻源所属地区先显示“国外新闻”，再显示“国内新闻”，无法识别的来源最后单列“来源地区待确认”。组内按来源排列；中英文收件人沿用原日报配置。这里按来源归类，不判断报道事件的发生地。

在 `news/` 目录执行，Crawlab 可另建独立任务使用同样的命令：

```powershell
python send_all_sources_email_report.py --dry-run  # 查询并生成 HTML，不发送
python send_all_sources_email_report.py --demo     # 使用示例数据生成排版 HTML
python send_all_sources_email_report.py --send     # 发送真实新闻日报
python send_all_sources_email_report.py --send --limit 0 --email-to reader@example.com # 完整收录并仅发给指定收件人
python send_all_sources_email_report.py --demo --send --email-to reader@example.com # 发排版测试
```

新日报沿用原日期起点：默认从数据库昨天零点起，截止到今天结束，排除未来发布日期，按发布日期倒序，默认不设条数上限。邮件阶段读取全部来源，按数据库 `tags` 中完整的 `IT投研日报` 标签选取，不调用 AI、不搜索标题或正文关键词。爬虫入库时通过 AI 阅读完整材料判断行业和主题，包括投资、融资、并购、IPO、基金及对行业投资研究有直接价值的业务、技术、政策分析。可用 `--hours`、`--limit`、`--email-to`、`ALL_SOURCES_EMAIL_REPORT_LIMIT` 或原有日期、SMTP 环境配置覆盖；显式设置数量上限时按上限截取。原日报仍保留原有标签条件和 `EMAIL_REPORT_LIMIT`，不受新日报独立配置影响。两份日报需分别运行或设置定时；新增脚本不会自动发送原日报。

共享入库 pipeline 遇到晚于北京时间今天的发布日期时，保留正文，将日期存为 NULL 并记录告警，等待核实源站发布时间；不拿抓取日期替代。历史异常日期不会被自动改写，补标脚本也排除未来日期。

所有采用共享 pipeline 的来源统一由 `news_tagging.classify_news_for_storage` 打标签，不沿用来源的关键词判断，不再只看前600字或限制为3个标签，也不在AI失败时回退关键词。通用行业/事件标签与日报适用标签同时写入 `tags`；日报范围仍为 IT、AI、软件/SaaS、芯片和数字基础设施。消费品牌营销仅顺带使用 AI、Token 或数字人时排除；面向这些行业提供软件/SaaS 的公司仍可纳入。混合行业汇总逐项保存所有相关项目的原文证据及中英文摘要到新增的 `email_report_metadata` LONGTEXT 字段，邮件直接复用，保留原文链接；源头全行业摘要和正文不被混合条目摘要覆盖。普通新闻继续显示数据库已有摘要。

分类失败、证据不足或接口内容过滤时保留新闻并写入 `分类待审核`，不会当作无关内容排除。邮件检查整个日期窗口中的标签和分类版本、内容指纹：旧标签、待审核、已更新但未重分的新闻会使发送停止，并提示先补标，避免静默漏报。`email_report_metadata` 只保存分类与日报条目，不含凭据；共享 pipeline 或补标执行会按需新增该字段，数据库账号须有该表的 ALTER 权限。标签与分类元数据随同一条新闻在同一 SQL 中写入。先完成标签核对再应用条数上限；HTML 旁的 `*_industry_review.json` 记录数据库里的分类结果。

历史数据不会自动变成新标签。上线时先同步所有使用共享 pipeline 的爬虫与邮件任务，再在 `news/` 执行补标；默认只盘点，`--execute` 才调用现有 AI 接口并更新标签和分类元数据，不发送邮件、不改原文和源头摘要。逐篇提交，已完成的记录下次自动跳过；接口失败仍标记待审核。并发采集修改了正文或摘要时不会用旧分类覆盖新数据。

```powershell
python backfill_news_tags.py --hours 24                     # 只读盘点
python backfill_news_tags.py --hours 24 --limit 1 --execute  # 先补一条
python backfill_news_tags.py --hours 24 --limit 0 --execute # 补完整个日报时间窗口
```

新日报默认仅生成本地 HTML；必须加 `--send` 才会发送。预览与邮件使用同一 HTML 模板，样式参考 `send_mna_email_report_v2.py`；邮件附带完整 HTML 文件。日报、并购周/月报及审核 JSON 默认统一保存到工程根目录 `output/`，不随启动目录变化；独立部署到 Crawlab 时保存到该任务项目下的 `output/`。`--output` 可显式指定其他 HTML 路径，`--language en` 生成英文预览，`--input-json` 回放新闻列表。`--limit 0` 表示完整收录符合主题和时间条件的新闻，不设条数上限。示例邮件的主题和正文均标注“排版测试/样例”。

发送模式（`--send` 且未启用 `--dry-run`）按数据库标签筛选后生成 `all_sources_daily_日期.html` 并自动发送，无需额外预览或人工确认；未启用发送时使用 `all_sources_daily_preview_日期.html`。邮件生成与发送均不调用 AI。HTML落盘只是保留报告副本；标签尚未补齐时会明确停止，不是在等待预览确认。

摘要黑点在源头由 AI 按完整事件组织：同一项目保留业务和关键进展，汇总新闻区分项目。邮件直接把已存摘要中的段落和显式黑点呈现为 HTML 列表，不按句号或字数拆分，不再做一轮 AI 分点审核。混合条目使用入库时保存的项目摘要，每个项目一个黑点。旁边的 `*_summary_review.json` 记录实际使用的字段和黑点。

源头入库的 AI 摘要由 `azure_ai.summarize_news_bilingual` 统一调用配置的 AI 接口，通过提示词判断正文质量、选择重点并生成对应的中英文摘要。当前配置模板使用 `gpt-5.6-terra`。普通新闻提示为2至3个短要点；汇总每个项目用一条可独立读懂的完整快讯说明核心业务、用途与关键进展，融资信息不能挤掉业务介绍，也不能压成业务标签或技术名词串，逐项保留所有行业项目。字数和点数仅为提示词中的编辑风格参考，程序不按字数拒收、截断、重试或删减内容，不用关键词挑选摘要或判断内容质量。代码仅保留接口异常处理、JSON/双语字段/连续原文证据校验，以及长文完整分段传输和AI标识的项目合并；多段普通文章统一交AI整理，合并不得漏项目。行业和主题判断在入库时完成并持久化，邮件直接按数据库标签筛选。正文不足由模型判定并返回空摘要；接口失败不会把本地摘录标成AI摘要。网页原有内容摘要保留。

TechCrunch、TechStartups、a16z、BVP、Thoma Bravo、Hongshan 及通用来源已去除正文2000/4000/12000字符截断。通用详情解析保留裸文本和 `<br>` 段落，避免只读到 `<p>` 图片说明。以上改动对采用此版本公共代码的后续采集生效；本地修改不会自动同步 Crawlab，也不会自动回填历史数据库记录。

## 上传与维护

预览 HTML、审核 JSON、样本、截图和调试结果等生成物统一放入根目录 `output/`，不提交 Git、不进入 Docker 构建上下文，也不上传 Crawlab。后续 agent 遵守 [工程技能中的生成物规范](.agents/skills/crawlab-manager/SKILL.md#生成物目录)。

在工程根目录执行：

```powershell
python scripts/update_news.py                      # 预览
python scripts/update_news.py --check-remote       # 只读核对目标
python scripts/update_news.py --names bvp --execute # 上传一个来源
python scripts/update_news.py --execute             # 更新映射中的全部任务
```

服务器和项目 ID 位于 `crawlab.server.json`，任务映射在 `news/.crawlab_tasks.map`。认证读取本机 `~/.crawlab/cli/config.json`，不要把此文件复制进工程。上传不自动运行爬虫或发送邮件。

新增来源时遵循 [工程技能](.agents/skills/crawlab-manager/SKILL.md)，复用共享 pipeline 与 AI 配置；详细操作见 [用户指南](docs/crawlab_user_guide.md)。

安装依赖后可在根目录验证：

```powershell
python -B -m unittest discover -s tests -q
```

## 提交代码

在本工程根目录执行：

```powershell
git status
git add .
git commit -m "描述本次修改"
git push
```

本目录有自己的 Git 仓库，远端为 `Weidy607/crawlab-news`。Crawlab 平台上传仍使用前述更新脚本。
