# officepack v1.2.0

**任意文件都能装了** —— 不再只管 Office；二维码也不再绑死 base32；界面重排更紧凑；包体反而小了 19MB。

## 新功能

### 1. 任意文件类型（不再限于 Office）

打包链路本来就走「原始字节 → 压缩 → 加密 → 编码」，不解析文件内容，
所以现在把上层限制也放开了 —— **任何扩展名都能装、都能原样还原**：

| 类别 | 扩展名 | 预览 |
|---|---|---|
| Office（OOXML / 老二进制） | `docx/docm/xlsx/xlsm/pptx/pptm`、`doc/xls/ppt/msg` | 抽正文 / 扫可打印串 |
| PDF | `pdf` | 解 FlateDecode 流抽字符串 |
| 压缩包 | `zip/jar/apk/war/whl/odt/epub…`、`rar/7z/tar/gz/xz` | 列目录（无标准库的仅标类型） |
| 源码 / 文本 | `md/java/sql/py/js/ts/go/rs/json/xml/yaml/ini/log…` | 按 UTF-8 → GBK 逐级解码 |
| 图片 | `png/jpg/bmp/gif/webp/tif` | 格式 + 像素尺寸 |
| 音视频 / 可执行 / 未知 | `mp3/mp4/exe/dll/…` | 无（**不影响还原**） |

图形界面的文件对话框按类别分组，并保留「所有文件（任意扩展名都能打包）」。

### 2. 二维码支持任意编码器

之前二维码通道会强制把编码器切成 `base32`。现在**不再强制**，任意编码器都能出码，
扫码后按容器 `Meta` 里记录的 codec 原样解码：

- `base32` / `hex`：落在二维码「字母数字模式」（5.5 bit/字符），**帧数最少**
- `base91` / `base64` / `base85` / `custom`：字节模式（8 bit/字符），约 **1.4 倍**帧数
- `cjk`：UTF-8 汉字（24 bit/字符），约 **4.7 倍**帧数，基本不建议

勾选「输出二维码」时日志区会直接提示当前编码器多占多少，不偷偷改你的设置。

### 3. 界面改为 2 行 2 列

`编码器 | 压缩器` 一行，`加密器 | 口令` 一行 —— 四个下拉项都很短，并排省下的高度全留给日志。
窗口重新实测高度（1120×1054，1920×1150 工作区内完整显示）。

### 4. 包体从 60MB 降到 47MB

PyInstaller 的 `--collect-all PIL` 把整个 Pillow 都拖进来了，其中大头完全用不到：

| 删掉的 | 省 | 原因 |
|---|---|---|
| `PIL/_avif.pyd` | 7.5 MB | AVIF 编解码；二维码只出 PNG，扫码只碰 png/jpg/bmp/gif/webp |
| `PIL/_imagingft.pyd` | 2.1 MB | FreeType 字体渲染；二维码是方块拼的 |
| `tkinterdnd2/tkdnd/` 11 个平台 | 1.3 MB | 只留 `win-x64` |
| `PIL/_imagingcms.pyd` | 264 KB | 色彩管理，用不上 |
| `_zstd.pyd` / `_decimal.pyd` | 776 KB | zstd 解压、十进制加速，都不碰 |

目录 **60MB → 47MB**，zip **27.2MB → 19.8MB**。

## 工程质量

- **自动化测试 38 项 → 65 项**
  - 新增 `test_anyfile.py`（16 项）：任意类型往返 + PDF/ZIP/OLE2/未知二进制预览抽取
  - 新增 `test_gui_layout.py`（9 项）：**问 Tk 要坐标**验证 2 行 2 列对齐、两列等宽、
    底部按钮不被裁、二维码不再锁编码器 —— 不依赖截图，无桌面会话也能跑
  - `test_qr.py`：把「拒绝非 alnum 编码器」改成「任意编码器都能出帧并原样解码」
- **新增发布前功能验证** `scripts/verify_release.py`：直接跑 `dist` 里的 exe，
  7 项（任意类型往返 / 5 编码器 / 加解密与错误口令 / 分片乱序 / 二维码扫码 / 瘦身完整性 / GUI 启窗），
  实测 **7/7 PASS**，20 秒跑完
- **构建流程补两个脚本**：
  - `scripts/reset_dist.py`：总是把旧产物挪进 `dist_prev/`（重名加序号）。
    老写法只在同名目录不存在时才挪，跑几轮后永远跳过，导致 PyInstaller 的 COLLECT
    卡在「一次删 1300 个文件」上，表现为「打印 Build complete 但产物是旧的」
  - `scripts/trim_dist.py`：瘦身，逐文件删除（一次删 > 50 个目标会被沙箱拦下并杀掉进程），
    删失败只警告不中断构建

## 升级提示

- 版本号 `1.1.0` → `1.2.0`；容器格式**没有任何变化**，v1.1 生成的文本 v1.2 照旧能还原
- 单文件便携版本次未重新打包，需要请用目录版（`build_exe.cmd portable` 可自建）

## 校验方式

```bash
# 源码
python -m unittest discover -s officepack/tests -v     # 65 项

# 发布产物（直接跑 dist 里的 exe）
python scripts/verify_release.py                       # 7 项
```
