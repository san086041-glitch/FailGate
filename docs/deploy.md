# 一键部署（docker compose）

一条命令把 FailGate 的全部组件跑起来：接 webhook 的服务、跑沙箱的 worker、数据库、队列。适合长期挂着接 GitHub 事件；只在本机试用的话，`failgate up`（不需要容器）更轻。

## 起来之后是什么样

| 容器 | 作用 |
|---|---|
| `api` | 接 GitHub webhook、分诊 / 查重 / 答疑、写评论、工作台 `/console`。只绑本机 `127.0.0.1:8080` |
| `worker` | 复现、核验、修复这些要跑沙箱的慢活。它通过 `docker.sock` 让**宿主机的 Docker** 起沙箱容器；安装依赖时只能经白名单代理访问 PyPI（ADR 0042，第一次安装时自动建好） |
| `postgres` / `redis` | 数据库和队列，只在 compose 内部网络里，不占宿主机端口 |
| `tunnel`（可选） | smee 转发：没有公网地址时，把 GitHub webhook 转进来 |

## 第一次部署

1. 装好 Docker Desktop（Windows / macOS）或 Docker Engine（Linux），确认 `docker info` 能用。
2. 复制配置：`cp .env.example .env`，至少填：
   - `GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY_PATH`（宿主机上 App 私钥的路径，compose 会把它挂进容器）、`GITHUB_WEBHOOK_SECRET`；建 App 的步骤见 [github-app-setup.md](github-app-setup.md)；
   - `LLM_API_KEY`（以及要用向量检索时的 `EMBED_API_KEY`）；
   - `CONSOLE_TOKEN`：工作台口令，随便一串长随机字符。容器里看不到"本机访问"，所以 compose 部署**必须**配它。
3. 启动：

   ```bash
   docker compose up -d --build
   ```

4. 确认：

   ```bash
   docker compose ps                               # api 应该是 healthy
   curl http://127.0.0.1:8080/healthz              # {"status":"ok",...}
   docker compose exec worker failgate sandbox check   # 16 项隔离自检，含出网白名单
   ```

5. 打开工作台：浏览器访问 `http://127.0.0.1:8080/console?token=<你的 CONSOLE_TOKEN>`（之后会记在 cookie 里）。

## 让 GitHub 的事件进来

- **有公网地址**：把 GitHub App 的 webhook URL 设成 `https://<你的域名>/webhooks/github`，在前面放一个反向代理（HTTPS）转到 `127.0.0.1:8080`。
- **没有公网地址**：用 smee 转发。在 `.env` 里填 `SMEE_URL=https://smee.io/<通道>`（和 App 的 webhook URL 一致），然后：

  ```bash
  docker compose --profile tunnel up -d
  ```

## 日常

```bash
docker compose logs -f api worker      # 看日志
docker compose restart                 # 改了 .env 之后
git pull && docker compose up -d --build   # 升级（数据库迁移在启动时自动做）
docker compose down                    # 停（数据保留在卷里）
docker compose down -v                 # 停并删除全部数据
```

和本机直接跑共用同一个 `.env`：compose 只覆盖容器里路径和地址不一样的几项（数据库、Redis、私钥路径、产物目录）。

## 注意

- **`docker.sock` 等同宿主机 root**：只挂给 worker，api 不挂。worker 里跑的是 FailGate 自己的代码；不可信的代码（PR、复现脚本）都在它起的沙箱容器里，沙箱的隔离参数见 `failgate sandbox check`。生产环境建议给 worker 一台单独的机器，或用 rootless Docker。
- **端口冲突**：`failgate up` 和 compose 都默认用 8080，二选一；也可以在 `.env` 里设 `FAILGATE_PORT=8090`。compose 的 postgres / redis 不占宿主机端口，和本机已有的数据库不冲突。
- **数据**：数据库、队列、沙箱日志都在 compose 的卷里；本机直接跑时用的数据库（比如 `failgate_live`）不会自动搬过来，需要的话用 `failgate db copy`。
- 镜像约 550 MB（Python 3.12 slim + 依赖 + docker 命令行）。
