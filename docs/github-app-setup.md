# 创建 GitHub App 并接到测试仓库

完成后，在测试仓库里开一个 issue，FailGate 会以 `你的App名[bot]` 的身份打上标签、发一条汇总评论。大约需要 15 分钟。

## 0. 准备

- 一个**测试仓库**（私有即可），例如 `你的账号/failgate-sandbox`。不要直接用 FailGate 自己的仓库。
- 本机已装 Node.js（用来运行 smee 转发工具）。

## 1. 开一个 webhook 转发通道

GitHub 访问不到你的本机，所以用 smee.io 中转：GitHub → smee.io → 本机。

打开 <https://smee.io/new>，复制页面上的通道地址（形如 `https://smee.io/AbCdEf123`）。

## 2. 创建 App

打开 <https://github.com/settings/apps/new>，填写：

| 字段 | 填什么 |
|---|---|
| GitHub App name | 全局唯一，例如 `failgate-dev-你的账号` |
| Homepage URL | 测试仓库的地址就行 |
| Webhook → Active | 勾选 |
| Webhook URL | 第 1 步的 smee 地址 |
| Webhook secret | 自己生成一个随机串（PowerShell：`[guid]::NewGuid().ToString("N")`），**同时**写进 `.env` 的 `GITHUB_WEBHOOK_SECRET` |
| Repository permissions → Issues | **Read and write** |
| Repository permissions → Metadata | Read-only（默认就是） |
| Subscribe to events | 勾选 **Issues**、**Issue comment** |
| Where can this GitHub App be installed? | Only on this account |

其他权限都不要开。Pull requests 的写权限等 M3 做修复时再加，改权限后需要在安装页面重新确认。

点 **Create GitHub App**。

## 3. 记下 App ID，生成私钥

- 页面顶部的 **App ID** 是一串数字，填进 `.env` 的 `GITHUB_APP_ID`。
- 页面底部点 **Generate a private key**，浏览器会下载一个 `.pem` 文件。把它移到**仓库外面**，例如 `D:\AgentProject\secrets\failgate-dev.pem`，再把这个路径填进 `.env` 的 `GITHUB_APP_PRIVATE_KEY_PATH`。

> 私钥等于 App 的密码：不要提交、不要发给任何人。仓库的 `.gitignore` 已经忽略了 `*.pem`，但放在仓库外更保险。

## 4. 安装到测试仓库

在 App 设置页左侧点 **Install App**，选你的账号，然后选 **Only select repositories**，只勾选测试仓库。

## 5. 检查配置

```bash
python -m failgate github check
```

能看到 App 名称、权限、订阅的事件，以及"安装 xxx … 令牌获取成功=True"，就说明私钥和 App ID 都对了。

## 6. 跑起来

开两个终端，都在 `failgate/` 目录下：

```bash
npx smee-client --url https://smee.io/<你的通道> --target http://127.0.0.1:8080/webhooks/github
```

```bash
python -m failgate serve
```

## 7. 切到正常模式

新仓库默认是**影子模式**：只记录，不发任何东西。先在测试仓库里随便开一个 issue，让 FailGate 登记这个仓库，然后：

```bash
python -m failgate repo list
python -m failgate repo mode 你的账号/failgate-sandbox live
```

之后再开的 issue 会真正收到标签和评论。切换之前记录下来的影子动作**不会**补发。

## 8. 验证

- 新开一个 bug issue：几秒后应该出现标签和一条"FailGate 验收报告"。
- 用只读协作者或非协作者的账号评论 `/failgate ignore`：不应该有任何效果。
- 用自己的账号评论 `/failgate ignore`：Case 进入 IGNORED（`python -m failgate cases` 可以看到）。
- 出错时查看 `curl http://127.0.0.1:8080/api/cases/<id>` 里 effects 的 `status` 和 `error`，或者执行 `python -m failgate effects flush` 手动补发。
