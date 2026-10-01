# 创建 Fixer App（自带修复 Agent 开 PR 的身份）

完成后，维护者在一个已复现（有封存考卷）的 issue 下评论 `/failgate fix`，FailGate 的修复 Agent 会修代码，补丁过了考卷就以 `你的Fixer名[bot]` 的身份推到 `failgate/fix-N` 分支并开 PR；PR 自动被核验，被驳回时修复 Agent 会按理由再改（最多 2 轮）。大约需要 10 分钟。设计见 ADR 0029。

## 为什么要单独一个 App

已有的核验 App（阅卷）只有 Issues / Pull requests 写权限，**不能改代码**。修复要推分支，需要 Contents 写权限，交给另一个 App：

- "阅卷的不能改代码"在权限上成立，不靠约定；
- Fixer 开的 PR 来自另一个机器人账号，核验 App 默认忽略所有机器人事件，只放行这个账号的 PR 事件；
- 不想要修复的仓库不装 Fixer App 就行，核验照常。

## 1. 创建 App

打开 <https://github.com/settings/apps/new>，填写：

| 字段 | 填什么 |
|---|---|
| GitHub App name | 全局唯一，例如 `failgate-fixer-你的账号` |
| Homepage URL | 演示仓库的地址就行 |
| Webhook → Active | **不勾选**（Fixer 不需要接收事件，事件由核验 App 接收） |
| Repository permissions → Contents | **Read and write** |
| Repository permissions → Pull requests | **Read and write** |
| Repository permissions → Metadata | Read-only（默认就是） |
| Where can this GitHub App be installed? | Only on this account |

其他权限都不要开。点 **Create GitHub App**。

## 2. 记下 App ID，生成私钥

- 页面顶部的 **App ID** 填进 `.env` 的 `FIXER_APP_ID`。
- 页面底部点 **Generate a private key**，把下载的 `.pem` 移到仓库外面，例如 `D:\AgentProject\secrets\failgate-fixer.pem`，路径填进 `.env` 的 `FIXER_APP_PRIVATE_KEY_PATH`。

> 私钥等于 App 的密码：不要提交、不要发给任何人。

## 3. 只安装到演示仓库

App 设置页左侧点 **Install App** → 选你的账号 → **Only select repositories** → 只勾选 `failgate-demo`。

## 4. 检查配置

```bash
python -m failgate fixer check san086041-glitch/failgate-demo
```

会打印 App 名、权限、**机器人登录名**（形如 `failgate-fixer-你的账号[bot]`），以及有没有装到这个仓库。把机器人登录名填进 `.env` 的 `FIXER_BOT_LOGIN`，再跑一次确认没有警告。

## 5. 试一次

1. 确认 `.env` 里 `REPRO_ENABLED=true`，仓库是 live 模式（`failgate repo mode <repo> live`），服务和 smee 转发都开着；
2. 在一个已经 REPRODUCED、有封存考卷的 issue 下评论 `/failgate fix`（评论的人要有写权限）；
3. 几分钟后 issue 下会出现修复 Agent 的说明；补丁过了考卷时，Fixer 会开一个 `Fixes #N` 的 PR，FailGate 随后在 PR 下发核验报告；被驳回时会看到"第 1 轮重修"的说明和新的提交。

## 撤销

不想让修复 Agent 推代码时：在仓库设置里卸载 Fixer App，或者清空 `.env` 的 `FIXER_APP_ID`（推送会记为失败，不会重试）。影子模式下推送只记录、不执行。
