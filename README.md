# arch-build

基于 GitHub Actions 的个人 pacman 软件源。支持两类包：

- **本地包**：`local/<name>/PKGBUILD`，版本由 PKGBUILD 中的 `pkgver`/`pkgrel` 决定。
- **外部包（AUR）**：在 `packages.toml` 中声明，版本、提交哈希（及拆分包的 `pkgbase`）锁定在 `packages.lock`。

构建产物和 pacman 数据库发布在固定的 GitHub Release tag 上，因此客户端的
`Server` 地址永远不需要修改；数据库同时以 `archlinux/<repo>.db` 的形式提交到
`main` 分支。

## 工作流

| 工作流 | 触发方式 | 作用 |
| --- | --- | --- |
| `.github/workflows/check-update.yml` | 每周定时 / 手动 | 运行本地包的 `update.sh`，通过 AUR RPC 检查外部包，全部检查成功且有变化时创建/更新 PR |
| `.github/workflows/build-packages.yml` | 合并 `automated/update-packages` PR / 手动 | 在 `archlinux:base-devel` 容器中构建版本变化的包，上传 Release，更新并提交数据库 |

整体流程：

```
定时检查 ──> 更新 PR ──合并──> main ──> 构建容器 ──> GitHub Release（固定 tag）
                                              └────> archlinux/<repo>.db（提交到 main）
```

## 客户端配置

在 `/etc/pacman.conf` 末尾加入（把 `<owner>/<repo>` 换成实际仓库；仓库名默认就是
数据库文件名）：

```ini
[arch-build]
SigLevel = Optional TrustAll
Server = https://github.com/<owner>/<repo>/releases/download/pacman
```

然后 `sudo pacman -Sy` 刷新数据库即可。仓库中的包和数据库都没有签名，所以使用
`SigLevel = Optional TrustAll`（或 `SigLevel = Never`）。

- `pacman` 是 Release tag，可通过 `packages.toml` 的 `[repo].tag` 修改。
- 仓库名/数据库文件名默认取 GitHub 仓库名，可通过 `[repo].name` 修改，且必须与
  pacman.conf 中的段落名一致。

## 目录结构

```
local/<name>/PKGBUILD     # 本地包
local/<name>/update.sh    # 可选：版本检查脚本
packages.toml             # AUR 包列表 + 可选仓库/构建配置
packages.lock             # 自动生成：AUR 包的版本、pkgbase 与提交锁定
pyproject.toml / uv.lock  # Python 依赖与 CLI 入口（uv 管理）
scripts/setup-builder.sh  # 构建容器工具链与 builder 用户初始化
src/arch_build/           # 更新检查、构建规划、数据库更新与发布模块
archlinux/<repo>.db       # 自动生成并提交：pacman 数据库
```

## 本地包

```
local/my-package/
├── PKGBUILD
└── update.sh       # 可选
```

如果存在 `update.sh`，检查工作流会在包目录下执行它。约定：

- 脚本自行下载上游元数据，并在有新版本时**原地修改 PKGBUILD**
  （更新 `pkgver`、重置 `pkgrel=1` 和校验和）；
- 没有更新时正常退出（退出码 0）；
- 出错时以非 0 退出。

版本的变化由工作流通过比较脚本执行前后的 `pkgver`/`pkgrel` 得出；如果包目录下有
文件改动但版本未变，也会照常创建 PR（构建阶段会自动跳过版本未变的包）。

只要任意一个包检查失败（`update.sh` 出错、AUR 查询失败等），本次不会写入
`packages.lock` 也不会创建 PR，工作流直接失败；修复问题后手动重新运行检查即可。

## 外部包（AUR）

```toml
[aur]
packages = [
    "yay-bin",
    "visual-studio-code-bin",
]
```

检查工作流通过 AUR RPC v5 批量查询版本：

```
https://aur.archlinux.org/rpc/v5/info?arg[]=yay-bin&arg[]=...
```

版本变化时会用 `git ls-remote` 获取对应 `PackageBase` 仓库的提交，写入
`packages.lock`。构建工作流按锁定的提交检出 AUR 仓库，保证可复现。`packages.lock`
由脚本生成，不要手动编辑。

对于 **AUR 拆分包**，只需在 `packages.toml` 中填写任意子包名（也可同时填写多个
子包名）：检查脚本会自动解析并记录其 `pkgbase`，构建阶段按 `pkgbase` 去重后只
构建一次，并将产出的全部子包加入仓库。

### 新增 AUR 包的顺序

1. 编辑 `packages.toml`；
2. 手动运行一次 **Check package updates** 工作流（或等待每周计划任务）；
3. 合并生成的 PR 会触发构建，并自动补上 `packages.lock` 条目。

## 构建与发布

构建会写入 Release 和数据库，因此除了手动触发外，只会在
`automated/update-packages` 的 PR 合并后自动运行。其他改动（手动修改 PKGBUILD、
删除包等）需要手动运行 **Build packages**。

构建工作流只构建数据库中版本不一致的包：

- 本地包：比较 PKGBUILD 的 `epoch:pkgver-pkgrel` 与数据库中的版本；
- AUR 包：比较 `packages.lock` 中的版本与数据库中的版本；
- 若数据库不存在（首次运行），会构建全部包；
- `pkgbase` 变化时（即使包名和版本不变）会重建，以刷新数据库中的 `%BASE%`，
  而不会误判为包已移除。

构建过程细节：

- `scripts/setup-builder.sh` 会在 `builder` 用户的 `~/.makepkg.conf` 中关闭默认
  `-debug` 拆分包（`OPTIONS+=(!debug)`）；
- 每构建完一个目标，产物会自动加入容器内的临时 pacman 仓库（且同时挂载已发布的
  Release 仓库），因此后续包若在构建期依赖先前构建或已发布的自定义/AUR 包，会被
  `pacman -S` 自动解析安装；
- 包文件名中若包含 GitHub Releases 会改写的字符（如 `epoch` 的 `:` 或 `+`），
  会在执行 `repo-add` 与上传前统一规范化为 `_`，确保数据库 `%FILENAME%` 与
  Release 资产名完全一致。

构建完成后：

1. `repo-add` 合并新包，生成 `<repo>.db` / `<repo>.files`（gzip 压缩的 tar，
   pacman 会自动识别）；
2. 包文件（及未来可能存在的 `.sig`）和数据库上传到固定 tag 的 GitHub Release
   （覆盖同名资产），并按严格白名单清理 Release 中不在数据库内的全部多余资产；
3. 数据库提交回 `main` 分支（提交信息带 `[skip ci]`，且由 `GITHUB_TOKEN` 推送，
   不会再次触发工作流）。

### Release 一致性校验与修复

数据流严格单向：`packages.lock` / `local/*` → 仓库内 `archlinux/<repo>.{db,files}` →
GitHub Release。每次构建前（非 `--force` / `--no-publish`）会通过 GitHub API 的
资产 SHA256 `digest` 做两级校验：

1. **仓库内 db 与 Release db 不一致**（缺失或 SHA256 不匹配）：视为严重异常，立即
   中止构建并输出差异，便于人工排查；确认原因后手动触发带 `force` 的构建，会从
   空白数据库全量重编并清空 Release 中的所有旧文件。
2. **Release db 与 Release 资产列表不一致**：自动恢复——对比数据库中每个包的
   `%FILENAME%` 与 `%SHA256SUM%`，缺失或哈希不匹配的包会自动加入本次构建计划，
   不在白名单内的多余文件会自动清理（即使没有版本更新也会执行清理）。

构建是「全有或全无」的：任意一个包构建失败，本次就不会更新数据库、不会上传
Release 资产，也不会提交数据库；修复后手动重新运行 **Build packages** 即可。
手动触发时可以勾选 `force`，从空白数据库强制全量重编并重置 Release。

### 移除包

直接删除 `local/<name>` 目录，或从 `packages.toml` 移除 AUR 包即可（AUR 包的
`packages.lock` 条目会由下一次更新检查清理）。删除操作不会自动触发构建，需要手动
运行 **Build packages**。构建工作流通过数据库中的 `%BASE%` 字段识别不再由任何来源
提供的包，使用 `repo-remove` 从 `<repo>.db` / `<repo>.files` 中删除，并按新的数据库
清理 Release 中不再引用的包文件。过程同样是全有或全无的。

## 首次启用

1. 仓库 **Settings → Actions → General → Workflow permissions** 选择
   **Read and write permissions**；
2. 同一页面勾选 **Allow GitHub Actions to create and approve pull requests**
   （创建更新 PR 需要）；
3. 若 `main` 启用了分支保护，需要允许 `github-actions[bot]` 直接推送数据库提交，
   或放宽对 `archlinux/**` 的限制；
4. 初始推送后手动运行一次 **Build packages**：它会创建 `pacman` Release 并构建
   所有包。之后客户端即可使用。

## 本地调试

使用 [uv](https://docs.astral.sh/uv/) 运行命令：

```bash
# 只打印构建与 Release 对账计划，不需要 root（离线调试可加 --no-publish）
uv run arch-build --dry-run
uv run arch-build --dry-run --no-publish

# 只做更新检查（会运行 local/*/update.sh，仅全部成功时改写 packages.lock）
# 全部成功时退出码为 0，任意检查失败时为 1
uv run arch-check-update

# 运行单元测试与代码检查
uv run pytest
uv run ruff check .
```

## PGP 校验

PKGBUILD 中声明了 `validpgpkeys` 时，构建脚本会先以构建用户身份从
`keys.openpgp.org` / `keyserver.ubuntu.com` 导入对应公钥（带超时控制），再交给
`makepkg` 校验签名。若密钥无法获取且确认可以跳过，可在 `packages.toml` 中设置：

```toml
[build]
skip_pgpcheck = true
```

## 已知限制

- 仅自动解析官方源依赖，以及已加入本仓库的构建期依赖（本地包先构建，AUR 包按
  `packages.toml` 顺序构建）；若某个 AUR 包存在运行时 AUR 依赖，请手动将该依赖也
  加入 `packages.toml`。
- 只构建 `x86_64`。
- 更新检查按版本判断；AUR 维护者修改 PKGBUILD 但未提升版本时不会触发更新。
- 移除整个包（AUR 条目/本地包目录）会自动清理；AUR 拆分包中仅上游移除某个子包时，
  该子包不会被自动删除（本地 PKGBUILD 的子包变化可以正确清理）。
- AUR PKGBUILD 以无 sudo 权限的普通用户身份执行，构建脚本也不会向该进程传递任何
  token；依赖由 root 在构建前安装，因此 PKGBUILD 无法提权。仍请只添加可信的 AUR 包。
