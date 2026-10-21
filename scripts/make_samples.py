"""生成测试样本（最小可解析的 docx/xlsx/pptx + 模拟老格式 doc）。

运行：python scripts/make_samples.py
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from officepack.tests.test_roundtrip import make_docx, make_pptx, make_xlsx  # noqa: E402

OUT = os.path.join(ROOT, "samples")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    paras = ["%d. 项目进展汇报：本期完成率 92%%，风险项已闭环。" % i for i in range(1, 201)]
    for name, data in {
        "项目报告.docx": make_docx(paras),
        "数据台账.xlsx": make_xlsx(["科目", "本期金额", "上期金额", "变动率"] + paras[:60]),
        "汇报材料.pptx": make_pptx(paras[:20]),
    }.items():
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(data)

    # 模拟老格式 doc：OLE2 头 + 大量可压缩文本内容（lzma 收益明显）
    body = ("合同条款正文，重复内容用于验证压缩率。" * 3000).encode("utf-8")
    with open(os.path.join(OUT, "老格式合同.doc"), "wb") as fh:
        fh.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + body)

    for name in sorted(os.listdir(OUT)):
        path = os.path.join(OUT, name)
        print("%-18s %8d B" % (name, os.path.getsize(path)))


if __name__ == "__main__":
    main()
