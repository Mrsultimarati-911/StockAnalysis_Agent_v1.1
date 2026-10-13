# StockAnalysis Agent v1.1

一个在本地运行的 A 股个股研究工具。输入六位股票代码和可选持仓成本，程序采集公开资料、计算技术指标、检查证据缺口，并导出可复核的研究报告、离线图表和运行日志。

项目提供自主研究 Agent 和固定规则分析两种模式。当前版本定位为需要人工复核的个人研究工具，研究结论的准确性、跨股票稳定性和核心分析测试覆盖仍需持续验证。

## 功能

- 采集行情、公司资料、财务、估值、公告、解禁、新闻、股吧等研究资料，主要通过 AKShare 和公开页面获取。
- 由本地代码计算均线、MACD、RSI、ATR、支撑压力等指标，过滤无效及未收盘日线。
- Agent 根据证据缺口选择查询、公告正文阅读和补查动作，再生成综合研判。
- 模型最多调用 12 次，工具最多调用 36 次，失败尝试也占用预算；最后一次模型额度预留给最终报告，默认研究时间预算为 600 秒。
- 保留来源失败、空结果、采样和正文覆盖限制，避免将“未查到”直接写成“没有风险”。
- 导出 Markdown 报告、Markdown 研究日志、独立 HTML 图表、结构化 JSON 证据，并持续保存 TXT 运行日志。

## 快速开始

以下命令在 Windows PowerShell 中执行。v1.1 已在 Windows x64、Python **3.13.5** 下验证；其他系统与 Python 版本尚未完成同等实跑验证。

```powershell
git clone https://github.com/Mrsultimarati-911/StockAnalysis_Agent_v1.1.git
cd StockAnalysis_Agent_v1.1
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\agent_project\requirements.lock.txt
Copy-Item .\agent_project\config.example.json .\agent_project\config.json
```

如果没有 Windows `py` 启动器，可使用指向 Python 3.13 的 `python -m venv .venv`。环境无需激活，后续命令直接使用其中的解释器。

编辑 `agent_project/config.json`，将 `task.stock_code` 设为 `"002517"`。启用 Agent 时，填写自己的 `llm.api_key` 并保留 `task.use_llm=true`；未持仓可将 `task.holding_cost` 保持为 `null`。密钥只保存在本地配置中，请勿提交。

先检查环境及配置，再执行研究：

```powershell
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py --check
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py
```

`--check` 不联网，也不请求模型。示例配置的密钥为空且 `task.use_llm=true`，因此直接检查会返回失败；填写密钥或将 `task.use_llm` 改为 `false` 后再检查。检查只确认本地配置与依赖，不代表密钥或远程服务已经验证。

当前代码将模型硬约束为 `deepseek-flash`，接口限定为 `https://api.deepseek.com`，并在 Agent 运行时核验模型身份；核验不通过会停止模型调用。该设置反映本版本代码约束，不保证该模型对所有账号或后续时间均可用。

没有模型密钥时，可以运行规则模式：

```powershell
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py analyze 002517 --no-llm
```

规则模式不调用语言模型，但采集股票数据仍需要网络。若需要让默认入口和 `--check` 也使用规则模式，请在配置中设置 `task.use_llm=false`。

## 配置与运行

| 配置项 | 含义 |
| --- | --- |
| `llm.api_key` | 本地模型 API 密钥 |
| `task.stock_code` | 六位 A 股代码，例如 `"002517"` |
| `task.holding_cost` | 持仓成本；未持仓为 `null` |
| `task.use_llm` | `true` 为 Agent 模式，`false` 为规则模式 |
| `task.refresh_data` | `true` 重新采集，`false` 允许使用缓存 |
| `output.directory` | 研究成果目录，默认 `./outputs` |
| `logging.directory` | TXT 运行日志目录，默认 `./logs` |
| `chart` | 显示日线数量、均线和指标选项 |
| `data` | 数据超时、缓存与股吧采样参数 |

默认读取启动脚本旁的 `agent_project/config.json`。相对输出、日志、缓存路径以配置文件所在目录为基准。可使用单独的配置文件：

```powershell
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py --config .\agent_project\config.json
```

公告 PDF 正文最多读取前 8 页，股吧最多分析 100 条采样帖子。这些限制会进入证据记录，报告不能据此声称完成全文核查或覆盖全部投资者观点。

## 输出与状态

默认在 `agent_project/outputs/` 中生成四类成果：

| 文件 | 用途 |
| --- | --- |
| `[股票代码]_yyyymmdd_hhmm_result.md` | 公司、技术与风险分析报告 |
| `[股票代码]_yyyymmdd_hhmm_log.md` | 研究活动、数据来源与证据缺口汇总 |
| `[股票代码]_yyyymmdd_hhmm_chart.html` | 内嵌数据和绘图库的离线行情图表 |
| `[股票代码]_yyyymmdd_hhmm.json` | 结构化研究证据，供复核和重新导出 |

TXT 日志位于 `agent_project/logs/`，在正式取证和模型调用前创建，运行中持续写入并刷新，失败或中止时保留已有记录。文件名和运行时间使用北京时间；同一分钟重复运行会使用独立子目录避免覆盖。日志记录可核查的工具活动和公开行动理由，不记录模型内部隐藏推理。

| 退出码 | 含义 |
| --- | --- |
| `0` | 完成 |
| `1` | 配置错误、运行失败或用户中止 |
| `2` | Agent 有限完成、综合研判未完成，或图表导出不完整；已有成果保留 |

`limited` 表示仍存在证据或研究限制，应结合研究日志判断报告可用范围。

## v1.1 验证记录

2026-10-13，在 Python 3.13.5 虚拟环境下完成依赖检查、`pip check` 和 **36 项离线测试**，全部通过。锁定依赖保存在 `agent_project/requirements.lock.txt`。

以 **002517（恺英网络）** 完成真实 Agent 流程，使用 **7 次模型请求、19 次工具调用**，成功生成四类成果及 TXT 日志。本次状态为 `limited`、退出码为 `2`，原因包括解禁接口空结果和部分公告正文覆盖有限；模型研判及文件导出成功。这是一次实跑验证记录，不能代表所有股票与数据来源均已充分验证。

运行离线测试：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s .\agent_project\tests -v
```

当前测试主要覆盖入口、配置、日志和图表兼容行为。核心数据转换、财务规则、技术指标计算和最终报告事实核验仍有补强空间。

## 架构与目录

运行流程：**读取配置 → 取证与补查 → 本地计算 → 综合研判 → 导出成果**。

| 模块 | 职责 |
| --- | --- |
| `run_main.py`、`src/cli.py`、`src/config.py` | 启动入口、参数和配置校验 |
| `src/agent.py`、`src/agent_tools.py` | 自主研究循环、工具执行、预算与停止条件 |
| `src/provider.py`、`src/fetch_worker.py` | 数据调度、缓存和子进程超时隔离 |
| `src/announcements.py`、`src/guba.py` | 公告正文和股吧资料处理 |
| `src/technical.py`、`src/analysis.py` | 技术指标、公司与风险规则分析 |
| `src/llm.py` | 模型身份核验、证据整理、综合报告与股吧分类 |
| `src/pipeline.py`、`src/report.py`、`src/charts.py` | 流程编排、报告与离线图表导出 |
| `src/runlog.py`、`src/models.py` | 运行日志及基础数据模型 |

```text
StockAnalysis_Agent_v1.1/
├─ README.md
├─ LICENSE
├─ CHANGELOG.md
├─ .venv/                         # 本地创建，不提交
└─ agent_project/
   ├─ run_main.py
   ├─ config.example.json         # 无密钥配置示例
   ├─ config.json                 # 本地配置，不提交
   ├─ requirements.txt
   ├─ requirements.lock.txt
   ├─ src/
   ├─ tests/
   ├─ outputs/                    # 运行生成，不提交
   ├─ logs/                       # 运行生成，不提交
   └─ .cache/                     # 运行生成，不提交
```

## 开源范围与许可

本次首次开源发布包含源码、配置示例、依赖清单、测试与文档，不包含密钥、虚拟环境、缓存、采集数据、研究产物或本机日志。

源码采用 [MIT 许可证](LICENSE)。MIT 授权适用于本项目源码，不授予上游行情、公告或其他数据的权利；使用或再分发相关数据时请遵循各来源的条款。研究报告用于辅助个人研究，需结合原始资料人工复核。
