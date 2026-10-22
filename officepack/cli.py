"""命令行入口。

    python -m officepack pack    报表.docx --preset compact --password 123456
    python -m officepack unpack  报表.docx.base91.opk.txt --password 123456
    python -m officepack inspect 报表.docx.base91.opk.txt
    python -m officepack verify  报表.docx.base91.opk.txt --password 123456
    python -m officepack codecs
    python -m officepack gui
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import container as ct
from . import codecs as codec_mod
from . import qr
from .compress import CompressError, compress
from . import crypto as crypto_mod
from . import __version__


def _read_text(path: str) -> str:
    """读取文本容器（UTF-8，兼容 BOM）。"""
    with open(path, "r", encoding="utf-8-sig") as fh:
        return fh.read()


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GiB" % n


def _password(args, confirm: bool = False, need: bool = False) -> str | None:
    if getattr(args, "password", None):
        return args.password
    if getattr(args, "ask_password", False) or need:
        pwd = getpass.getpass("请输入口令（回车表示不加密）：")
        if pwd and confirm:
            again = getpass.getpass("请再次输入口令：")
            if pwd != again:
                raise SystemExit("两次输入的口令不一致")
        return pwd or None
    return None


def _resolve_preset(args) -> tuple[str, str, int | None]:
    preset = getattr(args, "preset", None)
    if preset:
        if preset not in ct.PRESETS:
            raise SystemExit(
                "未知预设 %r，可用：%s" % (preset, ", ".join(ct.PRESETS))
            )
        cfg = ct.PRESETS[preset]
        return (
            args.codec or cfg["codec"],
            args.compress or cfg["compress"],
            args.level,
        )
    return (
        args.codec or ct.DEFAULT_CODEC,
        args.compress or ct.DEFAULT_COMPRESS,
        args.level,
    )


def cmd_pack(args) -> int:
    codec_name, compress_mode, level = _resolve_preset(args)
    password = _password(args, confirm=True)
    cipher_name = args.cipher or ("aes-256-gcm" if password else "none")
    if cipher_name != "none" and not password:
        raise SystemExit("已指定加密器 %s，但没有提供口令" % cipher_name)

    out_dir = args.out_dir
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if args.qr:
        return _pack_as_qr(args, codec_name, compress_mode, level,
                           cipher_name, password, out_dir)

    segment = 0 if args.no_split else (args.split or 0)
    parts = 0 if args.no_split else (args.parts or 0)
    target = args.target or 0

    failed = 0
    for src in args.files:
        if not os.path.isfile(src):
            print("✗ 文件不存在：%s" % src)
            failed += 1
            continue
        out_path = None
        if out_dir:
            out_path = os.path.join(
                out_dir, os.path.basename(ct.default_out_path(src, codec_name))
            )
        try:
            paths, meta = ct.pack_file(
                src,
                out_path,
                codec_name=codec_name,
                alphabet=args.alphabet,
                cipher_name=cipher_name,
                password=password,
                compress=compress_mode,
                level=level,
                preview=not args.no_preview,
                ascii_only=args.ascii_only,
                segment=segment,
                parts=parts,
            )
        except (ct.ContainerError, codec_mod.CodecError, CompressError,
                crypto_mod.CryptoError, OSError) as exc:
            print("✗ %s：%s" % (os.path.basename(src), exc))
            failed += 1
            continue
        text_bytes = sum(os.path.getsize(p) for p in paths)
        growth = text_bytes / meta["size"] if meta["size"] else 0
        algo = "%s/%s/%s" % (
            meta["codec"],
            (meta.get("compress") or {}).get("alg", "none"),
            (meta.get("cipher") or {}).get("alg", "none"),
        )
        print("✅ %s" % os.path.basename(src))
        print(
            "   原始 %s → 文本 %s（%s，%d 字符，%.2fx）"
            % (_human(meta["size"]), _human(text_bytes), algo, meta["text_chars"], growth)
        )
        chars = meta["text_chars"]
        if target:
            if chars <= target:
                print("   ✓ 总量 %d 字符，在 %d 字目标内" % (chars, target))
            else:
                need = (chars + target - 1) // target
                print(
                    "   ⚠ 总量 %d 字符，是 %d 字目标的 %.1f 倍"
                    % (chars, target, chars / target)
                )
                print(
                    "     已压到 %s/%s 的极限（无损还原下不能再降）"
                    % (
                        meta["codec"],
                        (meta.get("compress") or {}).get("alg", "none"),
                    )
                )
                print(
                    "     原因：文档含不可压缩内容（图片/嵌入对象），信息量本身超过一万字承载上限"
                )
                print("     建议：--split %d 分 %d 段逐次输入" % (target, need))
        if len(paths) > 1:
            print(
                "   分成 %d 段（每段 ≤ %d 字符），逐段输入后一起还原"
                % (len(paths), segment or ct.DEFAULT_SEGMENT_CHARS)
            )
            print("   输出 %s … %s" % (paths[0], os.path.basename(paths[-1])))
        else:
            print("   输出 %s" % paths[0])
    return 1 if failed else 0


def _pack_as_qr(args, codec_name, compress_mode, level, cipher_name, password, out_dir) -> int:
    """二维码通道：一帧一张 PNG，扫码工具（AirScan-QR / 鹊桥 / 手机相机）都能读。"""
    if codec_name not in qr.ALNUM_CODECS:
        factor = qr.bits_per_char(codec_name) / qr._BITS_ALNUM
        print("· 编码器 %s 不落在二维码的字母数字模式，同样数据要多占约 %.1f 倍空间；"
              "想少几张码请加 --preset qr（qr45 + max）" % (codec_name, factor))

    failed = 0
    for src in args.files:
        if not os.path.isfile(src):
            print("✗ 文件不存在：%s" % src)
            failed += 1
            continue
        try:
            with open(src, "rb") as fh:
                raw = fh.read()
            pack = ct.pack_bytes(
                raw,
                os.path.basename(src),
                codec_name=codec_name,
                alphabet=args.alphabet,
                cipher_name=cipher_name,
                password=password,
                compress=compress_mode,
                level=level,
                preview=False,
                ascii_only=True,
            )
            frames = qr.build_frames(pack, ec=args.qr_ec, chars_per_frame=args.qr_chars)
        except (ct.ContainerError, codec_mod.CodecError, CompressError,
                crypto_mod.CryptoError, qr.QRError, OSError) as exc:
            print("✗ %s：%s" % (os.path.basename(src), exc))
            failed += 1
            continue

        target_dir = out_dir or os.path.dirname(os.path.abspath(src))
        stem = os.path.basename(src)
        try:
            res = qr.render_images(frames, target_dir, stem, ec=args.qr_ec,
                                   sheet=not args.no_sheet, target_px=args.qr_px,
                                   single=not args.no_single)
            gif_path = None
            if args.qr_gif and len(frames) > 1:
                gif_path = os.path.join(target_dir, "%s.qr.gif" % stem)
                qr.render_gif(frames, gif_path, ec=args.qr_ec,
                              fps=args.qr_gif, target_px=min(args.qr_px, 720))
        except (qr.QRError, OSError) as exc:
            print("✗ %s：生成二维码失败 %s" % (os.path.basename(src), exc))
            failed += 1
            continue

        meta = pack.meta
        room = qr.single_frame_bytes(args.qr_ec, meta["codec"])
        print("✅ %s" % os.path.basename(src))
        print("   原始 %s → 压缩后 %s → %d 张二维码（%s，纠错 %s，%d 帧字符）"
              % (_human(meta["size"]), _human(len(pack.payload)), len(frames),
                 meta["codec"], args.qr_ec.upper(), meta["text_chars"]))
        if len(frames) == 1:
            print("   ✔ 一张码搞定（本组合单张上限约 %s 压缩后数据）" % _human(room))
        else:
            print("   单张上限约 %s，压缩后 %s，所以分成 %d 张；"
                  "总览图一张截图能全部带回，顺序无关"
                  % (_human(room), _human(len(pack.payload)), len(frames)))
        print("   输出目录 %s" % res["dir"])
        sheets = res.get("sheets") or []
        if len(sheets) == 1:
            print("   总览图 %s（一张截图即可带回全部帧）" % os.path.basename(sheets[0]))
        elif len(sheets) > 1:
            print("   总览图 %d 页：%s …（每页截一张图，覆盖该页 12 帧）"
                  % (len(sheets), os.path.basename(sheets[0])))
        if res["images"]:
            print("   单帧图 %s … %s" % (os.path.basename(res["images"][0]),
                                        os.path.basename(res["images"][-1])))
        elif sheets:
            print("   本次只写了总览图（--no-single）：总览里每张码和单帧同尺寸，"
                  "识别总览就等于识别单帧")
        print("   还原：officepack unpack 图片或目录 [-p 口令]")
        if len(sheets) > 1:
            print("   共 %d 帧 / %d 页总览图，截图按页来最省事" % (len(frames), len(sheets)))
        if gif_path:
            print("   动图 %s（%.1f 帧/秒，循环播放；也能直接当输入丢给 unpack）"
                  % (os.path.basename(gif_path), args.qr_gif))
    return 1 if failed else 0


def cmd_unpack(args) -> int:
    out_dir = args.out_dir
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    use_stdin = "-" in args.files
    paths = [p for p in args.files if p != "-"]

    # 输入按类型分流：图片（二维码）走扫码，其余当文本容器；目录整目录扫
    image_srcs = [p for p in paths
                  if os.path.isdir(p) or qr.is_image_path(p)]
    text_srcs = [p for p in paths if p not in image_srcs]

    missing = [p for p in text_srcs if not os.path.isfile(p)]
    for path in missing:
        print("✗ 文件不存在：%s" % path)
    targets = [p for p in text_srcs if os.path.isfile(p)]
    if not targets and not image_srcs and not use_stdin:
        return 1

    texts: list[str] = []
    if image_srcs:
        try:
            shots = qr.decode_images(image_srcs)
        except (qr.QRError, OSError) as exc:
            print("✗ 二维码识别失败：%s" % exc)
            return 1
        texts.extend(shots)
        print("· 从 %d 个图片来源扫到 %d 段内容" % (len(image_srcs), len(shots)))

    for src in targets:
        try:
            texts.append(_read_text(src))
        except (OSError, UnicodeDecodeError) as exc:
            print("✗ 读取失败 %s：%s" % (os.path.basename(src), exc))
    if use_stdin:
        data = _read_stdin_text()
        if not data.strip():
            print("✗ 标准输入为空：用 `-` 时需通过管道或粘贴把容器文本传进来")
            return 1
        texts.append(data)
    if not texts:
        return 1

    # 只要有一个加密容器就提示输入一次口令，其余复用
    need_pwd = False
    for text in texts:
        try:
            if (ct.inspect_text(text).get("cipher") or {}).get("alg", "none") != "none":
                need_pwd = True
                break
        except ct.ContainerError:
            continue
    password = _password(args, need=need_pwd)

    if out_dir is None:
        anchor = targets[0] if targets else (image_srcs[0] if image_srcs else None)
        if anchor:
            out_dir = (anchor if os.path.isdir(anchor)
                       else os.path.dirname(os.path.abspath(anchor)))

    try:
        results = ct.unpack_texts(texts, out_dir, password, args.name)
    except (ct.ContainerError, codec_mod.CodecError, CompressError,
            crypto_mod.CryptoError, OSError, UnicodeDecodeError) as exc:
        print("✗ 还原失败：%s" % exc)
        return 1

    for out_path, meta in results:
        print(
            "✅ → %s\n   %d 字节，SHA-256 校验通过（%s…）"
            % (out_path, meta["size"], meta["sha256"][:16])
        )
    return 1 if missing else 0


def cmd_inspect(args) -> int:
    failed = 0
    for src in args.files:
        try:
            meta = ct.inspect_text(_read_text(src))
        except (ct.ContainerError, OSError, UnicodeDecodeError) as exc:
            print("✗ %s：%s" % (os.path.basename(src), exc))
            failed += 1
            continue
        comp = meta.get("compress") or {}
        cipher = meta.get("cipher") or {}
        print("📄 %s" % os.path.basename(src))
        print("   文件名   : %s" % meta.get("name"))
        print("   原始大小 : %s (%d 字节)" % (_human(meta.get("size", 0)), meta.get("size", 0)))
        print("   SHA-256  : %s" % meta.get("sha256"))
        print("   编码器   : %s" % meta.get("codec"))
        print("   压缩器   : %s (level=%s)" % (comp.get("alg", "none"), comp.get("level")))
        print("   加密器   : %s" % cipher.get("alg", "none"))
        print("   文本字符 : %s" % f"{meta.get('text_chars', 0):,}")
        print("   压缩比   : %s" % meta.get("ratio"))
        print("   生成时间 : %s" % meta.get("created"))
    return 1 if failed else 0


def cmd_verify(args) -> int:
    failed = 0
    for src in args.files:
        try:
            meta = ct.inspect_text(_read_text(src))
            alg = (meta.get("cipher") or {}).get("alg", "none")
            password = _password(args, need=(alg != "none"))
            result = ct.unpack_text(_read_text(src), password)
        except (ct.ContainerError, codec_mod.CodecError, CompressError,
                crypto_mod.CryptoError, OSError, UnicodeDecodeError) as exc:
            print("✗ %s：%s" % (os.path.basename(src), exc))
            failed += 1
            continue
        print(
            "✅ %s 校验通过 — %s，%d 字节，SHA-256 %s…"
            % (
                os.path.basename(src),
                result.name,
                len(result.raw),
                meta["sha256"][:16],
            )
        )
    return 1 if failed else 0


def cmd_codecs(args) -> int:
    print("可用编码器（字符数 / 原始字节，越小则文本字符数越少）：")
    for name, cls in sorted(codec_mod.CODECS.items(), key=lambda kv: kv[1].expansion):
        print("  %-8s %.3fx" % (name, cls.expansion))
    print("\n可用压缩器：auto, lzma, zlib, bz2, none")
    print("可用加密器：none, aes-256-gcm")
    print("\n预设：")
    for name, cfg in ct.PRESETS.items():
        print("  %-8s codec=%s compress=%s" % (name, cfg["codec"], cfg["compress"]))
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="officepack",
        description="任意文件 ↔ 纯文本无损装换：Word/Excel/PPT/PDF/压缩包/源码/图片都能装（还原后 SHA-256 完全一致）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  officepack pack 报表.docx --preset compact --password 123456\n"
            "  officepack pack 代码.zip 说明.pdf --codec base32   # 任意文件都能装\n"
            "  officepack pack 报表.docx --qr              # 输出二维码图片\n"
            "  officepack unpack 报表.docx.base91.opk.txt --password 123456\n"
            "  officepack unpack 截图.png                  # 从二维码截图还原\n"
            "  officepack gui\n"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version="officepack " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pack_opts(p):
        p.add_argument("--preset", choices=list(ct.PRESETS), help="一键预设")
        p.add_argument("--codec", help="编码器：base91/base85/base64/base32/hex/cjk/custom")
        p.add_argument("--alphabet", help="custom 编码器的 64 字符码表")
        p.add_argument("--compress", help="压缩器：auto/lzma/zlib/bz2/none")
        p.add_argument("--level", type=int, help="压缩级别")
        p.add_argument("--cipher", help="加密器：none/aes-256-gcm")
        p.add_argument("--password", help="口令（不加密则留空）")
        p.add_argument("--ask-password", action="store_true", help="交互式输入口令")
        p.add_argument("--no-preview", action="store_true", help="不附带可读文本预览")
        p.add_argument("--ascii-only", action="store_true",
                       help="输出严格 7-bit ASCII（注释区中文转 \\uXXXX 转义）")
        p.add_argument("--target", nargs="?", type=int, const=ct.DEFAULT_TARGET_CHARS,
                       default=0, metavar="N",
                       help="目标总字符数（默认 %d），超出时报告差距" % ct.DEFAULT_TARGET_CHARS)
        p.add_argument("--split", nargs="?", type=int, const=ct.DEFAULT_SEGMENT_CHARS,
                       default=0, metavar="N",
                       help="按每段 N 字符分段（默认 %d），适配单次输入受限的渠道"
                            % ct.DEFAULT_SEGMENT_CHARS)
        p.add_argument("--no-split", action="store_true",
                       help="强制整份输出，不分段（默认行为，用于覆盖配置里的分段设置）")
        p.add_argument("--parts", type=int, default=0, metavar="K",
                       help="切成 K 段（尽量均分）")

    p_pack = sub.add_parser("pack", help="文件 → 文本 / 二维码")
    p_pack.add_argument("files", nargs="+")
    p_pack.add_argument("-o", "--out-dir", help="输出目录")
    p_pack.add_argument("--qr", action="store_true",
                        help="改为输出二维码图片：一帧一张 PNG，可用 AirScan-QR / 鹊桥 / 手机相机传递；"
                             "编码器用 base32 最省帧，其他编码器也能用")
    p_pack.add_argument("--qr-ec", choices=["L", "M", "Q", "H"], default=qr.DEFAULT_EC,
                        help="二维码纠错级别（默认 %s）：L 容量最大，H 最耐脏污" % qr.DEFAULT_EC)
    p_pack.add_argument("--qr-chars", type=int, default=0, metavar="N",
                        help="每帧载荷字符数上限（默认按容量自动算，可调小让每张码更好扫）")
    p_pack.add_argument("--qr-px", type=int, default=960, metavar="N",
                        help="每张二维码的目标像素宽（默认 960）")
    p_pack.add_argument("--no-sheet", action="store_true",
                        help="不生成总览图（默认每 %d 帧拼一页，多页自动续）" % qr.MAX_SHEET_TILES)
    p_pack.add_argument("--no-single", action="store_true",
                        help="只输出总览图，不写单帧 PNG（总览里每张码和单帧尺寸相同，"
                             "识别总览即可；只有 1 帧时仍写单帧）")
    p_pack.add_argument("--qr-gif", type=float, nargs="?", const=3.0, default=0.0,
                        metavar="FPS",
                        help="额外输出循环播放的 GIF 动图（默认 3 帧/秒），适合手机对着屏幕扫")
    add_pack_opts(p_pack)
    p_pack.set_defaults(func=cmd_pack)

    p_unpack = sub.add_parser("unpack", help="文本 / 二维码截图 → 原始文档")
    p_unpack.add_argument("files", nargs="+",
                          help="容器文件或二维码图片/目录；用 - 表示从标准输入读取（可粘贴，"
                               "也可管道传入），分片可一次传入多个，顺序随意")
    p_unpack.add_argument("-o", "--out-dir", help="输出目录")
    p_unpack.add_argument("--name", help="指定还原后的文件名")
    p_unpack.add_argument("--password", help="口令")
    p_unpack.add_argument("--ask-password", action="store_true", help="交互式输入口令")
    p_unpack.set_defaults(func=cmd_unpack)

    p_inspect = sub.add_parser("inspect", help="只读查看容器元信息")
    p_inspect.add_argument("files", nargs="+")
    p_inspect.set_defaults(func=cmd_inspect)

    p_verify = sub.add_parser("verify", help="校验文本能否完整还原")
    p_verify.add_argument("files", nargs="+")
    p_verify.add_argument("--password", help="口令")
    p_verify.add_argument("--ask-password", action="store_true", help="交互式输入口令")
    p_verify.set_defaults(func=cmd_verify)

    p_codecs = sub.add_parser("codecs", help="列出编码器/压缩器/加密器")
    p_codecs.set_defaults(func=cmd_codecs)

    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_gui.set_defaults(func=cmd_gui)

    return parser


def setup_console() -> None:
    """把标准输出统一成 UTF-8。

    Windows 默认代码页是 GBK，出现 ✅ / 中文时 print 会抛 UnicodeEncodeError，
    打包成 exe 后尤其致命（用户看到的是“Failed to execute script”）。
    """
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 - 老系统 / 无控制台时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _read_stdin_text() -> str:
    """从标准输入读文本（`unpack -` 用）。

    不能对 sys.stdin 调 reconfigure：管道场景下它会重置缓冲区、丢掉已读入的数据。
    直接读底层二进制流，再按 UTF-8 → GBK 依次尝试解码（cmd 粘贴常是 GBK）。
    """
    try:
        data = sys.stdin.buffer.read()
    except (AttributeError, ValueError):
        return sys.stdin.read()
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
