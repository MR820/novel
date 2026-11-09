# Novel：让 Trae 连续完成一本小说

在 Trae 中打开本仓库，输入 **「开始写书」**。Agent 会读取创作要求，制定设定与结局，逐章写作、审稿、更新事实记录，最后检查并导出全书。

这里的「开始写书」是本项目规则约定的自然语言入口，**不是 Trae 内置的斜杠命令**。Trae 负责生成内容，Python 工具负责校验、保存进度、断点恢复和合稿；无需额外模型 API、服务器或第三方 Python 依赖。

## 快速开始

需要 Python 3.10 或更新版本，以及能够读写项目文件、执行本地命令的 Trae Agent。

```bash
git clone https://github.com/MR820/novel.git
cd novel
python novel.py init
```

Windows 通常使用 `python`；macOS/Linux 如果没有这个命令，请改用 `python3`。后续命令都使用同一个解释器。

1. 用 Trae 打开 `novel` 文件夹。
2. 按需编辑 [`book.json`](book.json) 和 [`创作任务书.md`](创作任务书.md)。默认计划是约 10 万中文字符、40 章；未指定的故事内容交给 AI 决定。
3. 在项目规则中确认 `.trae/rules/novel.md` 已被识别；创建或修改规则后开启新聊天。
4. 在 Agent 中输入：

```text
开始写书
```

如果你的版本没有自动加载项目规则，直接发送：

```text
读取 START.md 并执行本仓库的自动小说创作流程。运行 next、完成它返回的任务并继续循环，直到全书验收与导出完成。普通创作选择自行决定，不逐章询问是否继续。
```

也可以跳过手动 `init`，由 Agent 按 `START.md` 执行初始化。

## 如何恢复

Trae 中断后，在同一项目的新聊天中输入：

```text
继续写书
```

Agent 会核对已保存正文与进度，恢复未完成步骤。它不会仅凭聊天记忆重新生成全书。

## 常用命令

以下是工具命令，可以由你或 Trae 执行：

| 命令 | 用途 |
| --- | --- |
| `python novel.py init` | 幂等初始化，保留已有文件 |
| `python novel.py next` | 获取下一项任务、应读材料、输出要求和模板 |
| `python novel.py status` | 查看当前进度 |
| `python novel.py accept-plan` | 校验并登记设定、结局和逐章计划 |
| `python novel.py snapshot --chapter 1` | 获取正文与前文／设定上下文的两个哈希，用于审稿和事实记录 |
| `python novel.py check --chapter 1` | 检查第一章是否满足提交要求 |
| `python novel.py commit --chapter 1` | 校验后提交第一章；随后继续 `next` |
| `python novel.py check` | 检查项目与已提交文件的一致性 |
| `python novel.py snapshot` | 获取全书快照，用于最终审稿 |
| `python novel.py reopen --chapter 1 --reason "修正首章时间线"` | 备份并撤销从第一章开始的提交，保留正文，移除失效的审稿与记忆供重新生成 |
| `python novel.py export` | 在完整验收通过后合稿 |

在仓库外运行时可以指定项目路径，例如：

```bash
python /path/to/novel/novel.py --root /path/to/novel status
```

**单独执行 `next` 不会调用 Trae，也不会在后台写小说。** 自动推进来自 Trae 按 [`START.md`](START.md) 反复执行任务。

## 它怎样减少人工介入

- 先确定结局与篇幅预算，再安排全书章节，避免无限增加支线。
- 普通取名、情节和措辞自行决定，不把章节结束当成人工审批点。
- 每章进行写作、审稿和有限修订；正文保存后更新事实与人物状态。
- 审稿和事实记录同时绑定正文及前文／设定上下文的哈希。正文或依赖的上下文变化后，旧检查结果不能继续冒充有效。
- 完成一个章节后登记进度，下一次从实际文件恢复。
- 全书完成后检查主线、人物弧线和伏笔，再生成 Markdown、纯文本与验收报告。

工具能验证文件、结构、长度和版本一致性，不能证明文笔优秀或剧情绝无漏洞。完整流程见 [`docs/工作流程.md`](docs/工作流程.md)。

## 文件与输出

```text
book.json             机器可读的篇幅与创作配置
创作任务书.md          叙事约束、偏好与人工要求
START.md              Agent 执行流程
.trae/rules/novel.md   项目入口规则
plans/                故事设定、结局、章节大纲
chapters/             分章正文
memory/               从最终正文提取的事实和人物记录
reviews/              章节审稿及最终审稿
state/                提交与恢复状态
revisions/            重新打开章节时的备份
exports/              全书.md、全书.txt、验收报告.json
```

正文、计划、记忆、审稿、进度、备份和导出目录默认被 Git 忽略，避免提交工具代码时顺带上传稿件；如需要版本管理小说，可自行调整 `.gitignore`，并另做备份。

## 运行边界

项目规则不能解除 Trae 的模型额度、单次运行、上下文、网络或工具确认限制。遇到这些限制仍可能需要恢复运行。对本地工具任务，电脑和应用需要保持可用。无需关闭全部权限保护，也不要用自动点击脚本绕过确认。

这套流程默认交付本地完整初稿，不自动充值、更换套餐、上传投稿或公开发布。若希望减少方向性返工，可以先要求它只完成设定和第一章供你校准，再开启全书连续写作。

官方参考：[Trae Rules](https://docs.trae.ai/ide/rules) · [Trae Agent 能力](https://docs.trae.ai/ide/what-is-trae)

## 开发验证

```bash
python -m unittest discover -s tests -v
```
