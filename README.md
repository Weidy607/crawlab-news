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

线上 Crawlab 任务通过环境变量设置 `AI_NEWS_MYSQL_*`、`OPENAI_*`（或 `AZURE_OPENAI_*`）；需要邮件时配置 `SMTP_*`，需要代理时配置 `KUAIDAILI_PROXY_URL` 等。更新工具不会上传本地配置，首次部署此版本前应在任务或节点环境中配置所需凭据。GitHub 代码发布不修改现有线上任务。

## 上传与维护

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
