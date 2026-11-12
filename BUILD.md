# Crawlab 镜像构建与启动

本工程仅部署 `news`，镜像依赖来自该目录的 `requirements.txt`。交易所 MNA 采集在独立工程维护；news 内的并购邮件报告仍随 news 部署。

## 1) 进入项目根目录

```bash
cd /home/foxit/apps/crawlab-news
```

## 2) 构建自定义 Crawlab 镜像

```bash
docker build -f Dockerfile.crawlab -t crawlabteam/crawlab:0.6.2-custom .
```

## 3) 启动服务（master + mongo）

```bash
docker compose -p crawlab up -d --build master mongo
```

使用 `-p crawlab` 保持原 Compose 项目名，避免因工程目录改名而切换项目。

## 4) 查看 master 日志

```bash
docker compose -p crawlab logs -f master
```

## 5) 仅重建 master（可选）

```bash
docker compose -p crawlab build master
docker compose -p crawlab up -d master
```
