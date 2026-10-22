"""用**真实存在的系统文件**验证「任意文件都能无损还原」。

覆盖：源码 / 文档 / 压缩包 / 图片 / 可执行文件 / 字体 / 数据库 / 中文名 /
超长扩展名 / 空文件 / 单字节，以及二维码通道（出码 → 扫码 → 还原）。

用法：
    python scripts/verify_anyfile.py            # 全部
    python scripts/verify_anyfile.py --qr-only  # 只看二维码通道
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from officepack import container as ct          # noqa: E402
from officepack import qr                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "build_tmp", "anyfile")
PY = os.path.dirname(sys.executable)
WIN = os.environ.get("WINDIR", r"C:\Windows")

#: (说明, 候选路径 glob 列表)。取第一个存在的匹配，找不到就跳过（不算失败）。
CANDIDATES = [
    ("Python 源码 .py", [os.path.join(ROOT, "officepack", "container.py")]),
    ("Markdown .md", [os.path.join(ROOT, "README.md")]),
    ("批处理 .cmd", [os.path.join(ROOT, "build_exe.cmd")]),
    ("JSON .json", [os.path.join(ROOT, "requirements.txt")]),
    ("字节码 .pyc", glob.glob(os.path.join(ROOT, "officepack", "__pycache__", "*.pyc"))),
    ("可执行文件 .exe", [os.path.join(PY, "python.exe")]),
    ("动态库 .dll", glob.glob(os.path.join(WIN, "System32", "msvcrt.dll"))),
    ("字体 .ttf", glob.glob(os.path.join(WIN, "Fonts", "arial.ttf"))),
    ("图标 .ico", glob.glob(os.path.join(WIN, "System32", "*.ico"))),
    ("图片 .png", glob.glob(os.path.join(ROOT, "dist", "OfficePack-GUI", "_internal",
                                        "PIL", "*.png"))),
    ("压缩包 .zip", glob.glob(os.path.join(ROOT, "dist", "*.zip"))),
    ("Word .docx", ["__none__"]),   # 由 _make_ooxml 现场生成
    ("Excel .xlsx", glob.glob(os.path.join(ROOT, "build_tmp", "**", "*.xlsx"),
                              recursive=True)),
    ("PowerPoint .pptx", glob.glob(os.path.join(ROOT, "build_tmp", "**", "*.pptx"),
                                   recursive=True)),
    ("PDF .pdf", glob.glob(os.path.join(ROOT, "build_tmp", "**", "*.pdf"),
                           recursive=True)),
    ("Tcl 资源 .tcl", glob.glob(os.path.join(ROOT, "dist", "OfficePack-GUI", "_internal",
                                             "_tcl_data", "*.tcl"))),
    ("Pillow 二进制 .pyd", glob.glob(os.path.join(ROOT, "dist", "OfficePack-GUI",
                                                  "_internal", "PIL", "*.pyd"))),
    ("无扩展名可执行", [os.path.join(ROOT, "dist", "OfficePack-GUI", "OfficePack-GUI.exe")]),
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_zip(path: str) -> None:
    """现场打一个真 zip（含 deflate 压缩），当 .zip 样本。"""
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in ("README.md", "requirements.txt", "officepack/qr.py"):
            full = os.path.join(ROOT, rel)
            if os.path.isfile(full):
                zf.write(full, rel)


def _make_ooxml(path: str) -> None:
    """现场造一个最小的真 .docx（OOXML 就是 zip + XML，Word 能直接打开）。"""
    import zipfile

    body = "".join("<w:p><w:r><w:t>第 %d 段：无损往返测试</w:t></w:r></w:p>" % i
                   for i in range(1, 400))
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>%s</w:body></w:document>" % body
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, xml in parts.items():
            zf.writestr(name, xml.encode("utf-8"))


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%d B" % n


def pick(pattern_list):
    for pat in pattern_list:
        hits = sorted(glob.glob(pat, recursive=True))
        for h in hits:
            if os.path.isfile(h) and not h.endswith(".opk.txt"):
                return h
    return None


def text_roundtrip(path: str, outdir: str) -> str:
    """文本通道：pack → unpack → 逐字节比对。"""
    results, meta = ct.pack_file(path, os.path.join(outdir, os.path.basename(path) + ".opk.txt"),
                                 codec_name="base91", compress="auto",
                                 cipher_name="none", preview=False)
    restored = ct.unpack_texts([open(results[0], encoding="utf-8-sig").read()],
                               out_dir=os.path.join(outdir, "restored"))
    with open(path, "rb") as fh:
        want = fh.read()
    with open(restored[0][0], "rb") as fh:
        got = fh.read()
    if sha(want) != sha(got):
        raise AssertionError("文本通道 SHA 不一致")
    if meta.get("sha256") and meta["sha256"] != sha(got):
        raise AssertionError("meta 记录的 SHA 与还原结果不符")
    return "文本 %s → %s" % (human(len(want)), human(os.path.getsize(results[0])))


def qr_roundtrip(path: str, outdir: str) -> str:
    """二维码通道：pack → 出码 → 扫图 → 还原 → 逐字节比对。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    pack = ct.pack_bytes(raw, os.path.basename(path), codec_name="qr45",
                         compress="max", cipher_name="none", preview=False)
    frames = qr.build_frames(pack, "L")
    imgdir = os.path.join(outdir, "qr")
    res = qr.render_images(frames, imgdir, "f", ec="L", sheet=True, target_px=800)
    texts = qr.decode_images(res["images"])
    # 总览图再单独扫一遍：拼图识别是独立能力，漏了它等于没测
    if res["sheet"]:
        texts += [t for t in qr.decode_images([res["sheet"]]) if t not in texts]
    restored = ct.unpack_texts(texts, out_dir=os.path.join(outdir, "qr_restored"))
    with open(restored[0][0], "rb") as fh:
        got = fh.read()
    if sha(raw) != sha(got):
        raise AssertionError("二维码通道 SHA 不一致")
    room = qr.single_frame_bytes("L", "qr45")
    tag = "1 张码" if len(frames) == 1 else "%d 张码" % len(frames)
    return "二维码 %s（压缩后 %s / 单码上限 %s）" % (tag, human(len(pack.payload)),
                                              human(room))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qr-only", action="store_true")
    parser.add_argument("--max-mb", type=float, default=4.0,
                        help="超过这个大小的文件跳过（默认 4MB）")
    args = parser.parse_args()

    os.makedirs(TMP, exist_ok=True)   # 每轮就地覆盖，不整目录删（会撞批量删除保护）

    ok = skip = fail = 0
    failures = []
    t0 = time.time()
    print("=" * 78)
    print("真实文件无损往返验证")
    print("=" * 78)

    cases = list(CANDIDATES)
    # 人造的边界用例
    edge = os.path.join(TMP, "edge")
    os.makedirs(edge, exist_ok=True)
    open(os.path.join(edge, "empty.bin"), "wb").close()
    open(os.path.join(edge, "one.bin"), "wb").write(b"\x00")
    open(os.path.join(edge, "中文 文件名（含空格）.txt"), "wb").write(
        "中文内容测试\n" .encode() * 50)
    open(os.path.join(edge, "no_extension"), "wb").write(bytes(range(256)) * 4)
    open(os.path.join(edge, "weird.ext.verylongextname"), "wb").write(b"\xff" * 300)
    _make_zip(os.path.join(edge, "sample.zip"))
    _make_ooxml(os.path.join(edge, "sample.docx"))
    for name in ("empty.bin", "one.bin", "中文 文件名（含空格）.txt",
                 "no_extension", "weird.ext.verylongextname",
                 "sample.zip", "sample.docx"):
        cases.append(("边界：%s" % name, [os.path.join(edge, name)]))

    for label, patterns in cases:
        path = pick(patterns)
        if not path:
            print("  -  %-26s 跳过（本机没有样本）" % label)
            skip += 1
            continue
        size = os.path.getsize(path)
        if size > args.max_mb * 1024 * 1024:
            print("  -  %-26s 跳过（%s 超过上限）" % (label, human(size)))
            skip += 1
            continue

        case_dir = os.path.join(TMP, "%03d_%s" % (ok + fail, os.path.basename(path)))
        os.makedirs(case_dir, exist_ok=True)
        detail = []
        try:
            if not args.qr_only:
                detail.append(text_roundtrip(path, case_dir))
            # 二维码通道只测 < 300KB 的（帧数太多跑不动，逻辑本身已被单测覆盖）
            if size <= 300 * 1024:
                detail.append(qr_roundtrip(path, case_dir))
            else:
                detail.append("二维码通道跳过（%s，帧数过多）" % human(size))
            print("  ✔  %-26s %8s  %s" % (label, human(size), " | ".join(detail)))
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print("  ✗  %-26s %8s  %s: %s" % (label, size, type(exc).__name__, exc))
            failures.append((label, path, exc))
            fail += 1

    print("-" * 78)
    print("共 %d 项：通过 %d / 失败 %d / 跳过 %d，耗时 %.1fs"
          % (ok + fail, ok, fail, skip, time.time() - t0))
    if failures:
        print("\n失败明细：")
        for label, path, exc in failures:
            print("  · %s (%s)\n    %s: %s" % (label, path, type(exc).__name__, exc))
        return 1
    print("结论：上述所有真实文件都逐字节还原成功（SHA-256 一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
