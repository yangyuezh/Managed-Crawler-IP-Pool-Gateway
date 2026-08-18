# Managed Crawler IP Pool Gateway

这是一个独立、可复用的本地爬虫多出口网关，不属于 NSFC 或任何单一数据项目。不同爬虫只需要连接固定 HTTP 代理端口；节点发现、目标网站实测、备用池维护和故障切换都由本项目负责。增加新网站时新增一个 `targets` 配置，不需要复制网关代码。

名称分工只有一套：

- 软件与 GitHub 仓库：`Managed-Crawler-IP-Pool-Gateway`
- Codex 操作 Skill：`manage-crawler-ip-pool-gateway`
- `route-nsfc-through-shadowrocket` 是已被本项目替代的旧 NSFC 专用分流方案，不是本项目的一部分。

订阅节点可以变化，但爬虫始终连接从基础端口开始的固定通道。当前默认是 3 路：

- 工作通道 1：`http://127.0.0.1:17891`
- 工作通道 2：`http://127.0.0.1:17892`
- 工作通道 3：`http://127.0.0.1:17893`

通道数不是写死的。`private/gateway.yaml` 中的 `work_lanes` 决定实际生成多少路，端口从 `work_port_base` 开始连续递增；例如 5 路就是 `17891-17895`。

网关本身不再绑定 NSFC。`targets` 可以配置一个或多个网站，每个网站分别定义请求方法、URL、成功状态和 JSON 身份字段。NSFC 结项详情只是当前第一个目标和爬虫适配器。出口优先级固定为：Shadowrocket 订阅节点、本地手工节点、本机物理直连。Shadowrocket 继续负责 Codex/OpenAI，本项目使用独立的 Mihomo 网关进程，不改 Shadowrocket 的主节点。

```text
订阅节点 / 本地节点 / 物理直连
              ↓
备用库存 → 按目标网站实测 → 目标可用池 → 固定工作端口
                                            ↓
                         NSFC、报告下载器、未来其他爬虫
```

## 主池与备用池

节点流转分为三层，统计口径不会混在一起：

1. **备用库存**：定时从全部 provider 更新得到的当前节点，尚未假定它能访问任何网站。
2. **目标可用备用池**：逐节点对某个真实目标做检测后，最新结果通过且未过期的子集。每个目标各有一份资格结果；一个节点可以对 NSFC 可用、对另一个网站不可用。
3. **主池**：固定工作端口当前租用的节点，只能从对应目标的可用备用池中选择。未分配、节点消失或绑定异常时默认转入 `REJECT`，不会悄悄回落到本机直连。

检测失败的节点不会被物理删除，因为以后可能恢复；它会保留失败事实并退出“可用备用池”，下一轮周期维护时重新检测。周期体检采用增量替换结果，不会在扫描开始时清空整池资格；只有新增节点或稳定配置指纹发生变化的节点会立即变为未检测。这样既能筛掉当前不可用节点，也不会在长时间扫描中制造虚假的“可用池骤降”。订阅更新、目标检测、池状态统计、主池分配分别有独立命令，也可以由 `maintain-reserve` 按顺序组合并定时重复。

## 操作入口

通用网关能力以命令行为准，适用于所有网站：

```bash
python3 -m crawler_gateway --config private/gateway.yaml init
python3 -m crawler_gateway --config private/gateway.yaml start
python3 -m crawler_gateway --human --config private/gateway.yaml maintain-reserve --once
python3 -m crawler_gateway --human --config private/gateway.yaml assign TARGET_NAME --lanes 3
python3 -m crawler_gateway --human --config private/gateway.yaml monitor TARGET_NAME
python3 -m crawler_gateway --config private/gateway.yaml proxy-list TARGET_NAME
```

把输出的固定代理地址交给任意爬虫即可。目标网站和验证规则写在私有配置的 `targets` 中，多个目标彼此独立维护资格。

`commands/` 目前还保留一组 macOS 双击入口，其中 `01`、`02`、`05-09` 是网关维护操作；`03`、`04` 是当前 NSFC 集成的便捷入口，不代表网关只属于 NSFC：

1. `01_查看状态.command`：查看 NSFC 已完成、剩余、百分比、下一断点和网关状态。
2. `02_刷新订阅并体检.command`：执行一次完整备用池维护，先更新全部 provider，再检测 `maintenance_targets` 中的一个或多个目标，最后打印池事实。
3. `03_启动NSFC补全.command`：只有至少 `work_lanes` 个节点通过 NSFC 真实详情验证后才会启动；是否识别出这些节点的公网 IP 不影响启动。已有 NSFC 进程时会拒绝重复启动。
4. `04_停止NSFC和网关.command`：停止备用池定时维护、同一数据目录的 NSFC 爬虫、守护进程和本项目代理通道；不会删除任何数据。
5. `05_仅更新备用池.command`：只更新订阅节点，不做网站检测。
6. `06_仅检测备用池.command`：只检测现有备用库存，不刷新订阅。
7. `07_持续维护备用池.command`：按 `reserve_refresh_interval_seconds` 周期持续执行更新、检测和统计。
8. `08_启用全自动维护.command`：安装并启动 macOS 后台服务。窗口关闭和再次登录后仍会自动维护，不需要再点击 Shadowrocket 的更新按钮。
9. `09_暂停全自动维护.command`：停用后台自动维护，不删除节点、检测结果或正式数据。

在当前 macOS/NSFC 工作流中，日常长期使用推荐只执行一次 `08_启用全自动维护.command`，以后用 `01_查看状态.command` 查看即可。`07` 保留为临时前台调试入口，需要保持窗口打开。

后台服务负责网关、订阅和节点池维护，不会自行启动任何正式爬虫或写入 NSFC 数据目录。正式补全仍由 `03_启动NSFC补全.command` 明确启动，并继续受重复写入保护。

不要同时启动旧桌面爬虫和本项目的 NSFC 入口。重复启动保护会检查同一数据目录，但人为修改数据目录仍可能绕过保护。

## NSFC 进度口径

状态中的“严格连续完成”才是断点进度。详情文件总数有时会多几条，因为以前的并发任务可能已经提前写入后面的记录。启动时始终从数据库排序后的第一个缺失记录继续，不会把这些后段文件误算成连续完成。

## 通道数量与故障隔离

默认 3 路只是当前机器和节点池的保守起点，不是技术上限。个人单机通常建议使用 3-5 路：先以 3 路观察目标成功率和单位时间产量，确认稳定后再逐级增加。默认 `max_work_lanes: 6` 是防止误配置的安全护栏；确有需要时可以主动提高，但吞吐量不会必然线性增长，节点质量、目标站容量和本机网络往往先成为瓶颈。修改 `work_lanes` 后需要重启网关，Mihomo 才会生成对应数量的监听端口。

每一路都有独立的固定端口、选择器、健康计数和爬虫 worker。健康检查并行进行；某一路超时、TLS 失败或目标验证失败，只增加该路的失败计数。达到阈值后只切换该路，另外的健康路不重选、不重启。若没有可用替代节点，故障路保留为 `degraded` 并在以后重试，健康路继续工作。

只有两路以上来自至少两个已识别的不同公网出口、并同时返回完全相同的明确目标异常时，监控才会把它视为疑似目标站整体故障，短期内不批量轮换节点。若共同异常连续达到失败阈值，只切换一条通道作为探路通道；其余通道保持不动。相同或未知出口的共同 404 不再被直接判为全站故障，避免把同一 IP 受限误认为官网停机，也不会因一次误判永久停止替换。

所有通道仍共享一个 Mihomo 内核进程；单个节点或通道故障已经隔离。后台维护会在每轮开始时检查内核进程和控制接口，Mihomo 异常退出后自动使用本地缓存重建并启动；失败轮按 `maintenance_error_retry_seconds` 短周期重试。

## 多目标与节点判断

每个目标网站都会为每个节点分别保存两级结果，两者互不替代：

1. **目标可用（准入条件）**：响应状态、JSON 内容和目标配置中的身份字段全部通过。满足这一项才进入该目标的可用备用池。
2. **出口 IP 可识别（辅助信息）**：程序尽量取得真实公网出口 IP，用于统计并优先把并行通道分散到不同的已知 IP。它不是淘汰条件；识别失败或与其他节点 IP 相同，都不影响节点进入候选池和继续运行。

节点连通不等于目标可用，Shadowrocket 的延迟测试显示 `Timeout` 也不等于目标不可用。程序验证真实目标响应并辅助查询出口 IP；传输超时或临时 5xx 按配置重试，404/406 等明确响应不重试。多个出口 IP 未知或相同的节点仍可分别成为工作通道，但状态只报告实际已知的独立 IP 数，不会把未知节点虚报成不同公网出口。

两个 `shadowrocket` provider 使用同一个 `ServerManager`：订阅组首次读取隐藏的订阅地址后，会把地址清单以 `600` 权限安全保存在 Git 忽略的 `runtime/` 中，后台以后直接从这些地址下载远端最新内容；本地组在后台使用已经转换好的手工节点缓存。程序不会点击 Shadowrocket、不会切换其主节点，也不会把刷新结果写回 Shadowrocket 界面。

每个远端订阅都有独立的本地缓存。某一个订阅临时失败时，只回退该来源的上一版；其他订阅继续更新。状态会分别报告全部成功、部分更新或缓存回退。调度器始终先遍历订阅组，耗尽后才遍历本地节点，最后才考虑本机物理直连。普通节点增删会随远端订阅全自动更新；只有在 Shadowrocket 中新增、删除或更换“整个订阅地址”后，才需要执行一次 `05_仅更新备用池.command`，由前台重新读取 `ServerManager` 并更新安全地址清单。

节点的原始 VLESS/TLS/WebSocket 参数由 Mihomo 读取；实际出口检测才是节点是否可用的判断依据。Xray 兼容层仍保留在代码中，但当前 Coffer 节点已实测在 Mihomo 下工作更稳定，因此默认使用 Mihomo。

Mihomo 网关对节点服务器域名和目标域名都使用 `direct_dns_servers` 独立解析，避免 Shadowrocket/TUN 的 `198.18.*` Fake-IP 污染。它同时使用 `direct_outbound_interface` 约束本地直连候选；macOS 的 Network Extension 可能在 VPN 开启时阻止物理网卡直接建立 HTTPS。本机直连无论是否可用都只作为最后兜底。

订阅节点使用独立的 `node_outbound_interface`，默认绑定物理网卡 `en0`。这样建立到节点服务器的外层连接不会再次钻进 Shadowrocket 的 TUN，避免代理套代理；爬虫最终访问目标网站的出口仍是选中的节点。程序不会修改或切换 Shadowrocket 的主节点。

自动替换以通道绑定的真实目标验证为准。代理连接、TLS、HTTP 状态、JSON 内容或关键字段任一不合格，都会只影响所在通道；单独的公网 IP 查询失败不会触发替换。只有所有通道同时出现相同的明确目标异常时，程序才暂缓本轮批量切换，以避免在目标站整体维护时无意义地耗尽节点。

增加多个目标的配置形式如下。`maintenance_targets` 是默认维护范围，命令行也可以临时指定其中一个或多个名称：

```yaml
gateway:
  reserve_refresh_interval_seconds: 3600
  maintenance_error_retry_seconds: 60
  probe_history_retention_days: 90
  maintenance_targets:
    - nsfc_final_detail
    - another_site

targets:
  nsfc_final_detail:
    method: POST
    url: https://kd.nsfc.cn/api/baseQuery/conclusionProjectInfo/有效记录ID
    expected_statuses: [200]
    json_checks:
      - path: data.ratifyNo
        present: true
  another_site:
    method: GET
    url: https://example.org/api/known-record
    expected_statuses: [200]
    json_checks:
      - path: data.id
        present: true
```

结题报告下载使用独立的 `nsfc_final_report` 目标，不能直接沿用详情接口的历史健康状态。把配置示例中的记录 ID 换成一个确定有公开报告的项目后，依次执行：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml probe-reserve nsfc_final_report
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml assign nsfc_final_report --lanes 3 --replace
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml monitor nsfc_final_report
```

最后一个命令在下载期间持续检测各通道；某一路失败时只替换该路。报告下载器继续连接固定的 `17891-17896`，不需要知道后端节点名称。

## 运行监督

启动、备用池更新、目标检测、持续维护和 NSFC 启动都会先打印完整的非敏感运行参数，包括端口、并发、超时、重试、刷新周期、provider 名称、目标名称、目标 URL 和验证字段。订阅 URL、provider 文件路径、请求头值和请求体值不会打印。每轮结束会打印备用库存、通过、失败、未检测、过期、主池健康和 provider 最近刷新状态；这些都是 SQLite 中的事实结果，不是估算。

后台服务的结构化运行记录写入 `logs/reserve-maintenance.jsonl`，单文件 10 MB 后自动轮转并保留 5 份备份；启动级错误写入 `logs/reserve-maintenance.err.log`。SQLite 探测明细默认保留 90 天，当前节点资格快照不受清理影响；Mihomo 只记录 warning 以上的底层日志。后台服务定义位于 `~/Library/LaunchAgents/com.yangyuezh.crawler-gateway.reserve-maintenance.plist`，其中只有程序路径和启动参数，不包含订阅 URL、UUID 或节点配置。

## 命令行口径

在 Terminal 中进入本项目：

```bash
cd /path/to/Managed-Crawler-IP-Pool-Gateway
```

查看状态：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml status --plain
```

四项功能可以独立运行。先启动网关：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml start
```

只更新备用库存：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml refresh-reserve
```

只检测默认目标，或者在命令末尾列出多个目标名称：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml probe-reserve
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml probe-reserve nsfc_final_detail another_site
```

查看全部参数和主池/备用池事实：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml pool-status --plain
```

立即维护一轮，或按配置周期持续维护：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml maintain-reserve --once
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml maintain-reserve
```

安装、查看或停用无人值守后台服务：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml install-service
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml service-status
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml disable-service
```

这类订阅节点可能共用账号或后端。若并发体检不稳定，可以给 `probe-reserve` 或 `maintain-reserve` 加 `--concurrency 1` 顺序检测；这只影响备用池检测速度，不改变正式爬虫的工作通道数。

按配置中的 `work_lanes` 启动 NSFC 补全：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --human --config private/gateway.yaml run-nsfc
```

临时使用少于配置容量的通道时可以加 `--lanes 2`。要从 3 路扩到 4 或 5 路，应先修改 `work_lanes`，再重启网关。

停止网关：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml stop-maintenance
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml stop
```

先停止同一数据目录的 NSFC 进程：

```bash
/opt/anaconda3/bin/python3 -m crawler_gateway --config private/gateway.yaml stop-nsfc
```

## 文件管理

- `crawler_gateway/`：程序代码。
- `config/gateway.example.yaml`：不含秘密的配置示例。
- `private/gateway.yaml`：本机订阅地址和目标探针，仅保存在本机，权限为 `600`。
- `runtime/`：运行状态、节点清单和 Xray 临时配置，不进入 Git。
- `logs/`：网关日志，不进入 Git。
- `commands/`：可双击的操作入口。

NSFC 是一个可选集成。设置 `NSFC_REPO` 和 `NSFC_DATA_DIR` 指定其爬虫代码与数据目录；如果 NSFC 仓库与本仓库同级并采用常用仓库名，程序也会自动识别。本项目只读 NSFC 基础 SQLite，并由既有爬虫继续向其 `details/` 写详情，不复制或搬动正式数据。

## 安装与测试

首次安装需要 Python 3.10+、Mihomo、PyYAML 和 requests。macOS 上可执行：

```bash
brew install mihomo
/opt/anaconda3/bin/python3 -m pip install -e '.[test]'
/opt/anaconda3/bin/python3 -m pytest -q
```

真实订阅 URL、UUID、控制密钥和运行态文件都被 `.gitignore` 排除。提交或同步代码前仍应运行 `git status` 检查。
