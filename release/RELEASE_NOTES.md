# OfficePack v1.0.0

把 Office 文档和纯文本互相转换，**还原后与原文逐字节一致**（SHA-256 校验）。
适合「只能粘贴文本」的渠道：邮件、IM、网页输入框、字数受限的表单。

## 下载哪个

| 附件 | 说明 |
| --- | --- |
| `OfficePack-GUI-win64.zip` | **推荐**。图形界面目录版，解压后双击 `启动.bat`。启动 1~2 秒 |
| `OfficePack-GUI-portable-singlefile.exe` | 图形界面单文件版，只有一个 exe，但每次启动要解压 17MB，约 20~45 秒 |
| `OfficePack-CLI-win64.zip` | 命令行版目录，解压后 `officepack.exe pack 文件.docx` |
| `OfficePack-fix-tkdata.zip` | 应急包。只有图形界面报 `Tk data directory not found` 时才需要 |

> 图形界面版是 PyInstaller `--onedir` 产物，**必须整个文件夹一起拷走**，只拷 exe 会报错。

## 核心特性

- **完全还原**：先把原始字节压缩、加密、再编码为可打印文本；解码反向还原，SHA-256 与源文件一致。不是"抽正文"，字体、样式、宏、嵌入对象全部原样保留。
- **7 种编码器**：`cjk` 最省字符（1 个汉字装 14 bit，膨胀率 0.571）、`base91` 纯 ASCII 最紧凑（1.231）、`base85`/`base64` 兼容性最好、`base32`/`hex` 用于严苛环境。
- **6 档压缩**：`max` 会穷举 lzma 参数取最小；`auto` 无收益时自动跳过。实测 167KB 的 doc 压到 783 个字符。
- **分片**：每片带 `#OPK1 doc_id 003/012 CRC 长度` 头，可乱序合并、可粘进多余文字自动截断、缺段或篡改会明确指出。
- **加密**：AES-256-GCM，scrypt 派生密钥，1MiB 分块，AAD 绑定原文长度与摘要，可选用口令保护。
- **界面内参数说明**：每个下拉框旁的 `?` 点开就有「这个参数干什么、各个选项差在哪」。

## 命令行速查

```bash
officepack pack 项目报告.docx                 # 默认 cjk + max，万字以内自动分片
officepack pack 项目报告.docx --no-split       # 强制整份输出
officepack pack 合同.doc -p 我的口令 --split 5000
officepack unpack 项目报告.opk.txt            # 还原，默认输出到源文件旁
type 分片.txt | officepack unpack -           # 从标准输入还原
officepack info 项目报告.opk.txt               # 只看元信息不还原
```

## 已知限制

- 文本体积下限受压缩率支配，已压缩过的文件（jpg/zip/mp4）几乎没有压缩收益。
- `cjk` 编码依赖中文常用字区，部分渠道会把生僻字替换掉，遇到请改用 `base91`。
- 仅支持 Windows（打包产物），源码本身跨平台。

## 自测

23 项单元测试覆盖往返一致性、乱序分片、缺段拒绝、错口令拒绝、篡改检测：`python -m unittest discover officepack/tests -v`
