# 备份吞吐与 35 云邮限速应对：实现规格

Status: ready for implementation

Applies to: `imapbackup312.py`、`imapbackup_gui.py`、`tests/`、`packaging/`、`README.md`

Related: [GLOSSARY.md](../../GLOSSARY.md)、[ADR 0003](../adr/0003-structured-message-callback-and-cooperative-stop.md)、[ADR 0004](../adr/0004-per-format-incremental-dedupe.md)、[ADR 0011](../adr/0011-connection-merged-into-backup-flow.md)、[ADR 0012](../adr/0012-independent-account-runs-and-stop-all.md)、[ADR 0013](../adr/0013-fixed-batching-no-adaptive-throttle.md)

## 0. 这份规格怎么用

本文件是备份吞吐 effort 的实现契约，汇总该 effort 的 16 个决策工单与 1 个汇总任务。它只描述行为、接口与验收，不包含生产代码；实现由后续 `/implement` 会话按第 13 节的工单顺序完成。

- 规格与已接受的 ADR 冲突时，先停下来指出冲突，不自行改口径。
- 规格未覆盖的新决策，按 wayfinder 的方式新开决策工单，不在实现里顺手发明。
- 术语一律使用 `GLOSSARY.md` 的规范词；本文只补充实现名，不另造领域词。

## 1. 问题与目标

### 1.1 问题

1. **首次全量慢**：典型场景是 6–7 个账户并发，单账户数千到数万封，以 eml 为主，也有双格式，备份目录在本机盘。当前核心逐封 `FETCH`，一封一个往返，且 GUI 每封都插列表行。
2. **服务端静默降速**：35 云邮在备份到约 50 封后开始变慢。服务商没有公开限速条款，客户端无法从协议层判断限速维度（消息数、字节数还是时间窗）。
3. **中断恢复的本地成本**：用户经常停止后继续；本地 mbox 越大，重扫越慢，恢复体验越差。
4. **缺少可见性**：GUI 第二行没有实时速率/预计剩余；取信失败与文件夹失败没有稳定的「部分完成」状态。
5. **滚动条被抢**：列表每插入一封都调用 `see`，用户上移查看旧邮件时会被强制拉回底部。

### 1.2 目标

| 编号 | 目标 | 判据 |
|---|---|---|
| G1 | 提高首次全量吞吐 | 正文按 8 封 / 8 MiB 批量取信；每账户同一时刻至多一个请求在途；批间不休眠、不排队 |
| G2 | 服务端降速时不失控 | 不做客户端自适应限速；用固定批量 + 现场诊断判断是否需要后续方案 |
| G3 | 降低中断恢复的本地扫描成本 | 本地扫描索引命中时不再逐封读 mbox/eml；索引失败不影响备份 |
| G4 | 让速度与失败可见 | 第二行显示本账户速率、预计剩余、合计速率；取信失败/文件夹失败判「部分完成」；10 秒 JSONL 诊断 |
| G5 | 修正列表滚动 | 仅当视图在底部时跟随新行；用户上移后不被抢回 |
| G6 | 不破坏既有契约 | CLI 参数/输出/退出码、只读 IMAP、按格式判重、账户目录命名、无新运行时依赖、v1 无新设置 |

### 1.3 非目标

- 通过多连接、重连重置、代理换 IP 绕过服务端限速。
- POP3 通道；远端 UID 游标；`COMPRESS=DEFLATE`。
- 新增运行时依赖、CLI 参数、退出码变化、GUI 架构重写。
- v1 新增用户可调设置项（包括「最大并发账户数」「速率档位」）。
- 由 agent 自动连接真实 IMAP 服务器做探测。
- 服务商可见范围/下载权限的自动检测、运行提示或阻断。
- 诊断数据的自动上传或一键打包导出。
- 修复无 Message-Id 邮件的合成 ID 碰撞。

### 1.4 成功标准

1. 第 12.2 节的假服务器断言组全部通过。
2. 现场验收（第 12.3 节）满足冷/停止续跑/热三组数据与吞吐阈值；服务端持续降速按「未解决但不回滚」处理。
3. 正确性硬条件全部满足：不丢信、不重复写、停止立即生效、索引不造成错误跳过、重连不重复/不丢、CLI 无回归、卸载器不碰备份目录。

## 2. 术语与不变量

### 2.1 术语

| 规范词 | 本文中的实现名 | 含义 |
|---|---|---|
| 账户 | `account` / `account_id` | 设置页里的一条邮箱配置；目录名只取邮箱地址 |
| 账户名称 | `account_display()` | 纯显示名，不参与任何路径或判重 |
| 备份运行 | run | 从活动账户集合空→非空开始，到集合排空结束 |
| 邮箱文件夹 | folder | IMAP 文件夹；显示名用于 UI 与日志 |
| 文件夹显示名 | `display_name` | 解码后的文件夹名，如 `INBOX/归档` |
| 待备份消息 | pending | 远端有、但至少一个已选输出格式缺失的消息 |
| 跳过消息 | skipped | 本地已存在、本轮不下载的消息；与取信失败严格区分 |
| 取信失败消息 | `fetch_failed` | 待备份但正文没取到；绝不能计入已备份 |
| 文件夹失败 | `folder_failures` | 未能完整处理的文件夹数；定义见 5.3 |
| 部分完成 | partial | 正常结束但 `fetch_failed > 0` 或 `folder_failures > 0` |
| 本地扫描索引 | `ScanIndex` | 应用数据目录里的 SQLite 缓存；可重建，非判重事实来源 |
| 诊断采样 | `diag-*.jsonl` | GUI 每 10 秒写的聚合数字，不含邮件内容 |
| 断线重连 | `run_with_reconnect` | 仅连接级错误的有界重试 |
| 冷运行 / 热运行 | cold / warm | 索引无可用快照 / 索引命中且远端无变化 |

### 2.2 不变量

| 编号 | 不变量 |
|---|---|
| I1 | IMAP 只读：`SELECT ... (readonly=True)` 与 `BODY.PEEK[]`；不得 `STORE`、`EXPUNGE`、`COPY` 或改变已读状态 |
| I2 | 增量判重按格式独立：任一已选格式缺失即为待备份；每封正文只 fetch 一次，只补写缺失格式（ADR 0004） |
| I3 | 账户目录名始终取 `sanitize_segment(user)`；账户名称只用于显示；任何索引键、输出路径、判重都不得使用账户名称 |
| I4 | CLI 参数、退出码、正常路径 stdout、`quiet/console` 行为保持不变；`main(argv)` 返回整数 |
| I5 | 运行时代码只用 Python 3.12 标准库；不新增依赖或包管理元数据 |
| I6 | v1 不新增设置项；固定批量、索引、诊断、FAQ 全部自动生效 |
| I7 | 索引与诊断是尽力而为：打开/写入/删除失败只记 WARNING，备份继续，且不改变服务器状态 |
| I8 | 每账户一个连接、同一时刻至多一个请求在途；账户之间不共享限速状态、锁或队列 |
| I9 | `backed`、`skipped`、`fetch_failed`、`pending` 互不混用；`fetch_failed` 不计入 `backed` |
| I10 | `UUID` 盐值、`MSGID_RE`、合成 Message-Id 契约不变；本 effort 只记录碰撞限制，不修复 |
| I11 | 停止优先于重连、下载下一批和继续扫描；停止后不再发起新请求 |

## 3. 35 云邮：事实、假设与边界

### 3.1 已确认事实

- IMAP 默认只暴露最近 30 天；用户已在网页端把收取范围改为「全部邮件」（2026-10-22）。
- 没有公开的限速数值或条款。
- `UID FETCH` 可用；`UID SEARCH ALL` + 分页头部 FETCH 是可行路径。
- POP3 只有收件箱，丢失文件夹结构，不适合本产品。
- 用户观察到备份到约 50 封后「单纯变慢」，邮件增长速度下降，没有报错。

### 3.2 假设

- 降速来自服务端的请求数/字节数/时间窗限制，可能按账户、按 IP 或两者组合；客户端无法从响应里区分。
- 客户端不尝试探测阈值；固定批量 + 每账户单请求在途是 v1 的全部客户端策略。
- 降速不是数据损坏：已写入的邮件保持有效，下次运行按格式判重继续。

### 3.3 不能自动检测的边界

- 无法判断「慢」是服务端限速、网络拥塞、邮箱服务端同步延迟，还是本地磁盘慢；只能靠诊断采样的时间序列与现场对照。
- 无法知道服务商是否对特定文件夹、特定日期范围或附件大小额外限制。
- 无法在不登录真实服务器的情况下验证 35 的实际阈值；agent 不做真实 IMAP 探测。
- 不做运行前检查、提示或阻断；只在菜单栏提供静态「常见问题」（第 9 节）。

### 3.4 对实现的推论

- 不设固定「50 封阈值」，也不在 50 封后休眠。
- 不做客户端 AIMD/令牌桶；仿真已证明常规模型下更慢，只在惩罚型限速下有保险价值（ADR 0013）。
- 把「是否持续降速」交给现场诊断与验收规则判断；若确认是服务端限速，v1 标记「未解决」，不假装客户端已解决。

## 4. 核心流程

### 4.1 总览

```text
connect + login + LIST
  │
  ├─ 逐文件夹扫描（CLI 与 GUI pass 1）
  │    UID SEARCH ALL
  │      → 每 500 UID 一次头部 UID FETCH
  │      → 缺 Message-Id 的再取 FROM/TO/CC/DATE/SUBJECT 合成
  │      → FolderScan(messages: Message-Id → (UID, size), uidvalidity)
  │    → 本地快照（索引命中或内容扫描）
  │    → pending_messages(remote, local_mbox, local_eml)
  │
  ├─ 下载（CLI 逐文件夹；GUI pass 2 逐文件夹）
  │    SELECT readonly → 校验 UIDVALIDITY
  │      → 按 UID 升序切批（≤8 封、≤8 MiB）
  │      → UID FETCH (UID INTERNALDATE BODY.PEEK[])
  │      → 整批解析完成后逐封写缺失格式
  │      → report / written / 索引登记 / on_batch
  │      → 失败隔离、重连、停止
  │
  └─ 账户结束：done / partial / stopped / failed
```

### 4.2 CLI 与 GUI 的差异

| 项目 | CLI | GUI |
|---|---|---|
| 账户数 | 单账户 | 多账户并发（ADR 0012） |
| 扫描/下载顺序 | 逐文件夹「扫描→下载」 | pass 1 扫完全部文件夹，pass 2 再下载 |
| 进度输出 | 现有 stdout/spinner；不新增行 | 事件队列 + 第二行 + 列表 |
| 索引 | 共用同一 `ScanIndex` | 共用同一 `ScanIndex` |
| 诊断 | 不写 JSONL | 每 10 秒写 JSONL |
| 失败 | 记录并继续文件夹；退出码不变 | 部分完成/失败图标 + 状态行 |

CLI 的「扫描→下载」在同一 SELECT 上完成，不需要二次比较 UIDVALIDITY；重连后的比较由 5.7 节负责。GUI pass 2 必须重新 SELECT 并比较。

### 4.3 核心数据结构

```python
@dataclass
class FolderScan:
    messages: dict[str, tuple[int, int]]  # Message-Id -> (UID, RFC822.SIZE)
    uidvalidity: int

@dataclass
class DownloadProgress:
    backed: int = 0
    fetch_failed: int = 0
    fetch_failed_missing: int = 0
    fetch_failed_no_bad: int = 0
    fetch_failed_malformed: int = 0
    done_bytes: int = 0                  # 已备份消息的 RFC822.SIZE 之和
    batches: int = 0
    last_batch: "BatchStats | None" = None
    stopped: bool = False

@dataclass(frozen=True)
class BatchStats:
    msgs: int
    bytes: int
    fetch_ms: float
    backed: int
    fetch_failed: int

@dataclass
class FolderPlan:
    name: tuple[str, str, str, str]       # foldername, filename, eml_relpath, display
    scan: FolderScan
    local_mbox: dict[str, str] | None
    local_eml: dict[str, str] | None
    pending: dict[str, tuple[int, int]]
```

`FolderScan.messages` 的键是消息标识（含合成 ID），值是新元组 `(UID, RFC822.SIZE)`。`size` 缺失或为 0 表示未知，切批时独占一批，字节口径按 0 计。

## 5. 核心设计

### 5.1 扫描：UID SEARCH ALL + 500 封/页头部

#### 5.1.1 常量

```python
SCAN_PAGE_UIDS = 500
```

#### 5.1.2 步骤

1. `server.select(f'"{foldername}"', readonly=True)`；`typ != "OK"` → `SkipFolderException`。
2. 从 `server.untagged_responses` 读取 `UIDVALIDITY`；缺失或畸形 → `SkipFolderException`，并且不运行 SEARCH。RFC 3501 要求 SELECT 响应必带 UIDVALIDITY；缺失说明选中态不可信——QQ「其他文件夹」这类分组容器接受 SELECT 却不切换邮箱，SEARCH 会返回上一个邮箱的 UID。
3. `typ, data = server.uid("SEARCH", "ALL")`；`typ != "OK"` → `SkipFolderException`。空结果返回 `FolderScan({}, uidvalidity)`。
4. 把 UID 列表按 500 切片，逐页请求：

   ```python
   server.uid("FETCH", "uid1,...,uid500",
              "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
   ```

5. 解析扁平响应：每个二元组 `(metadata, literal)` 是一条消息；忽略 `b")"`；从 `metadata` 用 `re.search(rb"\bUID (\d+)\b", meta)` 取 UID，**不得用响应开头的序号**。
6. 从 `metadata` 用 `re.search(rb"\bRFC822\.SIZE (\d+)\b", meta)` 取大小；缺失或非数字按 0 处理。从 `literal` 取 `Message-Id`；用现有 `MSGID_RE` 与解码逻辑。首尾空白、大小写按现有契约处理。
7. 缺 Message-Id 的 UID 收集起来，按 500 UID 一页再请求：

   ```python
   server.uid("FETCH", "uid1,...,uid500",
              "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])")
   ```

   用现有 `UUID + sha1` 合成消息标识，保持与存量备份兼容。
8. 同一 Message-Id 出现多次时，沿用现有 dict 覆盖语义（后出现者生效），记 WARNING；不重复下载。
9. 返回 `FolderScan(messages, uidvalidity)`。所有调用方从 `scan.messages` 取映射，不把 `FolderScan` 直接传给 `pending_messages`（后者保持泛型 dict 语义）。

#### 5.1.3 扫描失败

| 情况 | 处理 |
|---|---|
| SELECT NO/BAD | `SkipFolderException` |
| SELECT OK 但缺失/畸形 UIDVALIDITY | `SkipFolderException`；不运行 `UID SEARCH ALL`（选中态不可信） |
| `UID SEARCH ALL` NO/BAD | 单次重试；仍失败 → `SkipFolderException` |
| 某页头部 FETCH NO/BAD | 该页单次重试；仍失败 → `SkipFolderException`（整文件夹失败） |
| 某条响应畸形/缺 UID/缺字面量 | 该 UID 单封重试一次；仍畸形 → `SkipFolderException`（整文件夹失败，避免静默漏信） |
| 连接 abort/OSError/BYE | 不内部重试，交给 5.7 重连；重连后重跑整个文件夹扫描 |
| 认证/NO | 不重试，按现有失败路径向上抛 |

扫描是「全有或全无」的文件夹快照：只要有一页/一条无法可靠解析，就不使用半截结果，避免把未知消息误判为已跳过。

### 5.2 批量正文取信

#### 5.2.1 常量

```python
FETCH_BATCH_MAX_MESSAGES = 8
FETCH_BATCH_MAX_BYTES = 8 * 1024 * 1024
```

#### 5.2.2 切批算法

输入 `messages: dict[str, tuple[int, int]]`（Message-Id → `(UID, size)`）。

1. 按 UID 升序排序；Message-Id 只作为键，不参与排序。
2. 依次放入当前批：
   - 当前批为空时，无条件放入；
   - `size <= 0` 或 `size > FETCH_BATCH_MAX_BYTES` → 先结束当前批，该封独占一批；
   - 否则若 `len(batch) == 8` 或 `sum(bytes) + size > 8 MiB` → 先结束当前批，再放入下一批。
3. 批间不休眠、不拿令牌、不做 AIMD；上一批写完立即发下一批。
4. 单封超过 8 MiB 时整封读取，不使用 `partial()`（Python 3.12.13 的 `imaplib.partial()` 不可用）。

#### 5.2.3 请求与解析

```python
typ, data = server.uid("FETCH", "101,102,...",
                       "(UID INTERNALDATE BODY.PEEK[])")
```

- 响应是扁平列表：带字面量的项是 `(metadata, literal)`，收尾是独立的 `b")"`。
- 只遍历二元组；从 `metadata` 取 UID；解析为 `uid -> (metadata, literal)`。
- 按**请求的 UID 顺序**处理，不依赖服务器返回顺序。
- 重复 UID 取第一份并记 WARNING。
- 缺 UID、缺字面量、UID 不在请求集合中 → 畸形处理。
- 整批解析完成后才开始写盘；解析阶段出错不会写半批。

#### 5.2.4 写盘与报告

逐封按请求 UID 顺序：

1. 用 Message-Id 查 `skip_mbox` / `skip_eml`；只写仍然缺的格式。
2. mbox 写入沿用现有 `From nobody`、Thunderbird `From ` 转义、合成 Message-Id 注入规则。
3. eml 写入沿用现有 `unique_eml_path`、月度分组、合成 Message-Id 注入规则。
4. 一封的所有已选格式写完后，才把该 Message-Id 加入 `written`，并递增 `backed`。
5. `report` 只在该封至少写入一个 sink 后触发一次；字段不变：

   ```python
   {"folder", "index", "total", "timestamp", "from", "subject", "size", "msg_id"}
   ```

   - `index` 是该封在本次 `messages` 列表中的位置；取信失败会造成跳号。
   - `total = len(messages)`，跨批不变。
   - `size` 仍是**实际写入大小**（与现有 GUI 一致），不是 `RFC822.SIZE`。
6. `DownloadProgress.done_bytes` 按该封的 `RFC822.SIZE` 累加；`size=0` 的消息按 0 计。速度/ETA 用这个口径，不用写盘大小。
7. 索引登记见 5.6；索引失败不影响写盘与 `report`。

#### 5.2.5 停止

- 每批开始前检查 `should_stop()`；每封写完后再次检查。
- 停止时丢弃本批尚未写的数据，返回；已写消息保留，`written` 保留。
- 在途 FETCH 无法中断：等响应到达后丢弃，不写盘。
- 停止不把剩余 pending 计为 `fetch_failed`。

#### 5.2.6 接口

在现有 `download_messages` 位置参数之后追加可选关键字参数；现有调用点可以只忽略返回值：

```python
def download_messages(
    server, filename, messages: dict[str, tuple[int, int]],
    overwrite, nospinner, thunderbird, basedir, icloud, quiet, log,
    eml_dir=None, skip_mbox=None, skip_eml=None, foldername="",
    verbose=0, compact=False, report=None, should_stop=None,
    *,
    written: set[str] | None = None,
    progress: DownloadProgress | None = None,
    index_writer: "IndexWriter | None" = None,
    on_batch: Callable[[BatchStats], None] | None = None,
) -> DownloadProgress:
    ...
```

- `written`：调用方为每个文件夹维护的已写完集合；重连续跑用，不持久化。
- `progress`：调用方传入的可变累计器；重连后继续累加，不因异常丢失已完成的计数。
- `index_writer`：索引写入器；`None` 表示索引禁用。
- `on_batch`：每批完成后回调，用于诊断采样与 UI 批次统计。
- 返回传入的 `progress`；未传入时内部新建并返回。
- 现有 `download_messages` 的 `messages` 值从 `int` 改为 `(UID, size)`；所有调用点与测试同步迁移。

### 5.3 失败语义

#### 5.3.1 正文取信失败矩阵

| 情况 | 处理 |
|---|---|
| 批次 tagged NO/BAD | 丢弃该批已读数据，不写盘；整批重试一次；仍失败 → 逐 UID 单封 FETCH 隔离 |
| 隔离时某封 NO/BAD | `fetch_failed(no_bad)`，继续 |
| 隔离后整批每封都 NO/BAD | 抛 `FolderFetchError`，算一个文件夹失败；继续下一个文件夹 |
| OK 但缺少某个 UID 的响应 | 对缺失 UID 单封 FETCH 重试一次；仍缺失 → `fetch_failed(missing)` |
| 元数据畸形/缺 UID/缺字面量 | 能定位 UID 就单封重试一次；仍畸形 → `fetch_failed(malformed)` |
| 重复 UID | 取第一份，记 WARNING，不算失败 |
| 整批每封都是「缺失 UID」 | 全部记 `fetch_failed(missing)`；**不是**文件夹失败 |
| 隔离结果混合 missing/malformed/NO-BAD | 各按其桶计数；只要存在 missing 或 malformed，就不升级为 `FolderFetchError`，文件夹继续 |
| 连接 abort/OSError/BYE | 不在 `download_messages` 内重试；本批不写半批；交给 5.7 重连 |
| 认证/SELECT NO | 不重试；SELECT NO 算文件夹失败，认证失败按账户失败 |
| 本地写盘错误 | `BackupWriteError`；不算已备份；不触发重连；向上抛 |

`FolderFetchError` 是文件夹级异常，不是账户级异常。CLI 捕获后记录并继续下一个文件夹，退出码不变；GUI 计入 `folder_failures` 并继续。

#### 5.3.2 文件夹失败计数

`folder_failures` 只统计以下四类，每个文件夹最多计一次：

1. 扫描阶段 `SkipFolderException`（SELECT/SEARCH/头部 FETCH 失败或畸形）。
2. 下载阶段 SELECT 失败。
3. `FolderFetchError`（整批逐封都 NO/BAD）。
4. 同一次文件夹运行中第二次需要因 UIDVALIDITY 重扫（见 5.7.6）。

以下不计入：

- 停止导致未处理的文件夹。
- 没有 pending 的文件夹。
- 只有部分 `fetch_failed`、但文件夹整体继续完成的。
- 服务端返回空文件夹或文件夹没有邮件。

#### 5.3.3 账户结果

- `stopped=True`：结果 `stopped`，灰方块；即使同时有取信/文件夹失败也按停止优先。
- 未停止且 `fetch_failed > 0` 或 `folder_failures > 0`：结果 `partial`，黄色警告图标。
- 未停止且两者都为 0：`done`，绿勾。
- 账户级异常（认证、无法连接、未捕获异常）：`failed`，红叉。
- `partial` 优先于「已是最新」：即使 `pending_total == 0`，只要 `fetch_failed > 0` 或 `folder_failures > 0`，也不得显示绿勾或「已是最新」。

### 5.4 UIDVALIDITY

- `FolderScan.uidvalidity` 来自 SELECT 后的 `untagged_responses["UIDVALIDITY"]`，始终是整数：缺失或畸形时 `scan_folder` 抛 `SkipFolderException`，该文件夹按失败跳过，不跑 SEARCH（理由见 5.1.2 第 2 步）。
- `get_names` 不把 LIST 里带 `\Noselect` 的邮箱列为备份目标；其子文件夹照常列出。分组容器本身没有邮件，SELECT 也无法可靠切换。
- GUI pass 2 在**同一连接**上重新 SELECT：UIDVALIDITY 相同 → 继续；不同 → 重扫一次。两侧都不再出现 `None`。
- 重连后的**新连接**：SELECT 后重新取得 UIDVALIDITY，与旧值比较；不同 → 按 5.7.6 重扫一次，不能盲目复用旧 UID。新连接取不到可用的 UIDVALIDITY 时，按 `scan_folder` 的规则整文件夹失败。
- GUI pass 2 发现不同后：丢弃旧 UID，重跑该文件夹 `scan_folder`，复用本地快照，从新 pending 中减去 `written`；这次重扫记一次 `uidvalidity_changes`。
- 同一次文件夹运行中第二次需要因 UIDVALIDITY 重扫：`FolderFetchError`，文件夹失败，不再无限重扫（用 `state.uidvalidity_checked` 记录）。
- CLI 在同一 SELECT 上扫描后立即下载，不需要二次比较；重连后的比较由 5.7 负责。
- UIDVALIDITY 变化**不重扫本地输出**；只重扫远端。

### 5.5 待备份计算

保持现有 `pending_messages(remote, local_mbox, local_eml)` 语义：

- 只选 mbox：本地 mbox 缺失即 pending。
- 只选 eml：本地 eml 缺失即 pending。
- 双格式：任一格式缺失即 pending；下载时只写缺失格式。
- 本地快照由索引或内容扫描提供，返回 `Message-Id -> relpath` 映射。

`pending_bytes = Σ size`（`size` 缺失按 0）。`skipped = server_total - pending_total` 含义不变。

### 5.6 本地扫描索引

#### 5.6.1 位置与打开

```python
INDEX_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppDataLocal") \
            / "imapbackup-gui" / "index"
DB_NAME = "scan-index.sqlite3"
```

- `INDEX_DIR` 是模块属性，测试可重定向。
- DB 路径 `INDEX_DIR / DB_NAME`。
- 懒打开：第一次需要本地快照时打开；不需要索引的运行（如 `--overwrite` 且无输出）不创建文件。
- 每线程一个连接；GUI 每账户线程各自打开，CLI 单线程打开。
- 打开失败、目录不可写、DB 损坏、锁超时 → 本次运行禁用索引，记 WARNING，退回内容扫描；备份继续。
- 索引是缓存，不是判重事实来源；删除 DB 只会让下次运行变慢。

#### 5.6.2 表结构

```sql
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS sink (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('mbox','eml')),
    root TEXT NOT NULL,
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    relpath TEXT NOT NULL,
    size INTEGER,
    mtime_ns INTEGER,
    UNIQUE (kind, root, account, folder)
);

CREATE TABLE IF NOT EXISTS message (
    sink_id INTEGER NOT NULL REFERENCES sink(id) ON DELETE CASCADE,
    msg_id TEXT NOT NULL,
    PRIMARY KEY (sink_id, msg_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS eml_file (
    sink_id INTEGER NOT NULL REFERENCES sink(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    msg_id TEXT NOT NULL,
    PRIMARY KEY (sink_id, relpath)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS eml_file_msg ON eml_file (sink_id, msg_id);
```

连接 PRAGMA：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
```

- `sink.root` 存规范化绝对路径（`os.path.normcase(os.path.abspath(root))`）。
- `sink.account` 存 `sanitize_segment(user)` 的结果，不存账户名称。
- `sink.folder` 存文件夹显示名；`sink.relpath` 对 mbox 是相对 root 的文件路径，对 eml 是相对 root 的目录路径。
- `sink.size/mtime_ns`：mbox 文件指纹；eml sink 可为 NULL。
- `eml_file.relpath` 是相对 eml 目录的文件路径；`size/mtime_ns` 是逐文件指纹。
- `message` 是某 sink 的消息标识集合；eml 的 `message` 与 `eml_file.msg_id` 双写，用于快速判重与逐文件失效。

#### 5.6.3 快照接口

```python
class IndexStats:
    hits: int = 0
    misses: int = 0
    rebuilds: int = 0
    disabled: bool = False
    local_ms: float = 0.0

class ScanIndex:
    @classmethod
    def open(cls, log, stats: IndexStats | None = None) -> "ScanIndex | None": ...

    def local_mbox_ids(self, root, account, folder, relpath,
                       overwrite, nospinner, quiet, log) -> dict[str, str] | None: ...

    def local_eml_ids(self, root, account, folder, relpath,
                      overwrite, nospinner, quiet, log) -> dict[str, str] | None: ...

    def writer(self, kind, root, account, folder, relpath,
               overwrite: bool = False) -> "IndexWriter": ...

    def close(self) -> None: ...
```

- 返回 `None` 表示「没有可用本地快照」：调用方按空集处理（`overwrite=True`），或按内容扫描结果处理。
- `overwrite=True`：清空对应 sink 行（级联删 message/eml_file），返回 `None`；下载会覆盖文件，索引随写重建。
- 未选中格式的 sink 行保留，不因本次只备份 eml 而删除 mbox 索引。

#### 5.6.4 失效矩阵

| 情况 | 动作 | 统计 |
|---|---|---|
| DB/表不存在、`user_version != 1` | 建库/删库重建；内容扫描 | miss + rebuild |
| 无 sink 行 | 内容扫描；建 sink 快照 | miss + rebuild |
| mbox 文件不存在 | 本地集合为空；删 sink 行 | miss |
| mbox `(size, mtime_ns)` 与 sink 行一致 | 直接返回 message 行 | hit |
| mbox 指纹不一致 | 全量内容扫描；替换该 sink 的 message 行 | miss + rebuild |
| eml 目录不存在 | 本地集合为空；删 sink 行 | miss |
| eml 目录枚举 + 每个文件 `(size, mtime_ns)` 与 `eml_file` 完全一致 | 直接返回映射 | hit |
| eml 有新增/缺失/改大小/改 mtime | 全量内容扫描该 eml 目录；替换该 sink 的 eml_file/message 行 | miss + rebuild |
| `overwrite=True` | 清 sink；返回 `None` | 不计 hit/miss |
| 行内容损坏/Message-Id 解析失败 | 重建该 sink | miss + rebuild |
| DB 损坏 | 关闭、删除文件、重建；内容扫描 | miss + rebuild |
| 锁超时/不可写 | 本次运行禁用索引；内容扫描 | disabled |
| 写入过程中出错 | 禁用索引；不抛给备份 | disabled |

「内容扫描」沿用现有 `scan_file` / `scan_eml_dir` 的读取与 Message-Id 解析逻辑，只是把结果写回索引；不引入第二套判重。

#### 5.6.5 写后登记

`IndexWriter` 的方法：

```python
class IndexWriter:
    def mbox_written(self, msg_id: str) -> None: ...
    def eml_written(self, relpath: str, msg_id: str) -> None: ...
    def batch_end(self, mbox_fd: int | None = None) -> None: ...
    def finish(self) -> None: ...
```

- `IndexWriter` 在 `overwrite=True` 或首次创建 sink 时先清空该 sink 的旧行，避免覆盖写把旧 Message-Id 留在索引里。
- mbox：每写一封调 `mbox_written`；批次结束时先 `mbox.flush()`，用 `os.fstat(mbox_fd)` 取 `(size, mtime_ns)` 更新 sink 指纹，再提交。
- eml：每写一个文件调 `eml_written(relpath, msg_id)`；`os.stat` 取指纹；批次结束提交。
- 双格式消息：两个 writer 各登记一次；`message` 表按 sink 分开。
- 每批 `commit()`；不设 dirty flag。
- 进程在批中被强杀：mbox 可能有未登记的消息，eml 可能有未登记的文件；下次运行指纹不一致 → 内容扫描重建，不会错误跳过。这是「写后登记」的崩溃安全边界。
- `finish()` 在文件夹结束时提交并关闭 writer；异常路径尽力回滚未提交事务，不影响备份。
- 索引写入失败 → 标记 `IndexStats.disabled=True`，关闭连接，后续不再尝试；已写备份文件保留。

#### 5.6.6 多线程与共享

- GUI 多账户并发：每线程独立 `ScanIndex`/连接；WAL + `busy_timeout=5000` 允许并发读写。
- CLI 与 GUI 共用同一 DB 路径与 schema；不按入口分库。
- 同一账户同一文件夹不会被两个线程同时跑（ADR 0012 的账户独立运行）；不同账户的 sink 行互不冲突。
- 卸载器删除 `index` 目录的时机与「界面设置」复选框一致（第 11 节）。

### 5.7 断线重连

#### 5.7.1 归属

- 核心提供 `run_with_reconnect`；CLI 与 GUI 共用。
- `scan_folder` / `download_messages` 遇到连接级错误**不内部重试**，向调用方抛出。
- 首次连接（`connect_and_login`）沿用现有失败路径：认证失败走现有预检/密码补录或账户失败路径（GUI），或退出（CLI），不进入重连预算。只有已建立连接后的操作失败才重连。

#### 5.7.2 接口

```python
class ReconnectStopped(Exception): ...
class ReconnectAuthError(Exception): ...
class BackupWriteError(Exception): ...

@dataclass
class ReconnectStats:
    attempts: int = 0
    successes: int = 0
    exhausted: bool = False
    uidvalidity_changes: int = 0
    last_reason: str | None = None   # abort / oserror / bye

def run_with_reconnect(
    server: imaplib.IMAP4,
    connect: Callable[[], imaplib.IMAP4],
    operation: Callable[[imaplib.IMAP4], T],
    *,
    should_stop: Callable[[], bool] | None,
    log: logging.Logger,
    stats: ReconnectStats | None = None,
) -> tuple[T, imaplib.IMAP4]: ...
```

`operation` 每次收到一个新连接，负责自己的 SELECT、UIDVALIDITY 校验和续跑。成功返回 `(结果, 新连接)`；调用方用返回的连接替换旧连接。

#### 5.7.3 错误分类

| 异常 | 分类 |
|---|---|
| `imaplib.IMAP4.abort` | 可重试；`reason=abort` |
| `OSError`（非 `BackupWriteError`） | 可重试；`reason=oserror` |
| `imaplib.IMAP4.error` 文本含 `BYE`（不区分大小写） | 可重试；`reason=bye` |
| `imaplib.IMAP4.error` 其他（NO/BAD/认证） | 不重试 |
| `ReconnectAuthError` | 不重试 |
| `BackupWriteError` | 不重试，按写盘失败向上抛 |
| `FolderFetchError` / `SkipFolderException` / `ReconnectStopped` | 不重试，交给调用方 |

`connect` 回调负责把 `connect_and_login` 的 `SystemExit` 分类：

- 消息以 `IMAP authentication failed` 开头 → `ReconnectAuthError`。
- 其他 `SystemExit`（DNS/连接失败）→ 可重试的连接错误。

#### 5.7.4 退避

- 一次连续失败最多 5 次；成功一次后预算清零。
- 第 `n` 次重试（`n=1..5`）的延迟上界：

  ```python
  cap = min(300.0, 2.0 * 4 ** (n - 1))   # 2, 8, 32, 128, 300 秒
  delay = random.uniform(0, cap)          # 全抖动
  ```

- 退避期间每 0.2 秒检查 `should_stop()`；停止立即抛 `ReconnectStopped`。
- 退避期间 UI 显示 6.2.2 节的重连文案；CLI 只写运行日志。
- 5 次耗尽：`stats.exhausted=True`，把最后一次异常原样抛给调用方。
- 日志：断线 WARNING、每次重试 INFO、耗尽 ERROR；不含邮件内容。

#### 5.7.5 扫描阶段续跑

扫描没有持久游标。重连后直接重跑该文件夹的 `scan_folder`，不保存半截扫描结果。重连成功后的新连接由 `operation` 重新 SELECT。

GUI/CLI 的 `get_names`（LIST）也通过同一 helper：LIST 遇到连接级错误时重连后重跑 LIST，再继续文件夹扫描。

#### 5.7.6 下载阶段续跑

调用方为**每个文件夹**维护：

- `written: set[str]`：该文件夹本次运行中所有已选格式都已写完的 Message-Id。
- 本地快照 `local_mbox` / `local_eml`：扫描阶段的结果，重连不重扫本地。
- `state`：当前 `FolderScan`、`uidvalidity`、是否已因 UIDVALIDITY 重扫过一次。

`operation(new_server)`：

1. `SELECT` 当前文件夹（readonly）；失败 → 抛 `SkipFolderException`（文件夹失败），不重连。
2. 读新连接的 UIDVALIDITY（`scan_folder` 保证可用值，缺失即该文件夹失败）：
   - 相同：继续用 `state.messages`。
   - 不同：
     - `state.uidvalidity_checked` 为假：重跑 `scan_folder`；`uidvalidity_changes += 1`；用新 `FolderScan` 重算 pending（复用本地快照），再减去 `written`；把新结果存回 `state`，设 `uidvalidity_checked=True`。
     - `state.uidvalidity_checked` 已为真：抛 `FolderFetchError`，不再重扫。
   - 重扫导致该文件夹的 `server_total` / `pending_total` / `pending_bytes` 变化时，GUI 重发 `totals` 事件并更新 `AccountStats`；已备份计数不清零。
3. 从 `state.messages` 中排除 `written`，调用 `download_messages`；传入同一个 `progress` 与 `written`，索引 writer 继续用。
4. 重连成功后 `stats.successes += 1`，连续失败计数清零。

`written` 只服务于本次运行的重连续跑，不是判重事实来源；下次运行仍以索引/内容扫描为准。

#### 5.7.7 写盘错误

- 本地写盘错误包装为 `BackupWriteError`，不进入重连。`BackupWriteError` 不得继承 `OSError`，否则会被 5.7.3 的连接级分类误判为可重试。
- 该封不加入 `written`，不增加 `backed`；向上抛给调用方。
- GUI 账户级失败；CLI 按现有异常路径失败。下次运行按格式判重复位。

#### 5.7.8 停止优先

- 重连前、退避每个 0.2 秒切片、重连后发请求前都检查停止。
- 停止信号到达时：关闭旧连接，抛 `ReconnectStopped`；调用方按停止处理。
- 停止不触发第 6 次重试，也不把剩余 pending 计为失败。

### 5.8 停止

- 每批开始前、每封写完后检查（沿用现有检查点，ADR 0012）。
- 在途 FETCH 无法中断：等响应后丢弃，不写半批。
- 停止后不再扫描新文件夹、不再发新请求。
- 单账户「停止」只停该账户；「停止所有」/托盘停止对所有活动账户发信号（ADR 0012）。
- 退出程序的优雅停止上限 15 秒不变；备份线程写盘非原子的边缘风险仍由「停止后再退出」覆盖。

### 5.9 GUI 事件与状态契约

事件队列新增/调整：

| 事件 | 参数 | 说明 |
|---|---|---|
| `phase` | `(account_id, kind, text)` | `kind ∈ {connecting, scanning, download, backoff, stopping}`；`text` 是第二行阶段文案 |
| `totals` | `(account_id, server_total, pending_total, pending_bytes)` | 新增 `pending_bytes` |
| `message` | `(account_id, report_dict, fetch_size)` | `fetch_size` 是 `RFC822.SIZE`，供速度/ETA 用 |
| `account_done` | `(account_id, backed, skipped, seconds, stopped, pending_total, fetch_failed, fetch_failed_missing, fetch_failed_no_bad, fetch_failed_malformed, folder_failures)` | 新增失败计数 |
| `account_failed` | `(account_id, message)` | 不变 |
| `batch` | 无独立事件 | 批次统计通过线程安全的 `AccountStats` 快照共享，避免事件队列膨胀 |

`phase` 的 `kind` 到现有 `run_phase` 的映射：

- `connecting` → `PHASE_CONNECTING`
- `scanning` → `PHASE_SCANNING`
- `download` → `PHASE_BACKING`
- `backoff` → `PHASE_BACKING`（第二行显示重连文案）
- `stopping` → `PHASE_STOPPING`

`AccountStats`（线程安全，GUI 主线程读取快照）：

```python
@dataclass
class AccountStats:
    phase: str
    folder: str | None
    server_total: int
    pending_total: int
    pending_bytes: int
    progress: DownloadProgress
    index: IndexStats
    reconnect: ReconnectStats
    batch_durations_ms: deque[float]   # 最近 10 秒
    last_batch: BatchStats | None
```

worker 在每批/每封/每次重连后更新；主线程的 1 秒 tick 只读快照，不阻塞 worker。读写共用同一把 `threading.Lock`，主线程取一份浅拷贝后再计算，不直接遍历 worker 的可变容器。

## 6. GUI 设计

### 6.1 账户备份 worker

`_backup_account` 保持「单账户、独立线程、独立连接、独立停止事件」的 ADR 0012 模型，内部改为：

**pass 1（扫描与统计）**

1. `connect_and_login`；发 `phase(connecting)`。
2. `get_names`（通过 `run_with_reconnect`）；按账户勾选过滤。
3. 对每个选中文件夹：
   - 发 `phase(scanning, "扫描 i/N：display")`。
   - `scan = run_with_reconnect(server, connect_for_reconnect, lambda s: scan_folder(s, ...))`；`SkipFolderException` → `folder_failures += 1`，继续。
   - 用 `ScanIndex` 取本地快照；索引禁用时退回 `scan_file` / `scan_eml_dir`。
   - `pending = pending_messages(scan.messages, local_mbox, local_eml)`。
   - `server_total += len(scan.messages)`、`pending_total += len(pending)`、`pending_bytes += Σ size`。
   - 保存 `FolderPlan(name, scan, local_mbox, local_eml, pending)`。
4. 发 `totals(account_id, server_total, pending_total, pending_bytes)`。

**pass 2（下载）**

1. 若有 pending 且未停止：创建 mbox 目录结构（现有逻辑）。
2. 对每个 `pending` 非空的 plan：
   - 发 `phase(download, "备份 display（N 封）")`。
   - 建立 `written: set[str]`、`progress = DownloadProgress()`、`state = {"scan": scan, "uidvalidity": scan.uidvalidity, "uidvalidity_checked": False}`。
   - `run_with_reconnect(server, connect_for_reconnect, operation, should_stop=stop_event.is_set, stats=...)`。
   - `operation` 按 5.7.6 执行 SELECT、UIDVALIDITY、`download_messages`。
   - `FolderFetchError`/SELECT 失败 → `folder_failures += 1`，继续。
   - `ReconnectStopped` 或停止事件 → 结束下载循环。
   - `ReconnectAuthError` → 账户失败；`BackupWriteError`/其他异常 → 账户失败。
3. 发 `account_done`，带上所有计数。

`report` 闭包在每封写入后：

```python
sizes = {msg_id: size for msg_id, (uid, size) in new.items()}
eq.put(("message", account_id, d, sizes.get(d["msg_id"], 0)))
```

重连重扫后更新 `sizes`。

`message` 事件驱动的界面计数与 worker 的 `DownloadProgress.backed` 是同一事实的两份记账，不得相加：列表/第一行用前者，诊断用后者。

### 6.2 第二行速率/预计剩余

#### 6.2.1 文案与顺序

下载阶段（选中账户正在下载）：

```text
备份 INBOX（1519 封） · 本账户 84.0 封/分 · 1.8 MB/s · 预计剩余 18 分钟 · 全部 210.0 封/分 · 4.5 MB/s · 取信失败 2 封
```

顺序固定：**本账户消息速率 → 本账户字节速率 → 预计剩余 → 合计消息速率 → 合计字节速率 → 失败后缀**。预计剩余必须在本账户速率之后、合计之前，避免被误解为全部账户的剩余时间。

#### 6.2.2 状态表

| 状态 | 第二行内容 | 速率/ETA | 合计 |
|---|---|---|---|
| idle | 现有空闲文案（已是最新/上次结果） | 无 | 无 |
| connecting | 现有连接文案 | 无 | 无 |
| scanning | `扫描 i/N：display` + 失败后缀 | 无 | 无 |
| download | 见 6.2.1 | 有 | 满足条件时有 |
| backoff | `连接中断，8 秒后重试（第 2/5 次）` + 失败后缀 | 无 | 无 |
| stopping | `正在停止…` + 失败后缀 | 无 | 无 |

- 失败后缀：`取信失败 N 封`、`文件夹失败 F 个`，只在 `N>0`/`F>0` 时出现；顺序固定为取信失败在前。
- 只有**当前选中账户**在下载时才显示速率/ETA；选中账户在扫描/连接/重连/停止时不显示。
- 合计只在至少 2 个账户处于 `download` 时出现；只汇总处于 `download` 的账户，排除扫描/连接/重连/停止。
- 一个活动账户时隐藏合计（即使它是下载中）。

#### 6.2.3 速率窗口

- 本账户消息/字节速率：30 秒滑动窗口。
- 样本来自每封完成事件 `(monotonic, fetch_size)`；`fetch_size` 是 `RFC822.SIZE`。
- 重新进入 `download`（从连接/扫描/重连/停止切回）时清空窗口；**文件夹切换不清空**。
- 计算：

  ```text
  cutoff = now - 30
  samples = 窗口内样本
  elapsed = now - max(cutoff, 第一个样本时间)
  rate_msgs = len(samples) / elapsed * 60
  rate_bytes = sum(size) / elapsed
  ```

- `elapsed < 5 秒` 或 `len(samples) < 3` → 显示 `—`，不显示字节速率与 ETA。
- 达到阈值后即使速率为 0，也如实显示 `0.0 封/分`，不藏。
- 合计速率 = 各下载中账户的当前速率之和；若没有任何账户的速率可计算，显示 `全部 —`。

#### 6.2.4 预计剩余

- `remaining_bytes = max(0, pending_bytes - done_bytes)`。
- `done_bytes` 只累加已备份消息的 `RFC822.SIZE`；`size=0` 按 0。
- 使用 60 秒字节速率 `rate60`（同窗口口径，样本不足 10 秒或没有首批完成 → 不显示 ETA）。
- `eta_s = remaining_bytes / rate60`；`rate60 == 0` → 不显示。
- 显示：`<1 分钟`、`N 分钟`（向上取整）、`N 小时 M 分钟`（≥60 分钟）。
- 不做向下钳制：速率下降时 ETA 可以变长；不要为了「看起来好」强行单调递减。
- `size` 未知的邮件不进入字节口径；若未知占比高，ETA 只作参考。

#### 6.2.5 刷新

- 现有 100ms `_pump` 负责事件与列表；1 秒 tick 只重算第二行与诊断节奏。
- 第二行文本未变不重设，避免无谓重绘。
- 1 秒 tick **不得**触碰邮件列表、滚动位置或 `see`。

### 6.3 账户图标与完成冷却

- 新增 `partial` 状态：黄色警告图标，常驻到下次启动/重跑/取消勾选。
- 图标优先级：`stopped`（灰方块，5 秒，ADR 0012）> `failed`（红叉，常驻）> `partial`（黄色，常驻）> `done`（绿勾，60 秒冷却）。
- 完成冷却 `DONE_COOLDOWN_S = 60`：`done` 与 `partial` 都进入冷却；冷却内「备份所选」跳过该账户；右下「开始备份」单账户命令绕过冷却。
- 取消勾选立即清终态图标与冷却（现有行为）。
- 后台运行结束的托盘气泡沿用现有触发时机；有 `partial` 时文案包含取信失败/文件夹失败计数，不得显示「全部完成」。
- 运行中切换账户：`_restore_run_state` 恢复该账户的计数/状态/进度；第二行按选中账户重算，不显示别的账户的速率。

### 6.4 邮件列表滚动

当前问题：`_insert_message_row` 每插一行都 `tree.see(iid)`，用户上移后被拉回底部。

目标行为：

1. 移除 `_insert_message_row` 内的 `see`。
2. `_on_message_event` 只追加数据并放入待插入队列；100ms 的 `_pump` 合并插入。
3. 每次批量插入前记录 `at_bottom = self.tree.yview()[1] >= 0.999`。
4. 插入完成后：
   - `at_bottom` 为真 → `tree.see(最后一个 iid)`，跟随底部；
   - 为假 → 不调用 `see`，保持用户当前视口。
5. 切换账户的 `_render_messages`：清空待插入队列，重画该账户全部行，`tree.yview_moveto(1.0)`，让用户看到最新邮件。
6. 1 秒 tick、状态行更新、诊断写入都不得调用 `see` 或改变 `yview`。

测试：

- 列表在底部时追加 100 行 → 最后一行可见。
- 用户 `yview_moveto(0.2)` 后追加 100 行 → 顶部比例保持在 0.2 附近，不被拉到底部。
- 切换账户 → 显示该账户最新行（底部）。
- 状态行每秒更新时列表 `yview` 不变。

### 6.5 菜单栏「常见问题」

- 新增顶部菜单 `帮助` → `常见问题`；不改变现有「备份」「设置」页面菜单。
- 单例 Toplevel；已打开时聚焦，不重复创建。
- 只读、可滚动；Esc 与关闭按钮关闭。
- 不读账户、不连服务器、不写设置、不写日志中的 35 专用行。
- 内容见第 9 节。

### 6.6 设置页与账户切换

- 不新增设置项。
- 账户表单不新增字段。
- 账户切换时恢复该账户的 `run_phase`、计数、进度、图标、第二行；不共享速率窗口。
- 账户名称仍只用于显示；索引/路径/判重不接触账户名称。

## 7. 诊断采样（JSONL）

### 7.1 启用与位置

- 仅 GUI 写；CLI 不写、无新参数。
- 路径：`LOG_DIR / f"diag-{YYYY-MM-DD}.jsonl"`；`LOG_DIR` 用核心模块属性，测试可重定向。
- UTF-8 追加；每行 `json.dumps(..., ensure_ascii=False)` 后 `\n`，立即 `flush()`。
- 每次写入前按当天日期选文件；跨天自动换文件。
- 文件写入失败：禁用本次进程的诊断，记一次 WARNING，备份继续。

### 7.2 节奏

- 每个活动账户每 10 秒一条 `sample`。
- `phase`、`account_end`、`run_end` 立即写。
- 同一时刻 phase 与 sample 冲突：只写 phase，重置该账户的 10 秒计时。
- `run` 是 8 位十六进制，每个备份运行（ADR 0012 的活动集合周期）共享；追加账户不换 run。
- `run_end` 在活动集合排空时写一条，`account`/`host`/`folder` 为 null。
- `run_end.result` 的优先级：任一账户 `failed` → `failed`；否则任一 `stopped` → `stopped`；否则任一 `partial` → `partial`；否则 `completed`。

### 7.3 字段

所有行使用同一套字段；不适用的字段写 `null`，保持 schema 稳定。

| 分组 | 字段 | 类型 | 说明 |
|---|---|---|---|
| 身份 | `ts` | string | 本地时间 ISO 8601，带时区，毫秒 |
| | `schema` | int | 1 |
| | `app` | string | `imapbackup-gui` |
| | `run` | string | 8 位十六进制 |
| | `event` | string | `sample` / `phase` / `account_end` / `run_end` |
| | `account` | string/null | 邮箱地址；run_end 为 null |
| | `host` | string/null | 服务器地址 |
| | `folder` | string/null | 文件夹显示名 |
| | `phase` | string/null | `connecting/scanning/download/backoff/stopping/idle` |
| 进度 | `done_msgs` | int | 已备份消息数 |
| | `done_bytes` | int | 已备份消息的 `RFC822.SIZE` 之和 |
| | `pending_msgs` | int | 统计时的待备份总数 |
| | `pending_bytes` | int | 统计时的待备份字节总数 |
| 速度 | `rate_msgs_per_min` | float/null | 30 秒窗口；不可计算为 null |
| | `rate_bytes_per_s` | float/null | 30 秒窗口；不可计算为 null |
| | `eta_s` | int/null | 60 秒窗口；不可计算为 null |
| 批次 | `batch_msgs` | int/null | 最近完成批的消息数 |
| | `batch_bytes` | int/null | 最近完成批的请求字节 |
| | `fetch_ms_avg` | float/null | 最近 10 秒批次的平均取信耗时 |
| | `fetch_ms_p95` | float/null | 最近 10 秒批次的 p95 |
| 失败 | `fetch_failed` | int | 总取信失败 |
| | `fetch_failed_missing` | int | 缺失 UID |
| | `fetch_failed_no_bad` | int | 单封 NO/BAD |
| | `fetch_failed_malformed` | int | 畸形响应 |
| | `folder_failures` | int | 文件夹失败数 |
| 索引 | `index_hits` | int | 命中次数 |
| | `index_misses` | int | 未命中次数 |
| | `index_rebuilds` | int | 重建次数 |
| | `index_disabled` | bool | 本次运行是否禁用 |
| | `scan_local_ms` | int | 本地快照总耗时 |
| | `scan_remote_ms` | int | 远端扫描总耗时 |
| 重连 | `reconnect_attempts` | int | 重试次数 |
| | `reconnect_successes` | int | 重连成功次数 |
| | `reconnect_exhausted` | bool | 是否耗尽 |
| | `uidvalidity_changes` | int | UIDVALIDITY 变化次数 |
| | `reconnect_reason` | string/null | `abort` / `oserror` / `bye` |
| 终态 | `result` | string/null | `completed/partial/stopped/failed`；`completed` 对应 UI 的 `done`（含已是最新） |
| | `duration_s` | float/null | 账户或运行时长 |

示例（虚构数据）：

```json
{"ts":"2026-10-26T14:03:22.123+08:00","schema":1,"app":"imapbackup-gui","run":"9f3a1c2d","event":"sample","account":"manager@example.com","host":"imap.example.com","folder":"INBOX","phase":"download","done_msgs":1519,"done_bytes":1960000000,"pending_msgs":4200,"pending_bytes":5500000000,"rate_msgs_per_min":84.0,"rate_bytes_per_s":1887436.8,"eta_s":1875,"batch_msgs":8,"batch_bytes":8388608,"fetch_ms_avg":412.5,"fetch_ms_p95":980.0,"fetch_failed":2,"fetch_failed_missing":1,"fetch_failed_no_bad":1,"fetch_failed_malformed":0,"folder_failures":0,"index_hits":1,"index_misses":0,"index_rebuilds":0,"index_disabled":false,"scan_local_ms":320,"scan_remote_ms":8450,"reconnect_attempts":0,"reconnect_successes":0,"reconnect_exhausted":false,"uidvalidity_changes":0,"reconnect_reason":null,"result":null,"duration_s":null}
```

### 7.4 隐私白名单

允许写入：时间、应用名、run ID、账户邮箱、服务器主机、文件夹显示名、阶段、所有计数/速率/耗时/错误类别。

禁止写入：

- 账户名称（显示名）。
- 邮件主题、发件人、收件人、日期头、Message-Id、`.eml` 文件名、附件名、正文。
- 密码、OAuth/token、证书内容。
- 服务器原始响应文本、异常堆栈中的邮件内容。
- 任何可还原邮件内容的哈希或片段。

`reconnect_reason` 与失败原因只写类别，不写原始异常文本。诊断文件不自动上传、不提供导出按钮；用户自行打开日志文件夹发送。

### 7.5 轮转

- 保留 14 天。
- 启动时与每次运行开始时清理 `LOG_DIR` 下匹配 `diag-YYYY-MM-DD.jsonl` 且日期早于 14 天的文件。
- 只删除 `diag-*.jsonl`，不碰 `run-*.log`、设置、备份目录。
- 删除失败记 WARNING，继续。

### 7.6 CLI 边界

- CLI 不创建 `diag-*.jsonl`，不新增参数，不改变 stdout/退出码。
- 核心的重连/索引/批量日志仍写 `run-*.log`（INFO），CLI 控制台级别沿用现有规则。

## 8. 日志

- 运行日志继续是「每天一个 `run-*.log`，root FileHandler INFO，`RUN_LOG_LOGGER` 不 propagate」。
- 每封的 `debug` 行仍不写入文件（FileHandler 级别 INFO），避免日志暴涨。
- 新增 INFO/WARNING（不含邮件内容）：
  - 账户开始：现有行保留。
  - 扫描命中索引：`账户 X：文件夹 Y 索引命中（N 封）`。
  - 索引重建/禁用：WARNING。
  - 重连：断线 WARNING、重试 INFO、成功 INFO、耗尽 ERROR。
  - 文件夹失败：WARNING，含文件夹显示名与类别。
  - 账户结束：现有行扩展为 `备份 N 封，跳过 M 封，取信失败 K 封，文件夹失败 F 个，用时 ...`。
  - 运行结束：现有行扩展聚合失败计数。
- 不新增 35 专用日志行；不把服务端原始响应写入运行日志（现有异常消息可能含状态文本，保持现状）。
- CLI 正常成功路径的 stdout 不变。

## 9. 菜单栏「常见问题」

### 9.1 入口与行为

- 顶部菜单 `帮助` → `常见问题`。
- 只读 `Toplevel`，带滚动条；Esc/关闭按钮关闭；单例。
- 不读账户、不连服务器、不写设置、不写日志。
- 文案通用原因在前，35 特例单列在最后。

### 9.2 文案

**标题：常见问题**

> 这些说明适用于大多数邮箱服务商。各服务商的可见范围、下载限制和限速策略可能不同；如果下面的通用原因都不适用，请以服务商的说明为准。

**Q1：服务器上明明有很多邮件，软件只看到很少，或者备份出来只有几封？**

> 最常见的原因是邮箱服务商默认只让 IMAP 客户端看到最近一段时间的邮件（例如最近 30 天）。请登录网页邮箱，在「设置 → 邮件收取/转发」或「账户 → POP/IMAP」中把可收取范围改为「全部邮件」，保存后重新备份。
>
> 其他可能原因：只勾选了部分文件夹；文件夹在服务器上尚未同步完成；服务商关闭了 IMAP 访问。修改可见范围后，首次备份需要重新扫描全部邮件，耗时会明显增加。

**Q2：为什么会出现 0 封下载、下载失败或「取信失败」？**

> 常见原因是服务商对短时间内的连接数、请求数或下载量做了限制，或服务器暂时返回错误。软件会记录失败数量并继续处理其他邮件，不会修改服务器上的邮件；下次备份会再次尝试这些邮件。
>
> 如果失败集中在某个文件夹，可能是该文件夹的可见范围或权限受限；如果大量失败同时出现，先检查网络，等待一段时间后再试。

**Q3：为什么本地备份的邮件比服务器上显示的还多？**

> 本地备份是追加式的：软件只新增缺失的邮件，不会因为服务器上的邮件被删除或移动而删除本地文件。因此，服务器上删除过的邮件仍会留在本地备份中。
>
> 另外，不同客户端对文件夹的显示方式可能不同；请按文件夹和时间范围对照，而不是只看服务器网页上的总数。

**Q4：35 云邮（35 互联邮箱）需要特别注意什么？**

> 据公开资料，35 云邮的 IMAP 默认只显示最近 30 天，需要在网页端把收取范围改为「全部邮件」，否则软件看不到更早的邮件。35 云邮没有公开的限速数值；如果备份到几十封后明显变慢，通常是服务端在限速，可以暂停一段时间再继续，已备份的邮件不会重复下载。上面的通用说明同样适用。

### 9.3 测试

- 菜单项存在，点击打开 Toplevel。
- 文案包含 Q1–Q4 四个问题与 35 关键词。
- 打开/关闭不调用 IMAP、不读取账户凭证、不写设置文件。
- 重复点击只聚焦已有窗口。
- Esc 与关闭按钮都能关闭。
- 文本只读、可滚动。
- GUI 测试标记 `gui`；无显示环境跳过。

## 10. ADR

本 effort 新增一份 ADR：

- [ADR 0013：固定批量、无客户端自适应限速](../adr/0013-fixed-batching-no-adaptive-throttle.md)。

理由：早期设计最初选择自适应闭环 + 同主机共享总速率；一次离散事件仿真证明常规模型下闭环更慢，后续架构决策进一步取消共享控制器。这是一个**硬反转**、**未来读者会问为什么**、**有真实取舍**的决策，必须留下 ADR。其他决策（索引、重连、诊断、FAQ）已在相关 ADR 与本文中有上下文，不另开 ADR。

ADR 需要说明：

- v1 固定 8 封 / 8 MiB、每账户单请求在途、无休眠/令牌桶/AIMD/共享状态。
- 重评触发条件：现场出现锯齿、错误率上升、服务商公布限额，或验收显示持续降速且客户端策略有明确收益。
- 若未来引入控制器，必须是新 ADR + 新设置/参数，不在 v1 预留空接口。

## 11. 需要同步的调用点、测试、文档、卸载器

### 11.1 核心 `imapbackup312.py`

- 新增常量：`SCAN_PAGE_UIDS`、`FETCH_BATCH_MAX_MESSAGES`、`FETCH_BATCH_MAX_BYTES`、`INDEX_DIR`、`DB_NAME`。
- 新增：`FolderScan`、`DownloadProgress`、`BatchStats`、`ReconnectStats`、`ReconnectStopped`、`ReconnectAuthError`、`BackupWriteError`、`FolderFetchError`、`ScanIndex`、`IndexWriter`、`IndexStats`、`run_with_reconnect`、`connect_for_reconnect`。
- 修改：`scan_folder` 返回 `FolderScan`；`download_messages` 改为 UID 批量、返回值/进度/索引/批次回调；CLI `main` 的文件夹循环接入索引与重连；`configure_logging` 不变。
- 保持：`report_message` 字段不变；`MSGID_RE`/`UUID` 不变；CLI 参数与退出码不变。

### 11.2 GUI `imapbackup_gui.py`

- 修改：`_backup_account` 两遍流程、事件契约、`_on_message_event`、`_on_account_done`、`_apply_account_icon`、`_in_done_cooldown`、`_after_account_terminal`、`_render_messages`、`_insert_message_row`、`_pump`、1 秒 tick、菜单栏、诊断写入。
- 新增：`AccountStats` 线程安全快照、`SpeedTracker`/ETA 计算、`Diagnostics`、FAQ Toplevel。
- 保持：第一行信息栏完全不变；`APP_MUTEX_NAME` 不变；设置页字段不变。

### 11.3 测试

- `tests/fakes.py`：
  - `UidFetchServer` 实现 `uid("SEARCH","ALL")`、`uid("FETCH", ...)`、`select`、`untagged_responses["UIDVALIDITY"]`。
  - 记录每个连接的命令、UID 集合、在途请求数、SELECT 次数、重连次数。
  - 注入：缺失 UID、畸形元数据、缺字面量、重复 UID、批次 NO/BAD、单封 NO/BAD、abort、BYE、OSError、认证失败、UIDVALIDITY 变化。
  - 保留现有 `ListServer`/`MainServer`/`FetchServer`/`HeaderServer` 的简单行为，或迁移到 UID 版本；不得让测试依赖真实网络。
- `tests/conftest.py`：
  - 新增 `isolate_index`（autouse）：把 `core.INDEX_DIR` 指向 `tmp_path/index`，结束后恢复。
  - 新增 `isolate_log_dir`（按需）：把 `core.LOG_DIR` 指向 `tmp_path/log`。
- 新增/更新测试文件（名称可调整，覆盖必须存在）：
  - `tests/test_uid_scan.py`：SEARCH 分页、500 边界、合成 ID、畸形、SELECT NO。
  - `tests/test_batch_fetch.py`：8/8MiB、单封超限、UID 升序、响应解析、失败矩阵、report 一次、停止。
  - `tests/test_scan_index.py`：schema、命中/未命中、失效矩阵、损坏、锁、并发、overwrite、格式保留、索引失败不影响下载。
  - `tests/test_reconnect.py`：分类、退避、停止、预算、UIDVALIDITY、written、写盘错误、CLI。
  - `tests/test_gui_backup_flow.py`：部分完成、文件夹失败、account_done、图标、事件契约。
  - `tests/test_speed_eta.py`：窗口、阈值、格式、ETA、合计。
  - `tests/test_list_scroll.py`：底部跟随/上移不抢/切换账户。
  - `tests/test_diagnostics.py`：字段、节奏、隐私、轮转、失败禁用、CLI 不写。
  - `tests/test_faq.py`：菜单、内容、只读、无网络/设置写。
  - 更新 `tests/test_eml_output.py`、`test_incremental_per_format.py`、`test_should_stop_cancels_mid_folder.py` 等现有断言（UID 批量、元组值、fetch 次数按批）。

### 11.4 文档

- `README.md` Fork Notes：
  - 首量吞吐：UID 扫描、8 封 / 8 MiB、每账户单请求在途、无客户端限速。
  - 中断恢复：本地扫描索引位置、可删除、卸载行为。
  - 断线重连：有界重试、UIDVALIDITY、停止优先。
  - 部分完成：取信失败/文件夹失败含义；第二行速率/ETA。
  - 诊断：`diag-*.jsonl` 位置、10 秒、14 天、隐私、仅 GUI。
  - 常见问题：菜单入口；通用原因 + 35 特例。
  - CLI：参数/输出/退出码不变；CLI 不写诊断。
- `GLOSSARY.md`：已有术语足够，不新增；若实现中引入新领域词，先走 domain-modeling。
- ADR 0013：新增；ADR 目录无需索引文件（与现状一致）。

### 11.5 安装程序 `packaging/imapbackup-gui.iss`

- `DeleteSettings` 分支新增删除 `{localappdata}\imapbackup-gui\index`（递归）。
- `DeleteSettingsBox.Caption` 更新为同时提到「本地扫描索引」；具体文案可由实现微调，但必须让用户知道勾选会删除索引缓存。
- 不改变 `AppId`、`AppMutex`、备份目录删除范围；`DeleteLogs` 仍只删 `log`（含 `diag-*.jsonl`）。
- 安装/卸载按 ADR 0007 人工验收：勾选删除设置 → 索引与设置消失，备份目录不动；不勾选 → 索引保留。

### 11.6 版本与打包

- 版本号变更须用户明确批准；同步 `imapbackup312.py`、`imapbackup_gui.py`、`packaging/imapbackup-gui.iss`、`packaging/build.bat`。
- 修改 `packaging/` 后在 `cmd` 运行 `packaging\build.bat`；agent 不擅自构建安装包或提交产物。

## 12. 验收

### 12.1 命令

```bash
uv run python -m py_compile imapbackup312.py imapbackup_gui.py
uv run pytest -m "not gui" -q
uv run pytest -m gui -q
uv run pytest -q
ruff check imapbackup312.py imapbackup_gui.py tests
basedpyright imapbackup312.py imapbackup_gui.py
```

`ruff`/`basedpyright` 按仓库现有配置；无显示环境时 `-m gui` 可跳过，但发布前必须在有 Windows Tk 的环境跑一次。

### 12.2 假服务器断言组

假服务器是 6–7 并发与失败路径的**唯一权威**；现场只有 1 个大账户 + 1 个 QQ。

| 组 | 断言 |
|---|---|
| A 扫描 | `UID SEARCH ALL` 一次；每 500 UID 一页；无 `1:*` 游标；UID 从元数据取；缺 Message-Id 走合成回退；畸形/NO → `SkipFolderException`；UIDVALIDITY 随结果返回，缺失/畸形 → `SkipFolderException` 且不运行 SEARCH；`\Noselect` 容器不列为备份目标 |
| B 批量 | 8 封/8 MiB 边界；第 9 封新批；单封 8 MiB+ 独占；size 0 独占；批内 UID 升序；请求含 `UID INTERNALDATE BODY.PEEK[]`；无 `RFC822`/`partial()`；批间无 sleep |
| C 失败 | 缺失 UID 单封重试一次 → `fetch_failed(missing)`；畸形 → `fetch_failed(malformed)`；批次 NO 整批重试一次 → 逐封隔离；全 NO → `FolderFetchError`；全缺失不算文件夹失败；abort/OSError 不写半批；认证/NO 不重试 |
| D 索引 | 冷/热；mbox 指纹命中/失效；eml 逐文件命中/失效；overwrite 清 sink；未选格式保留；根路径入键；损坏/锁/不可写 → 禁用并内容扫描；并发 WAL；写失败不影响备份 |
| E 重连 | abort/BYE/OSError 重试；5 次耗尽；成功清零；认证/NO 不重试；退避停止立即生效；LIST 重连后重跑；UIDVALIDITY 不变续跑剩余；变化重扫远端、复用本地、不重复写；二次变化文件夹失败；写盘错误不重连 |
| F 停止 | 每批前/每封后检查；在途批丢弃；停止后无新请求；单账户停止不影响其他账户；停止优先于重连 |
| G 并发 | 6–7 个假账户并发；每账户同一时刻至多 1 个在途请求；无共享锁/等待；停止一个不影响其余；无跨账户限速状态 |
| H UI | 第二行格式与顺序；30 秒窗口；60 秒 ETA；`—` 阈值；0 如实显示；合计 ≥2 下载账户；扫描/连接/重连/停止无速率；部分完成黄色常驻；停止图标优先；冷却含 partial；列表底部跟随/上移不抢 |
| I 诊断 | 10 秒 sample；phase/account_end/run_end 立即；run ID 共享；字段齐全；隐私白名单；14 天只删 `diag-*`；写失败禁用继续；CLI 不写 |
| J FAQ | 菜单打开；Q1–Q4 文案；只读滚动；单例；Esc；不连服务器/不读凭证/不写设置 |
| K CLI | 参数、退出码、正常 stdout 不变；无诊断文件；批量/重连策略与 GUI 共用核心；文件夹失败继续 |

### 12.3 现场诊断协议

#### 账户与范围

- 仅 2 个账户：A = 真正需要备份的大账户；B = 用户自己的 QQ 邮箱（几乎无参考价值，只验证多账户独立）。
- 6–7 并发只由假服务器覆盖，不要求用户找 6–7 个真实账户。
- 不连接任何未授权服务器；所有现场运行由用户自己发起。

#### 三次运行

| 运行 | 条件 | 通过标准 |
|---|---|---|
| R1 冷运行 | 升级后首次运行；索引无该账户/根的快照；持续 ≥30 分钟且 ≥1,000 封，或直到全部 pending 完成（不足 1,000 封则记录实际数） | 无崩溃/卡死；`run-*.log` 有账户结束；`diag` 有连续 sample；消息不丢不重 |
| R2 停止续跑 | R1 跑 ≥5 分钟后，用 GUI 单账户「停止」；等待干净收尾；立即重新开始 | 停止后无新请求；重启只取剩余消息；已备份不重复写；`written`/索引不造成漏信 |
| R3 热运行 | R1/R2 完成后立即再跑一次（远端无变化） | `pending ≈ 0` 或只剩已知取信失败；`scan_local_ms ≤ 20%` R1；无大量重扫 |

B 账户：与 A 同时跑一次，验证停止 A 不影响 B、A/B 的 run/统计独立；不要求吞吐。

#### 数据包

用户回传：

1. 该次运行的全部 `diag-*.jsonl`。
2. 对应的 `run-*.log`。
3. 6 行上下文：
   1. 应用版本与安装包构建时间。
   2. 账户数量与每账户大致邮件量（用 A/B 标签，不写真实邮箱）。
   3. 网络环境与服务商。
   4. 运行日期、R1/R2/R3 标签与总时长。
   5. 观察到的症状（速度曲线、报错、停止是否即时）。
   6. 是否已把 35 网页端收取范围改为「全部邮件」。

不要求用户上传邮件、`.eml`、`.mbox`、密码或截图中的邮件内容。

#### 基线

- 优先用旧版本 GUI `run-*.log` 的账户结束行：`备份 N 封，跳过 M 封，用时 M:SS` → `N / 秒数` 得封/分。
- CLI 旧日志只有文件夹总字节、没有消息数；没有可用消息数时记「无基线」，不强行比较。
- 没有旧版日志（首次使用/已清理）→ 记「无基线」，只能用绝对吞吐与降速规则，不做 80% 判定。

#### 阈值

1. **热扫描**：同一账户/根的 R3 `scan_local_ms` ≤ R1 `scan_local_ms` 的 20%。
2. **吞吐**：同一账户、可比网络与时间段的 R1/R2 吞吐 ≥ 旧版基线的 80%（封/分）。
3. **持续降速**：对每个连续下载段（≥15 分钟）：
   - 丢弃前 5 分钟预热；
   - 把剩余时间按前后三等分，最后三分之一的 30 秒字节速率中位数 ÷ 前三分之一中位数 ≥ 0.50；
   - 任意 10 分钟窗口内，至少 40 个有效 10 秒 sample 的字节速率中位数 < 该运行峰值 5 分钟速率的 25% → 判持续降速。
4. **正确性**：R1–R3 与假服务器套件均无丢信、重复写、错误跳过、停止失效、重连丢/重、崩溃/卡死。
5. **CLI**：退出码/正常输出不变。
6. **卸载器**：勾选删除设置时索引消失，备份目录不动。

#### 判定与回滚

**硬回滚**（任一出现）：

- 消息丢失或重复写入。
- 索引导致错误跳过（本地有索引但远端有、本地缺）。
- 停止不生效或停止后仍发新请求。
- 重连导致丢信/重复写。
- 崩溃、卡死、无法退出。
- CLI 参数/退出码/正常 stdout 回归。
- 卸载器触碰备份目录。
- 索引或诊断失败影响备份。
- 诊断泄露邮件内容/凭证。

**发布前修复**（不硬回滚，但不得发布）：

- 吞吐 <80% 基线且没有服务端降速证据。
- 热扫描没有明显快于冷扫描。
- 批量没有减少 FETCH 次数（假服务器断言失败）。
- 第二行/图标/滚动行为错误。
- FAQ 文案与实现不符。
- 诊断字段与本文不符。

**服务端持续降速**：

- 若满足第 3 条降速规则且有 sample/日志证据，判为服务端限速；v1 标记「未解决」，**不硬回滚**（客户端无法在不对抗限流的前提下解决）。
- 记录峰值/降速比/恢复时间，作为后续共享上限或 AIMD 工单的输入；不在本次实现里临时加 sleep。

**回滚方式**：

- 重新安装上一版安装包即可；备份格式、目录、判重契约未变。
- 旧版本忽略 `index/` 与 `diag-*.jsonl`；可保留，也可手动删除。卸载器不删除备份目录。

### 12.4 rollout 建议

1. 合并后在本地跑完整套件 + `packaging\build.bat`，按 ADR 0007 人工验收安装/卸载。
2. 先在 A 账户做 R1；确认无硬回滚、吞吐/热扫描达标后，再让 B 账户与 A 并发。
3. 观察 3 次运行（R1/R2/R3）后再让其他用户升级。
4. 若现场出现持续降速，保留数据包，不在同一版本内临时加客户端 sleep；按 ADR 0013 的重评条件另开决策。
5. 不强制用户删除旧备份或旧日志；新版本自动兼容。

## 13. 实现工单

依赖顺序：

```text
WP-1 → WP-2 → WP-3 → WP-4 → WP-5 → WP-6
WP-7（FAQ，独立）
WP-8（文档/卸载器/ADR，依赖 WP-2、WP-6、WP-7）
WP-9（全量验收/现场数据包，依赖全部）
```

每个 WP 完成即跑对应测试；WP-1 是后续所有工作的基础，不得并行改 `download_messages` 与 `scan_folder`。

每个 WP 的 Definition of Done：实现 + 本节列出的测试通过 + `py_compile` + 相关 `pytest -m "not gui"`/`-m gui` 回归 + 不新增运行时依赖、CLI 参数或设置项。

### WP-1：核心 UID 扫描、批量取信与失败语义

**范围**

- `imapbackup312.py`：`SCAN_PAGE_UIDS`、`FETCH_BATCH_*`、`FolderScan`、`BatchStats`、`DownloadProgress`、`FolderFetchError`、`BackupWriteError`；`scan_folder` 改 UID 分页；`download_messages` 改 UID 批量、解析、失败矩阵、`written`、`progress`、`on_batch`。
- `tests/fakes.py`：UID 假服务器与注入。
- 更新所有受影响的现有测试与调用点（CLI/GUI 暂时可先适配新返回类型，GUI 的完整接入在 WP-4）。

**验收测试**

- 第 12.2 节 A、B、C 组。
- 现有按封 fetch 的断言改为按批；`scan_folder` 返回值从 `dict` 改为 `FolderScan` 的所有调用点更新。
- `uv run pytest -m "not gui" -q` 通过；`py_compile` 通过。

**依赖**：无。

### WP-2：本地扫描索引

**范围**

- `imapbackup312.py`：`INDEX_DIR`、`DB_NAME`、`IndexStats`、`ScanIndex`、`IndexWriter`；schema/PRAGMA/失效矩阵/写后登记/并发/失败降级。
- `tests/conftest.py`：`isolate_index` autouse fixture。
- CLI 与 GUI 的本地扫描调用点接入（GUI 可在 WP-4 完成完整接线）。
- `packaging/imapbackup-gui.iss` 的删除逻辑与文案可在此 WP 或 WP-8 做；建议 WP-2 做删除逻辑，WP-8 做文案复核。

**验收测试**

- 第 12.2 节 D 组。
- 冷/热基准：同一临时目录第二次扫描走索引；`scan_local_ms` 下降。
- 删除 DB 后自动重建；损坏/锁/只读目录不影响备份。
- 多线程 WAL 并发无 `database is locked` 泄漏。
- `uv run pytest -m "not gui" -q` 通过。

**依赖**：WP-1。

### WP-3：有界重连与 CLI 集成

**范围**

- `imapbackup312.py`：`ReconnectStats`、`ReconnectStopped`、`ReconnectAuthError`、`run_with_reconnect`、`connect_for_reconnect`；CLI `main` 的 LIST、扫描、下载循环接入；`FolderFetchError` 继续语义。
- 假服务器断线/UIDVALIDITY 注入。
- CLI 不新增参数/输出/退出码。

**验收测试**

- 第 12.2 节 E、F、K 组。
- 退避用 monkeypatch 去掉真实等待；断言 2/8/32/128/300 上界与全抖动范围。
- 停止在退避 0.2 秒切片内生效。
- `uv run pytest -m "not gui" -q` 通过。

**依赖**：WP-1、WP-2（重连续跑要走索引命中路径）。

### WP-4：GUI worker 接入与部分完成

**范围**

- `imapbackup_gui.py`：`_backup_account` 两遍流程、索引、重连、`DownloadProgress`、事件契约、`_on_message_event`/`_on_account_done` 新参数、`AccountStats`、partial 图标与冷却。
- 失败计数与账户结果文案。
- 保持 ADR 0012 的独立运行/停止/退出模型。

**验收测试**

- 第 12.2 节 C/F/G/H 中与 worker/图标/部分完成相关的断言。
- GUI 假服务器测试：取信失败 → partial；文件夹失败 → partial；停止 → stopped 优先；正常 → done；冷却含 partial；单账户开始绕过冷却。
- `uv run pytest -m gui -q` 在有显示环境通过。

**依赖**：WP-1、WP-2、WP-3。

### WP-5：第二行速率/ETA 与列表滚动

**范围**

- `imapbackup_gui.py`：`SpeedTracker`、ETA、第二行分段、1 秒 tick、100ms 列表合并、`see` 策略、账户切换恢复。
- 不改变第一行信息栏。

**验收测试**

- 第 12.2 节 H 组。
- 纯函数测试：30 秒窗口、5 秒/3 封阈值、60 秒 ETA、格式、合计 >=2、0 如实显示。
- GUI 测试：底部跟随、上移不抢、切换账户到底部、1 秒 tick 不改 `yview`。
- `uv run pytest -m gui -q` 通过。

**依赖**：WP-4。

### WP-6：诊断 JSONL

**范围**

- `imapbackup_gui.py`：`Diagnostics`、10 秒 sample、phase/account_end/run_end、run ID、字段、隐私、轮转、失败禁用。
- `tests/conftest.py`：`isolate_log_dir`。
- CLI 明确不写。

**验收测试**

- 第 12.2 节 I 组。
- 用临时 `LOG_DIR` 断言文件内容/字段/节奏/隐私；14 天轮转只删 `diag-*`。
- 写失败（只读目录/异常注入）后备份继续。
- `uv run pytest -m gui -q` 通过。

**依赖**：WP-4、WP-5（速率/ETA 字段来自同一套计算）。

### WP-7：菜单栏「常见问题」

**范围**

- `imapbackup_gui.py`：`帮助` 菜单、FAQ Toplevel、静态文案、单例/Esc/滚动/只读。

**验收测试**

- 第 12.2 节 J 组。
- 打开 FAQ 前后设置文件 mtime 不变；无 IMAP/凭证调用。
- `uv run pytest -m gui -q` 通过。

**依赖**：无（可与 WP-1–6 并行，但合并前要避免菜单栏冲突）。

### WP-8：文档、卸载器、ADR 收口

**范围**

- `README.md` Fork Notes。
- `packaging/imapbackup-gui.iss` 的 `DeleteSettings` 删除 `index` 与文案。
- `docs/adr/0013-fixed-batching-no-adaptive-throttle.md` 已在规格阶段创建；本 WP 只复核链接与内容。
- 版本号不擅自变更。

**验收测试**

- 文档链接可打开；README 覆盖批量/索引/重连/部分完成/诊断/FAQ/CLI。
- 安装器静态检查：`DeleteSettings` 包含 `index`、不包含备份目录；`AppId`/`AppMutex` 未变。
- 人工安装/卸载验收（ADR 0007）。
- `uv run pytest -q` 通过。

**依赖**：WP-2、WP-6、WP-7。

### WP-9：全量验收与现场数据包

**范围**

- 跑第 12.1 节全部命令。
- 准备用户现场数据包说明（第 12.3 节），不代替用户运行。
- 汇总假服务器结果与 R1/R2/R3 阈值判定。
- 输出 rollout/rollback 结论。

**验收测试**

- 第 12.2 节 A–K 全绿。
- 现场数据满足 12.3 阈值，或明确记录「无基线/服务端持续降速/未解决」。
- 硬回滚条件均未触发。

**依赖**：WP-1–WP-8。

## 14. 已知限制

1. **合成 Message-Id 碰撞**：无 Message-Id 且 FROM/TO/CC/DATE/SUBJECT 相同的邮件会合成同一个 ID，只保证至少一封被下载；本 effort 不修复，保持存量契约。
2. **在途 FETCH 不可中断**：停止/重连要等当前响应或超时；不会写半批。
3. **无客户端公平调度**：6–7 账户各自竞争服务端份额；某账户分得少只能靠诊断发现。
4. **限速维度未知**：35 云邮不公开限额；v1 不探测、不预检、不阻断，只用固定批量与诊断。
5. **`COMPRESS=DEFLATE` 未尝试**：服务端能力与收益不确定，不在 v1。
6. **无远端 UID 游标**：每次运行仍要远端扫描；本地索引只降低本地成本，不减少远端 LIST/SEARCH/FETCH 头部请求。
7. **写盘非原子**：mbox/eml 局部失败可能留下半封，依赖下次按格式判重复位；强杀也可能留下未登记文件。
8. **索引是缓存**：删除/损坏/禁用只会变慢，不会影响正确性；索引崩溃窗口靠指纹失效恢复。
9. **诊断仅 GUI**：CLI 用户没有 JSONL；需要现场数据时用 GUI 运行。
10. **诊断不含邮件内容**：无法从诊断还原具体哪封失败；只能看到计数与类别。
11. **UIDVALIDITY 缺失**：服务器不提供时无法确认选中态，该文件夹按失败跳过；不再尝试重扫。
12. **首次连接不重连**：初始 DNS/连接/认证失败沿用现有失败路径，不进入 5 次预算。
13. **大附件内存**：8 MiB 批量 + 单封超限整封读取；多账户同时遇到超大邮件时内存可能上升，v1 不引入流式 `partial()`。

## 15. 未决问题

无。16 个决策的结论已全部落入本规格；实现中若发现新决策，另开决策记录，不在代码里自行决定。
