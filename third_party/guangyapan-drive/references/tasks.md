# 后台任务与恢复

`run` 返回 `batch_id` 后，以及查询、恢复或停止已有写任务时读取本文件。
所有命令保留 [SKILL.md](../SKILL.md) 中当前实例的身份绑定；实例 ID 模式替换配置目录参数。

## 查询与判断顺序

写请求启动后台 worker 后立即返回，初始 `batch_state=RUNNING`。先查询一次：

```bash
node bin/guangyapan.mjs status --batch-id BATCH_ID --agent-name AGENT_NAME --agent-profile-dir AGENT_PROFILE_DIR
```

`status` 不启动 worker，也不重建远端任务。每次响应依次处理：

1. 用户已要求停止时，按末节跟踪停止结果，不自动恢复；登录相关状态按 [SKILL.md](../SKILL.md) 处理，取消后停止当前工作流。
2. 查询本身失败、没有返回 `batch_state`：处理查询错误，不据此判定任务失败或重新执行 `run`。
   `BATCH_NOT_FOUND` 表示当前 Agent 实例的本地记录中没有该批次；核对 `batch_id`、原实例绑定及本地记录是否还在。
   保持提交时的实例绑定，不更换请求 ID 自动重建任务。
3. `batch_state` 为 `SUCCEEDED`、`PARTIAL`、`FAILED` 或 `STOPPED`：交付逐项结果并结束轮询，不自动重新提交终态批次。
4. 非终态但 `code` 不为 `OK`：按下方“恢复”处理，不因 `batch_state=RUNNING` 而继续空轮询。
5. 正常运行：普通任务每 1 秒查询，云添加每 5 秒查询。

`batch_state` 为 `RUNNING` 不代表 worker 一定仍在执行；上传暂停或校验待恢复时，也可能保留此状态。

## 结果与进度

- `data.items[]` 是操作结果，用 `operation_id` 对应原请求的 `id`；检查各项 `state`，保留成功项。
  批次 `PARTIAL` 时分别说明成功、失败和未执行部分，不把整个批次报告为全部成功。
  `state=SKIPPED`、`code=OPERATION_NOT_EXECUTED` 表示批次终止时该项尚未执行；
  `error.cause_code` 是导致批次终止的原因，不能将它当作本项执行错误，也不自动重提。
- `transfer_state` 只描述本地上传/下载，云添加等远端操作的 `NONE` 是正常值。
  判断批次是否结束始终以 `batch_state` 为准。失败或部分成功终态将仍在运行或暂停的传输记为 `FAILED`，
  主动停止则记为 `STOPPED`；已完成的传输保留 `SUCCEEDED`，尚未开始的传输在失败终态可保留 `IDLE`。
  非终态的 `PAUSED` 表示传输等待恢复，不能当作整个批次已结束。
- 批次百分比读取 `data.progress.percent`，单项读取 `data.items[].remote_progress`。
  没有百分比时展示状态；直链云添加执行中只返回 `0` 时显示“进行中”，不虚构进度。
- 云添加默认 2 分钟无进展时返回 `data.progress.warning`，说明长时间无进展、后台仍在查询，可稍后查看；
  `remote_last_changed_at` 是上次状态或进度变化时间，`remote_last_observed_at` 是最近查询时间。提示不代表远端失败，不重新提交。
- 服务端任务成功终态显示 `100`，失败终态显示失败。错误原因以返回值为准，不自行推测。
  存在时读取 `data.error.task_id`、`data.error.remote_code`、`data.error.message`；
  `data.error.remote_reason_reported=false` 表示未提供服务端原因。逐项失败也检查该项的 `error`。
- `INTERNAL_ERROR` 存在底层错误码时，直接命令错误读取 `data.cause_code`，批次状态读取 `data.error.cause_code`。
  该字段用于定位原因；是否恢复仍按批次终态与 `retryable` 判断，不自行推测原始错误消息。

移动、复制、分享转存及云添加删除所附带的文件删除都是远端异步任务；worker 保存任务 ID 并跟踪，Agent 通过 `status` 查询。
`data.items[].remote_task_id` 是任务 ID，不能当作文件 ID。

连续云添加先逐项提交、保存任务 ID，再由后台合并查询未完成任务；某项下载慢不会阻塞同组后续资源提交。
拿到 `remote_task_id` 后可告知“云添加任务已提交”，只有该项 `state=SUCCEEDED` 才报告完成。
组内已经成功、失败或部分完成的结果会先保存；其他项仍运行时，批次保持 `RUNNING`，不能重提已结束项。
同组结束后才执行后面的其他操作；有失败或部分完成则汇总为失败/部分成功并停止后续操作。

云添加的阶段与结果码分别读取：

- `data.progress.phase=CLOUD_COLLECTION_RUNNING`：已进入云端任务跟踪阶段，非终态时按上面的间隔查询；这是进度阶段，不是错误码，不能单独用于判断成败。
- `CLOUD_COLLECTION_FAILED`：服务端已报告某项云添加失败，读取该项的 `error`；同组其他项可能已成功，批次也可能为 `PARTIAL`，按逐项结果交付，不自动重提。
- `CLOUD_COLLECTION_PARTIAL`：云添加只完成部分内容，或组内结果只能汇总为部分成功；保留成功结果，说明未完成部分，不报告全部完成，也不自动重提。

移动成功的 `data.items[].result.file_id` 为原文件 ID，`parent_id` 为目标目录。
复制成功的结果包含 `source_file_id`、`parent_id`，但 `created_file_id=null`、`created_file_id_reported=false`；
只有后续确实需要副本 ID 时，才浏览或搜索目标目录。

## 等待超时的区别

`remote_last_changed_at` 记录任务状态或进度最后变化的时间，`remote_last_observed_at` 记录
最后一次有效任务响应，`remote_last_observed_status` 保存该响应的状态。任务暂时不可见不会重置变化时间。
`remote_no_progress=true` 且 `progress.warning.code=CLOUD_TASK_NO_PROGRESS` 只表示警告，后台继续查询；
达到跟踪超时后才返回 `CLOUD_TASK_NO_PROGRESS_TIMEOUT` 并暂停本地跟踪。

- **Agent 主动等待云添加 10 分钟**：结束本次主动查询，保留 `batch_id` 并告知仍未确认完成。
  不发送 `stop`，不重复 `run`；后台仍可能继续跟踪。用户之后询问时再查询一次。
- **`CLOUD_TASK_NOT_VISIBLE`**：已有服务端任务 ID，但连续 10 分钟在任务列表不可见；worker 已退出，
  批次保留 `RUNNING`。这是恢复分支，按下一节只续跟踪原任务。
- **`CLOUD_TASK_NO_PROGRESS_TIMEOUT`**：云端任务可见但连续 10 分钟无进展，已暂停本地跟踪；批次保留 `RUNNING` 和任务 ID。
  不声称远端失败或已取消；允许按下方上限恢复一次原任务跟踪，不重复提交。
- **`REMOTE_TASK_STATUS_UNAVAILABLE`**：移动、复制、转存或文件删除任务连续 10 分钟取不到有效状态，
  可恢复原任务的查询；不是远端失败或撤销。正常运行时 `remote_status=UNKNOWN` 会继续跟踪。

worker 的任务可见性和状态查询超时可由部署配置调整。主动等待到期与收到明确恢复错误分别处理，
不能仅凭经过的时间推断远端状态。

## 恢复

### 错误对应动作

API 请求失败时，`code` 是 Skill 错误码，原服务端码保留在直接错误的 `data.api_code`、批次的 `data.error.api_code`
或逐项的 `error.api_code`。当服务端返回对应数字时，CLI 使用以下映射：`142 → REMOTE_DIRECTORY_NOT_FOUND`、
`143/146 → REMOTE_FILE_NOT_FOUND`、`154 → REMOTE_TARGET_NOT_DIRECTORY`、`160 → REMOTE_TARGET_CONFLICT`。
这些是响应码转换规则，不表示每项操作都有独立的触发场景；缺失目录也可能返回 `146`，此时按 `REMOTE_FILE_NOT_FOUND` 处理。
以实际响应为准，不按操作名称改写错误码，也不为触发 `142` 重复请求。
其他数字码使用 `REMOTE_API_ERROR_<api_code>`，例如 `REMOTE_API_ERROR_351`、`REMOTE_API_ERROR_352`、
`REMOTE_API_ERROR_353`；这是业务错误的兜底命名，不表示缺少错误原因。
按实际返回的消息、`retryable` 和任务状态处理，不仅凭数字猜测原因或重新提交。

- `UPLOAD_VALIDATION_PENDING`：零字节秒传已提交、文件结果尚未确认；恢复原批次校验，不新建上传。
- `UPLOAD_PAUSED`：恢复原批次，由 CLI 使用保存的断点或已提交任务继续处理。
- `WORKER_EXITED`：非终态且 `retryable=true` 时，使用原请求恢复；不持续调用 `status` 等待它自行启动。
- `PREFLIGHT_SELECTION_CHANGED`：并发预检的文件选择不同，原计划已保留；重试原请求，让 CLI 使用首次固定的选择，不换请求 ID。
- `CLOUD_TASK_NOT_VISIBLE` / `CLOUD_TASK_NO_PROGRESS_TIMEOUT` / `REMOTE_TASK_STATUS_UNAVAILABLE`：`retryable=true` 时恢复原请求，只跟踪已有远端任务。
- 其他非终态且 `retryable=true` 的执行错误：处理返回的原因后恢复原请求，遵守下方恢复次数上限。
- `LOGIN_REQUIRED`：按 [登录规则](../SKILL.md) 完成登录后才能恢复；账号与原批次不一致时不强行恢复。
- `UPLOAD_RESULT_UNKNOWN`：停止自动重试，说明远端可能已生成文件，先核对结果再由用户决定是否重新上传。
  不能只凭同名零字节文件认定本次成功。
- `REMOTE_NAME_CONFLICT`：按返回文案说明同名文件或目录冲突；目录上传提示“远端已存在同名目录，请改名或先清理”。
  不自动合并或清理远端目录，也不换请求 ID 重试。
- `LOCAL_TARGET_CONFLICT`：本地下载目标已存在或被替换，不覆盖、不合并；由用户决定换用空目录或处理冲突。
- `WORKER_RECOVERY_LIMIT_REACHED` / `CLOUD_TASK_RECOVERY_LIMIT_REACHED`：不可重试的 `FAILED` 终态，停止恢复，
  不通过更换请求 ID 绕过上限。其他不可重试错误也停止自动恢复。

### 如何恢复原请求

重新执行 `run`，通过标准输入传入首次提交的完整 JSON，保持相同 `request_id`、操作内容与实例绑定。
CLI 预检尚未提交的项并恢复原批次；已接受或已结束的云添加复用原计划，只继续查询已有任务，
不重新解析资源或读取本地种子，不打开确认页。若首次请求已保存为 `original-request.json`，可原样复用：

```bash
node bin/guangyapan.mjs run --agent-name AGENT_NAME --agent-profile-dir AGENT_PROFILE_DIR < original-request.json
```

没有保存为文件时，将上下文中的原 JSON 原样传入标准输入即可。
无法取得完整原请求或本地批次已不存在时，停止自动恢复并说明情况，不猜请求、不换 ID 重建，也不要求用户提供 CLI JSON。
已完成项由 CLI 复用，不拆成新请求重复执行。

### 恢复次数

`data.recovery` 返回当前次数与上限：普通 worker 恢复最多一次，云添加不可见或停滞后的续跟踪共计一次，
不消耗普通恢复额度。每次恢复后继续按返回状态判断，不自行重置计数或循环重建。
`status` 会清理已退出 worker 的遗留心跳和失效锁，不终止仍存活的 worker；它也可能将达到上限的批次固化为失败终态。

## 停止与远端执行

仅在用户明确要求停止时执行：

```bash
node bin/guangyapan.mjs stop --batch-id BATCH_ID --agent-name AGENT_NAME --agent-profile-dir AGENT_PROFILE_DIR
```

按返回结果处理：

- `USER_STOPPED` 且 `batch_state=STOPPED`：本地已按用户要求停止，是正常停止结果，不重试或恢复。
  停止可在本次调用中同步完成，无需先出现 `WORKER_STOP_PENDING`。
- `WORKER_STOP_PENDING`：停止请求已记录，但 worker 尚未退出或执行锁尚未释放；继续用 `status` 查询停止结果，不恢复任务。
  这是有条件出现的中间态，不能将它当作停止完成。
- `BATCH_NOT_FOUND`：当前实例找不到本地批次，按上方查询规则核对；不能据此声称云端任务已取消。
- 已为 `SUCCEEDED`、`PARTIAL`、`FAILED` 或 `STOPPED` 的批次保留原状态与错误码；
  再次 `stop` 不会改写为 `USER_STOPPED`。例如成功批次仍返回 `code=OK`、`batch_state=SUCCEEDED`，属于正常幂等结果。

`stop` 只停止本地执行与跟踪；当前接口不支持取消已提交的云添加、移动、复制、转存等云端任务。
创建请求在停止后才返回任务 ID 时，worker 会记录该 ID 并查询状态，不会删除云端任务或文件。

- 主动停止结果保留 `USER_STOPPED` 或 `ACCOUNT_LOGGED_OUT`；`data.remote_execution_may_continue=true` 时明确告知“本地已停止，云端可能继续执行”，退出码为 1。
  无远端未确认执行的正常 `USER_STOPPED`，`stop` / `status` 退出码为 0；失败或部分成功终态退出码为 1。
- `COMPLETED`、`FAILED` 或 `PARTIAL_COMPLETED`：云端已结束，按实际状态说明，不能说“已取消”。已生成的文件保留。
- `status` 只核对停止后的云添加状态，不恢复原任务、不新建任务。任务不可见、请求失败或缺少任务 ID 均不证明远端已结束。

`data.remote_stop_supported=false` 明确表示不支持云端取消。不能用删除任务记录接口代替取消；退出登录也不会撤销云任务。
用户明确要求删除云添加任务时，用 [`cloud_collection_delete`](operations.md#删除云添加任务) 单独提交，默认保留文件。
本地失败或主动等待超时同样不保证撤销远端操作。

## 本地记录保留

日常 CLI 命令结束时自动清理超过 7 天未更新的终态批次及过期孤立预检目录，当前命令返回的批次保留。
未完成、可恢复、worker 仍存活或远端执行结果未确认的记录不自动删除，以免丢失恢复和核对依据。
