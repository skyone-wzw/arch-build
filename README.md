# arch-build

基于 GitHub Actions 的个人 pacman 软件源。支持两类包：

- **本地包**：`local/<name>/PKGBUILD`，版本由 PKGBUILD 中的 `pkgver`/`pkgrel` 决定。
- **外部包（AUR）**：在 `packages.toml` 中声明，版本与提交哈希锁定在 `packages.lock`。

构建产物和 pacman 数据库发布在固定的 GitHub Release tag 上，因此客户端的
`Server` 地址永远不需要修改；数据库同时以 `archlinux/<repo>.db` 的形式提交到
`main` 分支。

## 工作流

| 工作流 | 触发方式 | 作用 |
| --- | --- | --- |
| `.github/workflows/check-update.yml` | 每周定时 / 手动 | 运行本地包的 `update.sh`，通过 AUR RPC 检查外部包，全部检查成功且有变化时创建/更新 PR |
| `.github/workflows/build-packages.yml` | 推送到 `main`（相关路径变化）/ 手动 | 在 `archlinux:base-devel` 容器中构建版本变化的包，上传 Release，更新并提交数据库 |

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
local/<name>/PKGBUILD    # 本地包
local/<name>/update.sh   # 可选：版本检查脚本
packages.toml            # AUR 包列表 + 可选仓库配置
packages.lock            # 自动生成：AUR 包的版本与提交锁定
scripts/check_update.py  # 更新检查
scripts/build.py         # 构建、发布、数据库更新
archlinux/<repo>.db      # 自动生成并提交：pacman 数据库
```

## 本地包

```
local/my-package/
├── PKGBUILD
└── update.sh       # 可选
```

如果存在 `update.sh`，检查工作流会在包目录下执行它。约定：

- 脚本自行下载上游元数据，并在有新版本时**原地修改 PKGBUILD**
  （至少更新 `pkgver`、`pkgrel` 和校验和）；
- 没有更新时正常退出（退出码 0）；
- 出错时以非 0 退出，工作流会报错并在 PR 描述中列出（其他包仍会继续检查）。

版本的变化由工作流通过比较脚本执行前后的 `pkgver`/`pkgrel` 得出；如果 PKGBUILD
被修改但版本未变，也会在 PR 中说明。

只要任意一个包检查失败（`update.sh` 出错、AUR 查询失败等），本次就不会创建 PR，
工作流会失败；修复问题后手动重新运行检查即可。

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

版本变化时还会用 `git ls-remote` 取得对应提交，写入 `packages.lock`。构建工作流
按锁定的提交检出 AUR 仓库，保证可复现。`packages.lock` 由脚本生成，不要手动编辑。

### 新增 AUR 包的顺序

1. 编辑 `packages.toml`；
2. 手动运行一次 **Check package updates** 工作流（或等待每周计划任务）；
3. 合并生成的 PR，推送会触发构建，并自动补上 `packages.lock` 条目。

## 构建与发布

构建工作流只构建数据库中版本不一致的包：

- 本地包：比较 PKGBUILD 的 `epoch:pkgver-pkgrel` 与数据库中的版本；
- AUR 包：比较 `packages.lock` 中的版本与数据库中的版本；
- 若数据库不存在（首次运行），会构建全部包。

构建完成后：

1. `repo-add` 合并新包，生成 `<repo>.db` / `<repo>.files`（gzip 压缩的 tar，
   pacman 会自动识别）；
2. 包文件和数据库上传到固定 tag 的 GitHub Release（覆盖同名资产），并清理数据库
   中不再引用的旧版本资产；
3. 数据库提交回 `main` 分支（提交信息带 `[skip ci]`；工作流本身也只监听
   `local/`、`packages*.toml`、`scripts/` 等路径，不会自触发）。

构建是「全有或全无」的：任意一个包构建失败，本次就不会更新数据库、不会上传
Release 资产，也不会提交数据库；修复后手动重新运行 **Build packages** 即可。
手动触发时可以勾选 `force`，强制重新构建所有包。

### 移除包

直接删除 `local/<name>` 目录，或从 `packages.toml` 移除 AUR 包即可（AUR 包的
`packages.lock` 条目会由下一次更新检查清理）。构建工作流通过数据库中的 `%BASE%`
字段识别不再由任何来源提供的包，使用 `repo-remove` 从 `<repo>.db` / `<repo>.files`
中删除，并按新的数据库清理 Release 中不再引用的包文件。过程同样是全有或全无的。

## 首次启用

1. 仓库 **Settings → Actions → General → Workflow permissions** 选择
   **Read and write permissions**；
2. 同一页面勾选 **Allow GitHub Actions to create and approve pull requests**
   （创建更新 PR 需要）；
3. 若 `main` 启用了分支保护，需要允许 `github-actions[bot]` 直接推送数据库提交，
   或放宽对 `archlinux/**` 的限制；
4. 首次合并后构建工作流会创建 `pacman` Release 并构建所有包。之后客户端即可使用。

## 本地调试

```bash
# 只打印将要构建的包，不需要 root
python scripts/build.py --dry-run

# 只做更新检查（会真的运行 local/*/update.sh 并改写 packages.lock）
python scripts/check_update.py
```

## PGP 校验

PKGBUILD 中声明了 `validpgpkeys` 时，构建脚本会先以构建用户身份从
`keys.openpgp.org` / `keyserver.ubuntu.com` 导入对应公钥，再交给 `makepkg` 校验
签名。若密钥无法获取且确认可以跳过，可在 `packages.toml` 中设置：

```toml
[build]
skip_pgpcheck = true
```

## 已知限制

- 构建环境只安装官方仓库依赖；AUR 包之间互相依赖时，需要自行把被依赖的包也加入
  `packages.toml`，并注意构建顺序（本地包先构建，AUR 包按配置顺序构建）。
- 只构建 `x86_64`。
- 更新检查按版本判断；AUR 维护者修改 PKGBUILD 但未提升版本时不会触发更新。
- 移除整个包（AUR 条目/本地包目录）会自动清理；AUR 拆分包中仅上游移除某个子包时，
  该子包不会被自动删除（本地 PKGBUILD 的子包变化可以正确清理）。
- AUR PKGBUILD 以无 sudo 权限的普通用户身份执行，构建脚本也不会向该进程传递任何
  token；依赖由 root 在构建前安装，因此 PKGBUILD 无法提权。仍请只添加可信的 AUR 包。
