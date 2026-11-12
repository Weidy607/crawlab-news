# -*- coding: utf-8 -*-
# Authors: Joe <xiaotu9639@gmail.com>
# Date: 2020/8/7
import hashlib
import os

from runtime_config import load_local_environment

load_local_environment()

import requests
import base64
import time

class ProxypoolMiddleware:

    proxy_pool_address = 'http://proxypool.scrapy.idata:15000/random'

    def process_request(self, request, spider):
        r = requests.get(self.proxy_pool_address)
        if r.ok:
            proxy_host_port = r.text
            request.headers["X-Forwarded-For"] = "_"
            request.meta['proxy'] = f'http://{proxy_host_port}'

class AbuyunProxyMiddleware:
    """
    阿布云代理: https://www.abuyun.com/http-proxy/dyn-manual.html
    """
    # 动态代理地址
    proxy_address = 'http://http-dyn.abuyun.com:9020'
    # 代理隧道验证信息
    proxyUser = os.getenv('ABUYUN_PROXY_USER', "")
    proxyPass = os.getenv('ABUYUN_PROXY_PASSWORD', "")
    # 认证信息
    auth_str = f'{proxyUser}:${proxyPass}'.encode()
    proxyAuth = b"Basic " + base64.b64encode(auth_str)

    def process_request(self, request, spider):
        request.headers["X-Forwarded-For"] = "_"
        request.headers["Authorization"] = self.proxyAuth
        request.meta["proxy"] = self.proxy_address

class KuaidailiProxyMiddleware:
    """
    快代理
    """
    proxy_address = os.getenv('KUAIDAILI_PROXY_URL', "")

    def process_request(self, request, spider):
        if spider.name not in spider.use_proxy_lst:
            return
        print('Kuaidaili 获取代理')

        # request.headers["Authorization"] = auth
        request.meta["proxy"] = self.proxy_address


class XundailiProxyMiddleware:
    """
    讯代理: http://www.xdaili.cn
    注意：
        scrapy 框架会删除 Proxy-Authorization 头信息，需要注释掉以下文件的2行代码
        site-packages/scrapy/core/downloader/handlers/http11.py
        ```
            if isinstance(agent, self._TunnelingAgent):
                headers.removeHeader(b'Proxy-Authorization')
        ```
        参考: https://blog.csdn.net/qq_26877377/article/details/82499087
             https://my.oschina.net/chenmoxuan/blog/3095926

    已解决, 在 http header 中使用 Authorization 代替 Proxy-Authorization
    """

    proxy_address = 'http://forward.xdaili.cn:80'
    orderno = os.getenv('XUNDAILI_ORDERNO', "")
    secret = os.getenv('XUNDAILI_SECRET', "")

    def process_request(self, request, spider):
        if spider.name not in spider.use_proxy_lst:
            return
        print('获取代理')
        timestamp = str(int(time.time()))
        string = "orderno=" + self.orderno + "," + "secret=" + self.secret + "," + "timestamp=" + timestamp
        string = string.encode()
        md5_string = hashlib.md5(string).hexdigest()
        sign = md5_string.upper()
        auth = "sign=" + sign + "&" + "orderno=" + self.orderno + "&" + "timestamp=" + timestamp

        request.headers["X-Forwarded-For"] = "_"
        request.headers["xdaili-Proxy-Authorization"] = auth
        # request.headers["Authorization"] = auth
        request.meta["proxy"] = self.proxy_address


if __name__ == '__main__':
    print(requests.get('http://proxypool.scrapy.idata:15000/random').text)
