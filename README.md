# Galaxy Watch BakaSU boot 云端编译

在 GitHub Actions 中下载原厂 boot 和三星开源包，按输入顺序合并源码，集成 BakaSU，编译并生成 boot 镜像。**只制作 boot，不生成单独的 vbmeta.img，不打包完整固件，不自动刷机。**

不同型号、地区、尺寸、蓝牙/LTE 版本请提供各自匹配的原厂 boot 和源码。构建使用原 boot 内核中提取的完整配置，保留模板头部与三星尾部；型号配置、SoC、内核版本、clang 版本从用户输入的源码与 boot 读取。

## 脚本布局

`scripts/` 根目录只放 GW7、GW8 共用的 Python 脚本：镜像处理、版本号重建、模块证书核验、结果收集、源码链接查询，以及安全归档解压、BakaSU/工具链准备和配置校验等辅助模块。机型入口按目录区分：`scripts/gw7/` 和 `scripts/gw8/` 分别包含 `source_archives.py`、`prepare_kernel.py`、`compile_kernel.py`；GW8 另外包含 `abi_symbols.py`。机型目录中的脚本文件名不再重复 `gw7` 或 `gw8`，工作流直接调用对应目录即可。

GW7、GW8 都不固定某一固件、基础内核、Android/KMI 版本或 `-4k` 等后缀。共用的 `scripts/kernel_release.py` 根据输入 boot 的完整版本和原配置动态重建版本字符串，兼容原配置后缀位于 Android/固件标识之前或之后的情况。系统升级后请同时换用匹配的新 boot 和源码；仍会核对源码基础版本与原 boot，并要求最终 `kernelrelease` 完全一致。GW8 的独立流程见 [GW8 说明](docs/GW8.md)。

## 许可证

本项目原创构建脚本、测试、工作流和文档采用 [GNU GPL v3](LICENSE)。随仓库提供的 `scripts/avbtool.py` 保留 AOSP 原有 MIT 许可证及版权声明，详见 [第三方许可说明](THIRD_PARTY_NOTICES.md)。构建时下载的三星内核、BakaSU、工具链和其他依赖分别适用其自身许可证。

## 1. 配置签名密钥

默认构建会使用 `SHA256_RSA4096` 对 **boot 自身**添加 AVB 签名。这里的 AVB 信息位于 boot 内部；不会输出 `vbmeta.img`。

**未勾选“不签名（不推荐）”时，必须配置 `AVB_PRIVATE_KEY_B64`。** 缺失、为空、Base64/PEM 无效、私钥有密码，或不是 RSA-4096 私钥，都会在下载 boot/源码和编译前失败；不会自动生成替代 AVB 私钥，也不会自动降级为不签名。只有主动勾选该选项才允许不提供密钥。

在仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret** 添加：

```text
Name: AVB_PRIVATE_KEY_B64
Secret: 你的 RSA-4096 私钥 PEM 文件的 Base64 文本
```

工作流读取 `${{ secrets.AVB_PRIVATE_KEY_B64 }}`，将它以同名环境变量提供给密钥检查和 boot 签名步骤。请添加到 **Secrets**，而非同名的普通 **Variables**；只设置 Variables 不会被读取。用于验证原厂 `.ko` 的公开证书以及构建自动生成的模块签名密钥，与这个 boot AVB 私钥是不同用途。

### 在本机生成新私钥并转换为 Base64

PowerShell（需要已安装 OpenSSL；先用 `openssl version` 确认可用）：

```powershell
$gwSigningDir = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.gw-signing'
New-Item -ItemType Directory -Force -Path $gwSigningDir | Out-Null
$gwPrivateKey = Join-Path $gwSigningDir 'gw-avb-rsa4096.pem'
if (Test-Path -LiteralPath $gwPrivateKey) {
    throw '密钥已存在，请使用下面的已有私钥步骤，或选择新的文件名。'
}

openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out $gwPrivateKey
if ($LASTEXITCODE -ne 0) { throw '生成 RSA-4096 私钥失败。' }
openssl pkey -in $gwPrivateKey -check -noout
if ($LASTEXITCODE -ne 0) { throw '私钥校验失败。' }

$gwKeyBase64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($gwPrivateKey))
$gwKeyBase64 | Set-Clipboard
```

私钥存储在用户目录的 `.gw-signing/gw-avb-rsa4096.pem`。剪贴板里的完整 Base64 文本粘贴到 GitHub Secret **`AVB_PRIVATE_KEY_B64`**。

Linux / WSL（没有 OpenSSL 时先执行 `sudo apt-get install openssl`）：

```bash
(
  set -eu
  umask 077
  mkdir -p "$HOME/.gw-signing"
  gw_private_key="$HOME/.gw-signing/gw-avb-rsa4096.pem"
  if [ -e "$gw_private_key" ]; then
    echo '密钥已存在，请复用已有私钥或选择新的文件名。' >&2
    exit 1
  fi
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out "$gw_private_key"
  openssl pkey -in "$gw_private_key" -check -noout
  base64 -w 0 "$gw_private_key"
  printf '\n'
)
```

将最后输出的完整 Base64 文本粘贴到同名 Secret。保留本机 PEM，后续构建可复用同一把密钥；不要把 PEM 或 Base64 文本提交到仓库。

### 已有私钥时转换为 Base64

在 PowerShell 读取已有 PEM 并复制 Base64 到剪贴板：

```powershell
$keyBytes = [System.IO.File]::ReadAllBytes((Join-Path ([Environment]::GetFolderPath('UserProfile')) '.gw-signing/gw-avb-rsa4096.pem'))
[Convert]::ToBase64String($keyBytes) | Set-Clipboard
```

Linux / WSL：

```bash
base64 -w 0 "$HOME/.gw-signing/gw-avb-rsa4096.pem"
```

私钥必须是未加密的 PEM RSA-4096 私钥。本地签名与三星生产签名是不同的；签名通过表示镜像内容与该密钥相符。

## 查找三星源码直链

已知手表型号和完整固件版本时，可以先打开 **Actions → Find Samsung source links → Run workflow**：

| 输入 | 示例 | 说明 |
| --- | --- | --- |
| `model` | `SM-L315F` | 手表完整型号 |
| `firmware` | `L315FXXS3BZF4` | 完整构建号，精确匹配，不自动替换为更新版本 |
| `archive_item` | 留空 | 自动查询三星索引；失败时自动从 Archive.org 查找。也可填写 `samsung-opensource-14120`、`14120` 或对应的 Archive.org 详情链接，跳过三星搜索直接定位 |

运行的 **Summary** 显示按“完整基础包 → 型号适配包 → 固件版本更新包”排列的下载链接，以及一行英文分号分隔的 `source_urls`。将这一行直接复制到下面的编译 Action。分析报告和 `source-urls.txt` 同时上传到该次运行的 Artifacts，不需要 AVB 签名 Secret。

例如 `SM-L315F` / `L315FXXS3BZF4` 对应 Archive.org 的 [`samsung-opensource-14120`](https://archive.org/details/samsung-opensource-14120)，选择基础包 `SM-L305U_Opensource.zip`、型号包 `SM-L315F_EUR_Opensource_dts.zip`，再选择文件名中精确包含 `L315FXXS3BZF4` 的更新包。不会把同条目中较早的 BZE1 更新包一起加入。

脚本读取三星公开搜索表和 Archive.org 的实际文件清单，读取小于等于 16 MiB 的型号／更新 ZIP 中的 README，核对其基础版本依赖及 Archive 提供的大小、校验值。不会下载完整基础源码包或编译内核。只识别可明确分类的基础、DTS、更新包；缺少基础包、型号适配包、版本不匹配、依赖冲突、多个候选或请求失败时，在 Summary 中说明原因并使任务失败，不生成猜测的链接。指定 `archive_item` 时跳过三星搜索，完整固件版本必须出现在归档文件名中。

三星站点返回 520、其他 5xx、403 或验证页面时，不再直接停止。脚本会先尝试已记录的型号归档线索（`data/archive-source-hints.json`），重新读取在线文件清单并精确核对完整版本；线索未命中时，再扫描 Archive.org 三星开源集合的文件索引。扫描有 10 分钟预算，必须完整扫描且没有请求错误才采用结果；扫描中断或仍无法匹配时，可填写 `archive_item` 缩小范围。成功回退后，Summary 会说明来源并折叠显示三星请求失败详情。

Archive.org 是第三方镜像，可能尚未收录某个三星条目或更新文件；链接分析通过也不代表该源码已经通过编译和实机测试。

也可以本地运行（只用 Python 标准库）：

```bash
python3 scripts/find_source_links.py \
  --model SM-L315F \
  --firmware L315FXXS3BZF4 \
  --output dist/source-links
```

需要指定归档时，追加 `--archive-item samsung-opensource-14120`。结果保存在 `dist/source-links/summary.md`、`source-links.json` 和成功时生成的 `source-urls.txt`。

## 2. 运行编译

把本仓库文件推送到 GitHub 默认分支后，打开：

**Actions → Build Galaxy Watch BakaSU boot → Run workflow**。

| 输入 | 填写方式 |
| --- | --- |
| `boot_url` | 对应系统的原厂 boot 下载直链；接受 `.img`、`.img.lz4`、`.img.gz` 内容。下载地址无需以这些后缀结尾 |
| `source_urls` | 三星源码包下载直链，用英文分号 `;` 分隔。先完整基础包，再型号适配包，再更新包；允许三个或更多，也可以只有一个完整包 |
| `bakasu_repository` | 可选；`owner/repo` 或 HTTPS Git 地址。留空使用 `https://github.com/Baka-SU/BakaSU.git` |
| `bakasu_ref` | 可选；commit 哈希、分支或 tag。留空使用所选仓库**默认分支的最新提交** |
| `unsigned` | 默认不勾选，此时必须配置 Secret **`AVB_PRIVATE_KEY_B64`**（RSA-4096 私钥 PEM 的 Base64），缺失或无效则失败。勾选后不生成新的 boot AVB，也不复制旧 boot AVB；**保留原厂三星尾部和恢复的官方模块证书，并补零至原 boot 大小**，无需签名 Secret |
| `toolchain_url` | 可选；clang 工具链压缩包直链。留空根据源码的 `CLANG_VERSION` 与 Android 分支下载 AOSP 工具链 |

示例：

```text
boot_url:
https://example.com/stock-boot.img

source_urls:
https://example.com/SM-L305U_Opensource.zip;https://example.com/SM-L315F_EUR_Opensource_dts.zip;https://example.com/BZF4-update.zip
```

URL 必须在云端无需登录就能直接下载文件；网盘预览页、需要会话 Cookie 的下载页不能作为直链。保留 URL 中的查询参数；构建报告会去掉查询字符串，避免把临时下载令牌写入 artifact。

## 3. 自动解压与覆盖规则

支持 ZIP、tar、tar.gz/tgz、tar.xz、tar.bz2，以及压缩包中继续嵌套的 ZIP/tar。每个链接按填写顺序处理；同一总包中的内嵌源码包按 ZIP/tar 成员顺序处理。

以下以一组三星源码包为例：

```text
SM-L305U_Opensource.zip
└── L305USQU3BZB4_Kernel.tar.gz
    ├── kernel/                 → work/build/src/kernel/
    ├── projects/               → work/build/src/projects/
    └── prebuilts/              → work/build/src/prebuilts/

SM-L315F_EUR_Opensource_dts.zip
└── L315FXXU3BZB4_kernel.tar.gz
    └── Kernel/
        ├── kernel/             → 覆盖相同的 src/kernel/
        └── prebuilts/          → 覆盖相同的 src/prebuilts/

BZF4-update.zip
└── L305USQS3BZF4_kernel.tar.gz
    └── Kernel/kernel/          → 继续覆盖相同的 src/kernel/
```

会自动定位外层目录中的内核根目录，保留旁边的 `projects/`、`prebuilts/` 和源码内部相对软链接。已识别为内核树后，不会继续解压源码里的测试用压缩包。第一个实际源码包必须是完整内核树；后续可以是增量目录。若同一个包中出现多个并列内核树，脚本会列出路径并停止，避免选错。

所有下载内容均记录 SHA-256；每次覆盖的同路径文件、前后哈希记录在 `source-manifest.json`。

## 4. 编译过程中做的修改

1. 从原 boot 提取 ARM64 `Image`、内嵌 IKCONFIG、原内核 release 和自签名内核模块公开证书，自动恢复模块信任。
2. 克隆完整 BakaSU Git 历史，解析选择的 ref，打印并保存完整 commit 哈希。
3. 添加 `drivers/kernelsu -> ../BakaSU/kernel`，在 `drivers/Makefile` 和 `drivers/Kconfig` 加入编译入口。
4. 基于原内核配置启用 `KSU`、`KSU_TRACEPOINT_HOOK`、`KSU_MULTI_MANAGER_SUPPORT`；关闭 manual hook、SUSFS，以及 UH/RKP/KDP/KDP_CRED/DEFEX；将恢复的官方模块公开证书加入 `SYSTEM_TRUSTED_KEYS`。
5. 安装 Ubuntu 编译依赖，下载匹配 clang，执行 `olddefconfig`、`prepare`、`Image modules`，保持原内核 release。原 ARM32 compat、模块保护和 SELinux 配置会被保留；内存较小的 runner 会自动补充 4 GiB 交换空间供 BTF 阶段使用。
6. 替换原 boot 的内核，更新 `kernel_size`，保留原头部其余字段和三星尾部。
7. 编译后从 `vmlinux` 的 `__cert_list_start/end` 符号读取实际内置信任列表，强制核对原厂模块证书确实位于信任列表及 Image 中。缺失时停止，不输出 boot。
8. 打包未签名 boot 时，在替换后的内容末尾补零至原 boot 大小。默认还会用同一个内核打包签名 boot，添加内部 AVB hash/footer，使用 Secret 的 RSA-4096 私钥签名并验签。每份镜像都单独核对内核内容、三星尾部和官方模块证书。

### 内核替换、大小与对齐

镜像按下面的结构重建：

```text
原 boot 前 4,096 字节头部（只更新 kernel_size）
+ 本次构建的完整 Image（替换原内核 payload）
+ 原内核之后、AVB 之前的三星尾部及原有填充
+ 新的 AVB/分区填充（签名）或纯零填充（不签名）
```

不会把整份原 boot 与新 Image 或另一份 boot 拼接。最终验证器独立检查实际输出：偏移 4,096 开始的内核逐字节等于构建的 Image；其后的尾部逐字节等于原尾部；头部只有 `kernel_size` 字段允许变化；最终文件大小必须等于原 boot，且是 4 KiB 的整数倍。未签名版本中，保留尾部之后必须全为零，不允许残留旧 AVB。

本例原 boot 和两种最终输出均为 `67,108,864` 字节。官方内核 payload 的末端本身不在 4 KiB 边界；原尾部开头的 `1,536` 字节填充让 `SEANDROIDENFORCE` 位于 4 KiB 边界。脚本保留这段填充并核对标记对齐，不会擅自把 Image 末端当成必须对齐的位置。

### 不签名时保留的内容

“不签名”只控制 **boot 的 AVB 重签名**，不会关闭内核模块签名保护，也不会删除三星尾部或模块信任证书：

- 原 boot 内核之后的三星尾部材料（本例为 2,336 字节，包含 `SEANDROIDENFORCE`）逐字节保留。
- 原厂 GKI 模块公开证书从原内核恢复，重新编入新内核的实际内置信任列表；签名和不签名构建均强制检查。
- 原厂 `.ko` 驱动的签名附在模块文件自身。此流程只输出 boot，不改写 vendor_boot、vendor_dlkm 或 system_dlkm 中的原厂模块及其签名。
- 原 boot 的旧 AVB hash、签名块和 footer 会去掉，因为它们对应旧内核；未签名输出的剩余空间全部补零，不声称通过旧 AVB 验证。原样保留的三星尾部也不等于三星为新内核重新签名。

本流程不需要提供原厂模块私钥；从原 boot 恢复的是公开证书。Action 不获取完整 AP、vendor_boot 或 super，因此不会重复历史本地构建对 258 个原厂模块的完整 ABI/签名核查。

默认最新版 BakaSU 会随上游更新；某次构建需要重现时，把报告里的完整 commit 填进 `bakasu_ref`。最新源码和成功启动之间仍需设备验证。

## 5. 下载结果

完成后从该次运行的 **Artifacts** 下载。默认签名构建的压缩包名称不带签名标记：

```text
gw7_boot-<BakaSU commit前12位>-<run id>.zip
```

勾选「不签名」时，压缩包名称带 `unsign`：

```text
gw7_boot-<BakaSU commit前12位>-unsign-<run id>.zip
```

其中包括：

- 签名构建的主工件包含 `boot.img`、`boot.img.lz4` 和 `boot-unsign.img`；附带的未签名版有独立的 `boot-unsign-manifest.json` 校验报告。另有一个按 UTC 时间命名的单独工件 `AP_YYYYMMDD_HHMMSS.tar.md5`，原始 tar 内只有 `boot.img.lz4`，文件尾部附加 Samsung 风格的原始大小和 MD5 信息。
- 勾选「不签名」时只提供 `boot-unsign.img`，不会生成 LZ4 或 tar。
- LZ4 内容使用 1 MiB 独立块、内容校验和及原始大小字段，生成后会解压核对；主工件中的镜像和 LZ4 均列入 `SHA256SUMS`。`.tar.md5` 的 MD5 覆盖最后一行校验值之前的全部字节，包括 tar 和三行 RBS 构建信息；`original_tar_file_size` 仍只记录 tar 大小。文件生成后会重新读取并独立验算，通过后才上传，校验值记录在 `BUILD.txt`。
- `Image`、`kernel.config`、`Module.symvers`、`System.map`：编译结果及核查材料。
- `bakasu.json`、`prepare.json`、`compile.json`、`module-trust.json`、`boot-manifest.json`、`config-check.json`、`source-manifest.json`：来源、完整 BakaSU commit、工具链、配置、原厂模块信任和镜像校验记录。
- `SHA256SUMS`、`BUILD.txt`：文件哈希与摘要。

失败时也会上传已完成的公开报告，帮助定位下载、合并、配置或编译问题。AVB 私钥和自动生成的内核模块签名私钥不会上传。没有自动发布 Release、定时更新或刷写步骤。

## 本地脚本测试

```bash
sudo apt-get install python3 python3-cryptography python3-lz4 python3-pyelftools openssl git gcc
python3 -m unittest discover -s tests -v
```

测试覆盖嵌套解压、覆盖顺序、子目录识别、目录穿越拒绝、相对软链接、BakaSU 默认分支/指定 commit、签名验签、纯未签名输出、官方模块证书保留，以及实际内置信任列表的正反例核查。

`scripts/avbtool.py` 来自 AOSP，保留原 MIT 许可头；它与本地已核验的 BZF4 打包工具一致。

## 已执行的本地验证

2026-11-23 使用真实 SM-L315F BZF4 原 boot、上述三个三星源码 ZIP 和上游默认分支提交 `63ad1ea3a90455dee9d77163cdafb028d8a90416`，完成源码自动合并、配置恢复、`Image modules` 全量编译、boot 打包和 RSA-4096 签名验签。生成的配置与历史成功构建一致。

本地还使用保留的原厂模块完成额外核查：258 个模块，14,422 条符号 CRC，0 个不一致、0 个未解析符号；60 个签名模块全部受新内核信任。这项额外核查使用了本机保存的 vendor_boot 和模块提取目录，不属于只输入 boot/源码链接的云端流程。

35 项脚本测试及 actionlint 1.7.12 语法检查通过。用历史 Image 与历史签名 salt 重打包时，也重现了已知成功 boot 的完整 SHA-256。默认新构建使用 Image SHA-256 作为 salt。补充检查确认：两种输出均为 64 MiB 且整体 4 KiB 对齐；未签名 boot 只增加零填充，三星尾部与原厂逐字节一致，官方模块证书同时位于编译内核的真实信任列表和最终 boot 内核中。测试同时覆盖整份 boot 错误拼接的拒绝、新内核缩小时清除旧内核残留、尾部填充及 AVB 残留拒绝，以及最终 boot 的 LZ4 压缩与哈希。

这次本地生成的签名 boot SHA-256 为 `bad177b496fbffe12a0c72b4f4337cc2881d625447f47e4b2a96ea0487c6f917`。这份新构建完成了文件和模块核查，尚未刷入手表；本次也未在 GitHub runner 实际运行。
