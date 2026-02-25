# 🐳 Docker 运维操作手册（服务器常用）

本文面向已使用 Docker 部署 `daily_stock_analysis` 的用户，覆盖日常运维命令：启动、暂停、查看日志、进入容器、资源排查等。

> 默认使用仓库内编排文件：`docker/docker-compose.yml`
>
> 下文统一使用 `docker compose`（Compose v2）。如果你的环境只有旧版，也可把命令替换为 `docker-compose`。

---

## 1. 进入项目目录

```bash
cd /path/to/daily_stock_analysis
```

---

## 2. 常用服务说明

- `stock-server`：Web/API 服务（对应 compose 服务 `server`）
- `stock-analyzer`：定时分析任务（对应 compose 服务 `analyzer`）
- `stock-rsshub`：RSS 新闻服务（对应 compose 服务 `rsshub`）

查看当前服务状态：

```bash
docker compose -f ./docker/docker-compose.yml ps
```

---

## 3. 启动 / 停止 / 重启

### 启动指定服务

```bash
# 启动 Web/API
docker compose -f ./docker/docker-compose.yml up -d server

# 启动定时分析
docker compose -f ./docker/docker-compose.yml up -d analyzer

# 同时启动所有服务
docker compose -f ./docker/docker-compose.yml up -d
```

### 停止服务（保留容器）

```bash
docker compose -f ./docker/docker-compose.yml stop
```

### 启动已停止服务

```bash
docker compose -f ./docker/docker-compose.yml start
```

### 重启服务

```bash
# 重启全部
docker compose -f ./docker/docker-compose.yml restart

# 只重启 server
docker compose -f ./docker/docker-compose.yml restart server
```

### 下线并删除容器（不删数据卷）

```bash
docker compose -f ./docker/docker-compose.yml down
```

---

## 4. 暂停 / 恢复（你提到的重点）

> `pause` 会冻结容器内进程，适合短时间“挂起”；长期建议用 `stop`。

### 暂停全部服务

```bash
docker compose -f ./docker/docker-compose.yml pause
```

### 恢复全部服务

```bash
docker compose -f ./docker/docker-compose.yml unpause
```

### 暂停/恢复单个容器（按容器名）

```bash
docker pause stock-server
docker unpause stock-server
```

---

## 5. 查看日志（你提到的重点）

### 实时跟踪日志

```bash
# 全部服务
docker compose -f ./docker/docker-compose.yml logs -f

# 仅 server
docker compose -f ./docker/docker-compose.yml logs -f server

# 仅 analyzer
docker compose -f ./docker/docker-compose.yml logs -f analyzer
```

### 查看最近 N 行日志

```bash
docker compose -f ./docker/docker-compose.yml logs --tail=200 server
```

### 查看带时间戳日志

```bash
docker compose -f ./docker/docker-compose.yml logs -f --timestamps server
```

---

## 6. 进入容器排查

### 进入 shell

```bash
docker exec -it stock-server /bin/bash
# 若无 bash，可改为 /bin/sh
```

### 容器内常用排查

```bash
# 查看环境变量
printenv | sort

# 查看应用日志目录
ls -lah /app/logs

# 查看最近日志
tail -n 200 /app/logs/*.log
```

---

## 7. 配置修改后如何生效

### 仅修改 `.env`

```bash
# 推荐重启相关服务
docker compose -f ./docker/docker-compose.yml up -d --force-recreate server analyzer
```

### 修改了代码 / Dockerfile / requirements

```bash
# 重新构建并启动
docker compose -f ./docker/docker-compose.yml up -d --build
```

---

## 8. 服务器内存不足时的建议（重点）

### 快速查看容器资源占用

```bash
docker stats
```

### 低内存场景建议

1. 先只启动 `server` 或只启动 `analyzer`，避免全开。
2. 降低并发：在 `.env` 设置较小 `MAX_WORKERS`（如 `1`）。
3. 分时运行：白天运行 `server`，收盘后再启动 `analyzer`。
4. 先用 `stop` 而不是长期 `pause`。

### 清理无用资源（谨慎）

```bash
# 清理悬空镜像/缓存
docker system prune -f

# 更激进（会清理未使用卷，注意数据风险）
docker system prune -a --volumes
```

---

## 9. 常见故障速查

### 服务起不来

```bash
docker compose -f ./docker/docker-compose.yml ps
docker compose -f ./docker/docker-compose.yml logs --tail=200 server
```

### API 无法访问

1. 检查容器是否运行：`docker compose ... ps`
2. 检查端口映射是否生效（默认 compose 中映射了 `80` 和 `${API_PORT}`）
3. 服务器防火墙/安全组是否放行

### 定时任务没执行

1. 看 `analyzer` 日志：`docker compose ... logs -f analyzer`
2. 检查 `.env` 中调度参数（如 `SCHEDULE_ENABLED`、`SCHEDULE_TIME`）

---

## 10. 推荐操作流（实用）

### 更新代码并平滑重启

```bash
git pull
docker compose -f ./docker/docker-compose.yml up -d --build
docker compose -f ./docker/docker-compose.yml logs -f --tail=100 server
```

### 临时维护窗口

```bash
# 先暂停（短时）
docker compose -f ./docker/docker-compose.yml pause

# 或停止（长时）
docker compose -f ./docker/docker-compose.yml stop

# 恢复
docker compose -f ./docker/docker-compose.yml start
```

---

如需我再补一版「**只保留 10 条最常用命令**」的极简速查卡片，我可以直接追加到本文末尾。