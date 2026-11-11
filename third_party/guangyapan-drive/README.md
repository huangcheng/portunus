# 光鸭网盘 Skill 内置依赖版（bundled）

通过 Agent 登录和操作光鸭网盘。本包已内置运行依赖，安装时无需访问 npm。
网盘操作仍需要联网访问光鸭服务。

本包连接光鸭线上环境：`api.guangyapan.com` 和
`account.guangyapan.com`。

## 安装

要求 Node.js 20+，支持 macOS、Windows 10/11 和 Linux（Ubuntu 22.04/24.04 为首批验收目标，x64/arm64）。

1. 将本包整个目录安装到当前 Agent 的 Skill 目录。
2. 向 Agent 说“登录光鸭”或直接描述网盘操作目标。

不要运行 `npm install`、`npm ci` 或精简版的依赖安装脚本。本包已包含运行就绪记录，
记录缺失或版本不匹配时，重新解压安装内置依赖版。
安装、更新和卸载 Skill 包不需要登录光鸭，也不应打开登录页。

## 使用与常见问题

两种包共用 [使用指南](docs/usage.md)，包括登录取消、多 Agent 隔离、能力限制、分享审核和更新方法。

Agent 的调用流程见 [SKILL.md](SKILL.md)，参数和任务规则见
[操作说明](references/operations.md) 与 [后台任务](references/tasks.md)。
