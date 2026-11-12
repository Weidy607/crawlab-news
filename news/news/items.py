# Define here the models for your scraped items
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/items.html

import scrapy


class Article(scrapy.Item):
    url = scrapy.Field()
    cls = scrapy.Field()
    title = scrapy.Field()
    pub_date = scrapy.Field()
    content = scrapy.Field()
    search_key = scrapy.Field()
    spider_name = scrapy.Field()
    text = scrapy.Field()
    status = scrapy.Field()
    company = scrapy.Field()
    stockCode = scrapy.Field()
    ori_pub_date = scrapy.Field()
    craw_date = scrapy.Field()



class CommentItem(scrapy.Item):
    url = scrapy.Field()
    cls = scrapy.Field()
    question = scrapy.Field()
    question_date = scrapy.Field()
    answer = scrapy.Field()
    answer_date = scrapy.Field()


class CnkiArticle(scrapy.Item):
    year = scrapy.Field(default=2019)
    fund = scrapy.Field()
    catalog = scrapy.Field(default='特约专稿')
    ztcls = scrapy.Field()
    pdf = scrapy.Field()
    doi = scrapy.Field()
    keyword = scrapy.Field()
    author = scrapy.Field()
    url = scrapy.Field()
    title = scrapy.Field()
    summary = scrapy.Field()
    state = scrapy.Field(default=True)
    organization = scrapy.Field()
    issue = scrapy.Field()


class TechCrunchNewsItem(scrapy.Item):
    """
    字段与 02_News_Scraper 输出保持一致。
    """
    标题中文 = scrapy.Field()
    标题英文 = scrapy.Field()
    链接 = scrapy.Field()
    网站 = scrapy.Field()
    发布日期 = scrapy.Field()
    作者 = scrapy.Field()
    标签 = scrapy.Field()
    内容摘要中文 = scrapy.Field()
    内容摘要英文 = scrapy.Field()
    AI摘要中文 = scrapy.Field()
    AI摘要英文 = scrapy.Field()
    完整内容英文 = scrapy.Field()


class SearchInfo(object):
    def format_search_key(self,search_keys):
        for item in search_keys:
            item = item.strip()
        return search_keys
    def __init__(self,item):
        self.name = item['name'].strip()
        self.stockCode = item['stockCode']
        self.search_keys = self.format_search_key(item['keywords'].split(","))
        pass
