# 操作参数与结果规则

组织 `run` 请求时读取对应操作章节。请求封装、实例绑定和登录处理见 [SKILL.md](../SKILL.md)，
写任务的查询、恢复和停止见 [后台任务与恢复](tasks.md)。

## 通用约定

- 每项包含字符串 `id`、`type`；`id` 在批次内唯一，长 1–128，只含字母、数字、点、下划线、冒号和连字符。
- 一个请求最多 100 项、JSON 最多 1 MiB；只读与写操作分批提交。只传本文件列出的业务字段。
- ID 使用字符串。本机路径使用绝对路径；`name`、`new_name` 是长 1–255 的单段名称，不能包含路径分隔符。
- 可选字段不用则省略，不传 `null`。根目录字段的空字符串与省略含义按具体操作处理。
- 各项结果在 `data.items[]`，按 `operation_id` 匹配请求的 `id`；`SUCCEEDED` 和 `PARTIAL` 均需读取 `result`，
  部分成功时结合逐项状态和错误判断。
  失败项读取自己的 `code`、`message` 和 `error`，保留同批其他成功结果。

以下“结果”均指该操作的 `data.items[].result`，外层 `data.items[]` 是操作列表。

## 浏览、搜索与权益（只读）

### 参数与约束

- `list`：必填字符串 `remote_parent_id`；可选整数数组 `file_types`、整数 `page`、`page_size`。
  `""` 表示根目录一层，目录 ID 表示该目录一层，`"*"` 表示全盘平铺文件；`"*"` 仅用于此操作，不能作为写入目标。
  指定非空 `file_types` 时只返回对应分类的文件，不混入目录；全盘平铺同样只返回文件。
- `search`：必填字符串 `keyword`；可选非空字符串 `remote_parent_id`、整数数组 `file_types` 与整数 `page`、`page_size`。
  省略父目录表示全盘搜索，限定目录时传非空 ID。
- `search_share`：必填字符串 `share_url`、`keyword`；可选字符串 `code`、整数 `page`、`page_size`。
  链接与提取码规则另见下方“分享与转存”。
- `return_text`：必填字符串 `remote_id`。每批最多 10 项，只支持合法 UTF-8 文本；
  单文件及批次累计均最多 1 MiB，`content.length` 均不超过 200,000。
- `account_rights`：无额外字段。

分页默认 `page=0`、`page_size=100`；页码从 0 开始，每页 1–100 项。
使用实际目录 ID 时会校验目录存在且类型正确。分类浏览通过服务端文件列表直接分页，不递归遍历目录。

`file_types` 省略或空数组表示全部类型，最多 20 项。可用整数为：`1` 图片、`2` 视频、`3` 音频、
`4` 文档、`5` 压缩包、`6` 字幕、`7` 字体、`8` 安装包、`9` 种子、`10` 代码、`11` 其他。
`0` 表示未指定/未知，不能用作“全部”。

无关键词的分类浏览用 `list`，例如全盘图片第一页：

```json
{"id":"images","type":"list","remote_parent_id":"*","file_types":[1],"page":0,"page_size":50}
```

有实际关键词时使用 `search`，可叠加 `file_types`。不要用 `.` 或 `*` 模拟匹配全部文件；
展示搜索结果使用返回的名称、ID 和父目录，不为补齐展示主动逐目录查询。用户要求查看目录内容时才浏览对应目录。

### 结果读取

- 浏览与搜索：文件在结果的 `items[]`，完整路径为 `data.items[].result.items[]`。
  字段为 `id`、`name`、`type`（`file` / `directory`）、`size`（字节）、`parent_id`；后续操作的 `remote_id` 取这里的 `id`。
  `parent_id=''` 表示根目录；`type=file` 且 `size=0` 表示空文件，目录大小不能用于判断目录是否为空。
  分页读取 `page`、`page_size`、`total`、`has_more`；
  合法空结果为 `items: []`、`total: 0`，`total=null` 表示总数未提供。
- 文本：读取结果的 `content`、`artifact_id`、`name`、`bytes`、`sha256`。
- 权益：读取 `space`、`upload_traffic`、`high_speed_download_traffic`、`membership`、`capabilities` 等分组。
  流量 `reported=false` 或数值为 `null` 时说明未提供，不补成 0；字节额度是十进制字符串，避免丢失精度。

`return_text` 错误码：

- `NOT_A_TEXT_FILE`：目标是目录或非文件，应先选择具体文本文件，不对目录请求下载。
- `TEXT_LIMIT_EXCEEDED`：单文件或批次文本超过读取限制，不把截断内容当作完整文本。
- `TEXT_DECODE_FAILED`：内容无法按合法 UTF-8 解码，不把二进制内容当作文本。

## 文件传输与整理（写入）

### 参数与约束

- `upload`：必填字符串 `local_path`、`remote_parent_id`，支持本机文件或目录。
- `download`：必填字符串 `remote_id`、`local_dir`，支持远端文件或目录。
- `move` / `copy`：必填字符串 `remote_id`、`target_parent_id`。
- `rename`：必填字符串 `remote_id`、`new_name`。
- `create_folder`：必填字符串 `remote_parent_id`、`name`。

目标父目录字段必传，空字符串表示根目录。下载目录不存在时 CLI 自动创建；目标文件或同名目录已存在，
或被并发任务先创建时，返回 `LOCAL_TARGET_CONFLICT`，不覆盖、不合并，换用空目录或先处理冲突。
不同目标名称可共用 `local_dir`。零字节文件由后端秒传，以 CLI 最终结果为准。
首次请求的首项为目录上传或下载时，预检发现同名冲突会直接拒绝，不创建批次；
后续操作、恢复及预检后的并发变化仍由 worker 检查，可能在批次结果中返回冲突。
目录下载恢复只复用本任务已保存身份的目录；目录被替换，或中断前仅记录了创建意图而无法确认归属时，
停止并报告冲突，不接管同名目录。
下载文件校验成功后，临时文件删除失败不影响成功结果；本地可能保留 `.part` 文件，不能据此重复下载。

目录上传不合并远端同名目录。遇到 `REMOTE_NAME_CONFLICT` 时，提示“远端已存在同名目录，请改名或先清理”；
先核对远端内容，由用户决定改名或清理后再上传，不自动删除残留目录。恢复原批次会复用已记录的远端目录 ID。

在根目录创建“资料”的单项示例，放入请求的 `operations[]`：

```json
{"id":"op-1","type":"create_folder","remote_parent_id":"","name":"资料"}
```

创建子目录时把父目录 ID 放入 `remote_parent_id`，目录名用 `name`；重命名使用 `new_name`。
不把底层 API 的 `parent_id` / `parentId` 套入请求。

### 结果读取

单文件上传的文件 ID 为结果的 `file.fileId`；目录上传返回 `root_id`、`uploaded_files`、`uploaded_bytes`。
下载返回 `root_id`、`downloaded_files`、`downloaded_bytes`；建目录返回 `fileId`，重命名返回 `file_id`、`name`。
移动、复制的任务状态与副本 ID 规则见 [后台任务与恢复](tasks.md)。
上传创建结果不明时不自动重建，也不只凭远端同名零字节文件认定本次成功。

## 分享与转存

### 参数与约束

- `create_share`（写入）：必填 `remote_ids`；可选字符串 `title`、`code`，整数 `validity_seconds`、
  `share_type`、`download_type`。
- `save_share`（写入）：必填字符串 `share_url`、`target_parent_id`；可选字符串 `code` 和数组 `remote_ids`。
  目标目录空字符串表示根目录；省略 `remote_ids` 时，CLI 固定首次预检可见的分享根文件，恢复时不追加后来加入的文件。
- `search_share`（只读）：参数见“浏览、搜索与权益”。

`remote_ids` 必须是 1–100 项的不重复字符串数组，每个 ID 长 1–40；不用时省略，不能传空数组。
分享链接使用 HTTPS，域名须在配置的光鸭分享域名范围内，路径包含 `/s/分享ID`。
无提取码分享可省略 `code`；有提取码时必须提供，链接的 `code` 查询参数已包含时无需重复传。
不要把有提取码的分享当作无提取码分享重试。转存先校验分享有效性和提取码，再检查是否由当前账号创建；
同一账号下，错误提取码返回 `SHARE_CODE_INVALID`，提取码正确但属于本人分享时返回 `SHARE_SELF_RESTORE`。

用户未指定分享标题时省略 `title`，CLI 按 Web/Android 的命名格式生成，名称长度采用 Android 规则：
先取 `remote_ids` 第一项名称的前 112 个 UTF-16 单元（不拆开代理对），单项直接使用该名称；
多项再追加完整的 `等N个文件`（N 为所选项数，不统计目录内文件）。超长名称不单独保留扩展名。
用户指定标题时传 `title`，长度为 1–255，不采用默认名称截短规则。
例如选择 `报告.pdf` 和 `图片`，默认标题为 `报告.pdf等2个文件`。同批先重命名再分享时使用重命名后的名称；同一请求重试沿用首次生成的标题。

创建分享默认随机提取码；仅按用户明确要求调整：

- `share_type=0`：无提取码；`share_type=1`：随机提取码，默认值。
- `share_type=2`：自定义提取码，必须同时提供 4 位字母或数字 `code`；其他模式不能传 `code`。
- `download_type` 默认 `1`（普通流量），仅在用户明确要求免登录下载时传 `0`。
- `validity_seconds` 默认 `0`，范围 `0`–`31536000` 秒。

分享不限制转存次数，不提供转存次数设置。

### 结果与特殊处理

创建结果的 `title` 是本次实际提交的分享标题。创建成功后交付 `shareUrl`，存在非空 `code` 时同时交付提取码。
`share_type=0` 的结果没有提取码，`code` 可省略；不补造提取码，也不要把 `shareCode` 当作提取码。
同时说明：“分享链接已创建，内容需经分享审核，审核通过前文件可能暂不可见。”
创建成功只表示链接生成，不等待审核、不主动搜索验证，也不重复创建。

分享内搜索没有可见匹配项，不能证明分享为空、创建失败或已失效。刚创建的分享可能尚未完成审核，
不归因为短暂索引延迟，不承诺审核时长。接口不返回审核状态，不能宣称“审核中”“已通过”或“已拒绝”。
只有用户明确要求核验时才搜索，仍无结果则说明情况并结束核验，不持续轮询。

- `SHARE_CONTENT_UNAVAILABLE`：转存前暂无可见文件，未创建远端任务；可稍后重试原请求，不自动轮询或重新创建分享。
- `TOO_MANY_SHARE_ROOTS`：分享根文件超过 100 项，需明确选择后用 `remote_ids` 提交。
- `SHARE_INVALID`（服务端 `201`）：告知分享已失效。
- `SHARE_CODE_INVALID`（服务端 `209`）：向用户索取或修正提取码，再提交请求。
- `SHARE_SELF_RESTORE`：分享属于当前账号，不支持转存回自己；校验通过也不会创建转存任务。

参数改变时生成新的 `request_id`；只有完全相同的请求才复用原 ID。转存提交后按 [任务规则](tasks.md) 跟踪。

## 云添加

### 参数与约束

- `resolve_cloud_collection`（只读）：必须且只能提供字符串 `url` 或 `torrent_path`。
- `cloud_collection_add`（写入）：同样二选一，另必填字符串 `remote_parent_id`；可选字符串 `selection_mode`、`new_name`。

`url` 支持 HTTP、HTTPS、FTP、eMule、磁力、Thunder；`torrent_path` 为本机 `.torrent` 普通文件的绝对路径，
文件大于 0 且不超过 10 MiB。父目录空字符串表示根目录，`new_name` 是单段名称。
FTP 由服务端解析和下载，Skill 本地不连接 FTP。

`selection_mode` 默认 `recommended`，使用服务器推荐文件；用户明确要求全部下载时才传 `all`。
不支持选择部分文件，不传 `file_indexes`。返回 `CLOUD_COLLECTION_SELECTION_REQUIRED` 时询问是否全部下载，
不自行全选；切换选择模式属于新请求，使用新的 `request_id`。
同一请求恢复时保留首次确定的文件选择，不随服务器推荐列表变化。

### 结果读取

解析结果包含 `res_type`、`url`、`name`、`size`、`files` 和推荐索引等信息。
解析结果的 `size=0` 表示未知，不能据此认定资源为空；返回的索引不作为 `cloud_collection_add` 请求字段提交。
云添加结果与进度按 [后台任务与恢复](tasks.md) 处理，以最终任务状态为准。
同批连续云添加通过预检后，后台逐项提交并立即保存任务 ID，再集中查询未完成任务（每次最多 100 个 ID）。
提交成功表示已进入云端队列，不表示下载完成；新资源可用新请求添加，不等待已有云任务完成。
同组单项远端失败或部分完成不放弃其他已提交任务；全部可确认结果落盘后汇总，失败组不执行后续其他类型操作。
创建任务时明确拒绝的资源（如 `353` 文件违规）记录为该项失败，不重试；
预检失败时先按错误处理，未经有效解析和文件选择的资源不会提交。提交结果未知时停止新增提交、核对已知任务，不重发未知项。

### 删除云添加任务

`cloud_collection_delete`（写入）：必填 `task_ids`，为 1–100 个不同的云添加任务 ID；
可选布尔值 `delete_files`，默认 `false`，只删除任务记录并保留云盘文件。
ID 从原云添加结果的 `task_id` 或对应项的 `remote_task_id` 取得，不能用文件 ID、本地 `batch_id` 或猜测值替代。
无法确定目标任务时先澄清，不传空数组、不按状态清空全部任务。

```json
{"id":"delete-1","type":"cloud_collection_delete","task_ids":["TASK_ID"],"delete_files":false}
```

只有用户明确要求同时删除任务生成的云盘文件时才用 `delete_files=true`；CLI 会跟踪服务端返回的文件删除任务，
确认完成后再返回成功。结果的 `file_delete_status` 枚举如下：

- `NOT_REQUESTED`：未请求删除云盘文件，仅处理任务记录。
- `COMPLETED`：服务端返回了文件删除任务，且已跟踪确认完成。
- `NO_TASK`：服务端未创建文件删除任务，不能据此声称删除了文件。
- `UNCONFIRMED`：文件删除缺少确认依据，不能声称文件已删除。

文件删除失败时，任务记录可能已删除；逐项的 `cloud_delete_receipt.task_ids` 保留服务端确认的记录删除结果，不能报告全部未执行。

结果区分本次删除与任务已不存在，按 `task_results[]` 中的 `task_id`、`status`、`code` 和 `message` 逐项说明：

- `DELETED` / `deleted_task_ids`：服务端回执确认本次删除。
- `NOT_FOUND` / `CLOUD_COLLECTION_TASK_NOT_FOUND` / `absent_task_ids`：回执未确认，但随后查询当前账号任务列表已不存在；无需再次删除记录。
  不推断是过期、被清理还是被其他客户端删除，也不声称本次删除了它。
- `NOT_DELETED` / `not_deleted_task_ids`：随后查询确认仍存在，读取该项 `error`，不自动重删。
- `UNKNOWN` / `unconfirmed_task_ids`：查询失败或返回不完整，读取该项 `error`；不能当作不存在或再次提交删除。

仅删除记录时，全部为 `DELETED` 或 `NOT_FOUND` 即成功；含仍存在或未确认项则为 `CLOUD_COLLECTION_DELETE_PARTIAL`。
这里的成功表示目标记录均已不存在，不表示本次删除了每条记录。即使批次为 `SUCCEEDED`，也应按
`deleted_task_ids` 和 `absent_task_ids` 分别报告“本次删除”和“已不存在”；`CLOUD_COLLECTION_TASK_NOT_FOUND` 不单独视为批次失败。
`delete_files=true` 时，缺失任务记录无法证明文件已删除，返回 `file_delete_status=UNCONFIRMED` 和部分成功，
按用户原意核对文件，不重建云添加任务或反复删除记录。
部分成功时批次和操作项的 `error` 提供汇总原因，`task_results[]` 提供任务级细节，不自动重提；
`REMOTE_RESULT_UNKNOWN` 表示请求结果未知，先核对客户端任务列表，不重复删除。
删除记录不会取消远端下载；要停止本地跟踪，另按 [停止规则](tasks.md#停止与远端执行) 执行 `stop`。
