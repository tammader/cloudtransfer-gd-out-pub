# cloudtransfer-gd-out

云盘之间的大文件搬运流水线，跑在 GitHub Actions 上。

技术栈：`rclone` + `ffmpeg` + `OpenList`（alist 分支）。

## Workflow

| 名称 | 作用 | 触发 |
|---|---|---|
| `gd-out2` | 源目录里的大文件**无损对半切分**后上传目标目录，校验通过再归档原件 | 每 3 小时 |
| `od2-dbjm` | 中转目录 → alist 挂载目录**单向同步**，去重上传，确认到位后清源 | 每 6 小时 |
| `out2-ty-sync` | 归档目录 → 另一网盘**单向同步**，两处齐全后归档 | 每 3 小时 |
| `gen-manifest` | 生成目录清单（编号 / 名称+大小），供其他流水线比对 | 每 3 小时 |
| `ty-upload-test` | 上传 / 下载往返测速 | 手动 |

所有 workflow 都支持 `workflow_dispatch` 手动触发，`apply=false` 时只演练不落地。

## 文件

| 文件 | 说明 |
|---|---|
| `tools/gd_out2_split.py` | 分批下载 → ffmpeg 无损对半切 → 上传 → 归档；非视频 / 切不开则跳过，绝不二进制切块 |
| `tools/gd_token.py` | 用 refresh_token 换新的 access_token 并写回 rclone 配置 |
| `tools/out2_to_dbjm.py` | 同步到 alist 挂载目录：同名同大小跳过、同名不同大小记为冲突不覆盖 |
| `tools/out2_ty_sync.py` | 网盘间单向同步 + 完成归档 |
| `tools/gen_manifest.py` | 目录清单生成 |
| `tools/ty_upload_speedtest.py` | 往返测速（测完自删测试文件，可用 `keep` 保留） |
| `tools/alist_bootstrap.py` | 在 CI 里拉起 OpenList、注入存储定义、登录取管理 token |

## Secrets

| 名字 | 内容 |
|---|---|
| `RCLONE_CONF` | rclone 配置（远端凭据） |
| `ALIST_STORAGES` | 一组 OpenList 存储定义 |
| `ALIST_STORAGES_TY` | 另一组 OpenList 存储定义 |

全部经 GitHub 加密 Secret 注入，**不落盘、不进仓库**。

## 安全约定

- 所有 workflow **仅由 `schedule` / `workflow_dispatch` 触发**，不接受外部 PR 触发；仓库接受 PR 时也不会自动执行工作流。
- 每个 workflow 声明 `permissions: contents: read` —— `GITHUB_TOKEN` 只读，无法写回仓库。
- 运行时产生的 token 一律先 `::add-mask::` 再输出，日志里不会出现明文凭据。
- 仓库中**不含任何凭据**；脚本只从环境变量 / Secret 读取。

## 本地运行

脚本本身不绑定 CI，本机装了 rclone 也能跑：

```bash
export ALIST_URL=http://127.0.0.1:5244
export ALIST_TOKEN=<your-token>
python3 tools/gd_out2_split.py --apply --max-total-gb 5
```
