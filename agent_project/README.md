# 个股研究 Agent v1.1

首次安装与开源说明见[仓库首页](../README.md)。从本目录的 `config.example.json` 复制出本地 `config.json`，配置密钥和目标股票，再使用项目根目录 `.venv` 中的 Python 运行 `run_main.py`。不需要启动网页服务。

```powershell
cd StockAnalysis_Agent_v1.1
Copy-Item .\agent_project\config.example.json .\agent_project\config.json
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py
```

也可以从其他目录用脚本绝对路径启动；默认仍读取脚本旁的 `config.json`。相对输出、日志及缓存路径均以配置文件所在目录为基准。复制配置后，启用 Agent 须先填写 `llm.api_key`；没有密钥可设置 `task.use_llm=false` 使用规则模式。

## 目录

```text
agent_project/
├─ run_main.py           # 唯一常用启动入口
├─ config.json           # 本地配置，含密钥，请勿分享或提交
├─ config.example.json   # 无密钥示例
├─ requirements.txt      # 依赖
├─ requirements.lock.txt # Python 3.13 下已验证的完整依赖版本
├─ src/                  # Agent、数据工具、分析及导出代码
├─ outputs/              # 默认研究成果目录
├─ logs/                 # 每次运行的实时 TXT 日志
└─ tests/                # 离线测试
```

## 配置

`llm.api_key` 填写本地密钥。模型固定为 `deepseek-flash`，接口为 `https://api.deepseek.com`，运行前核验 DeepSeek 4.1 Flash 身份。模型上限 12 次，工具上限 36 次，失败尝试也计入预算。

常用项：

| 配置项 | 含义 |
|---|---|
| `task.stock_code` | 六位 A 股股票代码，例如 `600875` |
| `task.holding_cost` | 持仓成本；未持仓填 `null` |
| `task.use_llm` | `true` 使用自主研究 Agent；`false` 使用规则分析 |
| `task.refresh_data` | `true` 重新获取数据；`false` 可使用缓存 |
| `output.directory` | 成果目录，默认 `./outputs` |
| `logging.directory` | TXT 运行日志目录，默认 `./logs` |
| `chart` | 离线图表显示范围及指标选项 |
| `data` | 数据超时、缓存和股吧采样参数 |

虚拟环境使用 Python 3.13，位于项目根目录 `.venv`。在新机器上，从项目根目录创建环境并安装已验证的锁定依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\agent_project\requirements.lock.txt
```

只检查配置和环境、不联网请求模型：

```powershell
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py --check
```

使用其他配置：

```powershell
.\.venv\Scripts\python.exe -X utf8 .\agent_project\run_main.py --config D:\my_configs\stock.json
```

兼容 `python run_main.py run`、`doctor`、`analyze 600875` 等入口。

## v1.1 本机验证

2026-10-13，在项目根目录 `.venv`（Python 3.13.5，Windows x64）完成环境检查、`pip check` 与 36 项离线测试，全部通过。`requirements.lock.txt` 保存本次验证的 45 项完整依赖版本。

以 `002517`（恺英网络）完成真实 Agent 运行：模型身份核验及综合研判成功，使用 7 次模型请求和 19 次工具调用，生成 `outputs/002517_20261013_1337` 对应的四类成果。图表内嵌绘图库，显示 250 根日线，行情截止 2026-10-12，并已核对与报告日期、收盘价一致。

本次研究状态为 `limited`，运行日志记录退出码 `2`：解禁接口为空、半年报正文只覆盖前 8 页等证据限制仍然保留。四类成果均完整导出，无图表导出警告。这是发布前的本地验证记录，研究产物、缓存与运行日志不随开源源码发布。

## 运行日志与成果

TXT 在正式取证和模型调用前创建，运行过程中持续写入并刷新。默认命名为 `logs/[stock_code]_yyyymmdd_hhmm_log.txt`，使用北京时间。同一分钟重复运行时，自动放入独立子目录，保留文件名并避免覆盖。

TXT 记录开始和结束时间、安全配置摘要、模型调用及预算、模型公开说明的工具选择理由、工具参数和返回摘要、证据缺口、补查动作、来源状态、未决事项、停止原因和成果路径。它记录可审计的研究活动，不记录模型内部隐藏推理。密钥、请求鉴权及原始 SDK 异常不会写入。

失败或用户中止时，已有 TXT 记录会保留。配置无法读取时，用 `logs/unknown_yyyymmdd_hhmm_log.txt` 记录安全错误说明。日志目录不可写时，程序停止，不继续执行付费模型调用。

`outputs` 默认生成以下文件：

- `[stock_code]_yyyymmdd_hhmm_result.md`：公司、技术和风险分析。
- `[stock_code]_yyyymmdd_hhmm_log.md`：最终研究日志、数据来源及证据缺口。
- `[stock_code]_yyyymmdd_hhmm_chart.html`：可双击打开的离线 K 线、均线、成交量、MACD 和支撑压力位图表。
- `[stock_code]_yyyymmdd_hhmm.json`：内部结构化证据，用于复核及重新导出。

TXT 是实时运行记录，`log.md` 是研究结束后的可读汇总，二者用途不同。HTML 内嵌数据和绘图库，打开时不调用模型。没有充分证据的事项会保留为未知，接口空结果不会自动解释为没有风险。

退出码：`0` 为完成，`1` 为配置错误、运行失败或中止，`2` 为有限完成或图表导出不完整；退出码 `2` 时仍保留已有成果。

## 离线测试

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s .\agent_project\tests -v
```
