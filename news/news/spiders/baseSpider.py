import scrapy


class BaseSpider(scrapy.Spider):
    """Thin base class shared by all news spiders."""
    name = "base"
    use_proxy_lst = []  # default; overwritten by pipeline's open_spider when proxy is needed

    async def start(self):
        """Bridge legacy start_requests() spiders to Scrapy 2.17+."""
        start_requests = getattr(self, "start_requests", None)
        if callable(start_requests):
            for request in start_requests():
                yield request
            return

        for url in getattr(self, "start_urls", []):
            yield scrapy.Request(url, dont_filter=True)
