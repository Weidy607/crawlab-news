# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html

import os

from runtime_config import load_local_environment

load_local_environment()

BOT_NAME = 'news'
SPIDER_MODULES = ['news.spiders']
NEWSPIDER_MODULE = 'news.spiders'

HTTPERROR_ALLOWED_CODES = [301,302]

# ------------------------------------------------------------
# 基础设置
# ------------------------------------------------------------
FEED_EXPORT_ENCODING    = 'utf-8'  #编码
ROBOTSTXT_OBEY          = False    #是否遵守网站 robots.txt 声明规则
COOKIES_ENABLED         = True    #是否启用 cookie
REDIRECT_ENABLED        = False    #是否允许重定向

# ------------------------------------------------------------
# 自动下载频率控制, 自动计算的下载延迟会 > DOWNLOAD_DELAY 且 < AUTOTHROTTLE_MAX_DELAY
# 参考: https://docs.scrapy.org/en/latest/topics/autothrottle.html
# ------------------------------------------------------------
AUTOTHROTTLE_ENABLED = True    #开启访问频率限制
AUTOTHROTTLE_START_DELAY = 1.0 #下载延迟初始化值（秒）
AUTOTHROTTLE_MAX_DELAY = 8.0   #最大下载延迟（秒）
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0 #Scrapy应该并行发送到远程网站的平均请求数
DOWNLOAD_DELAY = .5 #下裁延迟固定值（秒）

# ------------------------------------------------------------
# 下载中间件设置, 参考官网 https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
# ------------------------------------------------------------
DOWNLOADER_MIDDLEWARES = {
    'scrapy.downloadermiddlewares.useragent.UserAgentMiddleware': None,
    # 'news.middlewares.proxy.XundailiProxyMiddleware': 350, #讯代理
    'news.middlewares.proxy.KuaidailiProxyMiddleware': 350, #讯代理
    'scrapy.downloadermiddlewares.httpproxy.HttpProxyMiddleware': 400,
    'news.middlewares.user_agent.UserAgentMiddleware': 520,
}

# ------------------------------------------------------------
# item pipeline
# ------------------------------------------------------------
ITEM_PIPELINES = {
    'news.pipelines.DefaultValuesPipeline': 300,
    'news.pipelines.HandlePubDatePipeline': 301,
    'news.pipelines.HandleCleanTextPipeline': 302,
    'news.pipelines.HandleCleanTitlePipline': 303,
    'news.pipelines.HandleStatusPipeline': 304,  # 状态字段,默认0,
    'news.pipelines.MongoPipeline': 305,

}

# # ------------------------------------------------------------
# # scheduler 调度器设置
# # ------------------------------------------------------------
# # scheduler 更改为 scrapy-redis-bloomfilter 的调度器
# SCHEDULER = "scrapy_redis_bloomfilter.scheduler.Scheduler"
# # 是否将调度数据持久化
# SCHEDULER_PERSIST = True

# ------------------------------------------------------------
# dupefilter 去重设置
# ------------------------------------------------------------
# # 去重模块更改为 scrapy-redis-bloomfilter 的去重器
# DUPEFILTER_CLASS = "scrapy_redis_bloomfilter.dupefilter.RFPDupeFilter"
# # 散列函数的个数, 默认为6，
# BLOOMFILTER_HASH_NUMBER = 10
# # Bloom Filter 的 bit 参数，默认30（一亿级指纹池）
# BLOOMFILTER_BIT = 30

# ------------------------------------------------------------
# redis 配置
# ------------------------------------------------------------
REDIS_HOST = "yu-redis-svc"
REDIS_PORT = 6379
REDIS_PARAMS = {
    "db": 11,
    "password": os.getenv("REDIS_PASSWORD", ""),
}

# ------------------------------------------------------------
# mongodb 配置
# ------------------------------------------------------------
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/yuSpider')
MONGO_DB_NAME = "yuSpider"
MONGO_USERNAME = "yu"
MONGO_PASSWORD = os.getenv('MONGO_PASSWORD', '')


# ------------------------------------------------------------
# mysql 配置
# ------------------------------------------------------------

# 开发调试时使用外网elb的ip访问mysql
# MYSQL_URI = '59.36.169.216'
# 生产环境使用局域网IP访问mysql
MYSQL_URI = '172.17.0.1'
MYSQL_DB_NAME = 'smart'
MYSQL_USERNAME = 'yu'
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
MYSQL_PORT = 13306

MYSQL_DICT = {
    'host':MYSQL_URI,
    'port': MYSQL_PORT,
    'user':MYSQL_USERNAME,
    'password':MYSQL_PASSWORD,
    'db':MYSQL_DB_NAME,
}

# ------------------------------------------------------------
# TechCrunch API -> ai_news (MySQL)
# ------------------------------------------------------------
AI_NEWS_MYSQL = {
    'host': os.getenv('MYSQL_HOST', 'host.docker.internal'),
    'port': int(os.getenv('MYSQL_PORT', '3306')),
    'user': os.getenv('MYSQL_USER', 'quick-spider-admin'),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    'db': os.getenv('MYSQL_DB', 'quick-spider-admin'),
    'table': os.getenv('MYSQL_TABLE', 'ai_news'),
    'charset': 'utf8mb4',
}
