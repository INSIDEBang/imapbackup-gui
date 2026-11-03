# ADR 0014：正文取信超时先拆单封，仍超时只放弃该封

Status: accepted

> 部分被取代：拆批粒度由 ADR 0016 取代——超时后把受影响各封的批次上限**对半**缩小，而不是直接
> 逐封重取。本 ADR 的其余结论（只有 `TimeoutError` 进入该路径、只有单独取仍超时才计入 `timeout`
> 并本轮放弃、`BatchStalled` 重建链路但不烧重连预算）继续有效。

## Context

现场一次重跑在 2619/5814 封处整个账户判失败：正文 `UID FETCH` 读取超时
（`TimeoutError: The read operation timed out`），重连循环把预算烧光后
`raise last_error`，账户失败。

根因链：

- 读超时只会在**连续 60 秒收不到任何字节**后触发（进程级 `socket.setdefaulttimeout`），
  不是「总传输慢」。它指向一封读不动的邮件（服务端卡住的索引、巨型附件）或半死链路，
  而不是正常的大邮件。
- 重连后的续跑集合是 `pending − written`；超时的批次没写入任何一封，所以每次重连都
  重新取**同一批**。确定性卡死不会因为重试同一批而自愈。
- 一次连续失败最多 5 次，每次还要叠加全抖动退避（2/8/32/128/300 秒上界）。现场表现
  就是约 10 分钟卡在同一批上，最后账户失败。

ADR 0013 的固定批量没有「单封隔离」以外的降级手段；NO/BAD 批次已有逐封隔离，但
读超时根本不返回 tagged 响应，走不到那条路径。

## Considered Options

- **调小读超时或加大重试次数**：不改变「同一批永远重试」的性质，只是把 10 分钟换成
  另一个数字。
- **超时直接跳过整批**：一批最多 8 封，为了 1 封读不动丢掉 7 封好邮件，失败面过大。
- **每批都逐封取信**：往返次数 ×8，违背 ADR 0013 的吞吐目标；超时是罕见事件，不值得
  为它常态化。
- **超时后拆单封重取；单封仍超时才放弃该封，并让这种刻意的重建连接不烧重连预算
  （采纳）**：好邮件照常备份，README/规格里的 5 次预算留给真正的断线。
- **把所有 `OSError` 都当超时处理**：`ConnectionResetError` 等是真实断线，拆单封
  不会让它变好，还会把断线误记成取信失败。

## Decision

### 归属与状态

- 核心新增 `BatchStall`，由 CLI `backup_folder` 与 GUI `run_plan` **每个文件夹运行**
  创建一个，跨重连尝试保留：
  - `split`：下一轮要单封取信的 Message-Id；
  - `skipped`：本轮放弃的 Message-Id。
- 只有 `TimeoutError` 进入这条路径；abort/OSError/BYE 仍按规格 §5.7 的重连预算处理。

### 一次超时的处理

1. 多封批次超时：本批不写盘；把批内全部 Message-Id 记入 `split`；记 WARNING
   （几封、合计字节）；抛 `BatchStalled` 重建连接。
2. 单封批次超时：
   - 该封还不在 `split` 里（自带一批的超大/未知大小邮件，或第一次就独占一批）：
     记入 `split`，抛 `BatchStalled`；下一轮再单独试一次。
   - 该封已在 `split` 里：说明单独取仍然读不动。从 `split` 移除、记入 `skipped`；
     计入新的失败桶 `fetch_failed(timeout)`；记 WARNING（Message-Id、UID、大小）；
     抛 `BatchStalled` 重建连接。
3. `skipped` 的消息在本文件夹本轮不再请求；下一轮运行新建 `BatchStall`，自然重试。
4. `run_with_reconnect` 见到 `BatchStalled`：仍视为可重试的连接级错误（重建链路，
   imaplib 在读超时后不可信），但**不增加连续失败预算**，也不叠加抖动退避（读超时
   本身已经耗掉 60 秒）。真正的拨号失败仍按原预算和退避处理。
5. `stall=None`（未跟踪的调用方）保持旧行为：原样抛出 `TimeoutError`。这样可以避免
   「忘记传 tracker 导致永不耗尽预算」的无限重连。

### 记录

- 失败桶 `timeout` 与 missing/no_bad/malformed 并列：进入 `DownloadProgress.failures`、
  `fetch_failed`、`unreported_failures`；文件夹结束 WARNING 增加 `timeout=%d`。
- 重连回来补取成功时，既有换桶/清除逻辑同样适用于 timeout 桶。
- GUI `account_done` 事件末尾追加 `fetch_failed_timeout`，诊断字段表增加
  `fetch_failed_timeout`；两者都只写计数，不写邮件内容。

## Consequences

- 单封读不动的邮件不再拖垮整个账户：其余消息照常备份，账户以「部分完成」收口。
- 每个卡死邮件的代价从「10 分钟退避 + 账户失败」降到「一次 60 秒读超时 + 一次即时
  重建链路」；最坏情况下每封卡死邮件各付一次。
- 拆批会把一批好邮件从 1 次往返变成最多 8 次；这只发生在超时之后，是罕见路径。
- `BatchStalled` 是 `OSError` 子类，保证分类仍可重试；预算豁免由
  `run_with_reconnect` 显式识别类型完成，不靠异常文本。
- 被放弃的消息仍留在 pending，下一轮运行会重试；进度线/ETA 把它算作未完成。
- 不改变 ADR 0013 的固定批量、无自适应限速结论；也不新增设置项。
