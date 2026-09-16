# Context Continuity

[English](README.md) · [完整示例](examples/quickstart/README.md) · [v2.1.0](https://github.com/wangyuqin378-cpu/context-continuity/releases/tag/v2.1.0)

## 这是什么

一套用于长期任务恢复的 Agent Skill 和本地 Python 工具。它把当前目标、决策、证据和下一步保存为 Markdown 检查点，在中断、上下文压缩或换会话后，给新的 Agent 一张可执行的 **Resume Card（恢复卡）**。

适合跨会话、值得保留决策的工作；几分钟就结束的临时任务通常用不到。需要 Python 3.9+，只使用标准库，本地 CLI 已有 macOS 和 Linux 检查。

## 怎么使用

### 安装到 Codex

使用新的 Skill 目录，不要覆盖已有安装：

```sh
mkdir -p ~/.codex/skills
git clone https://github.com/wangyuqin378-cpu/context-continuity.git \
  ~/.codex/skills/context-continuity
python3 ~/.codex/skills/context-continuity/scripts/contextctl.py --version
```

安装后新建 Codex 任务，让宿主重新加载 Skill。其他支持 Skill 的宿主可以读取 [SKILL.md](SKILL.md)，但完整接入尚未在本仓库验证。

### 开始任务，再恢复任务

告诉 Agent 稳定的任务 ID 和真实工作目录：

```text
这项任务使用 context-continuity。
Task ID: checkout-timeout
Workspace root: /absolute/path/to/storefront
目标：修复结账超时，不改变公开 API。
验收：超时测试通过，预发布链路中没有重复扣款。
只在重要状态变化时保存检查点，不必每轮保存。
```

Agent 会通过评审、发布流程维护 `.continuity/checkout-timeout/`。在目标项目的 `.gitignore` 中加入 `**/.continuity/`；检查点可能包含本机路径和项目内容，公开前需要检查。

中断后，在新会话中说：

```text
用 context-continuity 恢复 checkout-timeout。
继续前先读取完整检查点，按其中的下一步与验收条件执行。
```

底层恢复命令：

```sh
python3 ~/.codex/skills/context-continuity/scripts/contextctl.py resume \
  /absolute/path/to/storefront/.continuity/checkout-timeout --full
```

恢复卡回答“当前目标、下一步、怎样算完成”。下面是简化示例，不是本轮实跑结果：

```text
GOAL: 修复结账超时，不改变公开 API。
DO NOW: 复现剩余的 payload-hash 不一致问题。
DONE WHEN: 三组固定输入得到预期的稳定哈希。
```

第一次使用请按[完整示例](examples/quickstart/README.md)完成建档、恢复和收尾。同一契约已读入的日常续跑，可以省略 `--full` 使用简短视图。

## 为什么做

长期对话保存了很多信息，也可能漏掉最关键的一次纠正。换一个 Agent 后，已经失败的方案可能再试一遍，旧需求可能重新生效，未经核对的状态也可能被当作完成。

这个项目把这些信息变成明确的任务状态：当前要求是什么，证据在哪里，哪条路不能再走，下一步怎样验证。它保留有用的变化，让后续工作能从具体位置继续。

结构检查通过不代表检查点里的每个结论都是真的，仍需要核对证据和评审判断。具体范围见[评测结果](docs/GUIDE.zh-CN.md#评测结果)和[信任边界](docs/GUIDE.zh-CN.md#能力边界与信任边界)。

[详细指南](docs/GUIDE.zh-CN.md) · [评测协议](evals/README.md) · [贡献说明](CONTRIBUTING.md) · [安全说明](SECURITY.md) · [MIT 许可](LICENSE)
