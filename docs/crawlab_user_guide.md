# Crawlab 使用说明（非开发人员版）

## 一、Crawlab 是什么

Crawlab 是一个用来管理爬虫任务的平台。

可以简单理解为：

- 爬虫代码负责去网站抓数据。
- Crawlab 负责上传代码、执行爬虫、查看运行日志、查看抓取结果。

也就是说，Crawlab 本身不是写代码的地方，而是一个爬虫任务管理平台。我们可以在 Crawlab 上创建任务、上传爬虫文件、点击运行，并通过日志判断爬虫是否执行成功。

目前有两套 Crawlab 环境：

### 1. 新加坡服务器 Crawlab

- 地址：http://47.245.111.142:18803/
- 登录凭据：使用管理员分配的账号和密码，勿提交到仓库。
- 用途：主要用于抓取外部网站、海外网站。

### 2. 福州服务器 Crawlab

- 地址：http://10.103.2.108:8080/
- 登录凭据：使用管理员分配的账号和密码，勿提交到仓库。
- 使用前提：需要先连接 VPN。
- 用途：主要用于连接公司内网数据库或处理内网相关任务。

---

## 二、新增一个爬虫的大致流程

新增一个爬虫，整体可以分为 6 个步骤：

1. 让 AI 参考 GitHub 上已有代码生成爬虫。
2. 准备和检查爬虫文件。
3. 在 Crawlab 上新建爬虫任务。
4. 上传爬虫文件。
5. 执行爬虫并查看日志。
6. 确认是否抓到数据。

---

## 三、第一步：让 AI 参考 GitHub 上已有代码生成爬虫

爬虫代码统一放在 GitHub 仓库中：

https://github.com/Weidy607/crawlab-news.git

其中 `crawlab-news` 目录是 Crawlab 爬虫脚本目录。

新增爬虫时，不建议从零开始写代码，可以让 AI 参考已有的 TechCrunch 抓取代码，生成一个新的爬虫脚本。

可以给 AI 这样的提示：

```text
请参考 GitHub 仓库 https://github.com/Weidy607/crawlab-news.git 中 crawlab-news 目录下已有的 TechCrunch 抓取代码，帮我新增一个用于抓取【目标网站名称】的 Crawlab 爬虫。

要求：
1. 参考 TechCrunch 爬虫的代码结构和写法；
2. 抓取字段包括：标题、链接、发布时间、来源、正文摘要；
3. 保持和现有 Crawlab 项目类似的目录结构；
4. 生成可以直接上传到 Crawlab 执行的代码；
5. 说明运行命令；
6. 如果需要新增依赖，请同步更新 requirements.txt；
7. 代码中要保留必要的日志输出，方便后续在 Crawlab 的 Log 页面排查错误。
```

AI 生成代码后，需要重点确认以下内容：

- 抓取的网站地址是否正确。
- 提取的字段是否符合要求，例如标题、链接、发布时间、正文等。
- 数据保存方式是否和现有项目一致。
- 爬虫名称是否清楚。
- Crawlab 中的执行命令是否正确。
- 是否需要新增依赖文件。
- 是否保留了必要的日志输出，方便后续排查问题。

---

## 四、第二步：在 Crawlab 上新建爬虫任务

登录 Crawlab 后，进入爬虫管理页面，点击 `New Spider` / “新建爬虫”。

新建爬虫时，需要填写以下信息。实际填写时，可以先参考已有任务的配置方式。

### 1. Name / 爬虫名称

填写爬虫名称，建议使用英文小写，方便后续识别和执行。

例如：

- `crunchbase`
- `techcrunch_api`
- `reuters`

命名建议尽量和目标网站或 spider 名称保持一致。

### 2. Execute Command / 执行命令

填写 Crawlab 执行爬虫时使用的命令。

如果是 Scrapy 爬虫，一般格式是：

```bash
scrapy crawl 爬虫名称
```

例如：

```bash
scrapy crawl crunchbase
```

如果是普通 Python 脚本，一般格式是：

```bash
python main.py
```

执行命令要和代码里的爬虫名称保持一致，否则任务会运行失败。

### 3. Project / 所属项目

选择爬虫所属项目。

目前新闻类爬虫一般选择：

- `news`

本工程的新闻来源及新闻并购邮件报告统一归入 `news`；交易所 MNA 采集在独立工程维护。

### 4. Param / 参数

如果爬虫不需要额外参数，可以先不填。

如果爬虫支持参数，例如抓取最近 24 小时的数据，可以根据代码要求填写参数。

例如：

```text
hours=24
```

具体是否需要填写参数，要看爬虫代码是否支持。

### 5. Default Mode / 默认运行节点

一般保持默认即可：

- `Random Node`

这表示 Crawlab 会自动选择可用节点执行任务。

如果后续有多个节点，并且某个爬虫只能在指定服务器运行，再单独调整。

### 6. Results Collection / 结果集合

填写抓取结果保存到 Crawlab `Data` 页面时使用的集合名称。

建议命名格式为：

```text
results_爬虫名称
```

例如：

- `results_crunchbase`
- `results_techcrunch_api`

这个名称主要用于后续在 Crawlab 的 `Data` 页面查看抓取结果。

### 7. Description / 描述

可以简单写这个爬虫的用途。

例如：

```text
用于抓取 Crunchbase 新闻、融资、并购相关信息。
```

如果暂时没有合适描述，也可以先留空。

### 填写示例

以 `Crunchbase` 爬虫为例，可参考如下填写：

- `Name`: `crunchbase`
- `Execute Command`: `scrapy crawl crunchbase`
- `Project`: `news`
- `Param`: 不填
- `Default Mode`: `Random Node`
- `Results Collection`: `results_crunchbase`
- `Description`: 用于抓取 Crunchbase 相关信息

填写完成后，点击 `Confirm` / `Save` 保存。

保存后，进入该爬虫详情页，继续上传文件、执行任务、查看日志和查看 `Data` 数据。

---

## 五、第三步：上传爬虫文件

新建爬虫任务后，进入该爬虫的详情页，点击顶部的 `Files` 页面，上传爬虫代码。

上传时建议选择 `Folder` / 文件夹，不要只上传单个文件。

### 操作步骤

1. 点击 `Files`。
2. 点击上传按钮。
3. 选择 `Folder`。
4. 点击 `Click to select folder to upload`。
5. 在本地电脑中找到 GitHub 仓库目录。
6. 进入：

	```text
	crawlab-news
	```

7. 如果是新闻类爬虫，选择：

	```text
	news
	```

8. 点击上传。

### 上传完成后如何检查

上传完成后，Crawlab 左侧文件列表中应该能看到 `news` 项目的相关文件，例如：

- `__init__.py`
- `azure_ai.py`
- `Dockerfile`
- `Makefile`
- `requirements.txt`
- `scrapy.cfg`
- `news/`

如果这些文件没有完整显示，说明上传可能不完整，需要重新上传整个 `news` 文件夹。

### 注意事项

上传时选择的是整个项目文件夹，不是单独的 spider 文件。

例如，`Crunchbase` 爬虫虽然只是其中一个爬虫，但仍建议上传整个：

```text
crawlab-news/news
```

不要只上传：

```text
news/spiders/crunchbase.py
```

因为 Scrapy 爬虫运行时还需要 `scrapy.cfg`、`settings.py`、`items.py`、`pipelines.py`、`requirements.txt` 等相关文件。

如果后续修改了代码，需要重新上传更新后的文件夹。

上传完成后，再点击运行按钮执行爬虫。

如果运行失败，先查看 `Tasks` / `Logs`，根据日志提示判断是文件缺失、依赖缺失、命令错误，还是代码逻辑问题。

### 一次更新多个已有爬虫

本地 `news/.crawlab_tasks.map` 保存名称、任务 ID 和预期执行命令，已登记 9 条新闻任务。远端 `Spiders` 实际运行 `panews`，使用本地映射名 `panews`，上传时保留远端显示名称。

在 `crawlab-news` 根目录预览：

```bash
python scripts/update_news.py
```

服务器地址保存在根目录 `crawlab.server.json`：`http://47.245.111.142:18803/api`。工具只依赖 Python 标准库，无需安装 CLI；认证读取本机 `~/.crawlab/cli/config.json` 的现有 token，不将凭据写入工程。先只读核对远端任务：

```bash
python scripts/update_news.py --check-remote
```

执行上传：

```bash
python scripts/update_news.py --execute
```

只更新指定任务：

```bash
python scripts/update_news.py --names a16z bvp --execute
```

在 `news` 目录有 make 时，也可用 `make plan-update-all` 预览、`make update-all` 执行，或 `make update NAME=a16z` 更新单条。上传前核对全部目标 ID、news 项目 ID 和命令；上传后逐文件核对 MD5，保留远端名称、执行命令及定时配置，不自动运行爬虫。遇到失败即停止，当前条目可能已部分更新，修复后可重新上传。快照不含缓存、测试、预览、`.env*` 和本地 ID 映射；远端残留旧文件需单独核对，脚本不会删除它们。更换电脑时需先配置该服务器的登录 token，可用 `--auth-config` 指定本机认证文件。

---

## 六、第四步：根据日志排查错误

如果爬虫运行失败，不要直接判断代码有问题，需要先看日志。

### 1. 文件缺失

日志中可能出现类似：

```text
No such file or directory
```

说明某些文件没有上传，或者路径不对。

处理方式：

- 检查文件是否上传完整。
- 检查执行命令中的文件路径是否正确。

### 2. 依赖缺失

日志中可能出现类似：

```text
ModuleNotFoundError
```

说明运行环境缺少某个 Python 包。

处理方式：

- 根据日志提示安装缺失依赖。
- 或检查 `requirements.txt` 是否完整。

### 3. 网站访问失败

日志中可能出现超时、连接失败等提示。

处理方式：

- 检查目标网站是否可以访问。
- 判断是否需要使用新加坡服务器。
- 如果是内网地址，确认是否应该放到福州 Crawlab 上运行。

### 4. 数据库连接失败

日志中可能出现数据库连接失败、账号密码错误、端口不通等提示。

处理方式：

- 检查数据库地址、账号、密码是否正确。
- 检查当前 Crawlab 环境是否可以访问数据库。
- 如果是内网数据库，通常应使用福州 Crawlab。

### 5. 代码逻辑错误

日志中可能出现 Python 报错，例如字段不存在、解析失败等。

处理方式：

- 根据日志中的错误行号定位代码。
- 修改代码后重新上传。
- 再次执行任务验证。

---

## 七、第五步：确认是否抓取成功

当日志没有明显报错后，还需要进一步确认是否真的抓到了数据。

主要看两个地方：

### 1. 看日志

日志中如果有类似以下信息，说明爬虫有正常抓取动作：

- 抓取文章
- 解析成功
- 写入数据库成功
- `insert success`
- `update success`

### 2. 看 Data 页面

进入 Crawlab 的 Data 页面，查看是否有新抓到的数据。

重点确认：

- 是否有数据
- 数据条数是否正常
- 标题、链接、发布时间等字段是否完整
- 是否有明显乱码
- 是否有大量重复数据
- 是否抓到了目标网站的最新内容

如果日志显示运行成功，但 Data 页面没有数据，需要继续检查：

- 爬虫是否真的 `yield` / 输出了数据
- pipeline 是否正常
- 数据是否写到了其他表
- 数据是否因为去重逻辑被过滤掉
- 抓取时间范围是否设置太短

---

## 八、成功后再设置定时任务

爬虫手动执行成功后，可以设置定时任务，让 Crawlab 自动按时间执行。

注意：建议先手动运行确认成功，再设置定时任务。不要在爬虫还没跑通时直接设置定时。

### 1. 进入定时任务页面

在 Crawlab 左侧菜单点击 `Schedules`，进入定时任务页面后，点击 `+ New Schedule` 新建一个定时任务。

### 2. 主要填写哪些信息

新建定时任务时，重点看以下字段：

#### Name / 定时任务名称

填写一个容易识别的名称。

例如：

- `scrapy crawl CrunchBase`
- `CrunchBase 每日抓取任务`

#### Spider / 选择爬虫

选择前面已经创建好的爬虫任务，例如：

- `crunchbase`

这个字段一定要选对，否则定时任务会执行到其他爬虫。

#### Cron Expression / 定时表达式

这里填写爬虫自动执行的时间规则。

例如：

```text
50 8,20 * * *
```

表示每天 `8:50` 和 `20:50` 各执行一次。

常见写法：

- `50 8 * * *`：每天 8:50 执行一次
- `50 8,20 * * *`：每天 8:50 和 20:50 各执行一次
- `0 * * * *`：每小时整点执行一次
- `*/30 * * * *`：每 30 分钟执行一次

填写后，右侧的 `Cron Info` 一般会自动显示下一次执行时间，可以用来确认设置是否正确。

#### Command / 执行命令

填写这个定时任务实际执行的命令。

如果是 Scrapy 爬虫，一般格式是：

```bash
scrapy crawl 爬虫名称
```

例如：

```bash
scrapy crawl crunchbase
```

如果是普通 Python 脚本，则可能是：

```bash
python main.py
```

这里的命令要和前面 `Spider` 中配置的执行命令保持一致。

#### Param / 参数

如果没有额外参数，可以不填。

如果代码支持参数，例如抓取最近 24 小时的数据，可以按实际代码要求填写，例如：

```text
hours=24
```

#### Default Mode / 默认节点

一般保持默认：

- `Random Node`

表示 Crawlab 自动选择可用节点执行任务。

#### Enabled / 是否启用

这里要保持开启状态。

如果关闭，定时任务虽然创建了，但不会自动执行。

#### Description / 描述

可以简单写这个定时任务的用途，例如：

```text
每天定时抓取 CrunchBase 数据。
```

也可以不填。

### 3. 填写示例

以 `CrunchBase` 为例，可以这样填写：

- `Name`: `scrapy crawl CrunchBase`
- `Spider`: `crunchbase`
- `Cron Expression`: `50 8,20 * * *`
- `Command`: `scrapy crawl crunchbase`
- `Param`: 不填
- `Default Mode`: `Random Node`
- `Enabled`: 开启
- `Description`: 每天 8:50 和 20:50 定时抓取 CrunchBase 数据

这个配置表示：Crawlab 会每天上午 `8:50` 和晚上 `20:50` 自动执行一次 `crunchbase` 爬虫。

### 4. 保存后怎么检查

填写完成后，点击 `Save` 保存。

保存后，返回 `Schedules` 列表，重点确认：

- `Name` 是否正确
- `Spider` 是否选对
- `Cron Expression` 是否正确
- `Enabled` 是否开启

定时任务创建完成后，不代表一定已经成功抓到数据，后续还需要继续检查：

- 到 `Tasks` 页面查看任务是否按时生成
- 打开任务日志，看是否有报错
- 到 `Data` 页面查看是否有新数据

可以简单理解为：

- `Schedules`：负责设置什么时候运行
- `Tasks`：负责查看有没有运行、运行是否报错
- `Data`：负责确认有没有抓到数据

---

## 九、简单检查清单

新增一个爬虫时，可以按下面清单检查：

- 是否参考了 GitHub 上已有爬虫代码
- 是否已经准备好完整的爬虫文件
- 是否在 Crawlab 中新建了爬虫任务
- 执行命令是否正确
- 文件是否上传完整
- 是否已手动执行过
- 日志是否没有明显报错
- Data 页面是否能看到抓取数据
- 数据字段是否完整
- 确认成功后，是否设置了定时任务

---

## 十、一句话总结

新增 Crawlab 爬虫的核心流程是：先参考 GitHub 写好爬虫代码，再到 Crawlab 上传文件并执行；如果报错，就根据日志修改；如果执行成功，再到 Data 页面确认是否抓到了数据，最后再设置定时任务。
