"""采样抖动算法的测试。

用户的原话是「有没有抖动算法？根据总帧数算出每轮扫完后后续抖动几帧收益
最大，算出共几轮的最优解」。这里把那条推导的三块都钉住：

1. 缺口怎么算（环形、跨环也算）；
2. 相位序列为什么是 0 / 1/2 / 1/4 / 3/4 …（每轮劈开现存最大空隙）；
3. 最优轮数怎么来（按步长模拟到收齐，而不是拍脑袋「多补几轮」）。
"""

from __future__ import annotations

import math
import unittest

from officepack import qr


def _texts(n: int, keep=None, doc: str = "d1") -> list[str]:
    """造 n 帧的段头文本；``keep`` 给定时只保留这些序号。

    段头格式 ``#OPK1 <docId> <序号>/<总数> <CRC> <段字符数>`` —— 只给段头、
    正文留空也照样能被 parse_segments 解析，够测「缺哪些帧」了。
    """
    if keep is None:
        keep = range(1, n + 1)
    return ["#OPK1 %s %d/%d CRCAA 0" % (doc, i, n) for i in keep]


class VanDerCorputTest(unittest.TestCase):
    """相位序列必须每项都落在现存最大缺口正中间。"""

    def test_sequence_bisects_the_largest_gap(self):
        got = [qr._van_der_corput(i) for i in range(8)]
        self.assertEqual(got[0], 0.0)
        self.assertAlmostEqual(got[1], 0.5)     # 劈开整条
        self.assertAlmostEqual(got[2], 0.25)    # 劈开前半
        self.assertAlmostEqual(got[3], 0.75)    # 劈开后半
        self.assertAlmostEqual(got[4], 0.125)

    def test_every_term_is_a_new_position(self):
        """前 16 项必须两两不同 —— 重复的相位等于白扫一轮。"""
        seen = {round(qr._van_der_corput(i), 6) for i in range(16)}
        self.assertEqual(len(seen), 16)


class FrameGapsTest(unittest.TestCase):
    """缺口统计：缺哪些、最大连缺多宽、跨环也要算。"""

    def test_counts_missing_frames(self):
        gaps = qr.frame_gaps(_texts(12, keep=(1, 2, 3, 4, 9, 10, 11, 12)))
        self.assertEqual(gaps["total"], 12)
        self.assertEqual(gaps["got"], 8)
        self.assertEqual(gaps["missing"], [5, 6, 7, 8])
        self.assertEqual(gaps["max_gap"], 4)

    def test_wraps_around_the_loop(self):
        """动图是循环的：末尾和开头之间也是一段缺口。

        少了这一笔，「缺 8、9、10 三帧」会被算成最大连缺 1，抖动就会往错误
        的方向挪。
        """
        gaps = qr.frame_gaps(_texts(10, keep=range(1, 9)))
        self.assertEqual(gaps["missing"], [9, 10])
        self.assertEqual(gaps["max_gap"], 2, "跨环缺口应当连起来算")

    def test_interior_gap_is_measured(self):
        gaps = qr.frame_gaps(_texts(10, keep=(1, 2, 3, 4, 6, 7, 8, 9, 10)))
        self.assertEqual(gaps["max_gap"], 1)

    def test_no_headers_gives_nothing(self):
        """别人的二维码 / 纯文本没有段头：无从统计，返回空而不是炸掉。"""
        self.assertEqual(qr.frame_gaps(["hello", "world"]), {})

    def test_complete_file_has_no_gap(self):
        gaps = qr.frame_gaps(_texts(5))
        self.assertEqual(gaps["missing"], [])
        self.assertEqual(gaps["max_gap"], 0)


class CoverModelTest(unittest.TestCase):
    """理想梳齿模型：步长 step 决定一轮能收到哪一批帧。"""

    def test_step_one_covers_every_frame(self):
        """采样和播放同速：一轮就该收齐（这就是为什么 10 帧/秒的动图配
        10 枪/秒能一轮齐）。"""
        cov = qr._coverage(10, 1.0, 10, 0.0)
        self.assertEqual(cov, set(range(1, 11)))

    def test_integer_step_only_covers_one_class(self):
        """步长正好 3：一轮只落在同一类相位上，只能收到 1/3 的帧 ——
        这就是「每轮都漏同几帧」的数学解释。"""
        cov = qr._coverage(12, 3.0, 4, 0.0)
        self.assertEqual(len(cov), 4)

    def test_half_tooth_offset_reaches_the_other_class(self):
        """往后挪半个齿，正好落在上一轮的空档里。"""
        a = qr._coverage(12, 3.0, 4, 0.0)
        b = qr._coverage(12, 3.0, 4, 0.5)
        self.assertFalse(a & b, "半个齿的错位应当完全不重合")
        self.assertEqual(len(a | b), 8)


class PlanJitterTest(unittest.TestCase):
    """最优相位 + 最优轮数。"""

    def test_step_one_asks_for_half_a_tooth(self):
        texts = _texts(12, keep=(1, 2, 3, 4, 9, 10, 11, 12))
        plan = qr.plan_jitter(texts, sample_fps=10.0, player_fps=10.0)
        self.assertEqual(plan["step"], 1.0)
        self.assertEqual(plan["rounds_left"], 1, "同速时补一轮就该齐")
        self.assertAlmostEqual(plan["next_ratio"], 0.5)

    def test_slow_sampling_needs_more_rounds(self):
        """采样只有播放的 1/3 快：一轮只能收 1/3，轮数必然跟着涨。"""
        texts = _texts(30, keep=range(1, 11))
        fast = qr.plan_jitter(texts, sample_fps=10.0, player_fps=10.0)
        slow = qr.plan_jitter(texts, sample_fps=10.0, player_fps=30.0)
        self.assertGreater(slow["rounds_left"], fast["rounds_left"])

    def test_tells_you_to_raise_the_frame_rate(self):
        """步长明显大于 1 时，正确建议是「提高帧率」，不是无限补轮。"""
        texts = _texts(30, keep=range(1, 11))
        plan = qr.plan_jitter(texts, sample_fps=10.0, player_fps=30.0)
        self.assertIn("帧/秒", plan["advice"])
        self.assertLessEqual(plan["rounds_left"], qr.JITTER_ROUNDS_MAX)

    def test_total_rounds_accumulates(self):
        """已经补过 2 轮时，「共几轮」要把已经花掉的算进去。"""
        texts = _texts(30, keep=range(1, 11))
        plan = qr.plan_jitter(texts, sample_fps=10.0, player_fps=30.0,
                              rounds_done=2)
        self.assertEqual(plan["total_rounds"], 2 + plan["rounds_left"])

    def test_falls_back_to_halving_when_rate_unknown(self):
        """测不出播放帧率时，退回「每轮把最大缺口劈半」的保守估计。"""
        texts = _texts(40, keep=range(1, 9))
        plan = qr.plan_jitter(texts, sample_fps=10.0, player_fps=None)
        self.assertIsNone(plan["step"])
        self.assertEqual(plan["rounds_left"],
                         int(math.ceil(math.log2(max(2, plan["max_gap"])))))
        self.assertIn("轮", plan["advice"])

    def test_complete_file_says_done(self):
        plan = qr.plan_jitter(_texts(6), sample_fps=10.0, player_fps=10.0)
        self.assertEqual(plan["rounds_left"], 0)
        self.assertIn("齐", plan["advice"])

    def test_rounds_are_capped(self):
        """缺口大到离谱时也别给出「补 200 轮」这种建议。"""
        plan = qr.plan_jitter(_texts(400, keep=(1,)), sample_fps=10.0,
                              player_fps=10.0)
        self.assertLessEqual(plan["rounds_left"], qr.JITTER_ROUNDS_MAX)


class FastestPathTest(unittest.TestCase):
    """优化目标必须是**总耗时最短**，不是轮数最少。"""

    def test_one_more_round_is_already_fastest(self):
        """步长≈1：一轮就齐，这时候提帧率不会更快。"""
        texts = _texts(24, keep=[i for i in range(1, 25) if i not in (5, 6)])
        plan = qr.plan_jitter(texts, sample_fps=10.0, player_fps=10.0)
        self.assertEqual(plan["fastest"], "one_more_round")
        self.assertEqual(plan["rounds_left"], 1)

    def test_raising_the_rate_beats_more_rounds(self):
        """步长 3 时补 3 轮要 3 圈；把枪速提到 1.2 倍播放率只要 1 圈。"""
        texts = _texts(24, keep=list(range(1, 11)))
        plan = qr.plan_jitter(texts, sample_fps=3.0, player_fps=10.0)
        self.assertEqual(plan["fastest"], "raise_fps")
        self.assertLess(plan["fastest_sec"], plan["round_cost"])
        self.assertGreaterEqual(plan["target_fps"], 10.0)
        self.assertIn("快", plan["advice"])

    def test_costs_are_expressed_in_seconds(self):
        """建议里必须给秒数 —— 用户问的是「最快」，不是「轮数最少」。"""
        texts = _texts(60, keep=list(range(1, 21)))
        plan = qr.plan_jitter(texts, sample_fps=3.0, player_fps=10.0)
        self.assertGreater(plan["fastest_sec"], 0)
        self.assertGreater(plan["round_cost"], 0)
        self.assertIn("秒", plan["advice"])


class SegmentIndexTest(unittest.TestCase):
    """收帧热路径上的段头解析：只读头一行，结果必须和完整解析一致。"""

    def test_reads_index_from_header(self):
        from officepack import container as ct
        text = "#OPK1 d1 7/40 CRCAA 0\n#OPK1 d1 8/40 CRCBB 0"
        self.assertEqual(qr.segment_index(text), 7)
        # 和权威解析器对齐（防止两处格式理解跑偏）
        self.assertEqual(ct.parse_segments(text)[0].index, 7)

    def test_rejects_non_segment_text(self):
        self.assertIsNone(qr.segment_index("hello world"))
        self.assertIsNone(qr.segment_index("#OPKmark|S|docx|40"))

    def test_body_lines_do_not_confuse_it(self):
        """正文里也出现 #OPK1 字样时，只能认**第一行**那一个段头。"""
        text = "#OPK1 d1 3/9 CRCAA 0\nsome body\n#OPK1 d1 4/9 CRCBB 0"
        self.assertEqual(qr.segment_index(text), 3)



    """从收帧时间戳反推播放器帧率。"""

    def test_estimates_playback_rate(self):
        samples = [(0.0, 1), (0.5, 6), (1.0, 11), (1.5, 16)]
        self.assertAlmostEqual(qr._player_fps(samples), 10.0)

    def test_ignores_loop_wrap_and_short_intervals(self):
        """回绕（序号倒退）和挤在同一毫秒的样本都要丢掉，不能算成负帧率。"""
        samples = [(0.0, 1), (0.5, 6), (0.51, 2), (1.0, 2), (1.5, 7), (2.0, 12)]
        self.assertAlmostEqual(qr._player_fps(samples), 10.0)

    def test_too_few_samples_gives_none(self):
        self.assertIsNone(qr._player_fps([(0.0, 1)]))


if __name__ == "__main__":
    unittest.main()
