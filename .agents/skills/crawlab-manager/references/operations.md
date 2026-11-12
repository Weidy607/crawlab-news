# 上传、维护和环境

## 管理模型

本地只维护一份 `news/`；Crawlab 按来源保留独立条目、运行命令、日志和定时。更新目标是单个任务 ID，不是截图中 `news` 分组下所有条目。修改 `azure_ai.py`、pipeline、基类等公共文件后，一次单条上传不能使其他任务自动得到更新。

每条上传同一完整项目：远端工作目录下应直接出现 `scrapy.cfg`、`azure_ai.py`、`send_email_report.py` 和内层 `news/`，不要多套一层外部目录。必须保留 `send_email_report.py`，因为爬虫发现时 `email_report` 模块会导入它。

`news/.crawlab_tasks.map` 当前格式：

```text
<local_name>=<actual_remote_id>|CMD=scrapy crawl <actual_spider_name>
```

从现有文件和已认证平台发现真实 ID，不从截图的名称猜 ID，不把示例 ID 或空占位项当可执行任务。映射不全不表示远端任务不存在；优先匹配现有条目，避免重复创建。

## 上传工作流

1. 用户已要求上传/同步时，在完成本地改动和检查后直接执行该范围的上传；无需重复请求许可。单纯新增爬虫时准备好部署命令，只有缺少必要且不可发现的信息才提问。
2. 读取当前 map、`crawlab.server.json` 与 Makefile。服务器配置只含地址和 news 项目 ID；认证默认从本机 `~/.crawlab/cli/config.json` 读取，不能展示或复制 token/password 到工程。当前脚本仅依赖 Python 标准库，无需 `make` 或 CLI。
3. 单条更新使用 `python scripts/update_news.py --names <mapped_name> --execute` 或 `make update NAME=<mapped_name>`。`--check-remote` 只读校验全部目标 ID、所属项目及命令；默认不联网预览。远端显示名称可以不同于映射名，例如 `Spiders` 实际执行 `panews`，保留现有名称。
4. 创建仍需可用的 CLI（如 `make create NAME=<name> CMD="scrapy crawl <name>"`）或平台；只对确认未存在且用户授权创建的条目执行，成功后保存真实 ID 与命令。
5. 批量使用根目录 `python scripts/update_news.py` 预览，加 `--execute` 上传；`--names a16z bvp` 限定任务。news 目录别名为 `make plan-update-all` / `make update-all`。脚本检查认证配置指向同一服务器，写入前核对所有目标，再以 POSIX 路径逐文件通过 API 上传并校验远端 MD5。不信任 CLI 退出码表示成功：部分旧版 CLI 即使文件上传失败也返回 0。
6. 公共代码同步通常影响多个来源；先列明需要更新的条目，再在既有授权内同步。上传成功仅代表文件同步，是否运行任务由用户请求决定。
7. Spider 代码更新后，按用户的持续要求核对 Crawlab 上关联的来源定时任务，并按其配置命令逐个、串行触发和检查任务状态及日志，避免同时派发造成节点资源耗尽。只验证新闻来源采集；邮件发送、回填等有额外副作用的任务不自动触发。若节点排队或资源不足，停止继续派发并如实报告，不把 API 接受任务当成运行成功。
8. 非幂等的创建或执行任务请求超时后，先查远端是否已成功，不直接重复创建/触发。鉴权、目标 ID 或命令不匹配时停止该目标；批量认证失败时停止剩余上传。不要用重复重试掩盖部署失败。

上传包可在临时目录按需排除缓存、预览和本地开发文件；不为上传便利直接删除源仓库文件。普通上传不包含根平台 `.crawlab/` 数据或密钥配置。不要依赖 `.dockerignore` 能被 Crawlab CLI 自动识别。

批量脚本会制作同一份临时快照，排除缓存、测试、预览、`.env*` 和本地 ID 映射；上传或校验失败即停止，不重试或触发任务；当前条目可能已部分更新，修复后可重新上传。脚本不主动删除远端残留文件，首次大规模清理后的同步需另核对远端旧文件。当前脚本仅部署 map 中登记的任务，不代表平台中全部条目；未登记的条目应先确认 ID 与用途。

## 环境与平台

- `MYSQL_*` 控制新闻库连接，`OPENAI_*` / `AZURE_OPENAI_*` 控制 AI；沿用现有来源的配置覆盖规则。
- 普通爬虫上传不等于安装新依赖。增加依赖时更新项目 requirements，并指出 Crawlab 节点需安装或重建镜像；未授权平台维护时不擅自重启容器。
- 根 `Dockerfile.crawlab` 只安装 `news/requirements.txt`，不要重新加入外部 MNA 工程的依赖。
- `.crawlab/master` 和 `.crawlab/mongo/data` 为配置/持久化目录，不当缓存清理。检查凭据只报告是否配置，不输出值。
- `news/send_mna_email_report_v2.py` 是本工程的新闻并购报告，独立 MNA 工程负责交易所采集，两者互不依赖。邮件报告当前默认生成预览，需要 `--send` 才发送；只有用户明确要求发送时使用该参数。制作技能或修改报告不授权发邮件。
- 定时任务和正在运行的任务是独立状态，修改/停用前确认用户请求覆盖该动作。已有任务 ID 和历史数据不能仅因本地暂时不用而删除。

## 清理与完成判断

当前仓库没有引用不能证明线上定制命令也未引用。删除前检查 Python 导入、动态配置字符串、Makefile、Docker COPY/CMD、任务映射以及用户已知线上用法。普通新增功能不顺便清理无关代码。

共享基类、pipeline、AI 参数变更要验证受影响行为；纯上传配置修改校验命令和实际文件是否存在即可。若服务返回 400、认证失败或数据库未连接，报告真实状态：日志 `Spider closed (finished)` 本身不能证明 AI 成功或数据已入库。
